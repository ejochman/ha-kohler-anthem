"""Binary sensors.

Two unrelated groups live here:

* **Valve diagnostics** — stream health, and per-zone and at-temperature readings. Mostly
  disabled by default; they matter when something is not behaving, not day to day.
* **Controller outlets** — created *only* on accounts with no Anthem Valve. Where a valve
  exists it owns the outlets, as switches.

The controller's view of a session driven through the valve's own API is unreliable —
measured across 95 such episodes: 51 reported immediately, 12 late, 32 never, and
preset-driven openings never (0 of 15). Outlet entities sourced from it on such an account
would freeze through an unpredictable share of showers, which is worse than not existing.
See ``anthem.models.resolve_outlet_source``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .anthem.models import OutletStateSource, resolve_outlet_source
from .const import DOMAIN, EXPOSE_CONTROLLER_WATER_STATE
from .coordinator import Controller, KohlerAnthemCoordinator, Valve
from .entity import KohlerControllerEntity, KohlerValveEntity, zone_label

_LOGGER = logging.getLogger(__name__)


@callback
def _async_purge_single_zone_active(
    hass: HomeAssistant, entry: ConfigEntry, valve: Valve
) -> None:
    """Drop a single-zone valve's retired `Shower Active` row from the entity registry.

    **Why this is not in `__init__._REMOVED_UNIQUE_ID_SUFFIXES`.** That list is
    unconditional, and `_zone_1_active` must survive on a multi-zone valve, where zone 1's
    entity is still created. The condition is the valve's zone count, which is only known
    once the coordinator has read the model — after the blanket purge has already run.

    So it is matched against **this valve's own device id**, not a bare suffix: an account
    with a single-zone valve and a two-zone valve purges the first and leaves the second
    alone. Runs on every setup and is a no-op once clean, so a downgrade and re-upgrade
    cannot strand a row.

    **Cleanup, so it never fails setup.** A stale registry row is cosmetic; the entities
    this platform is here to create are not. Any registry trouble is logged and stepped
    over rather than raised, which also keeps the platform constructible against a test
    double that carries no registry.
    """
    stale = f"{valve.device_id}_zone_1_active"
    try:
        registry = er.async_get(hass)
        rows = list(er.async_entries_for_config_entry(registry, entry.entry_id))
    except Exception:  # Cosmetic cleanup; never worth failing setup over.
        _LOGGER.debug("Entity registry unavailable; leaving %s alone", stale)
        return
    for row in rows:
        if row.unique_id == stale:
            registry.async_remove(row.entity_id)
            _LOGGER.info(
                "Removed %s: on a single-zone valve Status reports the same state",
                row.entity_id,
            )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up valve diagnostics, and controller outlets where they are the only source."""
    coordinator: KohlerAnthemCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []

    # One set per valve — each is its own device with its own state and layout.
    for valve in coordinator.valves:
        entities += [
            ValveAtTemperatureSensor(coordinator, valve),
            ValveProblemSensor(coordinator, valve),
            MqttConnectionSensor(coordinator, valve),
            # CLOUD CONNECTION WATCH. Created for every valve, including on valve-only
            # accounts — the quiet-interval trigger needs no controller. On an account
            # that also has one, the contradiction trigger wires itself up as well.
            ValveCloudConnectionSensor(coordinator, valve),
            ValvePresetActiveSensor(coordinator, valve),
        ]
        # One per zone the model actually has — but **only on a multi-zone valve**. With a
        # single zone "this zone is flowing" and "the system is running" are the same fact,
        # and `Status` already answers it with warm-up and pause besides, so the second
        # entity was a strictly worse copy of one already on screen. Its two useful
        # attributes moved to `Status` in 0.19.0, so nothing is lost by its absence.
        #
        # A multi-zone valve keeps them, where `Status` genuinely cannot help: pause and
        # warm-up are system-level on this hardware, so there is no per-zone `Status` to
        # split it into, and only these say *which* zone is running.
        if len(valve.model.zones) > 1:
            entities += [
                ValveZoneActiveSensor(coordinator, valve, zone)
                for zone in valve.model.zones
            ]
        else:
            _async_purge_single_zone_active(hass, entry, valve)

    # Everything derived from SHOWER_VALVE_STS is created on a controller-only account,
    # where it is the only water state there is — and, since 2026-08-18, on a both-devices
    # account too, via EXPOSE_CONTROLLER_WATER_STATE.
    #
    # It does put two contradicting answers on one dashboard during a valve-driven session:
    # the valve reports an open outlet, the controller `status: OFF` with an all-zero array.
    # They are answering different questions and both answers are true. See
    # EXPOSE_CONTROLLER_WATER_STATE for why the second one is worth a row.
    source = resolve_outlet_source(
        bool(coordinator.valves), bool(coordinator.controllers)
    )
    controller_water = source is OutletStateSource.HUB_MQTT or (
        bool(coordinator.controllers) and EXPOSE_CONTROLLER_WATER_STATE
    )

    # One set per controller. An account can carry several — the cloud lists them all and
    # the one stream carries messages for all — and each is its own device with its own
    # state, its own accessories, and its own outlet layout.
    for controller in coordinator.controllers:
        # The controller's own copy of the stream-health diagnostic. Same stream as the
        # valve's, deliberately duplicated per device — see `MqttConnectionMixin`.
        entities.append(ControllerMqttConnectionSensor(coordinator, controller))

        capabilities = controller.capabilities

        def attached(present: bool, known: bool = capabilities.known) -> bool:
            """Whether to create an accessory entity.

            ``not known or present``, never plain ``present``: an unread or failed
            configuration leaves every capability False, which is indistinguishable from a
            genuine "no accessories". Erring towards creating means a missed read costs a
            sensor reading unknown, not a silently absent entity.
            """
            return not known or present

        # The accessories are independent of the water path: MUSIC_STS and friends report
        # the controller's own hardware regardless of what drives the valve.
        #
        # Gated on `hub-configuration.parts`, which is the ONLY source of what hardware
        # exists. Message arrival cannot be used: the controller emits STEAM_STS and
        # LIGHT_STS on this system despite `parts` reporting both NotConnected — 10 and 12
        # messages respectively — so subscribing would create entities for hardware nobody
        # owns, permanently reading OFF.
        # Every controller: a fault, or a fitted accessory that has dropped off, is worth
        # seeing whatever else is attached.
        entities.append(ControllerProblemSensor(coordinator, controller))
        if attached(capabilities.music):
            entities.append(ControllerMusicSensor(coordinator, controller))
        if attached(capabilities.light):
            entities.append(ControllerLightSensor(coordinator, controller))
        if attached(capabilities.steam):
            entities.append(ControllerSteamSensor(coordinator, controller))

        if controller_water:
            # This controller's layout, not the entry's: a second bathroom need not have
            # the same valve model. See `Controller.model`.
            model = controller.model
            entities += [
                ControllerOutletSensor(coordinator, controller, zone, outlet)
                for zone in model.zones
                for outlet in range(1, model.outlets_in_zone(zone) + 1)
            ]

    async_add_entities(entities)


