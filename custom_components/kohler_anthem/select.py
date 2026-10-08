"""Favorite and experience selection for the Anthem valve and the Anthem Plus controller.

One dropdown that both **starts** a stored scene and **shows which one is running**, because
the valve reports the active scene itself (``presetOrExperienceId``) rather than leaving Home
Assistant to remember what it last sent. A scene started from the Konnect app or the
touchscreen therefore shows up here too.

**"Favorite" is the user-facing word; "preset" is the protocol word.** The Konnect app calls
these favorites, so that is what the entity is called. Everything below the entity layer
keeps Kohler's own vocabulary, because that is what the wire format and the documentation
use. Note the Anthem Plus *controller* has its own, unrelated favorites — those belong to
the "Anthem Plus" device, not this one.

⚠️ **The unique id and the state attributes keep the `favourite` spelling**, which the
entity name carried until 0.21.0. They are identifiers, not display text: moving the id
would orphan every automation and all recorded history, and renaming `favourite_count` or
`no_favourites_reason` would break whatever reads them. The name is what the owner sees,
and it is the only thing that changed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .anthem import WARMUP_MODES_CURRENT
from .const import (
    DEFAULT_ZONE_GROUPING,
    DOMAIN,
    OUTLET_RUN_TIME_APP_SAFE_MAX_SECONDS,
    OUTLET_RUN_TIME_CHOICES_SECONDS,
    PRESET_HIDDEN_IDS,
    WARMUP_LABELS,
    ZONE_GROUPING_OUTLET_LABELS,
    ZONE_GROUPING_SUBDEVICES,
)
from .coordinator import Controller, KohlerAnthemCoordinator, Valve
from .entity import KohlerControllerEntity, KohlerValveEntity, outlet_name

# Shown when no favorite is driving the valve. A `select` must always have its current
# option present in the option list or Home Assistant logs an error on every update, and
# "nothing is running" is a real state that needs a name.
OPTION_OFF = "Off"

# How long a just-chosen option is shown before the device's own answer takes over, if the
# device never agrees. Sized off the two confirmations measured live on 2026-08-21: a warmup
# write confirmed 2.2 s later with its MQTT echo 0.8 s after that, and a controller
# favorite's `FAVORITE_STS` arrived 1.5 s after activation. `async_set_warmup` already
# awaits its own readback chain (`WARMUP_READBACK_DELAYS`, up to ~6 s) before this even
# starts, so this is the margin on top, not the whole budget.
#
# It is a backstop, not the normal path: agreement clears it sooner, every time.
OPTIMISTIC_GRACE_SECONDS = 12.0


class OptimisticOptionMixin:
    """Hold a just-chosen option until the device confirms it, or the grace runs out.

    ⚠️ **Clearing on any coordinator update is not good enough**, and that is what all three
    selects here did until 2026-08-21. This coordinator pushes an update for *every* message
    the system sends, so the optimistic value was routinely dropped within milliseconds —
    while the device still reported the old value — and the dropdown visibly snapped back to
    the old option before jumping forward again when the real confirmation landed. The owner
    reported it as "flip flop", and the logs show exactly why:

    * **Warmup.** Selected at 00:21:53, confirmed by the REST readback at 00:21:55.586, MQTT
      echo at 00:21:56.399. Three coordinator updates inside that window, each one a
      snap-back.
    * **Controller favorite.** `FAVORITE_STS` for "Play Music" landed at 07:23:51.575, with
      `STEAM_STS` at 07:23:50.804 and `MUSIC_STS` at 07:23:51.054 arriving first — two clears
      before the one message that actually carried the answer.

    So the value is cleared on exactly two things: **the device agreeing**, or **the grace
    expiring**. A subclass supplies `_device_option`; this supplies `current_option`.

    The grace timer matters more than it looks. Without it a write the device silently
    ignored would leave the dropdown asserting something untrue until the next coordinator
    update happened to arrive — and this system has gone quiet for hours at a stretch. One of
    these dropdowns can start water, so it must not hold a claim it cannot support.
    """

    _optimistic: str | None = None
    _optimistic_cancel = None

    @property
    def _device_option(self) -> str | None:
        """What the device itself says, ignoring anything chosen but not yet confirmed."""
        raise NotImplementedError

    @property
    def current_option(self) -> str | None:
        if self._optimistic is not None:
            return self._optimistic
        return self._device_option

    def _set_optimistic(self, option: str) -> None:
        self._cancel_optimistic_timer()
        self._optimistic = option
        self.async_write_ha_state()

    def _clear_optimistic(self) -> None:
        self._cancel_optimistic_timer()
        if self._optimistic is None:
            return
        self._optimistic = None
        self.async_write_ha_state()

    def _cancel_optimistic_timer(self) -> None:
        if self._optimistic_cancel is not None:
            self._optimistic_cancel()
            self._optimistic_cancel = None

    def _arm_optimistic_expiry(self) -> None:
        """Give up on the guess after the grace, whatever the device has or has not said."""
        self._cancel_optimistic_timer()

        @callback
        def _expire(_now) -> None:
            self._optimistic_cancel = None
            self._clear_optimistic()

        self._optimistic_cancel = async_call_later(
            self.hass, OPTIMISTIC_GRACE_SECONDS, _expire
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        # The one clear that is always right: the device now reports what was asked for, so
        # the guess has been overtaken by fact and there is nothing left to hold.
        if self._optimistic is not None and self._device_option == self._optimistic:
            self._cancel_optimistic_timer()
            self._optimistic = None
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_optimistic_timer()
        await super().async_will_remove_from_hass()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the favorite selector when the account has a valve."""
    coordinator: KohlerAnthemCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = []
    for valve in coordinator.valves:
        entities.append(FavoriteSelect(coordinator, valve))
        entities.append(ValveExperienceSelect(coordinator, valve))
        entities.append(ValveWarmupSelect(coordinator, valve))
        entities.append(OutletRunTimeSelect(coordinator, valve))
    # Each controller keeps its own favorites on a different command surface from the
    # valve's. Both can exist on one account, on their own devices, which is why they are
    # separate entities rather than one merged list — and why a second controller gets
    # its own dropdown rather than a longer one.
    for controller in coordinator.controllers:
        entities.append(HubFavoriteSelect(coordinator, controller))
        entities.append(HubExperienceSelect(coordinator, controller))
    async_add_entities(entities)


