"""Coordinator: holds the MQTT stream, the REST client, and per-device state.

State is **push-only**. MQTT carries every change as it happens and there is no polling
interval at all — REST is read on events, never on a clock.

Three things this has to get right:

* **Cold start.** MQTT is event-driven and silent until the shower next changes, so a
  restart would leave every entity unknown. One REST read at setup seeds everything.
* **Reconnects.** The broker replays nothing on connect: measured across 27 sessions, the
  first message is always a change event and six sessions received nothing for hours. So
  every connect re-seeds, which is what makes dropping the poll safe.
* **Token rotation.** B2C issues a new refresh token on every refresh and invalidates the
  old one. Losing it strands the account, so it is written back to the config entry
  whenever it changes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import Counter, deque
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .anthem import (
    MSG_GCS_SOLO_STATUS,
    MSG_GCS_WARMUP_STATUS,
    WARMUP_README,
    AnthemMqttStream,
    AuthError,
    AuthUnavailable,
    CutoffDebugLog,
    Device,
    DeviceOffline,
    Envelope,
    GcsDevice,
    GcsState,
    HubCapabilities,
    HubDevice,
    HubState,
    KohlerAuth,
    KohlerClient,
    KohlerError,
    RawMqttLog,
    ReportLog,
    ValveModel,
    ZoneCutoff,
    ZoneCutoffDetector,
    ZoneReading,
    describe_topology,
    get_valve_model,
    model_for_topology,
    topology_from_hub_configuration,
    topology_from_valve_settings,
    unit_to_celsius,
)
from .anthem.entry_reload import reload_signature
from .anthem.state import outlet_limits_from_settings
from .anthem.valve_hex import (
    TEMPERATURE_MAX_TENTHS,
    TEMPERATURE_TENTHS_PER_DEGREE,
    UNUSED_VALVE_WORD,
    VALVE1_PREFIX,
    VALVE2_PREFIX,
    VALVE_STOP_MASK,
    ValveHexError,
    decode_word,
    encode_word,
    normalize_word,
)
from .anthem.warmup_resume import Decision, WarmupResume
from .cloud_watch import CloudConnectionWatch
from .const import (
    CONF_LAST_WARMUP_MODE,
    CONF_MOBILE_DEVICE_ID,
    CONF_OUTLET_RUN_TIMES,
    CONF_REFRESH_TOKEN,
    CONF_REPORT_LOG_FILE,
    CONF_RESTART_ON_RUNTIME_CUTOFF,
    CONF_TEMPERATURE_UNIT,
    CONF_TENANT_ID,
    CONF_VALVE_MODEL,
    CONF_VALVES,
    CONF_WARMUP_AUTO_RESTORE,
    CONF_WATER_UNITS,
    CONF_ZONE_OUTLETS,
    CUTOFF_DEBUG_LOG_KEEP_FILES,
    DEFAULT_FLOW_PERCENT,
    DEFAULT_PRESET_ID,
    DEFAULT_PRESET_TIMER_SECONDS,
    DEVICE_NAME_CONTROLLER,
    DEVICE_NAME_VALVE,
    DOMAIN,
    ENABLE_CUTOFF_DEBUG_LOG,
    ENABLE_RAW_MQTT_LOG,
    ENABLE_WARMUP_DEBUG_LOG,
    ENDLESS_SHOWER_NOT_SET_UP,
    ENDLESS_SHOWER_NOTHING_TO_RESTORE,
    ENDLESS_SHOWER_ON,
    ENDLESS_SHOWER_RESTARTED,
    ISSUE_NOT_SET_UP,
    OUTLET_WRITE_VERIFY_DELAY_SECONDS,
    RAW_MQTT_LOG_DIR,
    RAW_MQTT_LOG_KEEP_FILES,
    RAW_MQTT_LOG_MAX_BYTES,
    RELOAD_IGNORED_DATA_KEYS,
    RELOAD_IGNORED_OPTION_KEYS,
    REPORT_LOG_DIR_NAME,
    REPORT_LOG_MAX_BYTES,
    SCAN_INTERVAL,
    SYNC_DEFAULT_PRESET_TIMER,
    USAGE_REFRESH_DELAY_SECONDS,
    WARMUP_CONTEXT_AFTER_SECONDS,
    WARMUP_CONTEXT_BEFORE_SECONDS,
    WARMUP_CONTEXT_MAX_MESSAGES,
    WARMUP_DEBUG_LOG_KEEP_FILES,
)
from .warmup_manager import WarmupManager

_LOGGER = logging.getLogger(__name__)


#: Keys that hold the version a device is *running*, in the two nested blocks Kohler uses.
#: Ordered — the first match wins. Deliberately excludes anything desired/target shaped:
#: ``firmwareUpdate`` can carry the version the cloud wants installed, and reporting that as
#: the running version would be worse than reporting nothing.
_FIRMWARE_CURRENT_KEYS = (
    # Kohler's own shape, confirmed 2026-09-10 against two K-28210 valves: an OTA record
    # reports `updatedVersion` (what is now running) alongside `initialVersion` (what it was
    # before). `updatedVersion` leads for that reason.
    "updatedVersion",
    "currentFirmwareVersion",
    "currentVersion",
    "firmwareVersion",
    "swVersion",
    "firmware",
    "version",
)

#: A valve reports several OTA payloads, distinguished by `firmwareType`. `Application` is
#: the valve's actual firmware; `Assets` is the bundled UI artwork, which carries its own
#: unrelated version — 2.00 while the application is 2.20 on the owner's left valve. Reading
#: whichever arrived first gave the Assets number, so the application build is preferred and
#: anything else is only a fallback.
_FIRMWARE_PREFERRED_TYPE = "Application"


def _firmware_string(value: Any) -> str | None:
    """A firmware version as a non-empty string, or None.

    Numbers are accepted and stringified — a valve reporting `74` rather than `"00.74"` is
    still answering the question. Bools are rejected: `True` is not a version, and in Python
    it would otherwise pass an `isinstance(..., int)` test.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _firmware_from_block(block: Any) -> str | None:
    """Pull a running-firmware version out of one of Kohler's nested blocks.

    Handles the two shapes seen in the wild: a flat mapping of version keys, and an Azure IoT
    device twin whose real content sits under ``reported``. A string block is taken as the
    version itself, which is what a bare ``otaReportedProperties: "00.74"`` would be.
    """
    if isinstance(block, str):
        return _firmware_string(block)
    if not isinstance(block, dict):
        return None
    # Azure IoT device twins nest the live values one level down.
    for nested in ("reported", "properties"):
        inner = block.get(nested)
        if isinstance(inner, dict):
            found = _firmware_from_block(inner)
            if found is not None:
                return found
    for key in _FIRMWARE_CURRENT_KEYS:
        found = _firmware_string(block.get(key))
        if found is not None:
            return found
    return None


def entry_reload_signature(entry: ConfigEntry) -> tuple[Any, ...]:
    """Fingerprint the parts of a config entry that are worth a reload.

    Shared by the coordinator, which takes one at setup, and `_async_update_listener` in
    `__init__.py`, which takes one per update and compares. Both must apply the same
    exclusions or the comparison means nothing, so there is one call site for the pair of
    key sets rather than two that can drift.
    """
    return reload_signature(
        entry.data,
        entry.options,
        ignore_data=RELOAD_IGNORED_DATA_KEYS,
        ignore_options=RELOAD_IGNORED_OPTION_KEYS,
    )


def describe_duration(run_times: dict[int, int]) -> str:
    """Max Shower Duration in minutes, the way the Konnect app states it.

    Reads outlet 1 — zone 1's first outlet — because the app presents one duration for the
    whole system and every install seen has all outlets on the same value. Falls back to the
    lowest-numbered outlet that has reported, so a partly-learned valve still names a real
    number instead of nothing.
    """
    if not run_times:
        return "?"
    seconds = run_times.get(1) or run_times[min(run_times)]
    return f"{seconds / 60:g}"


def _command_half(value: str, field: str) -> str:
    """Validate a user-supplied valve word, accepting either length the system shows.

    `normalize_word` truncates to the first 8 characters, which is right for device data —
    the valve reports 16-character words whose second half is sensor feedback. But it means
    a 10-character typo silently becomes a valid, *different* command, and this input reaches
    something that opens water valves.

    So the length is checked first, and only the two lengths a person could legitimately have
    are allowed: **8** (a command word) or **16** (what `sensor.anthem_valve_zone_N_hex`
    displays, so it can be pasted straight in). Anything else is a mistake, not a shorthand.
    """
    text = str(value or "").strip()
    if len(text) not in (8, 16):
        raise HomeAssistantError(
            f"{field}: expected 8 characters (a command word) or 16 (as shown by the "
            f"Zone Hex sensor), got {len(text)}: {value!r}"
        )
    try:
        word = normalize_word(text)
    except ValveHexError as err:
        raise HomeAssistantError(f"{field}: {err}") from err

    # **The temperature ceiling applies here too.** This is the one path that reaches the
    # valve without going through `encode_word`, which clamps every other caller to
    # `TEMPERATURE_MAX_TENTHS`. The word carries a 10-bit temperature, so a hand-typed or
    # scripted word can encode 102.3 °C — 216 °F — and be sent verbatim. Whether the firmware
    # would honour it is untested, and this integration should not be the thing depending on
    # that answer.
    #
    # Only the temperature is checked. Outlet masks, flow bytes, pause and warm-up flags are
    # exactly what this escape hatch exists to experiment with, and none of them can scald.
    ceiling = TEMPERATURE_MAX_TENTHS / TEMPERATURE_TENTHS_PER_DEGREE
    try:
        commanded = decode_word(word).temperature_celsius
    except ValveHexError as err:  # pragma: no cover - normalize_word already validated
        raise HomeAssistantError(f"{field}: {err}") from err
    if commanded > ceiling:
        raise HomeAssistantError(
            f"{field}: that word commands {commanded:.1f} °C, above the "
            f"{ceiling:.1f} °C the valve is written to anywhere else. Refused — check the "
            f"temperature bytes."
        )
    return word


def _describe_word(word: str) -> str:
    """Plain-language reading of a command word, for logs and service responses.

    Deliberately tolerant: this only ever annotates something that has already been
    validated and is about to be sent, so a decode failure must not block the write.
    """
    try:
        decoded = decode_word(word)
    except ValveHexError:
        return "undecodable"
    if word == UNUSED_VALVE_WORD:
        return "unused / closed"
    open_outlets = [
        str(index + 1) for index in range(3) if decoded.outlet_mask >> index & 1
    ]
    return (
        f"{decoded.temperature_celsius:.1f}C, {decoded.flow_percent:.0f}% flow, "
        f"outlets {','.join(open_outlets) or 'none'}"
        f"{', paused' if decoded.paused else ''}"
    )


def credential_is_dead(err: Exception) -> bool:
    """True when `err` means the stored credential was rejected, not that Kohler was down.

    The distinction decides whether to prompt the user, so getting it wrong is expensive in
    both directions: prompt on a network blip and reauth cards appear whenever the WAN
    flaps; miss a real rejection and the entry sits silently dead. `AuthUnavailable` is the
    only `AuthError` raised without Kohler having actually rejected anything, and
    `KohlerError` is not an auth failure at all.
    """
    return isinstance(err, AuthError) and not isinstance(err, AuthUnavailable)


def _controller_offline(controller: Controller) -> str:
    """The message for a controller that answered `statusCode 900`.

    Named, because with several controllers on the account "the controller" no longer
    says which one to go and look at.
    """
    return (
        f"{controller.name} is offline. Check that the controller is powered on and "
        "connected, then try again."
    )


class Controller:
    """One Anthem Plus system controller, with everything the coordinator keeps for it.

    An account can carry several controllers — one per bathroom is the ordinary case — and
    the cloud lists them all under one tenant, so the coordinator holds a list of these rather
    than one set of singular fields. Every controller entity is bound to exactly one of them,
    and each MQTT envelope is routed to the one whose id it carries. Until 2026-09-08 the
    coordinator kept only the *first* controller the account listed and silently ignored the
    rest.
    """

    def __init__(
        self, device: Device, hub: HubDevice, state: HubState, name: str
    ) -> None:
        self.device = device
        #: Command surface — favorites, `valvecontrol`, `stopall`.
        self.hub = hub
        #: Live state, fed by the REST seed and then by MQTT.
        self.state = state
        #: Device name shown in Home Assistant. "Anthem Plus" on a single-controller account,
        #: so nothing changes for an existing install; see `controller_names`.
        self.name = name
        #: Which accessories are attached. Latched by the first successful configuration
        #: read; `known` is what says whether that read has happened.
        self.capabilities = HubCapabilities()
        #: This controller's favorites — seeded over REST, then replaced wholesale by every
        #: `FAVORITES_SNAPSHOT`. Ids are reassigned on delete, so always resolve by name.
        self.favorites: list[dict[str, Any]] = []

    @property
    def device_id(self) -> str:
        return self.device.device_id

    @property
    def model(self) -> ValveModel:
        """Outlet layout of the valve behind this controller.

        Starts as the entry's model and is replaced by what the controller's own
        `hub-configuration` reports on the first seed — two controllers on one account can
        front different valve models, and the entry stores only one. Lives on the state
        object, which is what decodes the per-zone outlet arrays with it, so there is exactly
        one copy to get out of step. See `KohlerAnthemCoordinator._apply_controller_topology`.
        """
        return self.state.model

    @property
    def water_is_running(self) -> bool | None:
        """Whether the **controller** believes water is running. Never asks the valve.

        This is deliberately the controller's own, possibly wrong, view — and the entities
        on the Anthem Plus device are the one place that is the right answer. They are
        answering "what does this controller think it is doing", and a controller that has
        not been told about a session is not doing anything: its ``stopall`` and
        ``valvecontrol OFF`` have nothing to stop, and its own timers are not counting.

        **This replaced a valve-backed property on 2026-08-18, because that produced a false
        positive.** `resolve_outlet_source()` is right that the valve owns the *physical*
        water state, and the Anthem Valve entities read it. But feeding it to the
        controller's switches made them report a system the controller knew nothing about.
        Measured that day: a 86-minute GCS-driven shower — open at 07:52:01 local, the
        valve's 3600 s pause and our restore at 08:52, stopped by hand at 09:18 — during
        which the controller published **not one message of any kind**, `SHOWER_VALVE_STS`
        included. The capture holds five `GCS_SOLO_STS` messages and nothing else. Both
        controller switches nonetheless tracked the shower perfectly, which looked like
        health and was actually the valve wearing the controller's name.

        Read from the outlet arrays rather than ``HubState.is_running``'s zone ``status``
        so this agrees exactly with the ``ControllerOutletSensor`` binary sensors — the
        Shower switch is on if and only if one of those outlet rows is on. The two sources
        do not disagree in any capture; matching them is about the dashboard being
        self-consistent, not about correctness.

        ``None`` — "unknown", not "off" — until the controller has reported a zone at all,
        since an empty ``zones`` map pads to all-False and would otherwise read as a
        confident "no water".
        """
        state = self.state
        if not state.zones:
            return None
        return any(state.outlets)

    def __repr__(self) -> str:
        return f"<Controller {self.device_id} {self.name!r}>"


def controller_names(devices: list[Device]) -> dict[str, str]:
    """Home Assistant device name per controller, keyed by device id. See `_device_names`."""
    return _device_names(devices, DEVICE_NAME_CONTROLLER)


def valve_names(devices: list[Device]) -> dict[str, str]:
    """Home Assistant device name per valve, keyed by device id. See `_device_names`."""
    return _device_names(devices, DEVICE_NAME_VALVE)


def _device_names(devices: list[Device], base: str) -> dict[str, str]:
    """Home Assistant device name per device of one kind, keyed by device id.

    One controller keeps the plain "Anthem Plus" that every existing install, the user guide
    and the README's entity ids were built on. With several, each takes its Konnect name —
    what the owner called it in the app, usually the bathroom — so the two devices' entity
    ids cannot collide. Two controllers sharing a Konnect name, or one with none, fall back
    to the device id, which is at least unique.

    Names only decide entity ids at first registration; renaming a device later in Home
    Assistant does not disturb this, and neither does this disturb an existing registry row.
    """
    if len(devices) <= 1:
        return {device.device_id: base for device in devices}
    labels = {
        device.device_id: (
            device.name.strip()
            if device.name and device.name.strip() != device.device_id
            else ""
        )
        for device in devices
    }
    seen = Counter(labels.values())
    # **Never the device id.** A device name reaches entity ids, the dashboard, screenshots
    # and every log line that names the device, so falling back to the id would publish a
    # cloud address more thoroughly than any log statement — and permanently, since entity
    # ids persist. A position is enough to tell two devices apart, which is all this is for.
    ordinals = {device.device_id: index + 1 for index, device in enumerate(devices)}
    return {
        device_id: (
            f"{base} {label}"
            if label and seen[label] == 1
            else f"{base} {ordinals[device_id]}"
        )
        for device_id, label in labels.items()
    }