class ValveAtTemperatureSensor(KohlerValveEntity, BinarySensorEntity):
    """On once the system reports reaching its temperature setpoint.

    The device's own ``atTemp`` judgement, not a comparison we make — it matches exactly
    when the touchscreen stops flashing and shows a solid setpoint.

    System-level: carried on the primary valve word even when another zone is the one
    delivering water, so it is right regardless of which zone the main shower is plumbed to.
    Expect a brief lag after a setpoint change, mirroring the screen's re-flash.
    """

    _attr_name = "At Temperature"
    _attr_icon = "mdi:thermometer-check"
    # `HEAT` gives the on/off states the labels "Hot"/"Normal" and the matching colour,
    # which reads better on a dashboard than a bare On/Off. The explicit icon is kept so the
    # thermometer survives — a device class supplies its own icon otherwise.
    _attr_device_class = BinarySensorDeviceClass.HEAT

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_at_temperature"

    @property
    def is_on(self) -> bool | None:
        state = self._state
        return None if state is None else state.at_temperature


class ValveProblemSensor(KohlerValveEntity, BinarySensorEntity):
    """Whether the valve is reporting a fault, from the status word's ``errorFlag``.

    **Enabled, and deliberately not a diagnostic.** A fault the owner cannot see is the
    one failure mode where a hidden entity is worse than no entity: every other reading
    here is a convenience, and this one is the only thing that would say the hardware is
    unhappy. It is on the valve device, in the ordinary entity list, so it appears on the
    card without anyone having gone looking for it.

    > ⚠️ **Never observed set — 0 of 992 captured valve words.** An earlier revision of
    > this integration removed the entity for exactly that reason: publishing it claimed a
    > fault detector nobody had been able to test, and a sensor stuck at "OK" is
    > indistinguishable from one that is broken. That reasoning was sound and is *not*
    > overturned here; what changed is the weighing. The decode is straightforward and its
    > cost when wrong is a false "OK" — which is what a user without the entity already
    > has — while its value when right is the only warning the integration can give. So it
    > ships, and says plainly in `fault_detection_verified` that it has never fired.
    >
    > If you ever see this turn on, please open an issue with diagnostics attached: it
    > would be the first captured fault, and it is what this entity is waiting for.

    Reads the flag from **either** zone: `GcsState.has_fault` is any-of across both words,
    so a single-zone valve is covered by valve1 alone and a two-zone valve reports a fault
    in either. `error_code` is deliberately *not* the trigger — byte 7 reads a constant
    ``1`` on the tested unit, so a nonzero code is not a fault. The flag is the only fault
    signal; the codes ride along as attributes.
    """

    _attr_name = "Problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_problem"

    @property
    def is_on(self) -> bool | None:
        state = self._state
        # None — "not known yet" — before the first word arrives, rather than a confident
        # "no problem" the integration has no basis for.
        return None if state is None else state.has_fault

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        codes = {} if state is None else state.error_codes
        return {
            # Per-zone byte 7. Constant `1` on the tested unit and **not** a fault
            # indicator on its own — published so a real fault can be characterised from a
            # bug report rather than guessed at.
            "error_codes": codes,
            # The second fault signal since 2026-10-07: the valve's own
            # `currentSystemState`, which Konnect 3.0.6 treats as a fault when it reads
            # `error`. Published so it is clear which signal fired.
            "system_state": None if state is None else state.system_state,
            # Honest about the caveat in the class docstring: this detector has never been
            # seen to fire, so a False here is weaker evidence than it looks.
            "fault_detection_verified": False,
        }


