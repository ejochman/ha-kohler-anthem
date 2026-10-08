"""Outlet switches for the Anthem valve.

One switch per outlet, showing state and accepting commands — the same element does both,
like the Konnect app. Because ``is_on`` reads the valve's reported state rather than
remembering what Home Assistant last sent, a switch follows changes from **any** origin:
the app, the physical touchscreen, a preset, or another automation.

Outlets are addressed **per zone**, matching the hardware: a multi-outlet Anthem is
physically two valve bodies joined, and every API surface addresses them separately. That
also removes a whole class of bug — a global "outlet 1-6" numbering has to be split across
zones differently on every model (2+2 on a K-28211, 3+3 on a K-28212), and getting that
split wrong silently operates the wrong outlet.

Turning one on re-sends the complete valve command with that outlet's bit set and every
other outlet in both zones preserved, at whatever the zone's temperature and flow numbers
hold. The valve accepts no partial write.

Switches are **optimistic**: the toggle moves immediately and the reported state corrects it
about a second later when the valve echoes back. Without that, every tap would sit visibly
stuck for the round trip (measured 1.1-2.1 s on real hardware).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_WARMUP_AUTO_RESTORE,
    DOMAIN,
    OUTLET_TYPE_NAMES,
    OUTLET_TYPE_VARIANTS,
    SHOWER_ON_PRESET_ID,
    WARMUP_AUTO_RESTORE_DELAY_SECONDS,
    WARMUP_AUTO_RESTORE_NO_TARGET,
    WARMUP_AUTO_RESTORE_ON,
)
from .coordinator import Controller, KohlerAnthemCoordinator, Valve
from .entity import (
    KohlerControllerEntity,
    KohlerValveEntity,
    outlet_name,
    slug,
)

_LOGGER = logging.getLogger(__name__)


@callback
def _async_purge_valve_only_warmup_restore(
    hass: HomeAssistant, entry: ConfigEntry, valve: Valve
) -> None:
    """Drop `Warmup Auto-Restore` from a valve on a controller-free account.

    Matched on **this valve's own device id**, not a bare suffix: an account can pair one
    valve with a controller and leave another standing alone, and only the second should
    lose the switch. The condition is the account's controller list, which is not known
    until the coordinator has read it — after `__init__`'s unconditional purge has run.

    **Cleanup, so it never fails setup.** A stale registry row is cosmetic; the switches
    this platform exists to create are not. Registry trouble is logged and stepped over,
    which also keeps the platform constructible against a test double carrying no registry.
    """
    stale = f"{valve.device_id}_warmup_auto_restore"
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
                "Removed %s: the warmup revert it guards against is caused by an "
                "Anthem Plus controller, and this account has none",
                row.entity_id,
            )


def _position_name(valve: Valve, zone: int, outlet: int) -> str:
    """The name `outlet_name` falls back to when the fixture is unknown — `Outlet 1.3`."""
    if len(valve.model.zones) > 1:
        return f"Outlet {zone}.{outlet}"
    return f"Outlet {outlet}"


@callback
def _async_migrate_outlet_unique_ids(
    hass: HomeAssistant, entry: ConfigEntry, valve: Valve
) -> None:
    """Move a position-named outlet switch onto its fixture id, keeping its entity id.

    An outlet's unique id follows its name (`ZoneOutletSwitch`), and its name follows the
    fixture once the valve's `outLetType` maps to one. So a switch first registered as
    `Outlet 1.3` — because the type had not arrived yet, or (before 2026-10-07) because the
    code was not in `OUTLET_TYPE_NAMES` — would come back as a *new* entity named `Foot
    Sprays`, orphaning the old one and every automation pointing at it.

    Rewriting the registry row's unique id instead keeps its entity id, its customisations
    and the automations; only the displayed name moves. Skipped when the fixture id is
    already registered, so a second outlet can never be folded onto the first.

    **Cleanup, so it never fails setup** — same reasoning as the warmup purge above.
    """
    try:
        registry = er.async_get(hass)
        rows = {
            row.unique_id: row
            for row in er.async_entries_for_config_entry(registry, entry.entry_id)
            if row.domain == "switch"
        }
    except Exception:  # Cosmetic cleanup; never worth failing setup over.
        _LOGGER.debug("Entity registry unavailable; outlet ids left as they are")
        return
    for zone in valve.model.zones:
        for outlet in range(1, valve.model.outlets_in_zone(zone) + 1):
            new_id = f"{valve.device_id}_{slug(outlet_name(valve, zone, outlet))}"
            old_id = f"{valve.device_id}_{slug(_position_name(valve, zone, outlet))}"
            if new_id == old_id or new_id in rows or old_id not in rows:
                continue
            row = rows.pop(old_id)
            try:
                registry.async_update_entity(row.entity_id, new_unique_id=new_id)
            except Exception:  # A clash or a registry error: leave the row alone.
                _LOGGER.debug("Could not migrate %s to %s", row.entity_id, new_id)
                continue
            rows[new_id] = row
            _LOGGER.info(
                "%s now names its fixture (%s); entity id kept",
                row.entity_id,
                outlet_name(valve, zone, outlet),
            )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a switch per outlet, per zone, when the account has a valve."""
    coordinator: KohlerAnthemCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SwitchEntity] = []

    # One set per valve. An Anthem Plus controller has no per-outlet command: outlets are
    # chosen by activating a favorite, so a controller-only account gets no outlet
    # switches.
    for valve in coordinator.valves:
        model = valve.model
        entities.append(ShowerSwitch(coordinator, valve))
        # ONLY WHERE THE FAULT CAN OCCUR. The single identified cause of a spontaneous
        # `warmUpDisabled` is the Anthem Plus controller's web UI writing it as a fixed step
        # of its signed-in routine — hub firmware, with the valve merely the recipient. On a
        # controller-free account every `warmUpDisabled` in the corpus traces to that routine
        # or to a post-reboot restatement where the mode did not change, so there is nothing
        # here to defend against and the switch could only add an unprompted writer to the
        # valve. `_attr_entity_registry_enabled_default = False` already hid it; not creating
        # it is the honest version of the same judgement.
        if coordinator.controllers:
            entities.append(WarmupAutoRestoreSwitch(coordinator, valve))
        else:
            _async_purge_valve_only_warmup_restore(hass, entry, valve)
        entities.append(ValveReportLogSwitch(coordinator, valve))
        _async_migrate_outlet_unique_ids(hass, entry, valve)
        entities.extend(
            ZoneOutletSwitch(coordinator, valve, zone, outlet)
            for zone in model.zones
            for outlet in range(1, model.outlets_in_zone(zone) + 1)
        )
    # One set per controller — each is its own device with its own command surface.
    for controller in coordinator.controllers:
        entities += [
            HubShowerSwitch(coordinator, controller),
            HubSystemSwitch(coordinator, controller),
            ControllerReportLogSwitch(coordinator, controller),
        ]
        # Only where a steam generator is attached — the same `parts` gate as the Steam
        # binary sensor. A controller with no steam has nothing for `steamcontrol` to run.
        if controller.capabilities.steam:
            entities.append(HubSteamSwitch(coordinator, controller))

    async_add_entities(entities)