class _TaggedJournal:
    """A journal that stamps every record with which valve it is about.

    The cutoff detector writes its own records straight to the journal, without knowing
    which valve it belongs to. With two valves sharing one file, an untagged `flow_end`
    would be unattributable. Passthrough when there is no tag, so a single-valve journal is
    byte-for-byte what it always was and the tools that read it need no change.
    """

    def __init__(
        self,
        journal: Any,
        tag: str | None,
        *,
        report_log: Any = None,
        kind: str = "",
        hass: Any = None,
    ) -> None:
        self._journal = journal
        self._tag = tag
        # The Report Log gets a copy of every decision, so one switch produces one
        # attachment holding the wire traffic and the reasoning about it, interleaved on one
        # clock. `kind` is `cutoff` or `warmup` — the two vocabularies reuse event names, so
        # a reader needs to know which watcher spoke. See `report_log.ReportLog.note`.
        self._report_log = report_log
        self._kind = kind
        self._hass = hass

    def note(self, event: str, **fields: Any) -> None:
        if self._tag is None:
            self._journal.note(event, **fields)
        else:
            self._journal.note(event, valve=self._tag, **fields)
        # Independent of the standalone journal above: that one is gated by its own enabled
        # flag, and a decision must reach an active report whether or not the dedicated
        # journal is switched on.
        if self._report_log is not None and self._kind:
            tagged = dict(fields)
            if self._tag is not None:
                tagged["valve"] = self._tag
            self._report_log.note(self._kind, event, tagged)
            # `note` never opens a file — the detector runs on the loop, and opening one
            # there is a blocking call. See `ReportLog.wants_open`.
            if self._report_log.wants_open and self._hass is not None:
                self._hass.async_add_executor_job(self._report_log.prepare)


def _setting_label(
    maximum_run_time: int | None,
    maximum_temperature_tenths: int | None,
    default_temperature_tenths: int | None,
) -> str:
    """Name the setting a write changed, for a message someone has to read."""
    if maximum_run_time is not None:
        return f"Max Shower Duration ({maximum_run_time // 60} minutes)"
    if maximum_temperature_tenths is not None:
        return f"Max Temperature ({maximum_temperature_tenths / 10:.1f} °C)"
    if default_temperature_tenths is not None:
        return f"Default Temperature ({default_temperature_tenths / 10:.1f} °C)"
    return "an outlet setting"