class ValveDiagnosticBinarySensor(KohlerValveEntity, BinarySensorEntity):
    """Base for valve diagnostics: hidden unless deliberately enabled."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False


class MqttConnectionMixin:
    """The MQTT-health entity, shared by both devices.

    **One stream serves the whole account**, so the valve's copy and the controller's copy
    always read the same value. That redundancy is deliberate: the connection is what every
    entity on *either* device depends on, and someone looking at the Anthem Plus device page
    should not have to know that the diagnostic lives on the valve. On a controller-only
    account there is no valve page for it to live on at all.

    Mixed in ahead of the device base so these properties win the MRO; the base supplies the
    device binding and the diagnostic category.
    """

    _attr_name = "MQTT Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool | None:
        stream = self.coordinator.stream
        return None if stream is None else bool(stream.connected)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Stream and credential detail — never any token material.

        Credentials are reported separately from the connection state rather than folded
        into it. A dead stream and a rejected login need different fixes (wait, versus sign
        in again), so collapsing them into one boolean would hide which one happened.
        """
        stream = self.coordinator.stream
        auth = self.coordinator.auth
        expires_at = auth.access_token_expires_at
        attributes: dict[str, object] = {
            "credentials_present": auth.has_credentials,
            "access_token_expires_at": (
                datetime.fromtimestamp(expires_at, tz=UTC).isoformat()
                if expires_at
                else None
            ),
        }
        if stream is not None:
            attributes["warming_up"] = stream.warming_up
            attributes["last_message_at"] = (
                datetime.fromtimestamp(stream.last_message_at, tz=UTC).isoformat()
                if stream.last_message_at
                else None
            )
        return attributes