class FavoriteSelect(OptimisticOptionMixin, KohlerValveEntity, SelectEntity):
    """Start a stored favorite, and show which one is running."""

    _attr_icon = "mdi:playlist-play"
    _attr_name = "Favorite"

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_favourite"
        # Holds the requested option until the valve reports back, matching the outlet
        # switches. Activation takes 1-2 s on real hardware.

    @property
    def _presets(self):
        state = self._state
        if state is None:
            return []
        return state.selectable_presets(hidden=PRESET_HIDDEN_IDS)

    @property
    def options(self) -> list[str]:
        """``Off`` plus every selectable favorite, lowest slot first.

        Rebuilt from current state on every read, so a favorite added, renamed, or deleted
        in the Konnect app appears here without a reload — the valve pushes those changes
        over MQTT.
        """
        return [OPTION_OFF] + [preset.name for preset in self._presets]

    @property
    def _device_option(self) -> str | None:
        """The running favorite, or ``Off``.

        ``presetOrExperienceId`` is cleared by **both** pause and stop, so a paused session
        reads as ``Off`` here while the outlet switches still show their assignment. It is
        also never set by a direct outlet command, so opening an outlet by hand leaves this
        at ``Off`` with water running — this reports *what started the session*, not whether
        water is on.
        """
        state = self._state
        if state is None:
            return None
        active = state.active_preset_id
        if active is None:
            return OPTION_OFF
        preset = state.presets.get(active)
        # An id we cannot name — a hidden favorite (preset 1, driven by the shower switch),
        # an experience, or one that arrived before the list did. Reporting an option that
        # is not in `options` makes Home Assistant log an error on every update, so fall
        # back rather than inventing an entry.
        if preset is None or not preset.is_selectable:
            return OPTION_OFF
        if preset.preset_id in PRESET_HIDDEN_IDS:
            return OPTION_OFF
        return preset.name

    @property
    def _experiences(self) -> list[str]:
        """Names of the stored experiences, which this dropdown leaves to its sibling.

        Experiences share the id space with favorites via ``presetOrExperienceId`` and start
        with the same command, but they are firmware programs rather than scenes the owner
        built, and the app lists them separately — so does this integration, on the
        **Experience** select. Named here so an automation that asks this entity for one gets
        pointed at the right place.
        """
        state = self._state
        if state is None:
            return []
        return sorted(preset.name for preset in state.experiences())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        if state is None:
            return {}
        attributes: dict[str, Any] = {
            # The raw id behind the current option, including the ids this entity hides —
            # useful when the dropdown reads Off but something is clearly running.
            "active_preset_id": state.active_preset_id,
            "favourite_count": len(self._presets),
        }
        # Experiences share the slot space but cannot be started, so they are named rather
        # than offered — see `_experiences`. This is *not* the usual reason the dropdown is
        # empty; see `_empty_reason` below for that.
        experiences = self._experiences
        if experiences:
            attributes["experiences"] = experiences
            attributes["experiences_note"] = (
                "Experiences are started from this valve's Experience select."
            )
        # **Why the dropdown is empty, when it is.** An `Off`-only picker is
        # indistinguishable from one that failed to load, and the difference matters:
        # usually nothing is wrong and no favorite has been created yet.
        reason = self._empty_reason
        if reason is not None:
            attributes["no_favourites_reason"] = reason
        return attributes

    @property
    def _empty_reason(self) -> str | None:
        """Why `options` holds nothing but `Off`, or None when it holds a favorite.

        A GCS valve has **ten preset slots**, of which slot 1 is the mandatory default
        shower — hidden here because the Konnect app hides it too, and because it is the
        Shower switch's business rather than a scene to pick (`PRESET_HIDDEN_IDS`). So a
        valve on which nobody has created a favorite has every offerable slot empty, and
        this entity correctly offers nothing. That is the ordinary case, not a fault.

        Written after mistaking exactly this for a bug: diagnostics reported
        ``selectable: 1`` and the picker still offered nothing, because `selectable`
        counts before slot 1 is hidden and the picker offers after. Both numbers are now
        in diagnostics, and this attribute says which case a user is looking at.
        """
        if self._presets:
            return None
        state = self._state
        if state is None:
            return None
        if self._experiences:
            return (
                "No favorites to start. This valve's stored slots are experiences, "
                "which the Experience select starts, plus the default-shower slot, which "
                "the Shower switch runs. Create a favorite in the Kohler Konnect app and "
                "it appears here."
            )
        return (
            "No favorites have been created on this valve. The default-shower slot is "
            "run by the Shower switch rather than listed here. Create a favorite in the "
            "Kohler Konnect app and it appears here — no reload needed."
        )

    async def async_select_option(self, option: str) -> None:
        """Start the named favorite, or stop the shower.

        Resolved **by name at call time**, never by a remembered id: preset ids are slots
        that get reused, so a stale id stays valid while pointing at a different scene.
        """
        if option == OPTION_OFF:
            await self._async_command(OPTION_OFF, self._valve.async_stop_shower())
            return

        state = self._state
        preset = (
            None if state is None else state.preset_by_name(option, PRESET_HIDDEN_IDS)
        )
        if preset is None:
            # An experience named by an automation reaches here, because `preset_by_name`
            # only resolves selectable slots. Saying so beats "no such favorite", which
            # is misleading when the thing plainly exists in the app.
            if option in self._experiences:
                raise HomeAssistantError(
                    f"{option!r} is an Anthem experience, not a favorite. Start it from "
                    "this valve's Experience select instead."
                )
            raise HomeAssistantError(
                f"No Anthem favorite called {option!r}. It may have been renamed or "
                "deleted in the Konnect app."
            )
        await self._async_command(
            option, self._valve.async_activate_preset(preset.preset_id)
        )

    async def _async_command(self, option: str, action) -> None:
        self._set_optimistic(option)
        try:
            await action
        except Exception:
            # The command failed, so stop showing a state the valve never reached.
            self._clear_optimistic()
            raise
        # The command was accepted. The device's own confirmation is still in flight — 1.5 s
        # for a controller favorite, measured — so hold the guess until it lands rather than
        # dropping it on the next unrelated message.
        self._arm_optimistic_expiry()