class Valve:
    """One Anthem digital valve, with everything the coordinator keeps for it.

    The counterpart of :class:`Controller`. Until 2026-09-08 the valve path — state, the
    run-time cutoff detector, warm-up auto-restore, the cloud reachability watch, the custom
    shower watcher and the learned limits — lived on the coordinator as singular fields,
    which meant the first valve the account listed and no other. Every one of those things
    is per valve, so they live here, and the coordinator holds a list.

    **The method bodies below are the coordinator's, moved.** They still say `self.gcs`,
    `self.gcs_state`, `self.hass`, `self.entry` and so on, which is why those names exist
    on this class as attributes and delegating properties: the history in the docstrings
    and the measurements they cite are the valuable part, and rewriting every line to a new
    vocabulary would have put all of it at risk for no behavioural gain.

    **Settings are per valve.** The Endless Shower and Warmup Auto-Restore switches, the
    remembered warm-up mode and the learned run times used to sit as flat keys on the config
    entry. They now sit under `CONF_VALVES`, keyed by device id — see `stored` / `option`
    — and the flat keys are migrated once, at setup, onto the first valve.
    """

    def __init__(
        self,
        coordinator: KohlerAnthemCoordinator,
        device: Device,
        model: ValveModel,
        name: str,
        tag: str | None,
    ) -> None:
        self.coordinator = coordinator
        self.gcs_device = device
        #: Device name shown in Home Assistant. "Anthem Valve" on a single-valve account,
        #: so nothing changes for an existing install; see `valve_names`.
        self.name = name
        #: Stamped onto every journal record when the account has several valves, so the
        #: shared cutoff and warmup journals stay attributable. None keeps a single-valve
        #: journal exactly as it was.
        self.tag = tag
        self.gcs = GcsDevice(
            coordinator.client, device.device_id, coordinator.temperature_unit, model
        )
        self.gcs_state = GcsState(model, coordinator.temperature_unit)
        # CLOUD CONNECTION WATCH: one per valve, because it is this valve's reachability it
        # reports. See `cloud_watch.py`.
        self.cloud_watch = CloudConnectionWatch(coordinator, self)
        #: Everything about the warm-up mode: writing it, watching it, putting it back.
        #: Its self-write bookkeeping is the same idea as
        #: `ZoneCutoffDetector.note_local_write`: a change we caused must not be treated as
        #: the device misbehaving, or turning warmup off from the dropdown would be undone
        #: a minute later.
        #: Its own object because it is a closed system — see `warmup_manager`.
        self.warmup = WarmupManager(self)
        # CUSTOM SHOWER: the "No pausing warm-up" watcher, one at a time, and
        # a serial that every command sent from here bumps, so the watcher can tell that
        # something else was sent after its own write. See `anthem/warmup_resume.py`.
        self._custom_shower_task: asyncio.Task | None = None
        # **Every task this valve starts is held here so `stop()` can cancel it.** Two of
        # these sleep for a minute or more and then write to the hardware — a warm-up
        # restore (60 s) and a cutoff restart — so one surviving an unload means an
        # HTTP write, and a config-entry mutation, from a coordinator Home Assistant has
        # already discarded. A reload inside that window is enough to trigger it.
        self._background_tasks: set[asyncio.Task] = set()
        self._local_write_serial = 0
        # The raw `gcs-preset` payload from the most recent seed, held only long enough for
        # `_async_sync_default_preset_timer` to consume it. Cleared on use — it feeds a
        # write path, and a stale payload is a silent edit.
        self._seeded_presets: Any = None
        # Tracks how long each zone has been flowing, so a valve-timer close can be told from
        # a real stop. Always fed, even with the option off — the cost is a dict update per
        # message, and it means enabling the option takes effect immediately rather than from
        # the next time the shower happens to start.
        self._cutoff = ZoneCutoffDetector()
        # Last outlet masks seen with water actually running. The valve wipes every mask at
        # a run-time cutoff, so this is the only record of what to restore.
        self._last_open_masks: dict[int, int] | None = None
        # Same idea, for flow. Exists for the zone a preset-off pauses *alongside* the one
        # that actually hit its limit — `ZoneCutoff.reading` only ever covers the zone whose
        # own duration matched, so without this the co-paused zone has no flow source and
        # falls back to `DEFAULT_FLOW_PERCENT` on restore. See `_remember_open_masks`.
        self._last_open_flows: dict[int, float] | None = None
        # Per-outlet `maximumRunTime`, keyed by the device's own 0-based `outLetId`.
        # Restored from the config entry so the cutoff feature works from the first second
        # after a restart — see `CONF_OUTLET_RUN_TIMES` for why it has to be remembered.
        self._run_times: dict[int, int] = {
            int(key): int(value)
            for key, value in (self.stored(CONF_OUTLET_RUN_TIMES) or {}).items()
        }
        # Whether the valve's own outlet split has been read yet — see `async_seed`.
        self._topology_checked = False
        # The `gcs-configuration` record, read once at the first seed. None means "not read
        # yet"; `{}` means the read was attempted and produced nothing usable, which is the
        # documented result on a controller-attached valve and is not an error.
        self.configuration: dict[str, Any] | None = None
        #: The most recent `gcs-usage` response, or {} when the read failed. Refreshed on
        #: the seed only — a monthly series does not change between reconnects, and this is
        #: a diagnostic rather than something an automation waits on.
        self.usage: dict[str, Any] = {}
        # The per-day series, refreshed when a shower ends — see `async_refresh_daily_usage`.
        self.usage_daily: dict[str, Any] = {}
        # Guards the refresh against a burst of stop-messages: the valve sends several as a
        # shower winds down, and each must not become its own cloud read.
        self._daily_usage_task: asyncio.Task | None = None
        self._was_running = False
        # The flow each zone's Flow number is currently showing, keyed by zone. Written by
        # that entity and read by the outlet switches, so toggling an outlet does not
        # silently reset a flow the user chose — see `async_set_zone_outlet`. Seeded with
        # `DEFAULT_FLOW_PERCENT`, which is what an unspecified write sends anyway, so the
        # behaviour before anyone touches the entity is exactly as it was.
        self.zone_flow: dict[int, float] = {
            zone: DEFAULT_FLOW_PERCENT for zone in self.model.zones
        }

    def __repr__(self) -> str:
        return f"<Valve {self.device_id} {self.name!r}>"

    @property
    def created_time(self) -> str | None:
        """When Kohler's cloud first created this device's record, as it reports it.

        The closest thing to an install date the API offers — **the cloud record's
        creation, not the day a plumber fitted the valve**, so a valve re-registered after
        a service call would read as newer than it is. Named for what it is rather than
        what it approximates.

        Returned as the raw string; `sensor.ValveInstalledSensor` parses it.
        """
        value = (self.configuration or {}).get("createdTime")
        return None if value in (None, "") else str(value)

    @property
    def firmware(self) -> str | None:
        """The **interface** firmware — the touchscreen's own version.

        Kept as `firmware` because it is what the device registry shows and what every
        earlier release meant by the word. The valve and gateway have their own versions and
        their own properties; see :meth:`component_firmware`.

        ⚠️ **Two bugs lived here until 0.11.0, and both reported a wrong number rather than
        nothing.** `about` is nested inside the record's own `configuration` block, not at
        the top level, so `configuration.get("about")` was always `None` and every report
        said `about_keys: []` on hardware that populates it in full. And `about.firmware` is
        a *mapping* (`version` / `latestVersion`), not a string, so it would not have parsed
        even at the right depth. Together they meant this fell through to the OTA blocks,
        where a valve whose `otaReportedProperties` describes **Assets** — the artwork
        bundle — reported the artwork version as its firmware: `2.00` where the Konnect app
        showed 2.2, on one of the owner's two otherwise identical valves.

        Order, first hit wins:

        1. ``configuration.about.uI2.firmware`` — the interface, where the record nests it.
        2. ``about.firmware`` at the top level, as a string — the reference install's shape,
           kept so an install that reads correctly today keeps reading correctly.
        3. ``otaReportedProperties`` / ``firmwareUpdate``, **Application only**. A block
           describing Assets is skipped rather than reported: it answers a different
           question, and that is exactly the confusion above.
        4. A bare top-level ``version`` string.

        None where no shape matches. A blank is honest; a number that silently means
        something else is not.
        """
        about = self.about
        value = _firmware_string((about.get("uI2") or {}).get("firmware"))
        if value is not None:
            return value

        # The reference install's shape: `about.firmware` as a plain string at top level.
        # Only accepted as a string here — where it is a mapping it is the *gateway's*
        # version (confirmed 2026-09-10: `about.firmware.version` equals
        # `about.gateway.firmware`), which `component_firmware("gateway")` reports instead.
        configuration = self.configuration or {}
        top_about = configuration.get("about")
        if isinstance(top_about, dict):
            value = _firmware_string(top_about.get("firmware"))
            if value is not None:
                return value

        blocks = [
            configuration.get(key)
            for key in ("otaReportedProperties", "firmwareUpdate")
        ]
        # Application first, wherever it appears.
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("firmwareType") == _FIRMWARE_PREFERRED_TYPE
            ):
                value = _firmware_from_block(block)
                if value is not None:
                    return value

        # Then any block that does not declare itself something else. **An untyped block is
        # not an Assets block** — the reference install's `otaReportedProperties` carries no
        # `firmwareType` at all, and skipping it would trade one wrong answer for a blank on
        # hardware that reads correctly today. Only a block explicitly naming a non-
        # Application type is refused, which is the case that caused the artwork version to
        # pass for an interface version.
        for block in blocks:
            declared = block.get("firmwareType") if isinstance(block, dict) else None
            if declared is not None and declared != _FIRMWARE_PREFERRED_TYPE:
                continue
            value = _firmware_from_block(block)
            if value is not None:
                return value

        return _firmware_string(configuration.get("version"))

    @property
    def about(self) -> dict[str, Any]:
        """The record's ``about`` block, wherever this account nests it.

        Two shapes are known: nested under the record's own ``configuration`` key (both of
        the owner's K-28210 valves, 2026-09-10) and at the top level (the reference
        install). Checked nested-first because that is the shape that carries the full
        per-component breakdown; a top-level ``about`` on the reference install holds only
        ``firmware``.
        """
        configuration = self.configuration or {}
        inner = configuration.get("configuration")
        if isinstance(inner, dict):
            about = inner.get("about")
            if isinstance(about, dict):
                return about
        about = configuration.get("about")
        return about if isinstance(about, dict) else {}

    def component_firmware(self, component: str) -> str | None:
        """The firmware of one named part of the system, or None.

        The Konnect app shows **three different firmwares** for one shower — the touchscreen
        interface, the valves, and the gateway — and they are genuinely different numbers
        (2.2, 10 and 00.74 on the owner's system). Collapsing them into a single `Firmware`
        entity is what let an artwork version pass for an interface version for three
        releases.

        ``component`` is a key of the ``about`` block: ``uI2``, ``primaryValve``,
        ``secondaryValve1``, ``gateway``. Each holds ``firmware`` plus, sometimes,
        ``assetsFirmware`` and ``bleVersion``; only the running firmware is read here.

        ⚠️ **Two valves on one account can differ**, and the app does not show it: the
        owner's read `10` and `11` on 2026-09-10 while Konnect displayed 10 for both. That is
        the case this method exists to make visible.
        """
        block = self.about.get(component)
        if not isinstance(block, dict):
            return None
        return _firmware_string(block.get("firmware"))

    # ------------------------------------------------------------------ #
    # What the moved methods reach for on the coordinator
    # ------------------------------------------------------------------ #
    @property
    def hass(self) -> HomeAssistant:
        return self.coordinator.hass

    @property
    def client(self) -> KohlerClient:
        return self.coordinator.client

    @property
    def entry(self) -> ConfigEntry:
        return self.coordinator.entry

    @property
    def temperature_unit(self) -> str:
        return self.coordinator.temperature_unit

    @property
    def cutoff_log(self) -> CutoffDebugLog | None:
        return self.coordinator.cutoff_log

    @property
    def warmup_log(self) -> CutoffDebugLog | None:
        return self.coordinator.warmup_log

    @property
    def stream(self) -> AnthemMqttStream | None:
        return self.coordinator.stream

    @property
    def device_id(self) -> str:
        return self.gcs_device.device_id

    @property
    def model(self) -> ValveModel:
        """This valve's outlet layout.

        Starts as the entry's model and is replaced by what the valve's own
        `gcsadvancestate` reports on the first seed — two valves on one account can be
        different models, and the entry stores only one. Lives on the state object, which is
        what decodes every word with it; `GcsDevice` keeps a copy for encoding, and
        `_apply_topology` moves both together.
        """
        return self.gcs_state.model

    @property
    def issue_id(self) -> str:
        """The Repairs issue id for an Endless Shower on this valve that cannot act.

        Per valve since 2026-09-08, so two valves raise two cards. The pre-existing id
        without a device suffix is deleted at setup and unload so an upgrade leaves no
        orphan.
        """
        return f"{ISSUE_NOT_SET_UP}_{self.entry.entry_id}_{self.device_id}"

    def _tagged(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Journal fields, stamped with this valve when the account has several."""
        return fields if self.tag is None else {"valve": self.tag, **fields}

    def _push(self) -> None:
        """Re-render every entity, the way a device push does."""
        self.coordinator.async_set_updated_data(self.coordinator._snapshot())

    # ------------------------------------------------------------------ #
    # Per-valve settings on the config entry
    # ------------------------------------------------------------------ #
    def stored(self, key: str, default: Any = None) -> Any:
        """A per-valve value from `entry.data[CONF_VALVES][device_id]`."""
        return ((self.entry.data.get(CONF_VALVES) or {}).get(self.device_id) or {}).get(
            key, default
        )

    def store(self, key: str, value: Any) -> None:
        """Write a per-valve value into `entry.data`. Reload-ignored, like the flat key was."""
        valves = dict(self.entry.data.get(CONF_VALVES) or {})
        valves[self.device_id] = {**(valves.get(self.device_id) or {}), key: value}
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_VALVES: valves}
        )

    def option(self, key: str, default: Any = None) -> Any:
        """A per-valve value from `entry.options[CONF_VALVES][device_id]`."""
        return (
            (self.entry.options.get(CONF_VALVES) or {}).get(self.device_id) or {}
        ).get(key, default)

    def set_option(self, key: str, value: Any) -> None:
        """Write a per-valve option. The switches call this; nothing reloads on it."""
        valves = dict(self.entry.options.get(CONF_VALVES) or {})
        valves[self.device_id] = {**(valves.get(self.device_id) or {}), key: value}
        self.hass.config_entries.async_update_entry(
            self.entry, options={**self.entry.options, CONF_VALVES: valves}
        )

    # ------------------------------------------------------------------ #
    # Lifecycle, driven by the coordinator
    # ------------------------------------------------------------------ #
    def attach_journal(self, journal: CutoffDebugLog | None) -> None:
        """Point the cutoff detector at the (shared) debug log, once it exists.

        Also hands it the Report Log, so every cutoff decision reaches an active report
        alongside the raw traffic that produced it — see `_TaggedJournal`.
        """
        if journal is not None:
            self._cutoff.journal = _TaggedJournal(
                journal,
                self.tag,
                report_log=self.coordinator.report_log,
                kind="cutoff",
                hass=self.hass,
            )

    def _note_local_write(self) -> None:
        """Count a command sent from this integration to this valve.

        Read by the custom-shower watcher: if the serial has moved since its own write,
        something else was sent in the meantime and the watcher must not resume on top
        of it. Controller commands bump every valve's serial through the coordinator, since
        which valve a controller fronts is not knowable from the cloud.
        """
        self._local_write_serial += 1

    def handle_envelope(self, envelope: Envelope) -> bool:
        """Apply one of this valve's MQTT messages. True if anything changed."""
        was_warmup = self.gcs_state.warmup_mode
        changed = self.gcs_state.apply_envelope(envelope)
        self._handle_warmup_mode_change(
            was_warmup,
            self.gcs_state.warmup_mode,
            announced=envelope.code == MSG_GCS_WARMUP_STATUS,
        )
        self._remember_open_masks()
        self._check_runtime_cutoff()
        self._note_running_for_usage()
        # A valve message is proof of reachability, and settles any pending
        # contradiction check. CLOUD CONNECTION WATCH.
        self.cloud_watch.note_gcs_message()
        return changed

    def _note_running_for_usage(self) -> None:
        """Re-read the daily usage when a shower ends.

        **The one moment the number can have changed.** Water usage moves only while water
        runs, and this integration has no polling clock (`SCAN_INTERVAL` is None) — so
        without this, `Water Used Today` would hold whatever it read at startup for the rest
        of the day. Tying the read to the event that changes the value keeps the push-only
        design intact: no timer, and no read on a day nobody showered.

        Fires on the running -> stopped edge only. The valve sends several messages as a
        shower winds down and the running flag can flicker, so `_daily_usage_task` makes a
        second edge a no-op while the first read is still in flight.
        """
        running = self.gcs_state.is_running
        was_running, self._was_running = self._was_running, running
        if running or not was_running:
            return
        if self._daily_usage_task is not None and not self._daily_usage_task.done():
            return
        self._daily_usage_task = self._track(self._async_refresh_daily_usage_soon())

    async def _async_refresh_daily_usage_soon(self) -> None:
        """Wait for Kohler to record the session, then re-read and re-render.

        The delay is not politeness: the cloud aggregates a session after the valve reports
        it closed, so reading the instant the water stops returns the total *without* the
        shower that just happened — the one reading a user would check.
        """
        await asyncio.sleep(USAGE_REFRESH_DELAY_SECONDS)
        try:
            await self.async_refresh_daily_usage()
        except (
            KohlerError
        ) as err:  # pragma: no cover - async_get_usage swallows its own
            _LOGGER.debug("Could not refresh daily usage: %s", err)
            return
        # Entities read `usage_daily` directly; this is what re-renders them.
        self.coordinator.async_refresh_entities()

    def forget_timings(self) -> None:
        """Drop the cutoff detector's clocks across a stream gap. See `_handle_connected`."""
        self._cutoff.forget()

    def _track(self, coro) -> asyncio.Task:
        """Start a background task and keep a reference until it finishes.

        Two things at once. Home Assistant's own guidance is to hold a reference to any
        task you create, because the event loop keeps only a weak one and a task nobody
        references can be garbage-collected mid-await. And holding them is what makes
        `stop()` able to cancel them — see `_background_tasks`.
        """
        task = self.hass.async_create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def stop(self) -> None:
        """Cancel everything that could fire into a torn-down coordinator."""
        self._cancel_custom_shower("the integration is shutting down")
        # The warm-up restore sleeps 60 s and the journal write 45 s before touching
        # anything, so both can outlive an unload by a wide margin. Cancelled here rather
        # than left to finish: the restore ends in a write to the valve and a config-entry
        # update, neither of which is safe against an entry that is going away.
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        self.warmup.reset_restore_task()
        # Before the stream, so a timer cannot fire into a half-torn-down coordinator.
        self.cloud_watch.async_stop()

    @callback
    def _apply_topology(self, settings: dict[str, Any]) -> bool:
        """Give this valve the outlet layout its own settings report.

        Returns **whether the read actually said something** — which is what the caller
        latches on. The entry's model came from the config flow, which asked the *first*
        valve: right for it, and not necessarily for a second one on the account. Same
        reasoning as `KohlerAnthemCoordinator._apply_controller_topology`, and the same
        fallback: a read that yields nothing leaves the entry's model in place.

        ⚠️ **The caller used to latch before knowing the answer** (fixed 0.15.0, found by
        GitHub Copilot's review of upstream #3). `_topology_checked` was set to `True` and
        *then* this was called, so a first read carrying no layout pinned the entry's model
        for the life of the entry — and on an account whose valves differ, that is the wrong
        layout on every valve but the first, permanently. Returning the answer lets the next
        reconnect try again. Entities already built keep their outlet count until a reload;
        the decode is what gets fixed immediately.
        """
        detected = topology_from_valve_settings(settings)
        if not detected:
            return False
        model = model_for_topology(*detected)
        current = self.model
        if (model.outlets_valve1, model.outlets_valve2) == (
            current.outlets_valve1,
            current.outlets_valve2,
        ):
            # The read answered and agreed with the entry — settled, so latch it. Returning
            # False here would re-read on every reconnect for ever.
            return True
        # The valve's NAME, not its id: this is INFO, so it lands in the log people paste
        # into issues, and a Kohler device id is a cloud address (see 0.9.0). The name is
        # what identifies the valve to its owner anyway.
        _LOGGER.info(
            "%s reports %s; using that for this valve instead of the entry's %s",
            self.name,
            describe_topology(detected),
            current.sku,
        )
        self.gcs_state.model = model
        self.gcs.model = model
        if not model.uses_valve2:
            self.gcs_state.valve2 = None
        return True

    async def async_write_outlet_setting(
        self,
        *,
        maximum_run_time: int | None = None,
        maximum_temperature_tenths: int | None = None,
        default_temperature_tenths: int | None = None,
    ) -> None:
        """Write one outlet setting across every outlet, then verify it landed.

        **There is no list form.** The app writes one call per outlet, each carrying that
        outlet's whole record, and issues the next only after a 2xx — so a failure part-way
        leaves the valve holding the new value on some outlets and the old one on others.
        That is not hypothetical: it is what produced the `outlets_agree: false` this
        integration already reports (`docs/gcs/api.md`, "one outlet per call").

        So this chains the same way and then **reads back** — but the read-back does not
        block the caller. A 201 from this endpoint means *accepted for delivery*, never
        *applied*: the response carries no echo of the value and the Konnect app performs no
        verification at all.

        ⚠️ **Verification runs in the background, and that is a responsiveness fix, not a
        weakening** (0.18.2). It has to wait ~30 s for the cloud document to catch up, and
        awaiting that inside a service call froze the slider for the whole time — Home
        Assistant logs a warning at 10 s, and the entity looked broken. The POSTs are still
        awaited, because a rejected write fails immediately and the caller should hear about
        it; only the waiting is deferred. A verification that fails raises a **repair issue**
        instead of an exception nobody is left to catch.

        Raises `HomeAssistantError` for a write that is refused or fails part-way — including
        which outlets took the new value and which did not. A partial write is reported
        rather than retried: retrying a half-applied safety setting without knowing why the
        first attempt failed is how one bad outlet becomes several.
        """
        limits = self.gcs_state.outlet_limits
        if not limits:
            raise HomeAssistantError(
                f"{self.name} has not reported its outlet configuration yet, so there is "
                "nothing to write back. Try again once it has."
            )
        self._note_local_write()

        written: list[int] = []
        try:
            for outlet_id in sorted(limits):
                await self.gcs.async_write_outlet_config(
                    limits[outlet_id],
                    maximum_run_time=maximum_run_time,
                    maximum_temperature_tenths=maximum_temperature_tenths,
                    default_temperature_tenths=default_temperature_tenths,
                )
                written.append(outlet_id)
        except KohlerError as err:
            # Say exactly how far it got: the outlets already written hold the new value.
            done = (
                ", ".join(str(self.model.outlet_from_id(o) or o + 1) for o in written)
                or "none"
            )
            raise HomeAssistantError(
                f"Writing {self.name} failed after outlet {done}. Outlets are now in a "
                f"mixed state — re-saving the setting rewrites them all. ({err})"
            ) from err

        # **Not awaited.** See the docstring: the wait is what made the entity unresponsive.
        self._track(
            self._async_verify_outlet_write(
                maximum_run_time=maximum_run_time,
                maximum_temperature_tenths=maximum_temperature_tenths,
                default_temperature_tenths=default_temperature_tenths,
            )
        )

    async def _async_verify_outlet_write(
        self,
        *,
        maximum_run_time: int | None,
        maximum_temperature_tenths: int | None,
        default_temperature_tenths: int | None,
    ) -> None:
        """Re-read the outlet configuration and confirm every outlet took the value.

        ⚠️ **An immediate read-back lies.** `gcsadvancestate` is a cloud document that
        updates only once the device reports, and a read ~1 s after a 201 still showed the
        old value in the live sweep of 2026-08-21; the change appeared within 25 s. Reading
        too early is exactly how a working write looks like a device-side limit, so this
        waits first.

        **Runs detached**, so nothing here may raise: there is no caller left to catch it.
        A failure becomes a repair issue, which is the surface Home Assistant has for
        "something needs your attention later".
        """
        setting = _setting_label(
            maximum_run_time, maximum_temperature_tenths, default_temperature_tenths
        )
        await asyncio.sleep(OUTLET_WRITE_VERIFY_DELAY_SECONDS)
        try:
            settings = await self.client.async_get_gcs_settings(
                self.gcs_device.device_id
            )
        except KohlerError as err:
            self._raise_write_issue(
                setting, f"Reading the value back from {self.name} failed: {err}"
            )
            return

        fresh = outlet_limits_from_settings(settings)
        if not fresh:
            self._raise_write_issue(
                setting,
                f"{self.name} reported no outlet configuration to check the change "
                "against.",
            )
            return
        self.gcs_state.outlet_limits.update(fresh)
        self._learn_run_times(self.gcs_state)

        wanted = {
            "maximum_run_time": maximum_run_time,
            "maximum_temperature_tenths": maximum_temperature_tenths,
            "default_temperature_tenths": default_temperature_tenths,
        }
        stale = [
            self.model.outlet_from_id(outlet_id) or outlet_id + 1
            for outlet_id, limit in sorted(fresh.items())
            for field, value in wanted.items()
            if value is not None and getattr(limit, field) != value
        ]
        if stale:
            self._raise_write_issue(
                setting,
                f"{self.name} is still reporting the old value on outlet(s) "
                f"{', '.join(str(o) for o in stale)}.",
            )
            return
        # Verified: clear any warning left by an earlier attempt.
        self._clear_write_issue()

    @property
    def _write_issue_id(self) -> str:
        """One issue per valve, so two valves cannot overwrite each other's warning."""
        return f"outlet_write_unverified_{self.device_id}"

    def _clear_write_issue(self) -> None:
        """Drop any standing warning — a verified write means the last one is stale."""
        ir.async_delete_issue(self.hass, DOMAIN, self._write_issue_id)

    def _raise_write_issue(self, setting: str, detail: str) -> None:
        """Surface an unverified write where the user will actually see it."""
        _LOGGER.warning("%s — %s", setting, detail)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._write_issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="outlet_write_unverified",
            translation_placeholders={"setting": setting, "detail": detail},
        )

    def _seed_independent_reads(self) -> asyncio.Task[None]:
        """Start the seed reads that depend on nothing, so they overlap the ones that do.

        `gcs-configuration`, `gcs-usage` and `gcs-preset` are independent of the outlet
        topology and of each other — none of them is decoded against the valve's model, so
        none needs the `gcs-settings` read that `gcs-state` genuinely does. Issuing them
        while that pair is in flight removes three serial round trips from every cold start.

        Wrapped in a task rather than awaited inline so the caller can keep working; every
        read inside is individually guarded, so one failure cannot blank the others and
        nothing here can fail setup. Cancellation-safe: the caller always awaits it.
        """

        async def _run() -> None:
            await asyncio.gather(
                self._async_seed_configuration(),
                self._async_seed_presets(),
            )

        return asyncio.ensure_future(_run())

    async def _async_seed_configuration(self) -> None:
        """Firmware and the structural fields, plus the usage series beside them.

        **First seed only:** installation-time data that cannot change while Home Assistant
        runs, so a reconnect must not spend a call on it. Entirely diagnostic; a failure is
        logged and setup continues.
        """
        if self.configuration is not None:
            return
        try:
            self.configuration = await self.client.async_get_gcs_configuration(
                self.gcs_device.device_id
            )
        except KohlerError as err:
            _LOGGER.debug("Could not read gcs-configuration: %s", err)
            # `{}` rather than leaving None, so a failed read is not retried on every
            # reconnect for data that is static anyway.
            self.configuration = {}

        # The monthly usage series, read once beside the configuration. Thirteen months
        # back covers a full year plus the current partial one, which is what a
        # year-on-year comparison needs. `async_get_usage` answers `{}` on failure rather
        # than raising, so this needs no guard of its own.
        # The two series are independent of each other, so they overlap rather than
        # queueing — the same reasoning as `_seed_independent_reads` one level up.
        now = datetime.now(UTC)

        async def _monthly() -> None:
            self.usage = await self.client.async_get_usage(
                self.gcs_device.device_id,
                from_date=(now - timedelta(days=400)).date().isoformat(),
                to_date=now.date().isoformat(),
            )

        await asyncio.gather(_monthly(), self.async_refresh_daily_usage())

    async def async_refresh_daily_usage(self) -> None:
        """Read the per-day usage series — `Interval=DAY`, verified working 2026-09-11.

        **Why DAY and not WEEK.** `WEEK` is refused at every range tried: 400 days, 90 days
        and 28 days alike, the last being four buckets against the fifteen `DAY` happily
        serves. So the earlier row-cap theory is dead and the endpoint simply does not take
        `WEEK` — while `DAY` gives both the day and the week, since seven daily buckets are
        a week. See `docs/gcs/api.md`.

        Validated on the owner's account the day it was added: the fifteen daily entries
        summed to exactly the 465 L the `MONTH` series reported for the same month, which is
        what rules out the series being an artifact of a different unit or window.

        Thirty-five days back: enough for a seven-day window whatever the timezone, plus
        slack so a restart never renders the week short.
        """
        now = datetime.now(UTC)
        self.usage_daily = await self.client.async_get_usage(
            self.gcs_device.device_id,
            from_date=(now - timedelta(days=35)).date().isoformat(),
            to_date=now.date().isoformat(),
            interval="DAY",
        )

    async def _async_seed_presets(self) -> None:
        """Seed the preset slots. Independent of topology — see `_seed_independent_reads`."""
        try:
            presets = await self.client.async_get_gcs_presets(self.gcs_device.device_id)
            self.gcs_state.apply_preset_list(presets)
            # Kept for `_async_sync_default_preset_timer`, which needs the *raw* record
            # — title, volume and each valve's `hexString` — none of which survive
            # `apply_preset_list`; `GcsPreset` keeps only id, name and is_experience.
            self._seeded_presets = presets
        except KohlerError as err:
            _LOGGER.debug("Could not read GCS presets: %s", err)

    async def async_seed(self) -> None:
        """Read this valve's state, limits and presets over REST.

        The valve half of `KohlerAnthemCoordinator._async_seed_state`, moved here
        unchanged apart from the topology read; that docstring says when it runs.
        """
        # Layout and limits first, state second — the reverse of the order the coordinator
        # used. The state read decodes the second zone's word only if the model has a
        # second zone, so a valve whose own layout differs from the entry's must have that
        # layout applied before its state is seeded, or a single-zone valve on a two-zone
        # entry starts life with a zone 2 it does not have.
        # Per-outlet limits, including `maximumRunTime` — the number Endless Shower
        # cannot act without.
        #
        # This used to arrive **only** over MQTT, unprompted and one outlet at a time,
        # which left a blind window of unknown length after a fresh install: the switch
        # read "on" while the feature was inert, and the owner was told to go change Max
        # Shower Duration in the Konnect app purely to provoke an announcement.
        # `gcsadvancestate` carries the same data and is readable on demand — it was
        # reachable all along, in a response this integration already fetched for
        # topology (see `docs/gcs/api.md` §1c, corrected 2026-08-17).
        #
        # Runs on every re-seed, not just the first: cheap, and it re-checks the limit
        # after a reconnect rather than trusting a value that may be hours stale.
        #
        # **The independent reads start here and are awaited at the end.** Only one
        # ordering in this method is real: `gcs-settings` decides the outlet topology, and
        # `gcs-state` cannot be decoded until it has been applied (see `_apply_topology`).
        # The configuration, usage and preset reads depend on none of that, so waiting for
        # the settings/state pair before issuing them spent three extra round trips of
        # wall-clock on every cold start for no ordering benefit. Launched as tasks now,
        # they overlap the pair above; the awaits below collect them.
        background = self._seed_independent_reads()
        try:
            await self._async_seed_topology_and_state()
        except BaseException:
            # **Cancelled or failed — do not leave the reads running.** A reload that
            # cancels this coroutine mid-seed would otherwise orphan them against an entry
            # that is going away: the class of bug 0.15.1 fixed for the reconnect reseed.
            # Awaiting inside a plain `finally` would not do it — the await is cancelled
            # too — so the task is cancelled explicitly and then reaped.
            background.cancel()
            with suppress(asyncio.CancelledError):
                await background
            raise
        await background

    async def _async_seed_topology_and_state(self) -> None:
        """The one genuinely ordered pair: settings decide topology, topology decodes state."""
        try:
            settings = await self.client.async_get_gcs_settings(
                self.gcs_device.device_id
            )
            # The same read says how the outlets split across the zones, which is what
            # this valve decodes and encodes every word with — its own layout, not the
            # entry's. See `_apply_topology`; once is enough, plumbing does not change.
            if not self._topology_checked:
                # Latched only once the read actually said something — see `_apply_topology`.
                self._topology_checked = self._apply_topology(settings)
            limits = outlet_limits_from_settings(settings)
            if limits:
                self.gcs_state.outlet_limits.update(limits)
                # Same path an MQTT announcement takes, so the value is persisted and
                # the cutoff detector is armed without waiting for the valve to speak.
                self._learn_run_times(self.gcs_state)
        except KohlerError as err:
            _LOGGER.debug("Could not read outlet limits over REST: %s", err)

        try:
            payload = await self.client.async_get_gcs_state(self.gcs_device.device_id)
            if self.cloud_watch is not None:
                # CLOUD CONNECTION WATCH. `connectionState` is a sibling of `state` in
                # this payload, and `apply_rest_state` below reads only `state` — so
                # without this line the field we already paid for is discarded, and the
                # sensor sits at `unknown` until a trigger fires hours later. Free: no
                # extra request, and it does not consume the check cooldown.
                #
                # `notify=False` — this runs during `async_setup`, before the platforms
                # exist; every caller of this method pushes a snapshot of its own.
                self.cloud_watch.note_rest_payload(
                    payload,
                    "REST seed (setup, reconnect or update_entity)",
                    notify=False,
                )
            was_warmup = self.gcs_state.warmup_mode
            self.gcs_state.apply_rest_state(payload)
            # Warm-up gets its own call rather than thirty lines here: a mode that moved
            # while the stream was down reaches nothing else, and the reasoning about why
            # belongs beside the rest of the warm-up machinery. See
            # `WarmupManager.note_seeded_mode`.
            self.warmup.note_seeded_mode(was_warmup, self.gcs_state.warmup_mode)
        except KohlerError as err:
            _LOGGER.debug("Could not seed GCS state: %s", err)

    def announce_readiness(self) -> None:
        """Say at startup whether the cutoff feature can act — the coordinator's old
        end-of-setup block, per valve."""
        # Say at startup whether the cutoff feature can act. The switch keeps its state
        # across restarts, so without this the only warning would be the one printed when
        # somebody last toggled it — possibly weeks ago, on a different set of known limits.
        self._journal(
            "arm",
            enabled=self.restart_on_runtime_cutoff,
            run_times=self.outlet_run_times,
            awaiting=self.outlets_awaiting_run_time,
            zone_limits={z: list(v) for z, v in self._zone_limits().items()},
        )
        if self.restart_on_runtime_cutoff:
            if self._run_times:
                # Stated positively on every start, at WARNING so it shows under default
                # logging. Silence is ambiguous — "armed" and "the feature quietly stopped
                # working" look identical from the log — and this is a feature that can
                # restart water with nobody present, so it should announce itself.
                _LOGGER.warning(
                    ENDLESS_SHOWER_ON, describe_duration(self.outlet_run_times)
                )
                # No "match the durations" nag here any more — removed 2026-08-22, owner's
                # decision. It fired on every start of every dual-product install whether or
                # not the durations differed, which this integration cannot know: the hub's
                # Max Shower Duration is not readable from the cloud (local API only, and
                # storing the hub PIN was ruled out). The warning that remains is
                # evidence-based and one-directional: `runtime_cutoff.py` warns when an
                # observed minute-boundary stop shows the controller PREEMPTING the valve
                # (hub limit below the valve's — Endless Shower silently defeated, and the
                # valve side is the one this integration can write). A controller sweep past
                # the valve's limit is journalled but not warned: no HA-side action exists.
            else:
                _LOGGER.warning(ENDLESS_SHOWER_NOT_SET_UP)

    def journal_baseline(self) -> None:
        """Record the mode in force when the warmup journal opened. See the comment inside."""
        # BASELINE: what mode was in force when this file opened, from the REST seed above.
        #
        # Without it a journal is unreadable on its own. **The valve never volunteers its
        # warmup mode on connect** — measured 2026-08-21 over all 74 raw captures: 17 hold a
        # `GCS_WARM_STS` at all, and in 16 the first one lands between 137 s and 7 h after
        # the log opened. The 17th, at +1.7 s, only looks like a connect announcement: it is
        # the echo of our own write on 08-21 at 03:40:10Z, which landed in a file that had
        # opened 1.7 s earlier *because* persisting the mode reloaded the entry — the bug
        # `cde9bf4` fixed, so that artefact cannot recur.
        #
        # So a file that records a `disabled` an hour in has no record of what was displaced
        # or since when, and an empty file cannot be told apart from a broken one.
        #
        # Written here rather than in `_async_seed_state` because the first seed runs before
        # this log exists, and this is the one place that happens exactly once per file.
        self.warmup._warmup_journal(
            "baseline",
            mode=self.gcs_state.warmup_mode,
            auto_restore=self.warmup_auto_restore,
            restores_to=self.last_warmup_mode,
            source="rest",
        )

    # ------------------------------------------------------------------ #
    # Moved from the coordinator, 2026-09-08 — bodies unchanged
    # ------------------------------------------------------------------ #
    @callback
    def async_refresh_setup_issue(self) -> None:
        """Raise or clear the Repairs card for an Endless Shower that cannot act.

        Called wherever either half of the condition can change: at setup, when the valve
        announces a limit, and when the switch is toggled. Idempotent — Home Assistant keeps
        one issue per id, so re-creating an existing one is a no-op and deleting a missing
        one is too.

        The condition is `zones_awaiting_run_time`, not "nothing known at all", so a valve
        that has reported one zone but not the other still raises it. Half-armed is not armed
        for the zone that has no limit, and that is exactly the silent case worth surfacing.
        """
        issue_id = self.issue_id
        if self.restart_on_runtime_cutoff and self.zones_awaiting_run_time:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_NOT_SET_UP,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @property
    def outlet_run_times(self) -> dict[int, int]:
        """Learned `maximumRunTime` per outlet, keyed by **1-based** outlet number.

        Empty until the valve announces, which it does unprompted and one outlet at a time.
        An outlet missing from here cannot be restarted after a cutoff — there is nothing to
        compare its run length against — so callers that report readiness must consult this
        rather than assuming the feature is live.
        """
        result: dict[int, int] = {}
        for outlet in range(1, self.model.total_outlets + 1):
            zone, bit = self.model.outlet_location(outlet)
            seconds = self._run_times.get(self.model.outlet_id(zone, bit + 1))
            if seconds is not None:
                result[outlet] = seconds
        return result

    @property
    def armed_zones(self) -> list[int]:
        """Zones the cutoff feature can actually act on.

        The unit that matters, since the valve times per zone: a zone is armed as soon as
        *any* of its outlets has reported a `maximumRunTime`, because that is enough to have
        something to compare the zone's flow duration against. Outlet-level readiness is
        still reported alongside — it is what the valve announces — but a zone with one
        known outlet is protected, not half-protected.
        """
        return [zone for zone, limits in self._zone_limits().items() if limits]

    @property
    def zones_awaiting_run_time(self) -> list[int]:
        """Zones where no outlet has reported a limit yet. Empty means fully armed."""
        return [zone for zone, limits in self._zone_limits().items() if not limits]

    @property
    def outlets_awaiting_run_time(self) -> list[int]:
        """Outlets with no known limit yet, 1-based. Empty means every outlet reported."""
        known = self.outlet_run_times
        return [
            outlet
            for outlet in range(1, self.model.total_outlets + 1)
            if outlet not in known
        ]

    @property
    def restart_on_runtime_cutoff(self) -> bool:
        """Whether to re-open an outlet the valve closed on its own run-time limit.

        Read live from the entry options rather than cached, so toggling the checkbox takes
        effect on the reload without needing a restart. Off unless explicitly enabled.
        """
        return bool(self.option(CONF_RESTART_ON_RUNTIME_CUTOFF, False))

    @callback
    def _remember_open_masks(self) -> None:
        """Keep the last outlet masks seen while water was actually running.

        **A fallback record of what a cutoff has to be undone with.** When a zone hits its
        limit the valve does not close one outlet, it clears that zone's whole mask and sets
        the pause flag in the same message — so by the time the close is detected, the record
        of what was running has already been destroyed, and rebuilding from current state
        restores nothing.

        The detector keeps its own per-zone copy of the pre-pause mask, which is more precise
        and is what the restore prefers. This snapshot still earns its place for the zone the
        detector did *not* fire on: when a preset drives the shower, the cut pauses every
        zone the preset owns, and only this has any record of what the un-expired zone was
        doing (measured 2026-08-13 20:52:46 — zone 2 expired at 3600 s, zone 1 was paused at
        1831 s).

        **Flow is snapshotted here too, for the same zone.** `ZoneCutoff.reading` only ever
        covers the zone whose *own* duration matched a limit — the detector never classifies
        the co-paused zone as a cutoff at all (its duration matches nothing), so it has no
        reading of its own to hand back. This is the only surviving record of what it was
        running, same reasoning as the mask.

        Only updated while something is open **and nothing is paused**, which is precisely
        what makes it survive the cutoff message: an all-closed or paused snapshot never
        overwrites it, so this always holds the last genuinely-flowing moment for both zones
        together.
        """
        state = self.gcs_state
        if state is None:
            return
        masks = {
            zone: (word.outlet_mask if word else 0)
            for zone, word in ((1, state.valve1), (2, state.valve2))
        }
        if not any(masks.values()):
            return
        if any(word and word.paused for word in (state.valve1, state.valve2)):
            return
        self._last_open_masks = masks
        self._last_open_flows = {
            zone: word.flow_percent
            for zone, word in ((1, state.valve1), (2, state.valve2))
            if word is not None
        }

    @callback
    def _learn_run_times(self, state: GcsState) -> None:
        """Absorb any newly announced `maximumRunTime` and remember it across restarts.

        The valve announces one outlet at a time, unprompted, so this fills in gradually and
        is the only way the figure can ever be obtained — nothing can ask for it.
        """
        learned = {
            outlet_id: limits.maximum_run_time
            for outlet_id, limits in state.outlet_limits.items()
            if limits.maximum_run_time is not None
        }
        new = {k: v for k, v in learned.items() if self._run_times.get(k) != v}
        if not new:
            return
        self._run_times.update(new)
        _LOGGER.info(
            "Learned run-time limit for outlet(s) %s: %s — the run-time cutoff feature is "
            "armed for them",
            ", ".join(str(self.model.outlet_from_id(k) or k + 1) for k in sorted(new)),
            ", ".join(f"{v}s" for _, v in sorted(new.items())),
        )
        self.store(
            CONF_OUTLET_RUN_TIMES,
            {str(k): v for k, v in sorted(self._run_times.items())},
        )
        # The reason the Repairs card can look after itself: this is the moment the owner's
        # trip to the Konnect app pays off, and it needs no restart to be noticed.
        self.async_refresh_setup_issue()

    async def _async_sync_default_preset_timer(self) -> None:
        """Take the hidden default preset's own timer out of the way, once.

        Preset 1 carries a `time` the owner cannot see or edit — it appears in neither the
        touchscreen nor the Konnect app — and it silently overrides the outlet limit whenever
        it is lower. Normalising it here leaves the hardware `maximumRunTime` as the single
        thing that ends a shower. `SYNC_DEFAULT_PRESET_TIMER` in `const.py` carries the full
        reasoning, including why presets 2-10 are deliberately left alone.

        **This never fails setup.** It is a convenience, not a prerequisite: the integration
        works fine against a preset with the wrong timer, so a Kohler outage, a rejected
        write, or an unexpected payload is logged and stepped over. Nothing downstream reads
        its result.
        """
        if not SYNC_DEFAULT_PRESET_TIMER or self.gcs is None:
            return
        # Consume the seed's payload rather than re-reading `gcs-preset`, which
        # `_async_seed_state` fetched on the line before this one — the second read of the
        # same endpoint per start, folded 2026-08-21.
        #
        # ⚠️ **Taken and cleared in one step, deliberately.** This payload is echoed back to
        # the valve verbatim for every field except `time`, so it must never be reused on a
        # later pass; `None` here simply means this reads for itself, which is always safe.
        presets, self._seeded_presets = self._seeded_presets, None
        try:
            plan = await self.gcs.async_sync_preset_timer(
                DEFAULT_PRESET_ID, DEFAULT_PRESET_TIMER_SECONDS, presets=presets
            )
        except (KohlerError, AuthError, DeviceOffline) as err:
            _LOGGER.debug(
                "Could not check preset %s's run timer (harmless, setup continues): %s",
                DEFAULT_PRESET_ID,
                err,
            )
            return
        if plan.needed:
            _LOGGER.info(
                "Preset %s (%s) had a hidden %ss run timer that would stop a shower before "
                "the valve's own limit; rewrote it to %ss so the outlet limit is the only "
                "thing that ends a shower",
                DEFAULT_PRESET_ID,
                plan.name or "unnamed",
                plan.previous,
                DEFAULT_PRESET_TIMER_SECONDS,
            )
        else:
            _LOGGER.debug(
                "Preset %s run timer needs no change (%s)",
                DEFAULT_PRESET_ID,
                plan.reason,
            )

    def _zone_limits(self) -> dict[int, tuple[int, ...]]:
        """The distinct `maximumRunTime` values configured for each zone's outlets.

        The valve reports this per outlet but **times it per zone** (see
        `anthem/runtime_cutoff.py`), so there is no single "the" limit for a zone unless
        its outlets happen to agree.

        ⚠️ **They do not always agree — observed 2026-09-10.** One of the owner's two valves
        read 3600 s on all three outlets at 08:38 and then 1800 s on two of them with 3600 s
        still on the third at 15:22. Until then every outlet on every install seen had
        matched, and comments here and in `sensor.py` said so as though it were guaranteed.

        **A mixed zone is a lost write, not a configuration.** There is one duration setting;
        the app writes it one outlet at a time and stops at the first failure
        (`docs/gcs/api.md`), so a dropped call strands the old value on the outlets it never
        reached.

        Which makes offering every distinct value as a candidate exactly right, and for a
        better reason than the one originally written here: the valve really may enforce
        either number, because its outlets really are holding different ones. Matching any of
        them keeps such a zone protected, and the cost is a handful of extra 10 s windows in a
        15-minute session that would each also have to coincide with a `0x40` pause to fire.

        ⚠️ **`maximumRunTime` only. Preset timers are deliberately excluded — do not add
        them here.** A preset carries its own `time` (`GCS_PRESET_STS`), a *second*
        independent limit that stops a preset-driven session early whenever it is lower than
        `maximumRunTime`; this install currently runs a 1800 s preset under a 3600 s hardware
        gate, so it is the preset that stops the shower. Those stops land as
        `verdict: "ignored"` with a large `off_by`, and that is the intended outcome: the
        hardware gate cutting a shower short is what this feature exists to defeat, whereas a
        preset ending at its own configured duration is the system doing what the user asked.
        Restarting those would override a setting somebody chose on purpose. Owner's decision,
        2026-08-17 — see `docs/gcs/api.md`, "two independent timers".
        """
        limits: dict[int, set[int]] = {zone: set() for zone in self.model.zones}
        for outlet, seconds in self.outlet_run_times.items():
            zone, _ = self.model.outlet_location(outlet)
            limits.setdefault(zone, set()).add(seconds)
        return {zone: tuple(sorted(values)) for zone, values in limits.items()}

    def run_time_limits_for_zone(self, zone: int) -> tuple[int, ...]:
        """The `maximumRunTime` candidates for one zone. Empty until the valve announces."""
        return self._zone_limits().get(zone, ())

    def zone_flowing_for(self, zone: int) -> float | None:
        """Seconds this zone has been flowing, from the cutoff detector's own clock.

        Fed on every message whether or not the restart option is on, so it is available
        regardless. None when the zone is idle, or after a reconnect until it next starts.
        """
        return self._cutoff.flowing_for(zone)

    @callback
    def _check_runtime_cutoff(self) -> None:
        """Re-open a zone the valve closed on its own timer, when the option is on.

        Off unless `restart_on_runtime_cutoff` is enabled — this **defeats a manufacturer
        cutoff**, and with no resume limit the water keeps coming back for as long as
        somebody leaves it running. That is the configured intent, not an oversight; the
        limit question was put to the owner and answered "unlimited". Every resume is logged
        at WARNING so there is always a record of water having been restarted automatically.

        **Per zone, not per outlet.** The valve's timer starts when a zone begins flowing and
        is not reset by outlet changes within that zone, so that is what has to be timed.
        Timing each outlet from its own opening — which this did until 2026-08-14 — fires
        only when a zone runs one unchanging outlet for the whole session, and misses
        everything else: 3 of 4 real cutoffs went undetected in the logs that exposed this.

        Temperature carries over because `async_apply_valve` preserves it, and flow follows
        `DEFAULT_FLOW_PERCENT` like every other write.

        Detection is duration-only and lives in `anthem/runtime_cutoff.py`, which
        documents why that is sound. Nothing fires without a positive match, and every
        decision — including every *non*-match — is written to the cutoff debug log.
        """
        state = self.gcs_state
        if state is None:
            return

        self._learn_run_times(state)
        masks: dict[int, int] = {}
        paused: dict[int, bool] = {}
        readings: dict[int, ZoneReading] = {}
        for zone in self.model.zones:
            word = state.zone_word(zone)
            masks[zone] = word.outlet_mask if word else 0
            paused[zone] = bool(word and word.paused)
            if word is not None:
                # Fahrenheit unconditionally, whatever the account displays: this feeds a
                # diagnostic log that gets read alongside captures from other sessions, and
                # a unit that changes with a setting makes those incomparable.
                readings[zone] = ZoneReading(
                    flow_percent=round(word.flow_percent, 1),
                    temperature_f=round(word.temperature_celsius * 9 / 5 + 32, 1),
                )

        fired = self._cutoff.update(masks, paused, self._zone_limits(), readings)
        if not fired or not self.restart_on_runtime_cutoff:
            if fired:
                # Detected but not acted on. Without this line the debug log would show a
                # `cutoff` verdict and no restore, which reads like a bug rather than the
                # switch being off.
                self._journal(
                    "restore",
                    skipped="restart_on_runtime_cutoff is off",
                    zones=[cut.zone for cut in fired],
                )
            return

        # Detection and restart used to log a line each. One message now covers both, and it
        # is emitted only once the water is actually back — so it never claims a restart that
        # then failed. The cut time is captured here rather than in the restart, which runs a
        # few seconds later as a task.
        cut_at = dt_util.now()
        self._track(self._async_restart_after_cutoff(fired, cut_at))

    @callback
    def _journal(self, event: str, **fields: Any) -> None:
        """Write to the cutoff debug log if it exists. No-op before setup finishes.

        This runs on the event loop, so the log deliberately refuses to open a file itself —
        see `CutoffDebugLog.wants_open`. When it asks for one, the open happens in an
        executor and the next record lands.
        """
        if self.cutoff_log is None:
            return
        self.cutoff_log.note(event, **self._tagged(fields))
        if self.cutoff_log.wants_open:
            self.hass.async_add_executor_job(self.cutoff_log.prepare)

    async def _async_restart_after_cutoff(
        self, fired: list[ZoneCutoff], cut_at: Any
    ) -> None:
        """Put back exactly what was flowing in the zones the valve cut.

        The valve clears the zone's mask in the same message that reports the cut, so current
        state says nothing about what the shower was doing. Two independent records survive
        it — the detector's own pre-pause mask, which is the precise instant before the cut,
        and `_last_open_masks` as a fallback — and either beats rebuilding from a mask that
        has already been wiped. Measured live: rebuilding from current state brought a
        four-outlet shower back as outlet 4 alone.

        **A second zone is restored too, but only if it is also paused.** Normally a cut
        pauses just the expiring zone and leaves the other's mask untouched — 10 of the 11
        cutoffs in the corpus. The exception is when a preset is driving the shower: the cut
        is internally `{preset, action:"Off"}`, so it pauses *every* zone the preset owns,
        and the zone that did not expire has had its mask wiped just as thoroughly. Its
        timing proves nothing (1831 s in the one captured instance), so the pause flag is
        what identifies it.

        A zone that is neither cut nor paused is re-sent exactly as it reads now, so anything
        changed there in the second between the cut and this write survives.

        **Flow is restored from `cut.reading`, not left to `DEFAULT_FLOW_PERCENT`.** This is
        deliberately a different rule from `async_apply_valve`'s ordinary writes, which never
        inherit flow — that rule exists so nothing silently *adopts* the touchscreen's last
        value on an unrelated write. A restore is not that: it is putting back a value this
        code itself observed running a moment before it force-closed the zone, which is
        squarely what "restore" should mean. Measured live 2026-08-14: a preset-driven shower
        running at 82.5% was cut and had been coming back at 100% — 2.9x on zone 1, which had
        no outlet open at all. `async_apply_valve` honours whatever flow byte it is given
        exactly, uncapped and unscaled against any ceiling (verified on hardware; see
        `docs/gcs/api.md#flow-the-valve-obeys-the-touchscreen-is-what-computes-limits`), so
        replaying the observed value reproduces the observed experience regardless of whether
        the valve is calibrated — there is no ceiling to reason about either way. **Covers the
        `also_paused` zone too**, from `_last_open_flows` — the same snapshot-of-last-resort
        `_last_open_masks` provides for its mask, and for the same reason: the detector never
        classifies that zone as a cutoff (its own duration matches nothing), so it has no
        `ZoneCutoff.reading` to draw on. Falls back to `DEFAULT_FLOW_PERCENT` only when
        neither source has a value for that zone.
        """
        state = self.gcs_state
        if self.gcs is None or state is None:
            return

        snapshot = self._last_open_masks or {}
        flow_snapshot = self._last_open_flows or {}
        masks: dict[int, int] = {}
        flows: dict[int, float] = {}
        also_paused: list[int] = []
        cut_zones = {cut.zone for cut in fired}
        for zone in self.model.zones:
            word = state.zone_word(zone)
            masks[zone] = word.outlet_mask if word else 0
            if zone in cut_zones or not (word and word.paused):
                continue
            # Paused alongside a cut it did not cause: the preset case above. Only the
            # snapshot can say what it was doing, since the detector never saw it expire.
            if snapshot.get(zone):
                masks[zone] = snapshot[zone]
                also_paused.append(zone)
                if zone in flow_snapshot:
                    flows[zone] = flow_snapshot[zone]
        for cut in fired:
            # The detector's mask is authoritative — it is the last mask seen flowing in that
            # exact zone. `_last_open_masks` covers the case where the detector was fed a
            # zero mask first (a snapshot ordering quirk), and 0 means "nothing to restore",
            # which is reported rather than silently sent.
            restore = cut.mask or snapshot.get(cut.zone, 0)
            if not restore:
                _LOGGER.warning(ENDLESS_SHOWER_NOTHING_TO_RESTORE)
            masks[cut.zone] = restore
            # The detector's own reading is authoritative for the zone it actually timed —
            # more precise than the snapshot, same precedence as the mask above.
            if cut.reading is not None:
                flows[cut.zone] = cut.reading.flow_percent
            elif cut.zone in flow_snapshot:
                flows[cut.zone] = flow_snapshot[cut.zone]

        self._journal(
            "restore",
            zones=[cut.zone for cut in fired],
            also_paused=also_paused,
            masks=masks,
            from_detector={cut.zone: cut.mask for cut in fired},
            from_snapshot=snapshot,
            was_flow_percent=dict(flows),
            was_temperature_f={
                cut.zone: cut.reading.temperature_f
                for cut in fired
                if cut.reading is not None
            },
            writing_flow_percent={
                zone: flows.get(zone, DEFAULT_FLOW_PERCENT)
                for zone in sorted(cut_zones | set(also_paused))
            },
            # True when every zone being restored — the cut zone(s) and any also_paused one —
            # had a captured flow to draw on, so the write below reproduces it exactly. False
            # means at least one zone had no reading and fell back to DEFAULT_FLOW_PERCENT —
            # a guess, not a restore.
            flow_preserved=all(zone in flows for zone in cut_zones | set(also_paused)),
        )
        if not any(masks.values()):
            return

        # Timed because this call, not our own logic, is where a slow restore comes from.
        # Measured over seven live cutoffs the decision above is a flat 0.4 ms while this
        # varies 0.64-5.05 s — so the only number worth recording is this one, and it belongs
        # in the cutoff log rather than `home-assistant.log`, which rotates away.
        started = time.monotonic()
        try:
            await self.async_apply_valve(
                zone_masks=masks, zone1_flow=flows.get(1), zone2_flow=flows.get(2)
            )
        except (KohlerError, HomeAssistantError) as err:
            # Never retried: a failed restart leaves the water off, which is the safe end
            # state, and a retry loop against a valve that is refusing is not.
            _LOGGER.warning("Restart after run-time cutoff failed: %s", err)
            self._journal(
                "restore_failed", zones=[cut.zone for cut in fired], error=str(err)
            )
            return
        restored = sorted(
            outlet
            for outlet in range(1, self.model.total_outlets + 1)
            for zone, bit in [self.model.outlet_location(outlet)]
            if masks.get(zone, 0) >> bit & 1
        )
        _LOGGER.warning(ENDLESS_SHOWER_RESTARTED, cut_at.strftime("%H:%M:%S"))
        self._journal(
            "restore_done",
            outlets=restored,
            write_seconds=round(time.monotonic() - started, 3),
        )
        # The valve does not reliably announce a restored zone — 17 of the corpus's 18
        # restores drew a GCS_SOLO_STS within 0.06-1.08 s, one drew nothing for 176.77 s
        # while the water ran — and the detector's clock used to start only on that
        # announcement. An anchor that late reads as "matches no limit" at the next cutoff
        # and leaves the water off with nothing saying why. The write that just succeeded is
        # when the water came back, so anchor there; a prompt announcement wins the race
        # harmlessly (`note_restore` skips zones already timed). Session 12 §3, fixed
        # 2026-08-22.
        self._cutoff.note_restore(
            {
                zone: masks[zone]
                for zone in cut_zones | set(also_paused)
                if masks.get(zone)
            },
            readings={
                cut.zone: cut.reading for cut in fired if cut.reading is not None
            },
        )

    async def async_apply_valve(
        self,
        *,
        zone1_temperature: float | None = None,
        zone2_temperature: float | None = None,
        zone1_flow: float | None = None,
        zone2_flow: float | None = None,
        zone_masks: dict[int, int] | None = None,
        paused: bool = False,
    ) -> None:
        """Re-send both valve words with selected fields overridden.

        The valve accepts no partial write: every command carries the complete state of
        both zones. So changing one zone's temperature means rebuilding both words from
        current state and re-sending. Anything not overridden is preserved, including which
        outlets are open — which is what makes it safe to adjust temperature mid-shower.

        This mirrors the Konnect app, which POSTs a fresh ``solowritesystem`` on every
        temperature, flow, or outlet adjustment.

        **Flow is the one field that does not carry forward.** Omitting it writes
        ``DEFAULT_FLOW_PERCENT`` (100%), not the valve's current value. Every other field is
        preserved, so this is a deliberate asymmetry: with no flow entities in the UI, no
        caller here can ever *mean* a particular flow, and inheriting one let the touchscreen
        dictate what Home Assistant sent. Pass ``zone1_flow``/``zone2_flow`` explicitly to
        write a specific value — the codec has always supported it.

        Consequence worth knowing: adjusting temperature from Home Assistant mid-shower now
        also restores full flow, if the wall panel had reduced it.
        """
        if self.gcs is None or self.gcs_state is None:
            raise HomeAssistantError("No Anthem valve on this account")

        state = self.gcs_state
        # Masks are per zone throughout — no global outlet numbering is involved, so no
        # model-dependent mapping can be applied wrongly here.
        masks = {
            1: state.valve1.outlet_mask if state.valve1 else 0,
            2: state.valve2.outlet_mask if state.valve2 else 0,
        }
        if zone_masks:
            masks.update(zone_masks)

        def resolve(zone: int, temperature: float | None, flow: float | None):
            word = state.valve1 if zone == 1 else state.valve2
            celsius = (
                unit_to_celsius(temperature, self.temperature_unit)
                if temperature is not None
                else (word.temperature_celsius if word else 38.0)
            )
            # Flow does NOT inherit from the current word — see `DEFAULT_FLOW_PERCENT`.
            # Carrying it forward meant every Home Assistant write silently adopted whatever
            # the touchscreen last set, which is below 100% in 31% of captured words and has
            # been as low as 8%.
            percent = flow if flow is not None else DEFAULT_FLOW_PERCENT
            return celsius, percent

        celsius1, flow1 = resolve(1, zone1_temperature, zone1_flow)
        valve1 = encode_word(VALVE1_PREFIX, celsius1, flow1, masks[1], paused=paused)
        if self.model.uses_valve2:
            celsius2, flow2 = resolve(2, zone2_temperature, zone2_flow)
            valve2 = encode_word(
                VALVE2_PREFIX, celsius2, flow2, masks[2], paused=paused
            )
        else:
            valve2 = UNUSED_VALVE_WORD

        # Mark before sending, with **what** was written. A close that follows our own
        # *closing* write is ours, not the valve's timer, and must not be undone — otherwise
        # stopping the shower from Home Assistant near the limit would be read as a timeout
        # and immediately restarted. An *opening* write gets no such grace: it cannot cause a
        # close, and pretending it could swallowed a real cutoff on 2026-08-14.
        #
        # `paused=True` counts as closing whatever it touches, since the water stops either
        # way and the valve reports the mask cleared.
        self._note_local_write()
        self._cutoff.note_local_write(
            {zone: (0 if paused else mask) for zone, mask in masks.items()}
        )
        try:
            await self.gcs.async_write_valves(valve1, valve2)
        except DeviceOffline as err:
            raise HomeAssistantError(
                "The Anthem valve is offline. Check that it is powered on and connected "
                "to Wi-Fi, then try again."
            ) from err
        except KohlerError as err:
            raise HomeAssistantError(f"Kohler command failed: {err}") from err

    async def async_send_valve_hex(
        self, zone1_hex: str, zone2_hex: str | None = None
    ) -> dict[str, Any]:
        """POST raw command words to ``solowritesystem``. **This can run water.**

        The escape hatch for everything the entities do not model — an outlet combination,
        flow value, or temperature the UI cannot express. It is the same endpoint every other
        control path uses; the only difference is that the caller supplies the words.

        Both words are validated with ``normalize_word`` before anything is sent. Malformed
        input is rejected locally rather than posted to a device that opens water valves, and
        the decoded meaning is logged so the journal records what was actually asked for.

        ``zone2_hex`` omitted **re-sends zone 2's current state**, so zone 2 keeps doing
        whatever it was doing. It emphatically does *not* send ``00000000``.

        That sentinel means "no valve addressed", and on a two-valve system it is measured to
        make the device **discard the entire command** — `v1=00000000 v2=11849C01` opened
        nothing, while `v1=0185C800 v2=1185C801` opened valve 2 immediately
        (`docs/gcs/api.md`). So a blank zone 2 filled with zeroes would silently throw away
        the zone 1 word the caller had just carefully built. A valve that should stay shut
        gets a well-formed word with mask ``0x00``; only a valve that does not physically
        exist gets the sentinel, which is why a single-zone model still sends it here.

        The protocol has no partial write — every POST carries both zones — so "leave zone 2
        alone" can only be expressed by sending zone 2's own current word, which is what this
        does. Flow follows `DEFAULT_FLOW_PERCENT` like every other write.

        Returns the decoded interpretation of what was sent, so an automation or a person can
        confirm the word meant what they thought.
        """
        if self.gcs is None or self.gcs_state is None:
            raise HomeAssistantError("No Anthem valve on this account")

        word1 = _command_half(zone1_hex, "zone1_hex")

        # A typed-in sentinel is treated exactly like a blank field on a two-valve system.
        # It cannot mean anything useful there — it addresses no valve, so the device
        # discards the whole command, taking the zone 1 word with it — and "00000000 leaves
        # zone 2 alone" is the natural reading for anyone who has seen the sentinel at all.
        # Honouring it literally would satisfy nobody's intent and void the command instead.
        if zone2_hex and zone2_hex.strip("0") == "" and self.model.uses_valve2:
            _LOGGER.info(
                "send_valve_hex: zone2_hex was all zeroes, which addresses no valve; "
                "re-sending zone 2's current state instead so the command is not discarded"
            )
            zone2_hex = None

        if zone2_hex and not self.model.uses_valve2 and zone2_hex.strip("0") != "":
            # **Refused, not silently sent.** The service form shows Zone 2 whenever *any*
            # valve on the account has one, so on a mixed account the field is offered for a
            # single-zone valve too — and the word used to be encoded and sent to a valve
            # with nothing to receive it. Found by GitHub Copilot's review of upstream #3.
            #
            # All zeroes is exempt: that is the sentinel this valve genuinely uses for "no
            # second valve", and the branch below produces it anyway. Only a word that tries
            # to *command* a zone that does not exist is a mistake worth naming.
            raise HomeAssistantError(
                f"{self.name} has one zone, so zone2_hex addresses nothing. Leave it empty "
                f"(or all zeroes) — the word {zone2_hex!r} was not sent."
            )
        if zone2_hex:
            word2 = _command_half(zone2_hex, "zone2_hex")
        elif not self.model.uses_valve2:
            # The only legitimate use of the sentinel: there is genuinely no second valve.
            word2 = UNUSED_VALVE_WORD
        else:
            # Re-send zone 2 as it stands — never the sentinel, which would risk the device
            # discarding the whole command. See the docstring.
            current = self.gcs_state.valve2
            word2 = encode_word(
                VALVE2_PREFIX,
                current.temperature_celsius if current else 38.0,
                DEFAULT_FLOW_PERCENT,
                current.outlet_mask if current else VALVE_STOP_MASK,
                paused=current.paused if current else False,
            )

        if word1 == UNUSED_VALVE_WORD:
            # Not blocked: this is the escape hatch, the failure mode is "nothing happens"
            # rather than unexpected water, and it is a documented experiment worth being
            # able to run. But it should never be a silent surprise.
            _LOGGER.warning(
                "send_valve_hex: zone1_hex is the all-zero sentinel, which addresses no "
                "valve — the device is expected to discard this entire command, zone 2 "
                "included. To close zone 1 instead, send a word with outlet mask 00."
            )

        decoded = {
            "zone1": _describe_word(word1),
            "zone2": _describe_word(word2),
        }
        _LOGGER.info(
            "send_valve_hex: zone1=%s (%s), zone2=%s (%s)",
            word1,
            decoded["zone1"],
            word2,
            decoded["zone2"],
        )

        # Same rule as `async_apply_valve`: only the zones this word actually closes earn the
        # grace. A raw word is decoded rather than trusted — an undecodable one records
        # nothing, so a genuine cutoff is still caught.
        closing: dict[int, int] = {}
        for zone, word in ((1, word1), (2, word2)):
            if word == UNUSED_VALVE_WORD:
                continue
            try:
                parsed = decode_word(word)
            except ValveHexError:  # pragma: no cover - already validated above
                continue
            # Not `decoded`: that name is the response built above, and reusing it here
            # returned the last zone's raw `ValveWord` instead (v0.2.7 and earlier).
            closing[zone] = 0 if parsed.paused else parsed.outlet_mask
        self._note_local_write()
        self._cutoff.note_local_write(closing)
        try:
            await self.gcs.async_write_valves(word1, word2)
        except DeviceOffline as err:
            raise HomeAssistantError(
                "The Anthem valve is offline. Check that it is powered on and connected "
                "to Wi-Fi, then try again."
            ) from err
        except KohlerError as err:
            raise HomeAssistantError(f"Kohler command failed: {err}") from err

        return {"zone1_hex": word1, "zone2_hex": word2, "decoded": decoded}

    async def async_custom_shower(
        self, zone1_hex: str, zone2_hex: str, *, keep_on_after_warmup: bool
    ) -> dict[str, Any]:
        """Send one complete shower command, optionally resuming it after the warm-up pause.

        The form-driven sibling of `async_send_valve_hex`: `services.py` builds the words
        from typed fields with `encode_shower`, so both are always supplied and nothing here
        is read from the valve's last report. It writes **once**. A second write during a
        warm-up hijacks it onto the written outlets (2026-09-06 live test, session 24 §2c),
        so nothing is ever held back, delayed or re-sent on its own — except the one case the
        caller opts into:

        ``keep_on_after_warmup`` — on a valve with warm-up enabled, the valve warms up and
        then **pauses** for two minutes, just as it always does; left alone, that pause
        ends the session (`anthem/warmup_resume.py` has the corpus). With this
        set, a background watcher follows the GCS reports and, when that pause arrives,
        re-sends the same two words once. It does nothing if no warm-up follows the write, if
        the warm-up ends in a plain stop, if someone takes over at the wall, if any other
        command is sent from here in the meantime, or if a new custom shower replaces it.
        """
        self._cancel_custom_shower("a new custom shower was sent")
        result = await self.async_send_valve_hex(zone1_hex, zone2_hex)
        if keep_on_after_warmup:
            task = self.hass.async_create_task(
                self._async_keep_on_after_warmup(
                    result["zone1_hex"], result["zone2_hex"], self._local_write_serial
                )
            )
            task.add_done_callback(self._custom_shower_done)
            self._custom_shower_task = task
        return result

    @callback
    def _custom_shower_done(self, task: asyncio.Task) -> None:
        """Drop the finished watcher, and log anything it died of that it did not expect."""
        if self._custom_shower_task is task:
            self._custom_shower_task = None
        if task.cancelled():
            return
        if (err := task.exception()) is not None:
            _LOGGER.error("custom_shower: keep-on watcher failed: %r", err)

    @callback
    def _cancel_custom_shower(self, reason: str) -> None:
        """Drop a pending keep-on watcher, saying why."""
        task = self._custom_shower_task
        self._custom_shower_task = None
        if task is not None and not task.done():
            _LOGGER.info("custom_shower: keep-on watcher cancelled, %s", reason)
            task.cancel()

    async def _async_keep_on_after_warmup(
        self, word1: str, word2: str, serial: int
    ) -> None:
        """Re-send a custom shower once the valve's warm-up ends in its pause.

        Woken by every coordinator update and once a second regardless, so the deadlines in
        `WarmupResume` are honoured even if the valve goes quiet. Reads only the valve's own
        state — `warmUpStatus`, the pause flag and the masks — never the controller's.
        """
        poke = asyncio.Event()

        @callback
        def _on_update() -> None:
            poke.set()

        remove = self.coordinator.async_add_listener(_on_update)
        watch = WarmupResume(time.monotonic())
        try:
            while True:
                try:
                    await asyncio.wait_for(poke.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                poke.clear()
                if self._local_write_serial != serial:
                    _LOGGER.info(
                        "custom_shower: another command was sent since; not resuming "
                        "after the warm-up"
                    )
                    return
                state = self.gcs_state
                if state is None:
                    return
                words = [state.valve1]
                if self.model.uses_valve2:
                    words.append(state.valve2)
                outcome = watch.observe(
                    time.monotonic(),
                    state.warmup_in_progress,
                    [bool(word and word.paused) for word in words],
                    [word.outlet_mask if word else 0 for word in words],
                )
                if outcome.decision is Decision.WAIT:
                    continue
                if outcome.decision is Decision.RESUME:
                    _LOGGER.info(
                        "custom_shower: %s; resuming zone1=%s zone2=%s",
                        outcome.reason,
                        word1,
                        word2,
                    )
                    await self.async_send_valve_hex(word1, word2)
                else:
                    _LOGGER.info("custom_shower: %s", outcome.reason)
                return
        except HomeAssistantError as err:
            _LOGGER.warning(
                "custom_shower: could not resume after the warm-up: %s", err
            )
        finally:
            remove()

    async def async_set_zone_outlet(
        self, zone: int, outlet: int, on: bool, *, flow: float | None = None
    ) -> None:
        """Open or close one outlet within a zone.

        ``outlet`` is 1-based **within that zone**, matching how the hardware and the API
        address it. Every other outlet, in both zones, is preserved.

        ``flow`` is the flow to write for this zone, and callers should pass the Flow
        number's current value. Omitting it falls back to `async_apply_valve`'s rule and
        writes `DEFAULT_FLOW_PERCENT`.

        **Why this is not "inherit the valve's flow".** `async_apply_valve` never carries
        flow forward, so that nothing silently adopts whatever the touchscreen last wrote.
        That rule is about the *valve's* byte, not about a value Home Assistant itself
        holds — the run-time-cutoff restore already makes the same distinction, replaying a
        flow it observed rather than defaulting. Passing the Flow entity's value is the
        same case: the user set it, so a subsequent outlet toggle should not silently undo
        it. The valve's idle byte is still never read here; see `GcsState.flow_is_live` for
        why it cannot be trusted.
        """
        if self.gcs_state is None:
            raise HomeAssistantError("No Anthem valve on this account")
        word = self.gcs_state.zone_word(zone)
        mask = word.outlet_mask if word else 0
        bit = 1 << (outlet - 1)
        mask = (mask | bit) if on else (mask & ~bit)
        key = "zone1_flow" if zone == 1 else "zone2_flow"
        await self.async_apply_valve(zone_masks={zone: mask}, **{key: flow})

    async def async_activate_preset(self, preset_id: int | str) -> None:
        """Start a stored GCS preset. **This runs water.**

        One call: the valve runs the preset itself, so no ``solowritesystem`` follow-up is
        needed. Verified live — the body is ``{preset, action}``.

        A preset only applies the zones it opens an outlet on; a zone left at mask ``0x00``
        keeps whatever setpoint it already had, so this cannot be used to set an idle zone's
        temperature.
        """
        if self.gcs is None:
            raise HomeAssistantError("No Anthem valve on this account")
        self._note_local_write()
        try:
            await self.gcs.async_activate_preset(preset_id, True)
        except DeviceOffline as err:
            raise HomeAssistantError(
                "The Anthem valve is offline. Check that it is powered on and connected "
                "to Wi-Fi, then try again."
            ) from err
        except KohlerError as err:
            raise HomeAssistantError(f"Kohler command failed: {err}") from err

    async def async_stop_shower(self) -> None:
        """Stop the water: mask byte ``0x00`` on both zones, **not** the ``0x40`` pause.

        This used to pause, which read nicely as "Paused" in the status sensor. It was
        changed on 2026-08-13 because **a pause is indistinguishable from the valve's own
        run-time cutoff** — the cutoff is internally ``{preset, action:"Off"}`` and writes
        exactly the same ``0x40``. With the restart-on-cutoff option enabled, Home Assistant
        turning the shower off and the valve timing out looked identical on the wire, leaving
        only `note_local_write()`'s 30 s grace between "stopped" and "helpfully restarted".

        ✅ **The strong guarantee is back, 2026-08-18: a stop issued from here can never be
        undone by Endless Shower.** The detector requires the ``0x40`` pause flag again, and
        this method writes ``0x00``, so its stops are outside the restart-eligible set
        entirely — by shape, not by timing.

        The requirement was briefly dropped on 2026-08-17, when a real cutoff arrived as
        ``0x00`` and was ignored. Five case studies established that as **two maximum
        durations set to different values** rather than a protocol gap: only the GCS valve
        cuts with ``0x40`` and only the Anthem Plus controller with ``0x00``, the valve fires
        marginally early and the controller marginally late, so with the two durations equal
        the pause always arrives first. See `anthem/runtime_cutoff.py` and
        `docs/case_studies/`.

        `note_local_write()`'s 30 s grace still applies and is now belt-and-braces rather than
        the only protection.

        Still routed through `async_apply_valve` rather than `GcsDevice.async_turn_off()`,
        which would write a flat 38.0 °C to both zones — this preserves each zone's own
        setpoint while clearing its outlets.
        """
        await self.async_apply_valve(zone_masks={1: 0, 2: 0})

    # ------------------------------------------------------------------ #
    # Warm-up
    #
    # The machinery lives in `warmup_manager.WarmupManager` — thirteen methods and six
    # pieces of state, all of them about one setting. What stays here is the surface the
    # rest of the integration already used, unchanged: entities, services and diagnostics
    # call these names and were not touched by the 0.10.0 move.
    # ------------------------------------------------------------------ #
    async def async_set_warmup(self, mode: str) -> None:
        """Set the valve's warmup mode. **This does not run water now.**"""
        await self.warmup.async_set_warmup(mode)

    async def async_read_warmup_mode(self) -> str | None:
        """Read the warmup mode from the REST API and apply it, returning what it said."""
        return await self.warmup.async_read_warmup_mode()

    @property
    def warmup_auto_restore(self) -> bool:
        """Whether to put the warmup mode back after something else disables it."""
        return self.warmup.auto_restore

    @property
    def last_warmup_mode(self) -> str | None:
        """The last *enabled* warmup mode seen on the valve, or None if never seen."""
        return self.warmup.last_mode

    def _message_window(self, since: float, until: float | None = None) -> list[dict]:
        """Messages between two monotonic instants, oldest first, without the clock field."""
        return self.warmup._message_window(since, until)

    @callback
    def _handle_warmup_mode_change(
        self, before: str | None, after: str | None, *, announced: bool = False
    ) -> None:
        """React to the valve announcing a new warmup mode."""
        self.warmup.handle_mode_change(before, after, announced=announced)


class KohlerAnthemCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the connection and the per-device state objects."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # `config_entry` must be passed on modern Home Assistant: without it
        # `async_config_entry_first_refresh()` refuses to run. Older releases do not accept
        # the keyword at all, so fall back rather than hard-failing on them.
        # `update_interval=SCAN_INTERVAL` disables interval polling entirely. State is
        # push-only: MQTT carries every change, and the REST reads happen on two *events* —
        # setup, and every MQTT (re)connect — rather than on a clock.
        #
        # ⚠️ **That was one event short of the truth until 2026-08-21.**
        # `async_config_entry_first_refresh()` runs immediately after `async_setup()` and the
        # base class turns it into a third read of everything. `_async_update_data` now
        # short-circuits that one, so the sentence above is enforced rather than merely
        # intended — see the comment there before removing it.
        #
        # `_async_update_data()` still exists and still works; with no interval it runs only
        # when something asks, which is what `homeassistant.update_entity` does. That is the
        # manual refresh, and there is no automatic one.
        try:
            super().__init__(
                hass,
                _LOGGER,
                name=DOMAIN,
                update_interval=SCAN_INTERVAL,
                config_entry=entry,
            )
        except TypeError:
            super().__init__(
                hass,
                _LOGGER,
                name=DOMAIN,
                update_interval=SCAN_INTERVAL,
            )
        # Kept under our own name rather than relying on the base class's `config_entry`,
        # whose presence varies by release.
        self.entry = entry
        # The entry as it looked when this coordinator was built, frozen. Home Assistant
        # mutates the `ConfigEntry` object in place, so `self.entry` is a live view and
        # cannot serve as a "before" — comparing it against the entry compares an object
        # with itself. `_async_update_listener` compares against this instead.
        self.reload_signature = entry_reload_signature(entry)
        # The stored split wins over the SKU: an install that matches no catalogue model
        # still reloads correctly, and a SKU label can never silently change topology.
        stored = entry.data.get(CONF_ZONE_OUTLETS)
        if isinstance(stored, (list, tuple)) and len(stored) == 2:
            self.model = model_for_topology(int(stored[0]), int(stored[1]))
        else:
            self.model = get_valve_model(entry.data[CONF_VALVE_MODEL])
        self.temperature_unit: str = entry.data.get(CONF_TEMPERATURE_UNIT, "Fahrenheit")
        # `Standard` (US gallons) or `Liters`, as the Konnect account is set. Captured at
        # config time beside the temperature unit; refreshed from the customer read below.
        self.water_units: str = entry.data.get(CONF_WATER_UNITS, "Standard")

        session = async_get_clientsession(hass)
        self.auth = KohlerAuth(session, entry.data.get(CONF_REFRESH_TOKEN))
        # Persist a rotated refresh token the moment B2C issues one. Without this the entry
        # keeps a token Kohler has already retired, and on a push-only install nothing else
        # writes it back for hours — see `KohlerAuth._async_token_request`.
        self.auth.on_token_rotated = self._store_refresh_token
        self.client = KohlerClient(session, self.auth, entry.data.get(CONF_TENANT_ID))

        # Every Anthem valve on the account, in the order the cloud lists them, plus the
        # same objects keyed by device id for envelope routing. Empty on a controller-only
        # account. See `Valve` for what each one carries — everything that used to be a
        # singular `gcs_*` field here, and everything that acted on it.
        self.valves: list[Valve] = []
        self._valves_by_id: dict[str, Valve] = {}
        # Every Anthem Plus controller on the account, in the order the cloud lists them,
        # plus the same objects keyed by device id for envelope routing. Empty on a
        # valve-only account. See `Controller` for what each one carries.
        self.controllers: list[Controller] = []
        self._controllers_by_id: dict[str, Controller] = {}
        self.stream: AnthemMqttStream | None = None
        self.raw_log: RawMqttLog | None = None
        # REPORT LOG: the consumer-side capture behind the "Report Log" switch — one file
        # per switch-on, appended across restarts. See `anthem/report_log.py`.
        self.report_log: ReportLog | None = None
        #: The reseed spawned on every MQTT connect. Held so `async_shutdown_stream` can
        #: cancel it — it outlives an unload otherwise, see `_handle_connected`.
        self._reseed_task: asyncio.Task | None = None
        # One-shot: `async_setup` seeds, then `async_config_entry_first_refresh()` runs
        # milliseconds later and would seed the identical state all over again. See
        # `_async_update_data`.
        self._seeded_during_setup = False
        # CUTOFF DEBUG LOG: built in `async_setup`, once `hass.config.path` is usable.
        self.cutoff_log: CutoffDebugLog | None = None
        self.warmup_log: CutoffDebugLog | None = None
        # Rolling record of recent messages, so a warmup disable can be journalled
        # with what surrounded it. Bounded by count and trimmed by age on read.
        self._recent_messages: deque = deque(maxlen=WARMUP_CONTEXT_MAX_MESSAGES)

    # ------------------------------------------------------------------ #
    # Setup / teardown
    # ------------------------------------------------------------------ #
    async def async_setup(self) -> None:
        """Discover devices, seed state from REST, then start the MQTT stream."""
        try:
            customer = await self.client.async_get_customer()
        except AuthUnavailable as err:
            # Kohler unreachable, not a bad credential — retry setup, do not ask the user
            # to sign in again.
            raise ConfigEntryNotReady(f"Cannot reach Kohler: {err}") from err
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except KohlerError as err:
            raise ConfigEntryNotReady(f"Cannot reach Kohler: {err}") from err

        self.temperature_unit = customer.temperature_unit or self.temperature_unit
        self.water_units = customer.water_units or self.water_units
        valves = customer.gcs_devices
        controllers = customer.hub_devices
        if not valves and not controllers:
            raise ConfigEntryNotReady("No Anthem devices on this account")

        # Every valve, not the first one. Each carries its own state, cutoff detector,
        # warm-up restore, cloud watch and settings — see `Valve`. The settings move
        # first, so the first valve's `Valve.__init__` finds its learned run times where
        # they now live rather than where the single-valve versions left them.
        if valves:
            self._migrate_valve_settings(valves[0].device_id)
        names = valve_names(valves)
        self.valves = [
            Valve(
                self,
                device,
                self.model,
                names[device.device_id],
                tag=device.device_id if len(valves) > 1 else None,
            )
            for device in valves
        ]
        self._valves_by_id = {v.device_id: v for v in self.valves}
        if len(self.valves) > 1:
            _LOGGER.info(
                "Account has %d Anthem valves: %s",
                len(self.valves),
                ", ".join(valve.name for valve in self.valves),
            )
        # Every controller, not the first one. Each gets its own command surface and its
        # own state, both keyed by its device id: the one account-level MQTT stream carries
        # messages for all of them, and `_handle_envelope` sorts them by that id. The
        # entry's model is only the starting layout — `_async_seed_state` replaces it per
        # controller with what that controller's own configuration says.
        names = controller_names(controllers)
        self.controllers = [
            Controller(
                device,
                HubDevice(self.client, device.device_id, self.temperature_unit),
                HubState(self.model),
                names[device.device_id],
            )
            for device in controllers
        ]
        self._controllers_by_id = {c.device_id: c for c in self.controllers}
        if len(self.controllers) > 1:
            _LOGGER.info(
                "Account has %d Anthem Plus controllers: %s",
                len(self.controllers),
                ", ".join(controller.name for controller in self.controllers),
            )

        try:
            await self._async_seed_state()
        except AuthUnavailable as err:
            # Same split as the customer read above. The seed swallows `KohlerError` per
            # read ("failures for one device do not blank the other"), but the token layer
            # under every read raises `AuthError`, which is not a `KohlerError` — left bare,
            # a rejection here escaped `async_setup_entry` as an unhandled exception: no
            # reauth prompt, no retry, an entry stuck on "Failed to set up". Found 2026-08-21
            # while proving the startup-read fold; fixed 2026-08-22.
            raise ConfigEntryNotReady(f"Cannot reach Kohler: {err}") from err
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        # `async_config_entry_first_refresh()` follows immediately in `async_setup_entry` and
        # would repeat every read above for nothing. Claimed here, spent in
        # `_async_update_data`.
        self._seeded_during_setup = True
        for valve in self.valves:
            await valve._async_sync_default_preset_timer()
        self._persist_refresh_token()

        # One identity for the life of this config entry. Generated on first setup and
        # persisted, so restarts and reconnects reuse it instead of leaving a trail of
        # dead registrations on the Kohler account.
        mobile_device_id = self.entry.data.get(CONF_MOBILE_DEVICE_ID)
        first_registration = not mobile_device_id
        if first_registration:
            mobile_device_id = uuid.uuid4().hex[:16]
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_MOBILE_DEVICE_ID: mobile_device_id},
            )

        # RAW MQTT LOG: constructed unconditionally and switched on at runtime, so capture
        # can be started from the UI mid-session without a reload. Nothing touches the disk
        # until a message arrives while it is on. See `anthem/raw_log.py`.
        self.raw_log = RawMqttLog(
            self.hass.config.path(RAW_MQTT_LOG_DIR),
            forced=ENABLE_RAW_MQTT_LOG,
            max_bytes=RAW_MQTT_LOG_MAX_BYTES,
            keep_files=RAW_MQTT_LOG_KEEP_FILES,
        )
        # Open the file up front when capture is already on, so it is findable immediately
        # rather than after the next push — which can be hours away. Executor, not the loop:
        # this creates a directory and opens a file.
        await self.hass.async_add_executor_job(self.raw_log.prepare)

        # REPORT LOG: the consumer capture, in the integration's own folder (owner's
        # choice — see the const.py section). The options key holds the active episode's
        # name; its presence here means the switch was on when Home Assistant stopped, so
        # re-attach to the SAME file — a capture of "it breaks when I restart" must not
        # lose the interesting part to the restart itself.
        self.report_log = ReportLog(
            os.path.join(os.path.dirname(__file__), REPORT_LOG_DIR_NAME),
            max_bytes=REPORT_LOG_MAX_BYTES,
        )
        episode = self.entry.options.get(CONF_REPORT_LOG_FILE)
        if episode:
            await self.hass.async_add_executor_job(self.report_log.resume, episode)

        # CUTOFF DEBUG LOG: same directory as the raw capture on purpose — the two are read
        # together, joined on `ts`. See `anthem/cutoff_log.py`.
        self.cutoff_log = CutoffDebugLog(
            self.hass.config.path(RAW_MQTT_LOG_DIR),
            forced=ENABLE_CUTOFF_DEBUG_LOG,
            keep_files=CUTOFF_DEBUG_LOG_KEEP_FILES,
        )
        for valve in self.valves:
            valve.attach_journal(self.cutoff_log)
        await self.hass.async_add_executor_job(self.cutoff_log.prepare)

        # WARMUP JOURNAL: a second journal in the same directory, on the same clock, for a
        # different open question — see `WARMUP_README`. Separate from the cutoff log because
        # the two are read for different reasons and `pause_resolution.py` and friends glob
        # `cutoff_*.jsonl`; mixing warmup records into that corpus would silently change what
        # those tools count.
        self.warmup_log = CutoffDebugLog(
            self.hass.config.path(RAW_MQTT_LOG_DIR),
            forced=ENABLE_WARMUP_DEBUG_LOG,
            keep_files=WARMUP_DEBUG_LOG_KEEP_FILES,
            prefix="warmup",
            readme=WARMUP_README,
            readme_fields={
                "before": int(WARMUP_CONTEXT_BEFORE_SECONDS),
                "after": int(WARMUP_CONTEXT_AFTER_SECONDS),
            },
            label="Warmup journal",
        )
        await self.hass.async_add_executor_job(self.warmup_log.prepare)
        for valve in self.valves:
            valve.journal_baseline()

        self.stream = AnthemMqttStream(
            self.client,
            self._handle_envelope,
            on_connect=self._handle_connected,
            on_auth_error=self._handle_auth_error,
            mobile_device_id=mobile_device_id,
            raw_log=self.raw_log,
            report_log=self.report_log,
            # Only a brand-new identity can plausibly need provisioning time. A reused one
            # has connected before, so silence from it is real silence.
            expect_warmup=first_registration,
        )
        try:
            await self.stream.async_start()
        except (AuthError, KohlerError) as err:
            # State is already seeded, so the integration is usable but frozen until the
            # stream recovers. A warning rather than a setup failure — the reconnect loop
            # keeps trying, and each success re-seeds.
            _LOGGER.warning("Kohler MQTT stream did not start: %s", err)
            if credential_is_dead(err):
                self._handle_auth_error(err)

        # The Repairs card used to be keyed by entry alone; it is per valve now, and an
        # upgrade must not leave the old one standing. Deleting a missing issue is a no-op.
        ir.async_delete_issue(
            self.hass, DOMAIN, f"{ISSUE_NOT_SET_UP}_{self.entry.entry_id}"
        )
        for valve in self.valves:
            valve.announce_readiness()
            # Arms trigger B's countdown. Nothing is asked of Kohler until the valve has
            # actually been quiet for the full interval, and any valve message resets it.
            valve.cloud_watch.async_start()
            valve.async_refresh_setup_issue()

    @callback
    def _migrate_valve_settings(self, device_id: str) -> None:
        """Move the flat per-valve keys under `CONF_VALVES`, once.

        Before 2026-09-08 the entry held one valve's worth of settings as flat keys —
        `CONF_OUTLET_RUN_TIMES` in data; `CONF_RESTART_ON_RUNTIME_CUTOFF`,
        `CONF_WARMUP_AUTO_RESTORE` and `CONF_LAST_WARMUP_MODE` in options. They belong to
        whichever valve that install had, which on an account that has just grown a second
        one is the first the cloud lists. Copied under that device id and removed, so there
        is one scheme afterwards; a value already present per valve is never overwritten.

        Every key involved is reload-ignored, so this write does not bounce the entry.
        """
        data = dict(self.entry.data)
        options = dict(self.entry.options)
        moved_data = {
            key: data.pop(key) for key in (CONF_OUTLET_RUN_TIMES,) if key in data
        }
        moved_options = {
            key: options.pop(key)
            for key in (
                CONF_RESTART_ON_RUNTIME_CUTOFF,
                CONF_WARMUP_AUTO_RESTORE,
                CONF_LAST_WARMUP_MODE,
            )
            if key in options
        }
        if not moved_data and not moved_options:
            return
        if moved_data:
            valves = dict(data.get(CONF_VALVES) or {})
            valves[device_id] = {**moved_data, **(valves.get(device_id) or {})}
            data[CONF_VALVES] = valves
        if moved_options:
            valves = dict(options.get(CONF_VALVES) or {})
            valves[device_id] = {**moved_options, **(valves.get(device_id) or {})}
            options[CONF_VALVES] = valves
        self.hass.config_entries.async_update_entry(
            self.entry, data=data, options=options
        )
        # No device id: this is INFO, and an id is a cloud address (see 0.9.0). The
        # migration targets the first valve the cloud lists and runs once, so naming the
        # keys is the whole of what a reader needs.
        _LOGGER.info(
            "Moved per-valve settings (%s) under the first valve on the account",
            ", ".join(sorted([*moved_data, *moved_options])),
        )

    @callback
    def _handle_auth_error(self, err: Exception) -> None:
        """Surface a rejected credential as a reauth prompt.

        Push-only removed the last thing that ran on a clock, and with it the only path that
        regularly reached ``ConfigEntryAuthFailed``. `_async_update_data` still raises it,
        but with ``SCAN_INTERVAL = None`` it fires only on a manual
        ``homeassistant.update_entity``. So without this, an expired or revoked refresh
        token leaves the entry looking healthy — MQTT down, entities frozen at their last
        values rather than unavailable, and no prompt anywhere — while the reconnect loop
        retries forever against a credential that will never be accepted.

        `async_start_reauth` is idempotent; the stream also latches, so repeated failures
        do not stack up flows.
        """
        _LOGGER.error(
            "Kohler rejected the stored credential (%s); reauthentication required", err
        )
        self.entry.async_start_reauth(self.hass)

    @callback
    def _handle_connected(self) -> None:
        """Re-seed whenever the stream connects.

        This is what replaces interval polling. The broker sends no state on connect — only
        future change events — so without a read here a reconnect would leave every entity
        holding whatever it had before the gap, with nothing to correct it until the shower
        was next used.
        """
        # Durations measured across a disconnect are meaningless — we cannot know what the
        # outlets did while the stream was down, and the gap has been as long as 11.9 hours.
        # Dropping the timings means a session spanning a reconnect is simply not judged,
        # rather than judged on a number we made up.
        for valve in self.valves:
            valve.forget_timings()
        # **Held, so an unload can cancel it.** This was the one `async_create_task` in the
        # file with no reference kept, and it is the longest-running: `_async_seed_state`
        # can be a dozen REST round trips. A reload inside that window left it awaiting HTTP
        # against a coordinator Home Assistant had already discarded — and it ends in
        # `_persist_refresh_token()` (a config-entry write) and `async_set_updated_data()`
        # (a push into entities that no longer exist), which is exactly the hazard
        # `Valve._background_tasks` was built for.
        if self._reseed_task is not None and not self._reseed_task.done():
            # A second connect while the first reseed is still running: let it finish rather
            # than starting a rival that would race it over the same state objects.
            return
        self._reseed_task = self.hass.async_create_task(
            self._async_reseed_after_connect()
        )

    async def _async_reseed_after_connect(self) -> None:
        try:
            await self._async_seed_state()
        except (AuthError, KohlerError) as err:
            # The stream is up regardless; pushes will still arrive. Do not fail the entry
            # over a re-seed, and do not retry here — the next connect will try again.
            _LOGGER.warning("Kohler re-seed after MQTT connect failed: %s", err)
            if credential_is_dead(err):
                # A rejected credential is the one failure the next connect cannot fix,
                # and this path would otherwise absorb it silently.
                self._handle_auth_error(err)
            return
        self._persist_refresh_token()
        self.async_set_updated_data(self._snapshot())

    async def async_shutdown_stream(self) -> None:
        """Stop the MQTT stream on unload."""
        # Before the valves and the stream: it writes to the config entry and pushes state
        # into entities, neither of which is safe against an entry that is going away.
        if self._reseed_task is not None:
            self._reseed_task.cancel()
            self._reseed_task = None
        for valve in self.valves:
            valve.stop()
        if self.stream is not None:
            await self.stream.async_stop()
            self.stream = None
        # The raw capture is closed by the stream's own teardown; this one has no stream to
        # ride on, so it is released here. Blocking close — off the loop.
        if self.cutoff_log is not None:
            await self.hass.async_add_executor_job(self.cutoff_log.close)
        if self.warmup_log is not None:
            await self.hass.async_add_executor_job(self.warmup_log.close)

    # ------------------------------------------------------------------ #
    # Push
    # ------------------------------------------------------------------ #
    def _handle_envelope(self, envelope: Envelope) -> None:
        """Apply an MQTT message and notify entities if it changed anything."""
        changed = False
        # Valves and controllers alike: the one account-level stream carries every
        # device's messages, and the device id says whose each one is.
        valve = self._valves_by_id.get(envelope.device_id)
        if valve is not None:
            changed |= valve.handle_envelope(envelope)
            self._remember_message(envelope)
        # The one account-level stream carries every controller's messages; the device id
        # says whose this is. A message from a controller this entry does not know — one
        # added in the app since setup — falls through untouched until a reload lists it.
        controller = self._controllers_by_id.get(envelope.device_id)
        if controller is not None:
            changed |= controller.state.apply_envelope(envelope)
            self._remember_message(envelope)
            if controller.state.favorites:
                controller.favorites = controller.state.favorites
            # Trigger A: a controller report of a zone ON, with the valve silent. Every
            # valve's watch hears every controller: which controller fronts which valve
            # is not knowable from the cloud, and a spurious trigger costs one
            # rate-limited read, not a verdict.
            for valve in self.valves:
                valve.cloud_watch.note_hub_envelope(envelope)
        if changed:
            self.async_set_updated_data(self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        """A cheap dict so DataUpdateCoordinator has something to hand entities.

        Entities read the state objects directly; this only carries freshness markers.
        """
        return {
            "gcs_last_update": {
                v.device_id: v.gcs_state.last_update for v in self.valves
            },
            "hub_last_update": {
                c.device_id: c.state.last_update for c in self.controllers
            },
            "mqtt_connected": bool(self.stream and self.stream.connected),
            # CLOUD CONNECTION WATCH. Carried here so a check result re-renders the entity
            # the same way a device push does — the value itself lives on the watch.
            "cloud_connected": {
                v.device_id: v.cloud_watch.connected for v in self.valves
            },
        }

    @callback
    def async_refresh_entities(self) -> None:
        """Re-render entities from what is already in memory, with no network read.

        For state that changes without a message arriving — currently only the cloud
        reachability check, which is answered over REST on its own schedule and has no push
        source to ride in on.
        """
        self.async_set_updated_data(self._snapshot())

    # ------------------------------------------------------------------ #
    # Poll
    # ------------------------------------------------------------------ #
    async def _async_update_data(self) -> dict[str, Any]:
        if self._seeded_during_setup:
            # **The first refresh after setup is not a refresh.** `async_setup_entry` calls
            # `async_setup()` and then `async_config_entry_first_refresh()` on the next line,
            # and the base class turns that into a `_async_update_data()` — so without this,
            # every start read the whole account twice, milliseconds apart, for state that
            # could not have changed in between. Measured 2026-08-21: **five duplicate REST
            # calls per start** (gcs-state, gcsadvancestate, presets, hub-state, favorites;
            # each controller's configuration read is already skipped once its
            # `capabilities.known`).
            #
            # What the first refresh is actually *for* is populating `coordinator.data`
            # before the platforms are forwarded — `async_setup` never calls
            # `async_set_updated_data`, so `data` is None until this returns. That needs the
            # snapshot, not the network.
            #
            # **Why this is safe, and not merely cheap.** `async_setup` ends by awaiting
            # `stream.async_start()`, which returns with the socket up — so `_handle_connected`
            # has already scheduled a full re-seed of its own by the time this runs. Anything
            # that changed in the gap between the setup read and the stream coming up is
            # caught by *that* read, which happens after the connection exists rather than
            # before it. This one was redundant with it, a few hundred milliseconds earlier
            # and strictly worse placed.
            #
            # ⚠️ **Only the first one.** A manual `homeassistant.update_entity` is the only
            # other way in — with `SCAN_INTERVAL = None` there is no clock — and that one
            # must read for real, so the flag is spent here and never set again.
            self._seeded_during_setup = False
            self._persist_refresh_token()
            return self._snapshot()
        try:
            await self._async_seed_state()
        except AuthUnavailable as err:
            raise UpdateFailed(f"Kohler auth service unreachable: {err}") from err
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except KohlerError as err:
            raise UpdateFailed(f"Kohler poll failed: {err}") from err
        self._persist_refresh_token()
        return self._snapshot()

    async def _async_seed_state(self) -> None:
        """Read current state over REST into the state objects.

        Runs at setup, on every MQTT connect, and on a manual `update_entity`. Failures for
        one device do not blank the other.
        """
        # **In parallel.** Each device's reads are independent, and the only ordering that
        # matters is inside one device — settings before state on a valve, configuration
        # before state on a controller — which stays sequential within each coroutine. Run
        # serially this was ~14 round trips end to end on a two-valve, two-controller
        # account: several seconds of waiting on every restart and reload.
        #
        # `return_exceptions=True` preserves the existing behaviour that one device's
        # failure does not blank the others; each coroutine already catches `KohlerError`
        # per read, so anything reaching here is unexpected and is logged rather than
        # allowed to cancel its siblings.
        results = await asyncio.gather(
            *(valve.async_seed() for valve in self.valves),
            *(
                self._async_seed_controller(controller)
                for controller in self.controllers
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.warning("Seeding a Kohler device failed: %s", result)

    async def _async_seed_controller(self, controller: Controller) -> None:
        """Seed one controller. Extracted so every device can be seeded concurrently."""
        device_id = controller.device_id
        # Zones, outlet types, and installed parts — installation-time facts that no
        # message ever pushes because nothing changes them at runtime. Read once and
        # keep it; re-reading on a timer polls forever for an event that happens when a
        # plumber visits.
        #
        # Read BEFORE the state, not after it as this used to: the same response says
        # how many outlets each of this controller's zones has, which decides how its
        # state decodes the zone arrays in everything that follows. See
        # `_apply_controller_topology`.
        if not controller.capabilities.known:
            try:
                config = await self.client.async_get_hub_configuration(device_id)
                configuration = config.get("configuration") or {}
                controller.capabilities = HubCapabilities.from_configuration(
                    configuration
                )
                self._apply_controller_topology(controller, configuration)
            except KohlerError as err:
                _LOGGER.debug(
                    "Could not read HUB configuration for %s: %s", device_id, err
                )
        try:
            controller.state.apply_rest_state(
                await self.client.async_get_hub_state(device_id)
            )
        except KohlerError as err:
            _LOGGER.debug("Could not seed HUB state for %s: %s", device_id, err)
        try:
            payload = await self.client.async_get_hub_favorites(device_id)
            favorites = payload.get("favorites")
            if isinstance(favorites, list):
                # Favorite ids are reassigned when one is deleted, so this list is the
                # only safe way to resolve a favorite — never hardcode an id.
                controller.favorites = favorites
        except KohlerError as err:
            if getattr(err, "status", None) == 404:
                # Not a failure: this endpoint 404s when the controller has **no** saved
                # favorites, rather than returning an empty list. Confirmed 2026-08-17 —
                # the route is handled (it answers with the application's own error
                # envelope, unlike a genuine bad path), MQTT `FAVORITES_SNAPSHOT` agrees
                # with `attributes: []`, and `docs/hub/cloud_api.md` §5.2 has a captured
                # 200 from when this account still had one. Logging it as an error made
                # three misleading lines per startup.
                controller.favorites = []
                _LOGGER.debug("No HUB favorites are saved on %s", device_id)
            else:
                _LOGGER.debug("Could not read HUB favorites for %s: %s", device_id, err)

    @callback
    def _apply_controller_topology(
        self, controller: Controller, configuration: dict[str, Any]
    ) -> None:
        """Give a controller the outlet layout its own configuration reports.

        The entry's model is what the config flow detected — from the valve where there is
        one, else from the first controller that answered — and it is right for that device.
        It is not necessarily right for a second controller: the ordinary reason an account
        has two is two bathrooms, and nothing says they were plumbed with the same valve
        model. So each controller decodes its zone arrays with the split its own
        `hub-configuration` states, and keeps the entry's model only when that read yields
        nothing — which is exactly the case in which the config flow would have asked.

        The model decides how many outlet entities the controller gets, which is why this
        runs inside the setup seed, before the platforms are built, and never again:
        `capabilities.known` gates the read, and a plumber's visit needs a reload anyway.
        """
        detected = topology_from_hub_configuration(configuration)
        if not detected:
            return
        model = model_for_topology(*detected)
        current = controller.model
        if (model.outlets_valve1, model.outlets_valve2) == (
            current.outlets_valve1,
            current.outlets_valve2,
        ):
            return
        # Name, not id — same reasoning as the valve's topology message above.
        _LOGGER.info(
            "%s reports %s; using that for this controller instead of the entry's %s",
            controller.name,
            describe_topology(detected),
            current.sku,
        )
        controller.state.model = model

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _persist_refresh_token(self) -> None:
        """Write the current refresh token back to the config entry.

        Kept for the callers that already invoke it. Rotation itself now persists through
        `_store_refresh_token`, so this is a safety net rather than the mechanism.
        """
        self._store_refresh_token(self.auth.refresh_token)

    @callback
    def _store_refresh_token(self, token: str | None) -> None:
        """Persist one refresh token, if it is new.

        Called from `KohlerAuth` the instant a rotation happens — which is inside the auth
        lock, on the event loop, so `async_update_entry` is safe to call directly here.
        """
        if token and token != self.entry.data.get(CONF_REFRESH_TOKEN):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_REFRESH_TOKEN: token}
            )

    # ------------------------------------------------------------------ #
    # Local writes — what the valves' custom-shower watchers count
    # ------------------------------------------------------------------ #
    def _note_local_write(self) -> None:
        """Count a controller command against every valve's custom-shower watcher.

        A valve command bumps only its own serial (`Valve._note_local_write`). A
        controller command — a favorite, the controller's own shower on/off, stop-all —
        cannot be attributed to one valve from the cloud, so it counts against all of
        them: a watcher that then declines to resume is the safe direction of error.
        """
        for valve in self.valves:
            valve._note_local_write()

    # ------------------------------------------------------------------ #
    # Report log — the consumer capture behind the "Report Log" switch
    # ------------------------------------------------------------------ #
    @property
    def report_log_active(self) -> bool:
        """Whether a capture episode is in force.

        Read from the entry options, not from the log object: the options key is what
        survives a restart, and the switch must show ON after one even in the moments
        before `async_setup` has re-attached the file.
        """
        return bool(self.entry.options.get(CONF_REPORT_LOG_FILE))

    async def async_start_report_log(self) -> None:
        """Begin a new capture episode — a fresh file, named for this moment.

        Idempotent while an episode is running: turning an already-on switch on again must
        not split the file. The episode name is persisted to the entry options so a
        restart resumes the same file; the key is in `RELOAD_IGNORED_OPTION_KEYS`, so this
        write does not reload the entry and drop the stream being captured.
        """
        if self.report_log is None or self.report_log_active:
            return
        episode = await self.hass.async_add_executor_job(self.report_log.start)
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_REPORT_LOG_FILE: episode},
        )
        # Both devices carry this switch; refresh them together so they never disagree.
        self.async_update_listeners()

    async def async_stop_report_log(self) -> None:
        """End the capture episode. The files stay on disk until deleted by hand."""
        if self.report_log is not None:
            await self.hass.async_add_executor_job(self.report_log.stop)
        if CONF_REPORT_LOG_FILE in self.entry.options:
            self.hass.config_entries.async_update_entry(
                self.entry,
                options={
                    k: v
                    for k, v in self.entry.options.items()
                    if k != CONF_REPORT_LOG_FILE
                },
            )
        self.async_update_listeners()

    # ------------------------------------------------------------------ #
    # Message record — context for the valves' warmup journals
    # ------------------------------------------------------------------ #
    @callback
    def _remember_message(self, envelope: Envelope) -> None:
        """Keep a light record of every message, for the warmup journal's context windows.

        Deliberately small: a code, a sku, a timestamp, and — only for the valve's own status
        message — the four fields that tell a configuration write apart from an ordinary
        status. The raw capture beside this holds every payload in full; duplicating it here
        would make the journal unreadable for the one thing it is for.
        """
        record: dict[str, Any] = {
            # The same stamp shape the journal and the raw capture use, so the three sort
            # together on one clock.
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "at": time.monotonic(),
            "sku": envelope.sku,
            "code": envelope.code,
            # Which device, so `Valve._message_window` can leave other valves' traffic
            # out. Stripped again before the record reaches the journal.
            "device": envelope.device_id,
        }
        if envelope.code == MSG_GCS_SOLO_STATUS:
            attribute = envelope.attribute() or {}
            for key in (
                "configChangeIndent",
                "configWriteAllowedFlag",
                "currentSystemState",
                "warmUpStatus",
            ):
                if key in attribute:
                    record[key] = attribute[key]
        self._recent_messages.append(record)

    # ------------------------------------------------------------------ #
    # Controller commands
    # ------------------------------------------------------------------ #
    async def async_activate_favorite(
        self, controller: Controller, favorite_id: Any, name: str
    ) -> None:
        """Start a controller favorite. **This runs water.**

        The controller's only way to set water state: it has no direct temperature/outlet
        command, so a favorite is created holding that configuration and then activated.
        Activation is allowed even while something else is running.
        """
        self._note_local_write()
        try:
            await controller.hub.async_activate_favorite(favorite_id, name, True)
        except DeviceOffline as err:
            raise HomeAssistantError(_controller_offline(controller)) from err
        except KohlerError as err:
            raise HomeAssistantError(f"Kohler command failed: {err}") from err

    async def async_set_hub_shower(self, controller: Controller, on: bool) -> None:
        """Run or stop the controller's own default shower. **On runs water.**

        ``valvecontrol {valveOnOff}`` — the controller's one direct water command, and the
        only place in the system where a bare on/off exists. It works because the controller
        stores its own default configuration; the GCS valve has no equivalent, which is why
        the valve's shower switch has to name a preset instead.

        Off stops the water only, leaving music, steam, and lighting running. Use
        :meth:`async_stop_hub` to idle everything.
        """
        self._note_local_write()
        try:
            await controller.hub.async_set_shower(on)
        except DeviceOffline as err:
            raise HomeAssistantError(_controller_offline(controller)) from err
        except KohlerError as err:
            raise HomeAssistantError(f"Kohler command failed: {err}") from err

    async def async_stop_hub(self, controller: Controller) -> None:
        """Stop everything the controller is running — water, steam, music, lighting.

        Uses ``stopall`` rather than deactivating the active favorite, because the
        favorite may already have been replaced by whatever is running now, and a stop
        should not depend on correctly identifying what to stop.
        """
        self._note_local_write()
        try:
            await controller.hub.async_stop_all()
        except DeviceOffline as err:
            raise HomeAssistantError(_controller_offline(controller)) from err
        except KohlerError as err:
            raise HomeAssistantError(f"Kohler command failed: {err}") from err