class MqttConnectionSensor(MqttConnectionMixin, ValveDiagnosticBinarySensor):
    """Whether the MQTT stream that carries all state is alive.

    This is the health signal that matters: with no polling interval, **push is the only
    way state changes reach Home Assistant**. If this is off, every entity is frozen at
    whatever it last saw, and nothing will correct it until the stream returns.

    It replaces an earlier ``Connection`` sensor that reported Kohler's own
    ``connectionState`` — whether the *cloud* considered the *valve* reachable. That was a
    fact about the plumbing, not about this integration, it had no push source so it went
    stale as soon as polling was removed, and a valve dropping off the cloud is fixed in the
    Konnect app rather than here.

    **Caveat: this reflects what the MQTT client believes.** A half-open socket reads
    connected until the 60 s keepalive fails. Pair it with ``sensor.anthem_valve_last_update``
    — a stale timestamp alongside ``connected`` is the signature of a dead-but-unnoticed
    stream.
    """

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_mqtt_connection"


class ValveCloudConnectionSensor(KohlerValveEntity, BinarySensorEntity):
    """Whether **Kohler's cloud** can reach the valve — not whether *we* can reach Kohler.

    This is the answer to "the Konnect app says my valve is offline": the valve drops off
    Kohler's cloud on its own and returns only when it is physically power-cycled. While it
    is gone the controller, the account and this integration are all healthy, so
    ``MQTT Connection`` stays on and every other entity simply freezes.

    ⚠️ **This is not the sensor that was removed in favour of ``MQTT Connection``**, though it
    reports the same REST field. That one polled, and when polling was deleted it had no push
    source and went stale. This one is driven by two push events that decide when to read —
    see `cloud_watch.py` for both, and for why silence alone can never be one of them.

    **Enabled and visible, unlike the other valve diagnostics.** They are disabled outright,
    which costs nothing until someone wants them. This one has to keep *running* — its whole
    value is the record it builds while nobody is looking, and a disabled entity builds none.

    It was created hidden from 0.2.7 until 0.11.2, on the reasoning that a healthy connection
    is not worth dashboard space. That was the wrong trade: hiding it put "(Hidden)" beside
    the name everywhere the entity appeared, which reads as a broken entity rather than a
    deliberate default, and the one moment it matters — the valve has dropped off Kohler's
    cloud and every other entity has silently frozen — is exactly when nobody wants to go
    unhide something first. It is a connectivity sensor, so it shows as a plain
    Connected/Disconnected row and costs nothing while it says Connected.

    ⚠️ **Visibility applies only when the entity is first created**, so an installation that
    already has it hidden stays hidden — unhide it from the device page, or delete the entity
    and let it be recreated.

    States:

    * **on** — the last successful read said ``Connected``.
    * **off** — the last successful read said otherwise. Only ``Connected`` has ever been
      observed on this account, so the exact negative string is surfaced in
      ``connection_state`` rather than assumed.
    * **unknown** — no successful read yet. ⚠️ A *failed* read does not move this: "we could
      not reach Kohler" and "Kohler cannot reach the valve" are different faults, and the
      error is reported in ``last_error`` instead of being rendered as an outage.
    """

    _attr_name = "Cloud Connection"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_cloud_connection"

    @property
    def is_on(self) -> bool | None:
        watch = self._valve.cloud_watch
        return None if watch is None else watch.connected

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """When it was last asked, why, and what came back verbatim.

        ``checked_because`` matters more than it looks: a result from the contradiction
        trigger was taken while a shower was running, which is far stronger evidence than one
        taken because the valve had been quiet overnight.
        """
        watch = self._valve.cloud_watch
        return {} if watch is None else watch.attributes