class ValveExperienceSelect(OptimisticOptionMixin, KohlerValveEntity, SelectEntity):
    """Start one of the valve's stored experiences, and show which one is running.

    Experiences are firmware programs — Wake Up, Cool Down, the ice-shower routines — stored
    in the same slots as favorites (``isExperience: "True"`` in ``gcs-preset``) and started
    with the **same** ``controlpresetorexperience {preset, action}`` body: that is what
    Konnect 3.0.6's current screens send for an experience id (17 and up).

    ⚠️ **App-confirmed, not yet live-verified here.** Until 2026-10-07 this integration held
    that the valve ignores the command for an experience, on no recorded test, and offered
    none. If the valve does ignore it on some firmware, the dropdown falls back to ``Off``
    once the grace runs out, because the valve never reports the experience as running.
    """

    _attr_icon = "mdi:creation"
    _attr_name = "Experience"

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_experience"

    @property
    def options(self) -> list[str]:
        state = self._state
        names = [] if state is None else [x.name for x in state.experiences()]
        return [OPTION_OFF, *names]

    def _running(self):
        state = self._state
        if state is None or state.active_preset_id is None:
            return None
        preset = state.presets.get(state.active_preset_id)
        return preset if preset is not None and preset.is_experience else None

    @property
    def _device_option(self) -> str | None:
        """The running experience, from ``presetOrExperienceId``, or ``Off``."""
        if self._state is None:
            return None
        running = self._running()
        return OPTION_OFF if running is None else running.name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        running = self._running()
        return {"active_experience_id": None if running is None else running.preset_id}

    async def async_select_option(self, option: str) -> None:
        """Start the named experience, or stop the running one. Resolved by name."""
        if option == OPTION_OFF:
            running = self._running()
            if running is None:
                return
            await self._async_command(
                OPTION_OFF,
                self._valve.async_control_experience(running.preset_id, False),
            )
            return
        state = self._state
        experience = None if state is None else state.experience_by_name(option)
        if experience is None:
            raise HomeAssistantError(
                f"No Anthem experience called {option!r} on this valve. Add it in the "
                "Konnect app first."
            )
        await self._async_command(
            option, self._valve.async_control_experience(experience.preset_id, True)
        )

    async def _async_command(self, option: str, action) -> None:
        self._set_optimistic(option)
        try:
            await action
        except Exception:
            self._clear_optimistic()
            raise
        self._arm_optimistic_expiry()