class ShowerSwitch(KohlerValveEntity, SwitchEntity):
    """Whole-shower stop, as ``switch.anthem_valve_shower``.

    **Off stops the system**, sending mask byte ``0x00`` on both zones while each zone keeps
    its own temperature — see ``async_stop_shower()``.

    It used to *pause* (``0x40``), which showed as "Paused" in the status sensor. It moved
    to ``0x00`` on 2026-08-13 so a stop could never be mistaken for the valve's own run-time
    cutoff, which also pauses; see ``async_stop_shower()`` for why it stays.

    **On activates preset ``SHOWER_ON_PRESET_ID``** — one call, no valve write. The valve has
    no "run my default", so a whole-shower start has to name a stored scene; the preset
    supplies the outlets, temperature, and flow that this entity cannot.

    **Not redundant with the outlet switches, despite both running water.** They answer
    different questions. An outlet switch opens *one* outlet at whatever temperature the
    zone already holds — manual control, one outlet at a time. This runs the shower the
    owner configured: outlets, temperature and flow together, in a single command, from a
    preset edited in the Konnect app rather than in this integration. Change that preset
    and this switch starts something different with no code change. The redundancy is only
    at the *off* end, where both stop the water.

    Which means **what "on" does is stored on the valve, not here.** Edit the preset in the
    Konnect app and this switch starts something different, with no code change — the reason
    the id is a constant rather than a hardcoded literal.
    """

    _attr_icon = "mdi:shower"
    # **Renamed from "Shower" in 0.6.5.** Beside the outlet switches — `Rainhead`,
    # `Showerhead`, `Handshower` — a plain "Shower" read as one more outlet, when it is the
    # opposite: the whole-shower control that drives the valves themselves. The name says
    # which thing it acts on rather than what the water comes out of.
    _attr_name = "Shower on"

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        # Unchanged deliberately: the entity id follows this, and `_shower` is what every
        # existing automation and dashboard refers to. A friendlier label is not worth
        # breaking them — unlike the outlet rename, which replaced a name that carried no
        # information at all.
        self._attr_unique_id = f"{self._device_id}_shower"
        self._optimistic: bool | None = None

    @property
    def is_on(self) -> bool | None:
        """True while water is actually flowing from any outlet, in either zone."""
        if self._optimistic is not None:
            return self._optimistic
        state = self._state
        if state is None or state.valve1 is None:
            return None
        return state.is_running

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        if state is None or state.valve1 is None:
            return {}
        return {"paused": state.is_paused}

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_command(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_command(False)

    async def _async_command(self, target: bool) -> None:
        """Start the preset, or stop with mask 0x00. Optimistic, like the outlet switches."""
        self._optimistic = target
        self.async_write_ha_state()
        try:
            if target:
                await self._valve.async_activate_preset(SHOWER_ON_PRESET_ID)
            else:
                await self._valve.async_stop_shower()
        except Exception:
            # The command failed, so stop showing a position the valve never reached.
            self._optimistic = None
            self.async_write_ha_state()
            raise


class WarmupAutoRestoreSwitch(KohlerValveEntity, SwitchEntity):
    """Put the warmup mode back when something outside Home Assistant turns it off.

    **The problem this exists for.** The valve's warmup mode does not stay where it is put:
    the Anthem Plus hub writes it back to `warmUpDisabled` on every signed-in use of its web
    UI — a constant in the hub's login/UI routine, solved 2026-08-21 after six live
    reproductions in a day. Nothing reachable from outside the hub's firmware prevents it,
    so putting the mode back is the fix that exists. See `docs/gcs/api.md` §3h.

    **What this does.** When warmup goes to `warmUpDisabled` and this integration did not
    cause it — announced by the valve over MQTT, or discovered by the reconnect reseed after
    an MQTT outage (a hub sign-in during one causes exactly that; the reseed path acts since
    2026-08-22) — wait 60 seconds, re-check that it is still disabled, and set the mode back
    to the last enabled one seen on the valve. That target is remembered in the entry
    options, so it survives a restart and reinstates what the fixture actually had — never a
    default, because "all outlets" and "selected outlets" are different fixtures' worth of
    water. With no remembered mode it does nothing and says so.

    ⚠️ **This treats a symptom.** It cannot stop the hub's routine writing the field — that
    is hub firmware — and a restore is a write to Kohler's cloud like any other. It is off by
    default because only installs whose hub web UI gets used ever see the disable; turn it on
    when the reverting is actually bothering you.

    Three things it deliberately will not do:

    * **Undo you.** Choosing `Off` on the Warmup dropdown is a write this integration made,
      and a disable it caused is recognised and ignored. Otherwise `Off` would be unusable.
    * **Fight forever.** If the mode is disabled again after each restore, it stops after five
      consecutive attempts and logs why. A retry loop against something actively rewriting the
      field is not a fix, it is just traffic.
    * **Interrupt a shower.** The write is refused while water is running, mirroring the
      Konnect app. The next disable schedules another attempt.

    State lives in the config entry's options, read live by the coordinator, so a toggle takes
    effect on the next message rather than needing a restart.
    """

    _attr_name = "Warmup Auto-Restore"
    _attr_icon = "mdi:restore-alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Off unless someone goes looking for it: this is a workaround for a device fault, not a
    # feature of the shower, and an owner who has never seen the mode revert does not need it.
    #
    # ⚠️ **Specifically a fix for a HUB fault.** The one identified cause of a spontaneous
    # disable is the Anthem Plus controller's web UI, which writes `warmUpDisabled` to the
    # valve as a fixed step of its signed-in routine (`docs/gcs/api.md` §3h) — the write
    # originates in the hub's firmware and the valve is only the recipient. On a
    # **controller-free account there is no known cause at all**, and every `warmUpDisabled`
    # in the corpus traces to that routine or to a post-reboot restatement where the mode did
    # not change. So on a valve-only system this switch defends against nothing observed, and
    # enabling it only adds an unprompted writer to the valve.
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_warmup_auto_restore"

    @property
    def available(self) -> bool:
        """A setting, not a reading — usable before any valve state has arrived."""
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        return self._valve.warmup_auto_restore

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What it would restore to, how long it waits, and whether the fault can occur.

        `hub_present` is the one that decides whether this switch is worth enabling: the only
        identified cause of a spontaneous disable lives in the Anthem Plus controller's
        firmware, so `false` means there is nothing here for it to defend against.
        """
        return {
            "restores_to": self._valve.last_warmup_mode,
            "delay_seconds": WARMUP_AUTO_RESTORE_DELAY_SECONDS,
            "hub_present": bool(self.coordinator.controllers),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, value: bool) -> None:
        self._valve.set_option(CONF_WARMUP_AUTO_RESTORE, value)
        if not value:
            _LOGGER.info("Warmup Auto-Restore disabled")
        elif self._valve.last_warmup_mode is None:
            # On but inert, which is indistinguishable from broken unless it says so.
            _LOGGER.warning(WARMUP_AUTO_RESTORE_NO_TARGET)
        else:
            _LOGGER.warning(
                WARMUP_AUTO_RESTORE_ON,
                self._valve.last_warmup_mode,
                WARMUP_AUTO_RESTORE_DELAY_SECONDS,
            )
        self.async_write_ha_state()


class ZoneOutletSwitch(KohlerValveEntity, SwitchEntity):
    """One outlet within one zone, readable and controllable.

    Entity id is ``switch.anthem_valve_zone_<z>_outlet_<n>``: Home Assistant composes it
    from the device name ("Anthem Valve") and the entity name ("Zone z Outlet n").
    """

    _attr_icon = "mdi:shower-head"

    def __init__(
        self,
        coordinator: KohlerAnthemCoordinator,
        valve: Valve,
        zone: int,
        outlet: int,
    ) -> None:
        super().__init__(coordinator, valve)
        self._zone = zone
        self._outlet = outlet
        self._attr_name = outlet_name(valve, zone, outlet)
        # **The unique id follows the name**, so an outlet whose fixture is known gets
        # `..._rainhead` rather than `..._zone_1_outlet_1`. That is a deliberate break: an
        # entity id naming the fixture is worth more than one naming a position, and Home
        # Assistant keeps the registry entry keyed on this string. See `entity.outlet_name`
        # for what happens when the fixture is not known.
        self._attr_unique_id = f"{self._device_id}_{slug(self._attr_name)}"
        # Holds the requested position until the valve reports back. None means "no
        # pending command — show what the valve says".
        self._optimistic: bool | None = None

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        state = self._state
        if state is None or state.zone_word(self._zone) is None:
            return None
        return state.zone_outlets(self._zone)[self._outlet - 1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The assignment a paused session will resume to.

        A paused valve keeps its outlet bits set while no water flows, so ``is_on`` is off
        and this stays on — the difference between "not running" and "not selected".
        """
        state = self._state
        if state is None or state.zone_word(self._zone) is None:
            return {}
        assigned = state.zone_outlets(self._zone, flowing=False)
        attributes: dict[str, Any] = {"assigned": assigned[self._outlet - 1]}
        # The valve's own type code for this outlet — 62, 52, 1, 11, 39, 21 and so on. It
        # is the only per-outlet identity the hardware reports, and it is what tells a
        # handshower from a tub filler on an install whose outlets are otherwise just
        # numbers. Published raw beside its name. Absent until the valve announces this
        # outlet — the messages arrive one at a time, unprompted.
        outlet_type = self._outlet_type(state)
        if outlet_type is not None:
            attributes["outlet_type"] = outlet_type
            # From the app's own table — see `OUTLET_TYPE_NAMES`. A code outside it (none
            # is known) leaves these keys absent rather than inventing a fixture name.
            name = OUTLET_TYPE_NAMES.get(outlet_type)
            if name is not None:
                attributes["outlet_type_name"] = name
            variant = OUTLET_TYPE_VARIANTS.get(outlet_type)
            if variant is not None:
                attributes["outlet_variant"] = variant
        return attributes

    def _outlet_type(self, state: Any) -> int | None:
        """This outlet's type code, looked up by the valve's own flat 0-based `outLetId`.

        `outlet_limits` is keyed by that flat id, while this entity is addressed per zone —
        the deliberate split described in `ValveModel.outlet_id`. Each valve body
        occupies three `outLetId` slots, so the conversion goes through the model rather
        than assuming the two numbering schemes line up.
        """
        flat = self._valve.model.outlet_id(self._zone, self._outlet)
        limits = state.outlet_limits.get(flat)
        return None if limits is None else limits.outlet_type

    @callback
    def _handle_coordinator_update(self) -> None:
        """Real state has arrived, so the optimistic guess is no longer needed."""
        self._optimistic = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, target: bool) -> None:
        self._optimistic = target
        self.async_write_ha_state()
        try:
            await self._valve.async_set_zone_outlet(
                self._zone,
                self._outlet,
                target,
                # The flow the Flow number is showing for this zone. Without it every
                # outlet toggle would rewrite the word with `DEFAULT_FLOW_PERCENT` and
                # silently undo a flow the user had set.
                flow=self._valve.zone_flow.get(self._zone),
            )
        except Exception:
            # The command failed, so stop showing the position we never reached.
            self._optimistic = None
            self.async_write_ha_state()
            raise