class ControllerDiagnosticBinarySensor(KohlerControllerEntity, BinarySensorEntity):
    """Base for controller diagnostics: hidden unless deliberately enabled."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False


class ControllerMqttConnectionSensor(
    MqttConnectionMixin, ControllerDiagnosticBinarySensor
):
    """The controller's copy of the MQTT-health diagnostic. See :class:`MqttConnectionMixin`.

    Reports identically to the valve's copy — same stream, same account. Pair it with
    ``sensor.anthem_plus_last_update``: connected alongside a stale timestamp is the
    signature of a half-open socket that has not yet failed its keepalive.
    """

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_mqtt_connection"


class ValveZoneActiveSensor(KohlerValveEntity, BinarySensorEntity):
    """Whether this zone is actually delivering water.

    **Created only on a multi-zone valve**, since 0.19.0. With one zone it answered exactly
    what `Status` answers, minus warm-up and pause; see `async_setup_entry`.

    Named `Shower Active 2` for zone 2, per :func:`zone_label`. The unique id still carries
    `zone_{n}`, from when a single-zone valve had one too, so history survives.

    **"Active" means flowing, which is not the same as "has outlets assigned".** A paused
    valve keeps its assignment in byte 3 — `0x41` is "paused, outlet 1 still assigned" — but
    no water comes out. This reads the same definition the run-time cutoff detector uses:
    a non-empty mask *and* not paused. Anything else would make the two disagree on screen
    about the state one of them is acting on.

    **Not diagnostic.** It was, with an `enabled by default` override to undo the half of
    that categorisation that would have hidden it — which is the shape of a miscategorised
    entity. On the valve it now exists on, "which of my two showers is running" is primary
    state, the same kind of fact as `Temperature 2`, not an aid to debugging one.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:water"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, valve: Valve, zone: int
    ) -> None:
        super().__init__(coordinator, valve)
        self._zone = zone
        self._attr_name = zone_label(valve, zone, "Shower Active")
        self._attr_unique_id = f"{self._device_id}_zone_{zone}_active"

    @property
    def _word(self):
        state = self._state
        return None if state is None else state.zone_word(self._zone)

    @property
    def is_on(self) -> bool | None:
        word = self._word
        if word is None:
            return None
        return bool(word.outlet_mask) and not word.paused

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Enough to see *why* it reads as it does, and how close a cutoff is.

        `seconds_remaining` is the useful one during a shower. It is None when the limit is
        unknown, when the zone is idle, or after a reconnect — the detector drops its timings
        across a gap rather than reporting a duration it cannot stand behind, and this shows
        that honestly instead of substituting a zero.
        """
        word = self._word
        if word is None:
            return {}
        model = self._valve.model
        base = sum(model.outlets_in_zone(z) for z in model.zones if z < self._zone)
        attributes: dict[str, object] = {
            "outlet_mask": f"0x{word.outlet_mask:02X}",
            # The outlets this zone would resume to — present even while paused, which is
            # exactly when `is_on` is False but the assignment still matters.
            "assigned_outlets": [
                base + bit + 1
                for bit in range(model.outlets_in_zone(self._zone))
                if word.outlet(bit)
            ],
            "paused": word.paused,
        }
        flowing_for = self._valve.zone_flowing_for(self._zone)
        attributes["flowing_for_seconds"] = (
            None if flowing_for is None else round(flowing_for, 1)
        )
        limits = self._valve.run_time_limits_for_zone(self._zone)
        attributes["run_time_limit_seconds"] = list(limits)
        attributes["seconds_remaining"] = (
            None
            if flowing_for is None or not limits
            else round(min(limits) - flowing_for, 1)
        )
        return attributes


class ValvePresetActiveSensor(KohlerValveEntity, BinarySensorEntity):
    """Whether a stored preset or experience is currently driving the valve.

    From `presetOrExperienceId`, and it answers **"is a preset driving this"** — never "is
    water running". Two consequences worth knowing, both measured:

    * It **latches for the whole session.** Changing temperature, flow, or which outlets are
      open does not clear it, so it stays on even once the shower bears no resemblance to the
      preset as saved.
    * It is cleared by **both pause and stop** — any `0x40` or `00`/`00`.

    Opening an outlet directly leaves this off with water flowing, which is the normal case
    for anything driven from Home Assistant.

    It also has a bearing on the run-time cutoff: a cutoff during a preset-driven session
    pauses *every* zone the preset owns, not only the one that expired.

    **Known false negative: a preset started during warm-up.** The valve applies the preset's
    valve word but never sets `presetOrExperienceId`, so this reads OFF while the preset is
    demonstrably in effect — and it does not correct itself when warm-up ends, because
    warm-up ending pauses the valve and ends the session. All 12 `warmUpInProgress` samples
    in the capture corpus carry a preset id of `0`. This is the device's behaviour, not a
    decode problem, and it is reported rather than papered over: guessing "a preset is
    probably running" from a warm-up flag would be inventing state. Pair it with
    `select.anthem_valve_warmup` and the valve word if the distinction matters. Full evidence in `docs/gcs/api.md`.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Preset Active"
    _attr_icon = "mdi:playlist-star"

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_preset_active"

    @property
    def is_on(self) -> bool | None:
        state = self._state
        return None if state is None else state.active_preset_id is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        if state is None:
            return {}
        preset_id = state.active_preset_id
        preset = state.presets.get(preset_id) if preset_id is not None else None
        return {
            "preset_id": preset_id,
            # None rather than a placeholder when the id is one no stored preset matches:
            # ids are slots, and an experience run from the controller shares the id space
            # without appearing in `presets` at all.
            "preset_name": preset.name if preset else None,
        }