class ValveWarmupSelect(OptimisticOptionMixin, KohlerValveEntity, SelectEntity):
    """The valve's warmup mode — the dropdown, and the state, are the mode itself.

    Warmup runs water up to temperature before the session proper. This entity is the
    **mode**: which warmup the valve will do, with ``Off`` as one of the choices. It is not
    whether a warm-up is happening at this instant — those are two independent axes in the
    device's own state (``warmUpState.warmUp`` vs ``warmUpState.state``), and a control bound
    to the second reads "off" almost always, because a warm-up is over in seconds. "Warming
    Up" is reported by the valve Status sensor; both axes appear in the attributes here.

    **Three options** — ``Off``, ``All Outlets``, ``Started Outlets``, all with no start
    delay. These are the three modes the current Konnect app can write, but the labels are
    this integration's own since 2026-08-21 and no longer echo the app's wording; see
    ``WARMUP_LABELS`` in ``const.py``. Which outlets ``Started Outlets`` refers to is not
    exposed by any cloud API: it is per-zone `warmupOutlets` on the controller's local API,
    so this dropdown chooses the *mode* and the selection itself is configured on the device.

    ⚠️ **A valve can hold a mode this list does not offer.** Two legacy delayed-start values
    still parse in firmware. If the valve reports one, it is appended to the options for as
    long as it is in force, so the entity reports the truth rather than an error — but it is
    never on the menu otherwise, because nothing establishes what their delay does.

    ⚠️ **The Anthem Plus hub sets this back to Off on every signed-in use of its web UI.**
    Solved 2026-08-21: a fixed routine in the hub's firmware writes ``warmUpDisabled`` on
    every login/UI action — a PIN sign-in alone is enough — and no setting reachable from
    outside the hub prevents it. **If this dropdown moves to Off on its own, someone used
    the hub's web UI**; the Warmup Auto-Restore switch is the mitigation. See
    `docs/gcs/api.md` §3h.

    The value here is the REST field, `warmUpState.warmUp` — read at setup, on every MQTT
    reconnect, and again after every write, because a 200 from the cloud is not evidence the
    valve applied anything. The device's `GCS_WARM_STS` push corrects it too; measured
    2026-08-20, that echo lands **3.42 s** after the write and the REST field catches up on
    about the same schedule, which is why the confirmation is retried rather than read once.
    """

    _attr_icon = "mdi:thermometer-water"
    _attr_name = "Warmup"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_warmup"

    @property
    def _mode(self) -> str | None:
        state = self._state
        return None if state is None else state.warmup_mode

    @property
    def options(self) -> list[str]:
        """The three current modes, plus whatever the valve is holding if it is not one.

        Home Assistant logs an error on every update when `current_option` is absent from
        this list, so a mode we would never write still has to appear while it is in force.
        """
        labels = [WARMUP_LABELS[mode] for mode in WARMUP_MODES_CURRENT]
        held = self._mode
        if held is not None and held not in WARMUP_MODES_CURRENT:
            labels.append(WARMUP_LABELS.get(held, held))
        return labels

    @property
    def _device_option(self) -> str | None:
        mode = self._mode
        if mode is None:
            # Never announced. `Off` would be a guess, and the wrong one to show for a
            # setting somebody may be trying to confirm is on.
            return None
        return WARMUP_LABELS.get(mode, mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The raw mode string, and the axis this entity deliberately does not show."""
        state = self._state
        if state is None:
            return {}
        attributes: dict[str, Any] = {
            "warmup_mode": state.warmup_mode,
            "warmup_in_progress": state.warmup_in_progress,
        }
        # **A valve that has never reported a mode is not the same as one set to Off**, and
        # the difference is not otherwise visible: both read blank here. Seen on a
        # controller-free K-28210 pair where one valve reports `warmUpDisabled` and the
        # other reports nothing at all — no `warmUpState` in its REST seed and no
        # `GCS_WARM_STS` since. A write to such a valve is accepted by the cloud with
        # HTTP 200 and may simply be ignored, which looks identical to it working.
        if state.warmup_mode is None:
            attributes["mode_never_reported"] = True
            attributes["note"] = (
                "This valve has not reported a warm-up mode. The cloud accepts a warm-up "
                "command with HTTP 200 whether or not the valve applies it, so a change "
                "made here may silently do nothing. Watch this entity after setting it: "
                "if the value does not stick, the valve is not honouring the command."
            )
        return attributes

    async def async_select_option(self, option: str) -> None:
        """Write the mode behind the chosen label.

        Optimistic like the favorite selector: the valve echoes the new mode back as a
        `GCS_WARM_STS` message, and that echo is the only real confirmation there is — a 200
        from the cloud means the command was accepted, never that the valve applied it.
        """
        mode = next(
            (value for value, label in WARMUP_LABELS.items() if label == option), None
        )
        if mode is None:
            raise HomeAssistantError(f"{option!r} is not a warmup mode")
        if mode not in WARMUP_MODES_CURRENT and mode != self._mode:
            raise HomeAssistantError(
                f"{option!r} is a legacy mode this integration does not write. It is listed "
                "only because the valve is currently holding it."
            )
        self._set_optimistic(option)
        try:
            await self._valve.async_set_warmup(mode)
        except Exception:
            self._clear_optimistic()
            raise
        # `async_set_warmup` has already read the mode back and applied it, so in the normal
        # case the device agrees by now and the next coordinator update clears this. The
        # timer only matters for the case that call warns about: the cloud accepting a
        # command the valve then ignores.
        self._arm_optimistic_expiry()


class HubFavoriteSelect(OptimisticOptionMixin, KohlerControllerEntity, SelectEntity):
    """Start a stored controller favorite, and show which one is running.

    The controller's equivalent of the valve's preset picker, and its **only** unit of
    control: there is no "set temperature and outlets now" command on this device — you
    create a favorite holding that configuration and activate it.

    Two differences from the valve side worth knowing:

    * **Favorite ids are genuinely reassigned.** Deleting one shifts the others, confirmed
      by an `AllOff-omit` moving from id 6 to id 5 between two reads. GCS presets are fixed
      slots; these are a list. So resolving by name at call time is not a nicety here, it is
      the only correct approach.
    * **Editing is blocked while the system runs** (HTTP 400, `statusCode 902`), though
      *activating* is allowed at any time. That is why the practical pattern is one
      favorite per state, switched by activation.

    Options come from the favorites list, which is seeded over REST and then kept current
    by ``FAVORITES_SNAPSHOT`` — the controller pushes a full list after every create, edit,
    and delete, so the dropdown follows the app without a reload.
    """

    _attr_icon = "mdi:playlist-star"
    _attr_name = "Favorite"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_favourite"

    @staticmethod
    def _name_of(favorite: dict) -> str:
        """The favorite's name, from whichever key this source happens to use.

        **The two sources disagree, and the list is fed by both.** REST ``hub-favorites``
        returns ``title`` with no ``name``; MQTT ``FAVORITES_SNAPSHOT`` returns ``name`` with
        no ``title`` — same ids, same favorites, different key. Since the REST seed is later
        replaced wholesale by snapshots, reading only one key works until the first snapshot
        arrives and then silently empties the dropdown.
        """
        return str(favorite.get("name") or favorite.get("title") or "").strip()

    @property
    def _favorites(self) -> list[dict]:
        """Named favorites only, excluding experiences.

        ``isExperience`` appears in the REST list and marks a firmware program rather than a
        user scene. It is absent from the MQTT snapshot, so this filters what it can see and
        treats a missing flag as "not an experience" — the same direction of error as the
        name fallback above, preferring to show a favorite over hiding one.
        """
        return [
            f
            for f in self._controller.favorites
            if self._name_of(f)
            and str(f.get("isExperience", "")).strip().lower() != "true"
        ]

    @property
    def options(self) -> list[str]:
        names = [self._name_of(f) for f in self._favorites]
        # `FAVORITE_STS` can name a favorite the list has not caught up with — one created
        # moments ago, or a cold start before the first snapshot lands. Home Assistant logs
        # an error on every update when `current_option` is missing from `options`, so carry
        # it while it is in force, the same way `ValveWarmupSelect` carries a legacy mode.
        state = self._state
        running = None if state is None else state.active_favorite_name
        if running and running not in names:
            names.append(running)
        return [OPTION_OFF, *names]

    @property
    def _device_option(self) -> str | None:
        """The running favorite, or ``Off``.

        ``FAVORITE_STS`` reports the active favorite's **name** alongside its id, and the
        name is preferred: it is right even before the favorites list has been seeded, and
        it cannot be thrown off by ids being reassigned when a favorite is deleted. The id
        lookup stays as a fallback for a message that somehow carried no name.

        ``active_favorite_id`` of ``None`` means nothing is driving the system — either a
        ``status: "OFF"`` message or an id of ``"0"``. An id that resolves to no name falls
        back to ``Off`` rather than inventing an option.
        """
        state = self._state
        if state is None:
            return None
        active = state.active_favorite_id
        if active is None:
            return OPTION_OFF
        # Safe to return directly: `options` carries this name whether or not the favorites
        # list knows it yet.
        if state.active_favorite_name:
            return state.active_favorite_name
        for favorite in self._favorites:
            if str(favorite.get("id")) == str(active):
                return self._name_of(favorite)
        return OPTION_OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        return {
            "active_favorite_id": None if state is None else state.active_favorite_id,
            "active_favorite_name": (
                None if state is None else state.active_favorite_name
            ),
            "favourite_count": len(self._favorites),
        }

    async def async_select_option(self, option: str) -> None:
        """Activate the named favorite, or stop everything.

        Resolved by name at call time. Ids shift when a favorite is deleted, so a
        remembered one would eventually start the wrong scene.
        """
        if option == OPTION_OFF:
            # `valvecontrol OFF`, not `stopall`: this dropdown selects a *water* scene, so
            # its Off should stop water and leave music, steam, and lighting alone. The
            # whole-system stop lives on the System switch.
            await self._async_command(
                OPTION_OFF,
                self.coordinator.async_set_hub_shower(self._controller, False),
            )
            return
        wanted = option.strip().lower()
        favorite = next(
            (f for f in self._favorites if self._name_of(f).lower() == wanted), None
        )
        if favorite is None:
            raise HomeAssistantError(
                f"No Anthem Plus favorite called {option!r}. It may have been renamed or "
                "deleted in the Konnect app."
            )
        await self._async_command(
            option,
            self.coordinator.async_activate_favorite(
                self._controller, favorite.get("id"), self._name_of(favorite)
            ),
        )

    async def _async_command(self, option: str, action) -> None:
        self._set_optimistic(option)
        try:
            await action
        except Exception:
            self._clear_optimistic()
            raise
        # The command was accepted. The device's own confirmation is still in flight — 1.5 s
        # for a controller favorite, measured — so hold the guess until it lands rather than
        # dropping it on the next unrelated message.
        self._arm_optimistic_expiry()


class HubExperienceSelect(OptimisticOptionMixin, KohlerControllerEntity, SelectEntity):
    """Start one of the controller's experiences, and show which one is running.

    The controller's firmware programs — Breathe, Cool Down, Focus, the steam coaches, the
    ice showers — listed by ``hub-experience/{id}/experiences`` under three categories, each
    with its own control endpoint. The command is ``{name: <title>, status}`` on the
    category's endpoint (``KohlerAnthemCoordinator.async_control_hub_experience`` picks it);
    run state comes back as ``SHOWER_EXP_STS`` / ``STEAM_EXP_STS`` / ``ICE_SHOWER_EXP_STS``.

    Experiences run from zone 1's first outlet — the app says so — whatever the favorite
    setup. Added 2026-10-07 from Konnect 3.0.6; the endpoints were already known, the
    catalogue and the run-state messages were not read until then.
    """

    _attr_icon = "mdi:creation"
    _attr_name = "Experience"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_experience"

    @property
    def _titles(self) -> list[str]:
        titles: list[str] = []
        for items in self._controller.experiences.values():
            for item in items:
                title = str(item.get("title") or item.get("name") or "").strip()
                if title and title not in titles:
                    titles.append(title)
        return titles

    @property
    def options(self) -> list[str]:
        titles = self._titles
        state = self._state
        running = None if state is None else state.active_experience
        # Same carry as the favorite picker: a running title the catalogue lacks must still
        # be a valid option, or Home Assistant logs an error on every update.
        if running and running not in titles:
            titles.append(running)
        return [OPTION_OFF, *titles]

    @property
    def _device_option(self) -> str | None:
        state = self._state
        if state is None:
            return None
        return state.active_experience or OPTION_OFF

    async def async_select_option(self, option: str) -> None:
        """Start the named experience, or stop the running one."""
        if option == OPTION_OFF:
            state = self._state
            running = None if state is None else state.active_experience
            if running is None:
                return
            await self._async_command(
                OPTION_OFF,
                self.coordinator.async_control_hub_experience(
                    self._controller, running, False
                ),
            )
            return
        await self._async_command(
            option,
            self.coordinator.async_control_hub_experience(
                self._controller, option, True
            ),
        )

    async def _async_command(self, option: str, action) -> None:
        self._set_optimistic(option)
        try:
            await action
        except Exception:
            self._clear_optimistic()
            raise
        self._arm_optimistic_expiry()


def _duration_label(seconds: int) -> str:
    """`1800` -> `"30 minutes"`, the way the Konnect app words it."""
    return f"{seconds // 60} minutes"


class OutletRunTimeSelect(KohlerValveEntity, SelectEntity):
    """Max Shower Duration — the Konnect app's own six options.

    **A select rather than a slider, and that is a finding rather than a style choice.**
    `docs/gcs/api.md` left it open whether 3600 s is a ceiling or the top of an allowed list:
    the app's picker is curated — 15/20/25/30/45/60 minutes — skipping 35, 40, 50 and 55,
    which are legal multiples of 300 s that no shipped picker can produce. Whether the valve
    would accept one is untested. Offering exactly what the app offers claims only what is
    known; a slider would imply the gaps are reachable.

    3600 s is live-verified writable (write sweep, 2026-08-21), which is what makes 45 and 60
    real options rather than guesses.

    ⚠️ **Konnect 3.0.1 misreads anything above 30 minutes.** Its picker snaps the device's
    value into 15-30 before choosing a wheel index, so a valve set to 45 or 60 displays as 25
    and a single tap of Save silently writes 1500 s. Fixed in 3.0.5. Choosing a higher value
    here is safe for the valve and safe in a current app; it is only an out-of-date app that
    would quietly undo it, which `long_duration_app_warning` says on the entity itself.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-cog-outline"
    _attr_entity_registry_enabled_default = True

    @property
    def options(self) -> list[str]:
        """Built per instance — a shared mutable class attribute is one edit from a bug."""
        return [_duration_label(s) for s in OUTLET_RUN_TIME_CHOICES_SECONDS]

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_name = "Max Shower Duration"
        # Not the diagnostic sensor's id: that entity stays as the read-only reading, and
        # reusing its id would swap one for the other in every existing dashboard.
        self._attr_unique_id = f"{self._device_id}_max_run_time_setting"

    @property
    def _seconds(self) -> int | None:
        """What the valve currently holds — the shortest, as the sensor reports."""
        run_times = self._valve.outlet_run_times
        return min(run_times.values()) if run_times else None

    @property
    def current_option(self) -> str | None:
        """`None` where the valve holds something the app cannot offer.

        A duration outside the six is not an error — 2700 and others are legal and this
        integration could have written one before the list existed — but reporting it as one
        of the options would be a lie, and inventing an option would let a save rewrite it.
        `reported_minutes` publishes the real figure either way.
        """
        seconds = self._seconds
        if seconds is None or seconds not in OUTLET_RUN_TIME_CHOICES_SECONDS:
            return None
        return _duration_label(seconds)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the valve holds, and whether its outlets agree about it.

        `outlets_agree: false` means **a write was lost**, not that the outlets are
        configured differently — there is one duration to configure, and the app writes it
        one outlet at a time, stopping at the first failure. Selecting a duration here
        rewrites every outlet and repairs it.

        Carried over from the diagnostic sensor this control replaced in 0.18.1: the
        distinction was established on real hardware (one valve at 3600 s on its Showerhead
        and 1800 s on the other two) and would have been lost with it.
        """
        seconds = self._seconds
        run_times = self._valve.outlet_run_times
        attributes: dict[str, Any] = {
            # Always the truth, including when it is not one of the six above.
            "reported_minutes": None if seconds is None else seconds / 60,
            "in_app_picker": seconds in OUTLET_RUN_TIME_CHOICES_SECONDS,
            # See the class docstring: only an out-of-date Konnect build is affected.
            "long_duration_app_warning": (
                seconds is not None and seconds > OUTLET_RUN_TIME_APP_SAFE_MAX_SECONDS
            ),
        }
        if run_times:
            attributes["outlets_agree"] = len(set(run_times.values())) == 1
            per_outlet: dict[str, float] = {}
            raw_grouping = getattr(self._valve, "zone_grouping", DEFAULT_ZONE_GROUPING)
            grouping = (
                ZONE_GROUPING_OUTLET_LABELS
                if raw_grouping == ZONE_GROUPING_SUBDEVICES
                else raw_grouping
            )
            for outlet, value in sorted(run_times.items()):
                # `outlet_run_times` is 1-based and `outlet_location` expects that — passing
                # `outlet + 1` here is the off-by-one that made the old sensor raise on every
                # attribute read and show `unknown` (fixed 0.16.1).
                zone, index = self._valve.model.outlet_location(outlet)
                name = outlet_name(self._valve, zone, index + 1, grouping=grouping)
                per_outlet[name] = value / 60
            attributes["per_outlet"] = per_outlet
        return attributes

    async def async_select_option(self, option: str) -> None:
        try:
            seconds = next(
                s
                for s in OUTLET_RUN_TIME_CHOICES_SECONDS
                if _duration_label(s) == option
            )
        except (
            StopIteration
        ) as err:  # pragma: no cover - Home Assistant validates first
            raise HomeAssistantError(f"{option} is not a Max Shower Duration") from err
        await self._valve.async_write_outlet_setting(maximum_run_time=seconds)
        self.async_write_ha_state()