class HubShowerSwitch(KohlerControllerEntity, SwitchEntity):
    """The controller's own shower, via ``valvecontrol {valveOnOff}``.

    **The only bare on/off in the whole system.** It works because the controller stores its
    own default water configuration, so "on" has a meaning without naming a scene. The GCS
    valve has no equivalent — every start there must specify the complete valve state, which
    is why the valve's shower switch activates a preset instead.

    Off stops **water only**, leaving music, steam, and lighting untouched. For a true
    system-wide stop use the System switch, which calls ``stopall``.

    ``is_on`` follows reported state rather than what we last sent, so a shower started from
    the touchscreen or the app shows up here too.

    **It reports the controller's view only — never the valve's.** A shower driven straight
    at the valve through ``solowritesystem`` — which is every shower Home Assistant starts —
    reaches this switch only if the controller happens to register it, which is unreliable
    (51 of 95 immediately, 12 late, 32 never; preset-driven ones never). That is the
    intended reading, not a gap: the switch shows what the controller knows, and its own
    ``valvecontrol OFF`` can only stop a session the controller is party to. For whether
    water is physically running, read the **Anthem Valve** device's Shower on switch and
    sensors, which are authoritative.

    See ``Controller.water_is_running`` for the 2026-08-18 measurement that made this
    the rule.
    """

    _attr_name = "Shower"
    _attr_icon = "mdi:shower"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_shower"
        self._optimistic: bool | None = None

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._controller.water_is_running

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, target: bool) -> None:
        self._optimistic = target
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_hub_shower(self._controller, target)
        except Exception:
            self._optimistic = None
            self.async_write_ha_state()
            raise