class ControllerAccessorySensor(KohlerControllerEntity, BinarySensorEntity):
    """Base for the controller's accessory on/off sensors.

    Each is created only when ``hub-configuration.parts`` reports the hardware attached, and
    each reads a single boolean the controller pushes for that subsystem. Independent of the
    water path — these are the controller's own hardware, reported the same way whatever
    drives the shower, so they are unaffected by the valve-versus-controller split that
    governs the outlet entities.

    All three are on/off only. The controller reports no detail on these channels, and the
    REST equivalents are cached: ``amplifierSettings.monoVolume`` was observed not following
    a live volume change made on the touchscreen, so a volume entity built on it would lie.
    """

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller, key: str
    ) -> None:
        super().__init__(coordinator, controller)
        self._key = key
        self._attr_unique_id = f"{self._device_id}_{key}"

    @property
    def is_on(self) -> bool | None:
        state = self._state
        return None if state is None else getattr(state, f"{self._key}_on")


class ControllerProblemSensor(KohlerControllerEntity, BinarySensorEntity):
    """Whether the controller reports a fault, or a fitted accessory has dropped off.

    Three sources, each one the Konnect app itself shows (Konnect 3.0.6, 2026-10-07):

    * **Fault flags** — ``hub-state`` ``errorState`` / ``errorComponent`` and the
      ``errorstate`` every accessory message carries (`HubState.has_fault`).
    * **Active errors** — ``hub-diagnostics/{id}/active``, what the app words as
      "`<title>` error `<errorCode>` detected".
    * **Disconnected accessories** — a valve body, steam generator, amplifier or light
      group the configuration says is fitted, whose ``parts`` entry is not ``Connected``
      (`HubSettings.disconnected`), plus an absent or empty SD card. The accessory entities
      are gated on ``parts``, so without this a fitted accessory that drops off simply
      vanishes rather than reading as a problem.

    Active errors and the disconnected list are read on each seed (setup and reconnect),
    the flags live. Never seen on the reference system, which has had no fault to show.
    """

    _attr_name = "Problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_problem"

    @property
    def is_on(self) -> bool | None:
        state = self._state
        flags = None if state is None else state.has_fault
        if self._controller.active_errors or self._controller.settings.disconnected:
            return True
        return flags

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        return {
            "error_components": (
                sorted(k for k, v in state.error_components.items() if v)
                if state is not None
                else []
            ),
            "disconnected": list(self._controller.settings.disconnected),
            "active_errors": [
                {
                    "code": entry.get("errorCode"),
                    "title": entry.get("title"),
                    "component": entry.get("component"),
                }
                for entry in self._controller.active_errors
            ],
        }


class ControllerMusicSensor(ControllerAccessorySensor):
    """Whether the controller's amplifier is playing. From ``MUSIC_STS``.

    The only accessory exercised live on the tested system — 24 clean state transitions in
    the captures. Note ``parts`` reports the amplifier under **``amplifier``**, while the
    ``music`` key is null, so a presence check must look at both.
    """

    _attr_name = "Music"
    _attr_icon = "mdi:music"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller, "music")


class ControllerLightSensor(ControllerAccessorySensor):
    """Whether the controller's lighting is on. From ``LIGHT_STS``.

    **Never exercised** — no lighting is attached to the tested system, and the captured
    ``LIGHT_STS`` messages arrive with an empty ``attributes`` array, so even the parse is
    unverified against a system that has the hardware.
    """

    _attr_name = "Light"
    _attr_icon = "mdi:lightbulb"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller, "light")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Each light group's own state — on if **any** group is on.

        `LIGHT_STS` reports one group per message (`component` `lightgroupA/B/C`). Until
        2026-10-07 the last message alone decided this sensor, so a second group turning off
        read as "lights off" while the first was still lit.
        """
        state = self._state
        return {
            "groups": {} if state is None else dict(state.lights),
            "configured_groups": list(self._controller.settings.light_groups),
        }


class ControllerSteamSensor(ControllerAccessorySensor):
    """Whether the steam generator is running. From ``STEAM_STS``.

    **Never exercised** — no steam is attached to the tested system. The captured messages
    do carry a populated attribute (``status``, ``totaltime``, ``temperature``,
    ``starttime``), so the shape is known even though the ON case has never been seen.
    """

    _attr_name = "Steam"
    _attr_icon = "mdi:hot-tub"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller, "steam")

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """The detail `STEAM_STS` carries, which this sensor dropped until 2026-10-07.

        `status` is the raw value — `ON`, `OFF`, or `POWERCLEAN` while the generator cleans
        itself (the app: "Power clean is in progress. Please stay out of your shower.").
        Temperature is as the controller sends it, °F on every surface the app reads.
        """
        state = self._state
        if state is None:
            return {}
        return {
            "status": state.steam_status,
            "power_clean": state.steam_powerclean,
            "temperature": state.steam_temperature,
            "start_time": state.steam_start_time,
            "total_time": state.steam_total_time,
        }


class ControllerOutletSensor(KohlerControllerEntity, BinarySensorEntity):
    """One outlet within one zone, as the controller reports it.

    **This is the controller's belief, not the plumbing.** It does not observe a
    valve-driven session, so during one it reads OFF with an all-zero array while water is
    running — the **Anthem Valve** outlet sensors are what answer "is water coming out of
    this outlet". What these rows answer instead is "does the controller know", which is
    what decides whether its ``stopall`` and its 60-minute session ceiling apply.

    ``Controller.water_is_running`` is the any-of over exactly these, and backs both
    Anthem Plus switches, so a switch here can never disagree with the rows beneath it.

    Addressed per zone for the same reason as the valve's switches: the controller's data is
    per zone, and a global numbering needs a model-dependent split that can be got wrong.
    Each zone reports a **6-slot array padded regardless of hardware**, so only the leading
    slots belonging to that zone's valve carry meaning — reading all six would give a
    2-outlet valve six outlets.
    """

    _attr_icon = "mdi:shower-head"

    def __init__(
        self,
        coordinator: KohlerAnthemCoordinator,
        controller: Controller,
        zone: int,
        outlet: int,
    ) -> None:
        super().__init__(coordinator, controller)
        self._zone = zone
        self._outlet = outlet
        self._attr_name = f"Zone {zone} Outlet {outlet}"
        self._attr_unique_id = f"{self._device_id}_zone_{zone}_outlet_{outlet}"

    @property
    def is_on(self) -> bool | None:
        state = self._state
        if state is None:
            return None
        zone = state.zones.get(self._zone)
        if zone is None:
            return None
        if self._outlet - 1 >= len(zone.outlets):
            return None
        return zone.outlets[self._outlet - 1]