class HubSystemSwitch(KohlerControllerEntity, SwitchEntity):
    """Is anything running **that the controller knows about**, and the one control that
    stops all of it.

    ``is_on`` is true when **any** subsystem is active — water, music, steam, or lighting —
    so it answers "is the shower room doing something" in a single row, with an attribute
    breakdown naming which. Off calls ``stopall``, the only command that idles everything.

    **All four subsystems are the controller's own view, water included.** A shower driven
    at the valve through ``solowritesystem`` is invisible here, because it is invisible to
    the controller — and ``stopall``, this switch's off action, would not stop it either.
    Scoping the switch to what its own off action can reach is the point: it stays honest
    about both. The **Anthem Valve** device owns the question "is water running".

    **The two directions are deliberately asymmetric, and this is the honest part.** There is
    no "start everything" concept: the controller cannot turn on music and steam and water
    from one command, and inventing a meaning would be guesswork. So turning it **on** runs
    the controller's default shower (``valvecontrol ON``) — the nearest thing to "on" the
    hardware offers — while turning it **off** stops every subsystem.

    If that asymmetry is unwanted, the alternative is a read-only binary sensor plus a
    separate stop button. That is arguably cleaner but costs two dashboard rows for what is
    usually one glance and one tap.
    """

    _attr_name = "System"
    _attr_icon = "mdi:power"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_system"
        self._optimistic: bool | None = None

    @property
    def _subsystems(self) -> dict[str, bool | None]:
        state = self._state
        if state is None:
            return {}
        return {
            # The controller's own outlet arrays, deliberately — not the valve's. Reading
            # the valve here made this switch report sessions the controller had never been
            # told about; see `Controller.water_is_running`.
            "water": self._controller.water_is_running,
            "music": state.music_on,
            "steam": state.steam_on,
            "light": state.light_on,
        }

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        subsystems = self._subsystems
        if not subsystems:
            return None
        return any(bool(v) for v in subsystems.values())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Which subsystems are on, so "System: on" is never a mystery.

        Accessories the controller does not have report ``None`` rather than ``False``: it
        emits STEAM_STS and LIGHT_STS even for hardware that is not installed, so a flat
        False would imply a steam generator that is merely idle.
        """
        return dict(self._subsystems)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Water only — see the class docstring on why "on" cannot mean everything.
        await self._async_set(
            True, self.coordinator.async_set_hub_shower(self._controller, True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False, self.coordinator.async_stop_hub(self._controller))

    async def _async_set(self, target: bool, action) -> None:
        self._optimistic = target
        self.async_write_ha_state()
        try:
            await action
        except Exception:
            self._optimistic = None
            self.async_write_ha_state()
            raise


class HubSteamSwitch(KohlerControllerEntity, SwitchEntity):
    """The controller's steam generator, at its own default temperature and time.

    ``steamcontrol {steamOnOff}`` — the steam twin of the Shower switch's ``valvecontrol``,
    and the same thing the Konnect app's "Steam start" card sends. There is no temperature
    or duration in the command: the controller runs ``steamSettings.defaultTemperature`` for
    ``defaultTime``, set in the app or on the controller.

    **Added 2026-10-07, app-confirmed but never run against hardware by this integration** —
    no steam generator is attached to the reference system. The app refuses to start steam
    while the shower runs, and so does this (`KohlerAnthemCoordinator.async_set_hub_steam`).

    ``is_on`` reads ``STEAM_STS``; a generator running its self-clean (``POWERCLEAN``)
    reads off with ``power_clean`` true, because it is not steaming for anyone and should
    not be stood in.
    """

    _attr_name = "Steam"
    _attr_icon = "mdi:weather-fog"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        # Distinct from the Steam binary sensor's `_steam`, which stays for existing
        # automations; this is the control, that is the plain reading.
        self._attr_unique_id = f"{self._device_id}_steam_control"
        self._optimistic: bool | None = None

    @property
    def is_on(self) -> bool | None:
        if self._optimistic is not None:
            return self._optimistic
        state = self._state
        return None if state is None else state.steam_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        settings = self._controller.settings
        attributes: dict[str, Any] = {
            "default_temperature": settings.steam_default_temperature,
            "default_time": settings.steam_default_time,
        }
        if state is not None:
            attributes["power_clean"] = state.steam_powerclean
        return attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        self._optimistic = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, target: bool) -> None:
        self._optimistic = target
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_hub_steam(self._controller, target)
        except Exception:
            self._optimistic = None
            self.async_write_ha_state()
            raise


class _ReportLogSwitch(SwitchEntity):
    """Shared half of the Report Log switch — the consumer-side raw MQTT capture.

    **What it is for.** A user who hits a bug — or wants to document a healthy run on
    hardware this integration has never been verified against — flips this on, uses the
    shower, flips it off, and attaches the resulting file to a GitHub issue. It is the
    quick evidence switch, distinct from the development capture in
    `/config/kohler_anthem_raw/` (pinned by `const.py`, per-run files): this one is
    **one file per switch-on**, and a Home Assistant restart mid-capture appends to the
    **same** file rather than starting a new one, because "it breaks when I restart" is a
    bug report too. Semantics live in `anthem/report_log.py`.

    **One capture, two switches.** The same switch appears on the valve and the controller
    device pages (whichever exist), mirroring the diagnostics buttons — both toggle the one
    underlying capture and always show the same state, refreshed together through
    `async_update_listeners`.

    Files land inside the integration's own folder
    (`custom_components/kohler_anthem/reports/`) — the owner's choice, so reports sit
    with the code they describe. Known consequence, also stated in the folder's README: a
    HACS update or reinstall replaces that folder, so files worth keeping should be moved
    out before updating.
    """

    _attr_name = "Report Log"
    _attr_icon = "mdi:record-rec"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        """A tool, not a reading — usable before any device state has arrived."""
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        return self.coordinator.report_log_active

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Where the capture is going, visible without shell access."""
        log = self.coordinator.report_log
        path = log.path if log is not None else None
        return {
            "file": os.path.basename(path) if path else None,
            "folder": "custom_components/kohler_anthem/reports",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_start_report_log()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_stop_report_log()


class ValveReportLogSwitch(_ReportLogSwitch, KohlerValveEntity):
    """The Report Log switch on the Anthem Valve device page."""

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_report_log"


class ControllerReportLogSwitch(_ReportLogSwitch, KohlerControllerEntity):
    """The Report Log switch on the Anthem Plus device page."""

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_report_log"
