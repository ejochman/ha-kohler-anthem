"""Device state assembled from the MQTT stream and REST snapshots.

Each device type keeps its own state object. They are fed from two directions:

* ``apply_envelope()`` for live MQTT updates, and
* ``apply_rest_state()`` once at startup, because MQTT is event-driven and says nothing
  until the shower next changes. Without a seed read, a restart leaves every entity unknown
  until somebody touches the shower.

Both are pure data — no Home Assistant imports — so they can be exercised offline against
captured payloads.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Container
from dataclasses import dataclass, field, replace
from typing import Any

from .const import (
    MSG_GCS_OUTLET_CONFIG,
    MSG_GCS_PRESET_STATUS,
    MSG_GCS_SOLO_STATUS,
    MSG_GCS_WARMUP_STATUS,
    MSG_HUB_FAVORITE,
    MSG_HUB_FAVORITES_SNAPSHOT,
    MSG_HUB_LIGHT,
    MSG_HUB_MUSIC,
    MSG_HUB_SHOWER_VALVE,
    MSG_HUB_STEAM,
    SKU_GCS,
    SKU_HUB,
    WARMUP_IN_PROGRESS,
)
from .hub import outlet_flags, zone_number
from .models import ValveModel
from .mqtt import Envelope
from .valve_hex import (
    FLOW_BYTE_MAX,
    FLOW_BYTE_MIN,
    ValveHexError,
    ValveWord,
    celsius_to_unit,
    decode_word,
)

_LOGGER = logging.getLogger(__name__)


def _is_warmup_in_progress(value: object) -> bool:
    """Whether a warmup status field means warmup is actually running.

    Compare the whole value, never a suffix. The two states are ``warmUpInProgress`` and
    ``warmUpNotInProgress``, and **"NotInProgress" ends with "InProgress"** — a suffix test
    reports warmup running 100% of the time. That shipped, and because ``Warming Up``
    outranks ``Water Running`` in the status sensor, it pinned the sensor to a single value
    forever. 444 of the 453 captured messages carry the negative form.
    """
    return str(value).strip().lower() == WARMUP_IN_PROGRESS.lower()


def _flag(value: object) -> bool | None:
    """Normalise the HUB's ``"0"``/``"1"`` flags, distinguishing absent from false.

    The controller sends these as **strings**, and only ``"0"`` and ``"1"`` have ever been
    observed. Returning None for anything else — including the ``null`` every non-shower
    message carries for ``showerwarmup`` — is what keeps an unrelated ``MUSIC_STS`` from
    clearing a warm-up that is genuinely still running. A plain ``bool()`` would do exactly
    that, and ``bool("0")`` is True, which would invert it.
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "on"}:
        return True
    if text in {"0", "false", "off"}:
        return False
    return None


def _preset_id_or_none(value: object) -> int | None:
    """Normalise ``presetOrExperienceId``. ``"0"`` means *no preset*, not preset zero."""
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number or None


def outlet_limits_from_settings(payload: Any) -> dict[int, OutletLimits]:
    """Read per-outlet limits from a ``gcsadvancestate`` response.

    The same data the valve announces over MQTT as ``READ_GCS_OUTLET_CONFIG_CFG``, but
    **readable on demand** — which MQTT is not, since it arrives unprompted roughly twice a
    session. Verified live 2026-08-17; see ``docs/gcs/api.md`` §1c.

    Two traps, both of which yield plausible-looking wrong numbers rather than an error:

    * **The key spelling differs from MQTT.** REST says ``maximumRuntime`` /
      ``maximumFlowrate`` / ``minimumFlowrate`` / ``defaultFlowrate``; MQTT capitalises the
      ``T`` and ``R``. Reading with the MQTT spelling silently finds nothing at all.
    * **REST returns DISPLAY units where MQTT returns WIRE units.** Flow arrives as ``50``
      where MQTT says ``200``. Everything is converted to the byte scale here, so the two
      sources are interchangeable — ``OutletLimits`` means byte scale whatever filled it.

    Outlets that cannot be parsed are skipped rather than guessed at.
    """
    limits: dict[int, OutletLimits] = {}
    # Accepts either the whole `gcsadvancestate` response or the `setting` block on its own,
    # because `KohlerClient.async_get_gcs_settings` already unwraps it while a raw capture
    # does not. Cheaper than making every caller remember which one it is holding.
    source = payload if isinstance(payload, dict) else {}
    if "valveSettings" not in source:
        source = (
            source.get("setting") if isinstance(source.get("setting"), dict) else {}
        )
    if not isinstance(source, dict):
        return limits
    settings = source.get("valveSettings")
    for valve in settings if isinstance(settings, list) else ():
        if not isinstance(valve, dict):
            continue
        configurations = valve.get("outletConfigurations")
        for entry in configurations if isinstance(configurations, list) else ():
            if not isinstance(entry, dict):
                continue
            try:
                outlet_id = int(str(entry.get("outLetId")))
            except (TypeError, ValueError):
                continue

            def _flow(key: str, item: dict = entry) -> int | None:
                """Display flow -> byte scale, the units OutletLimits is defined in."""
                try:
                    return round(float(str(item.get(key))) * 4)
                except (TypeError, ValueError):
                    return None

            try:
                run_time: int | None = int(str(entry.get("maximumRuntime")))
            except (TypeError, ValueError):
                run_time = None
            low, high = _flow("minimumFlowrate"), _flow("maximumFlowrate")
            if low is None or high is None:
                continue
            try:
                outlet_type: int | None = int(str(entry.get("outLetType")))
            except (TypeError, ValueError):
                outlet_type = None
            try:
                # REST reports this in display °C (`45`, `47.8`); stored in tenths.
                max_temperature: int | None = round(
                    float(str(entry.get("maximumOutletTemperature"))) * 10
                )
            except (TypeError, ValueError):
                max_temperature = None

            def _tenths(key: str, item: dict = entry) -> int | None:
                """Display °C -> tenths, the scale this model stores temperatures in."""
                try:
                    return round(float(str(item.get(key))) * 10)
                except (TypeError, ValueError):
                    return None

            try:
                outlet_flags: int | None = int(str(entry.get("outLetFlags")))
            except (TypeError, ValueError):
                outlet_flags = None
            limits[outlet_id] = OutletLimits(
                outlet_id,
                low,
                high,
                run_time,
                _flow("defaultFlowrate"),
                outlet_type,
                max_temperature,
                _tenths("minimumOutletTemperature"),
                _tenths("defaultOutletTemperature"),
                outlet_flags,
            )
    return limits


@dataclass(frozen=True)
class OutletLimits:
    """Per-outlet flow bounds the valve reports, on the **byte** scale (0x10-0xC8).

    Confirmed against the Konnect decompile: the app bounds its flow slider with
    ``outletConfigurations[].getMinimumFlowrate()`` / ``getMaximumFlowrate()`` taken from
    this same configuration, so these are the real limits rather than a convention. They are
    **not** guaranteed to be 16/200 on every system — this install reports identical figures
    for all six outlets only because flow control is disabled system-wide.
    """

    outlet_id: int
    minimum_flow_byte: int
    maximum_flow_byte: int
    # Seconds before the valve closes the water on its own — `maximumRunTime`. Reported
    # per outlet, but **timed per zone**: the clock starts when the zone begins flowing
    # and outlet changes within it do not reset it (see `runtime_cutoff.py`, where getting
    # that wrong is documented in detail). 900 s on the reference install today, 3600 s
    # before it was reconfigured — every outlet there has always agreed.
    #
    # None when this outlet's value has not been learned yet. Never assume one.
    maximum_run_time: int | None = None
    # The outlet's configured starting flow, byte scale, from `defaultFlowRate`.
    #
    # **Nothing reads this yet.** Captured deliberately on 2026-08-17 for a later question:
    # flow control is disabled system-wide on this install, and if a Kohler firmware update
    # ever fixes the controller's flow handling, the per-outlet default and maximum are what
    # a flow entity would have to be bounded by. Cheap to record now, impossible to
    # reconstruct retroactively.
    default_flow_byte: int | None = None
    # The valve's own outlet **type code** — 62, 52, 1, 11, 39, 21 and so on, standing for
    # handshower, rainshower, tub filler and the rest.
    #
    # **A label on this device, not a behaviour.** The controller derives an outlet's flow
    # envelope from its type; the valve does not, and honours whatever flow byte it is
    # given within its calibrated range. So this changes nothing about how the valve runs —
    # it is recorded because it is the only per-outlet identity the hardware reports, and
    # `docs/architecture.md` documents the two devices holding *different* codes for the
    # same physical fixture on purpose (id 4 is 39 to the valve, 38 to the controller).
    #
    # Deliberately **not** translated to a name here. Only three codes are documented
    # (1=handshower, 11=showerhead, 21=tub filler) and `docs/hub/cloud_api.md` says the
    # others are install-specific, so a lookup table would be inventing names for codes
    # nobody has confirmed. The raw code is published instead, and a map can follow once
    # real installs have been compared against what the Konnect app shows.
    #
    # None when the valve has not announced this outlet yet — the same "not learned"
    # meaning the run time carries, never a real type.
    outlet_type: int | None = None
    # The scald limit — `maximumOutletTemperature`, in **tenths of °C** here, matching the
    # wire scale the valve's own words use. REST reports it in display °C (`45`) and MQTT in
    # tenths (`450`); both are normalised to tenths on the way in, so this field means one
    # thing whichever source filled it.
    #
    # **A setting, not a hardware limit** — changed from the app or the panel and observed
    # moving 450 -> 477 within minutes on 2026-09-10. Never cache it as a device property.
    #
    # 🚨 **This is a safety setting.** `docs/gcs/api.md` warns that a whole-record write
    # which omits it, or sends it on the wrong scale, silently changes it. Nothing here
    # writes it — this integration only reads it — but that is why it is recorded exactly
    # as read rather than rounded.
    #
    # None when the valve has not reported it, which is not the same as no limit.
    maximum_temperature_tenths: int | None = None
    # The remaining three fields of `writeoutletconfig`'s eleven, captured 2026-09-11 so a
    # write can replace the record without inventing them.
    #
    # 🚨 **`writeoutletconfig` is a whole-record replace.** Omitting a key, or sending a
    # guess, changes the setting it names — and one of these sits beside the scald limit.
    # Reading them is the only way a write can put back what it did not mean to change; see
    # `docs/gcs/api.md` §1c.
    #
    # Tenths of °C, normalised from REST's display °C exactly as the maximum above.
    minimum_temperature_tenths: int | None = None
    # The temperature a shower starts at when nothing else specifies one — the Konnect app's
    # "Default Temperature", bounded below by the minimum and above by the scald limit.
    default_temperature_tenths: int | None = None
    # `outLetFlags`. Meaning undocumented and deliberately not interpreted: it is read so a
    # write can echo it back unchanged, which is the only thing this integration needs from
    # it. `1` on every outlet of every install seen so far.
    outlet_flags: int | None = None


@dataclass(frozen=True)
class GcsPreset:
    """One stored preset slot.

    ``name`` is empty for a free slot — that is exactly how a deletion is reported, so an
    empty name means "this slot holds nothing", not "unnamed preset".
    """

    preset_id: int
    name: str
    is_experience: bool = False

    @property
    def is_empty(self) -> bool:
        """True for a free slot: no name, or no valve data."""
        return not self.name.strip()

    @property
    def is_selectable(self) -> bool:
        """Whether this can be offered to a user as a scene to run.

        Experiences are excluded: they carry no valve data and cannot be started the same
        way, despite sharing the id space via ``presetOrExperienceId``.
        """
        return not self.is_empty and not self.is_experience


#: Litres to US gallons. `gcs-usage` reports `volume` in litres regardless of the account's
#: unit setting; the Konnect app multiplies by this exact constant when `waterUnits` is
#: `Standard`. Verified from the app's own bytecode, so it matches what the chart shows to
#: the last digit rather than being a rounder conversion of our own choosing.
GALLONS_PER_LITRE = 0.264172


def usage_volume_gallons(litres: float) -> float:
    """One `gcs-usage` volume in US gallons."""
    return litres * GALLONS_PER_LITRE


def usage_series(payload: dict[str, object]) -> list[dict[str, object]]:
    """The per-period entries from a `gcs-usage` response, oldest first.

    Empty for a malformed or failed read, so a caller can treat "no data" and "the call did
    not work" the same way — which is right for a diagnostic sensor.

    **Only `volume`, `onDuration` and `timestamp` are trustworthy.** The app never reads
    `averageBlendTemperature` or `numberOfTimesValveSwitchedOn` — they have no call sites in
    its bytecode at all — and the observed temperature values (~78 against real setpoints of
    104.9 °F) do not correspond to any plausible unit, so nothing here surfaces them.
    """
    entries = payload.get("gcsUsageDataDetailsList")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


@dataclass
class GcsState:
    """Live state of one Anthem digital valve.

    Outlet, temperature, and flow all come from the valve command word, which is the
    authoritative source whenever a GCS device exists.
    """

    model: ValveModel
    temperature_unit: str = "Fahrenheit"

    valve1: ValveWord | None = None
    valve2: ValveWord | None = None
    warmup_mode: str | None = None
    warmup_in_progress: bool | None = None
    # Reported by the device but not exposed: the value changes erratically between
    # messages and does not behave like a monotonic counter, so any statistics built on it
    # would be meaningless.
    total_volume: str | None = None
    # 🚫 **`totalFlow`, and it is not a water meter.** Recorded raw for diagnostics only; no
    # entity publishes it, and `Total Water Used` was retired in 0.14.0 because it did.
    #
    # Across the whole reference corpus — ten captures over two days, both valves — this field
    # took **three distinct values** (2056.0, 6283.25, 8224.0 on one valve) and cycled among
    # them with **no water running and every outlet closed**. Worse, the values come in pairs
    # exactly 4x apart: 2056/8224, 6488.75/25955, 3659.25/14637 — so the cloud serves one
    # underlying number at two scales and which one arrives varies per read.
    #
    # ⚠️ **That 4x is what produced the 0.7.3 divide-by-four bug.** Two captures a day apart
    # showed a clean 4.0 ratio and it was read as a unit conversion. It was two members of a
    # repeating set, and a divisor was right half the time and wrong the other half.
    #
    # Water figures come from `gcs-usage` instead — Kohler's own per-month series, in litres,
    # the same data the app charts. See `ValveMonthlyWaterSensor`, `ValveYearlyWaterSensor`.
    total_flow: float | None = None
    # `currentSystemState` — `normalOperation` or `showerInProgress`, as the valve itself
    # reports it. Kept as the device's own string rather than folded into the four-state
    # `Status` vocabulary, because it is a second, independent opinion: it is the valve's
    # own session flag, not a decode of the command word, and the two can disagree.
    system_state: str | None = None
    last_update: float | None = None

    # Stored presets, keyed by slot id. Ids are **slots, not positions**: creating fills the
    # lowest free slot and deleting empties one in place, so a deleted preset arrives as a
    # record with an empty name. Kept as a dict rather than a list so an empty slot can
    # overwrite its predecessor without disturbing the others.
    presets: dict[int, GcsPreset] = field(default_factory=dict)
    # Which preset is currently driving the valve, from `presetOrExperienceId`.
    #
    # Latches for the whole session and survives temperature and flow changes, but is
    # cleared by **both** pause and stop — so it answers "is a preset driving this", never
    # "is water running". Opening an outlet directly leaves it None with water flowing.
    #
    # **None does not mean "no preset" while warm-up is running.** A preset activated during
    # warm-up applies its valve word but never sets the field, and does not latch
    # retroactively when warm-up ends. All 12 `warmUpInProgress` samples in the corpus carry
    # `0`. Treat None + warm-up as *unknown*, not as absence — see `docs/gcs/api.md`.
    active_preset_id: int | None = None
    # Per-outlet flow bounds, keyed by the device's own 0-based `outLetId`. Arrives over
    # MQTT one outlet per message, unprompted, so this fills in gradually and may stay
    # partial — every reader must tolerate a missing entry.
    outlet_limits: dict[int, OutletLimits] = field(default_factory=dict)

    def _flags(self, *, flowing: bool) -> list[bool]:
        words = {1: self.valve1, 2: self.valve2}
        result: list[bool] = []
        for outlet in range(1, self.model.total_outlets + 1):
            valve_number, bit = self.model.outlet_location(outlet)
            word = words.get(valve_number)
            assigned = bool(word and word.outlet(bit))
            if flowing and word is not None and word.paused:
                assigned = False
            result.append(assigned)
        return result

    def zone_outlets(self, zone: int, *, flowing: bool = True) -> list[bool]:
        """Outlet flags for one zone, indexed from 0 within that zone.

        The zone-native view, matching how the hardware and every API surface address
        outlets. ``flowing=False`` returns the assignment a paused session will resume to.
        """
        word = self.valve1 if zone == 1 else self.valve2
        count = self.model.outlets_in_zone(zone)
        if word is None:
            return [False] * count
        if flowing and word.paused:
            return [False] * count
        return [word.outlet(bit) for bit in range(count)]

    def zone_word(self, zone: int) -> ValveWord | None:
        """The decoded command word for one zone."""
        return self.valve1 if zone == 1 else self.valve2

    @property
    def outlets(self) -> list[bool]:
        """Per-outlet flags for outlets actually **flowing water**.

        A paused valve keeps its outlet assignment in byte 3 (``0x41`` is "paused, outlet 1
        still assigned"), but no water comes out. Anything answering "is this outlet on"
        therefore has to clear the assignment while paused, or a paused shower reads as
        running.
        """
        return self._flags(flowing=True)

    @property
    def assigned_outlets(self) -> list[bool]:
        """Per-outlet flags as stored in byte 3, ignoring pause.

        This is what the session will resume to. Use :attr:`outlets` for "is water coming
        out of this outlet".
        """
        return self._flags(flowing=False)

    @property
    def is_running(self) -> bool:
        """True when water is actually flowing from any outlet."""
        return any(self.outlets)

    @property
    def is_paused(self) -> bool:
        """True when the session is held.

        The pause bit is independent of the outlet bits, so this only checks the flag —
        with the guard that a system with water flowing somewhere is running, not paused.
        A genuinely paused system does not have one valve held while another flows.
        """
        words = [w for w in (self.valve1, self.valve2) if w is not None]
        if not words:
            return False
        return any(w.paused for w in words) and not self.is_running

    @property
    def at_temperature(self) -> bool | None:
        """Whether the system has reached its temperature setpoint.

        **System-level, and read from the primary valve only.** The secondary valve never
        asserts this bit — 0 of 133 in a session where it was the *only* zone running and
        the water demonstrably came up to temperature. The primary word carries the
        judgement for the whole system.

        That is consistent with Kohler's own expectation that zone 1 / outlet 1 is the main
        shower: system-level status lives on ``primaryValve1`` regardless of which zone the
        plumbing actually feeds. An install that puts the main shower on zone 2 still gets
        its at-temperature signal here.

        Verified against the hardware: the bit set at the exact moment the touchscreen
        stopped flashing and showed a solid setpoint, and cleared when the shower stopped.
        It may lag a second or two after a setpoint change, matching the brief re-flash the
        screen shows.
        """
        return None if self.valve1 is None else self.valve1.at_temperature

    @property
    def at_flow(self) -> bool | None:
        """Whether the system has reached its flow setpoint.

        Read from the primary valve, matching :attr:`at_temperature`.

        Never observed set on the test system — but that system has **flow control disabled**
        at the fixture, worked around because it is reportedly broken on HUB firmware 2.88.
        The likely reading is therefore "flow control is off, so nothing reports flow",
        not "the firmware never drives this bit". The measured-flow byte is also flat zero
        there, which is consistent.

        Untested either way: confirming it needs a system with flow control enabled.
        """
        return None if self.valve1 is None else self.valve1.at_flow

    @property
    def has_fault(self) -> bool | None:
        """Whether any valve is reporting a fault (byte 3's errorFlag).

        Surfaced by `binary_sensor.ValveProblemSensor`. The bit has never been observed
        set — 0 of 992 captured valve words — and that caveat travels with the entity
        rather than being a reason to withhold it: a fault nobody can see is the one case
        where a hidden entity is worse than an untested one. See that class for the full
        reasoning, including why this was withheld previously.

        See also :attr:`error_codes` — byte 7 reads a constant ``1`` on the tested unit, so
        a nonzero code is not a fault. This flag is the only fault signal.
        """
        words = [w for w in (self.valve1, self.valve2) if w is not None]
        if not words:
            return None
        return any(w.error_flag for w in words)

    @property
    def error_codes(self) -> dict[str, int]:
        """Per-zone error code from the status word's byte 7."""
        codes = {}
        for number, word in ((1, self.valve1), (2, self.valve2)):
            if word is not None and word.error_code is not None:
                codes[f"zone{number}"] = word.error_code
        return codes

    # ---------------------------------------------------------------- #
    # The measurement block: decoded, but not surfaced
    # ---------------------------------------------------------------- #
    # `ValveWord.measured_temperature_celsius` and `.measured_flow_percent` are decoded from
    # bytes 5-6 and published as attributes on the Hex sensor and in diagnostics. Four
    # `GcsState` properties used to wrap them — `_measuring_word`, `reports_measurements`,
    # `measured_temperature`, `measured_flow_percent` — for a binary sensor that was removed
    # in 0.6.x; nothing has called them since, so they went with 0.8.1.
    #
    # **The knowledge in them is worth keeping.** A valve that does not populate the block
    # reports the whole thing as zero, temperature AND flow together, and any future reader
    # must gate on both: the encoding represents sub-25.6 C fine (4.0 C is `0028C8xx`), so an
    # ice-shower session can legitimately report a very low temperature while water flows.
    # Gating on temperature alone would discard that case, and gating on flow alone cannot
    # distinguish "closed" from "not reported". Without the pair, a unit that never populates
    # the block shows a confident 32 F on a dashboard — worse than showing nothing.

    @property
    def temperature(self) -> float | None:
        """Setpoint in the account's unit, taken from valve1."""
        if self.valve1 is None:
            return None
        return celsius_to_unit(self.valve1.temperature_celsius, self.temperature_unit)

    @property
    def flow_percent(self) -> float | None:
        """Flow as a percentage, taken from valve1."""
        return None if self.valve1 is None else self.valve1.flow_percent

    @property
    def flow_is_live(self) -> bool:
        """Whether `flow_percent` is the flow somebody actually asked for.

        **The idle byte is not the flow setting**, and it is not enough to check that it
        holds still. Two installs, two different-looking failures, same conclusion:

        * Capture corpus: 296 idle words carry a flow nobody selected, in recurring pairs
          like 34.5 %/82.5 %, collapsing and returning within seconds. Obviously junk.
        * Controller-free K-28210 pair (2026-09-10): idle bytes of 24.5 % and 26.5 %,
          byte-identical across reports 2 h 23 m apart — and the owner's panel held
          **100 %** on both valves throughout. Stable junk, which is the more dangerous
          kind: it looks like a setting.

        The second was briefly read as evidence that a stable idle byte *is* the stored
        setting, and 0.6.2 relaxed this flag on that basis. The panel reading disproved it.
        Stability is not meaning. Anything presenting "the flow setting" must gate on this,
        or it will show a number nobody chose — and a number that does not move is more
        convincing, not less.

        See `docs/architecture.md` for both observations.
        """
        word = self.valve1
        if word is None:
            return False
        return bool(word.outlet_mask) and not word.paused

    def _accept_total_flow_raw(self, value: object) -> bool:
        """Record `totalFlow` verbatim. True if it changed.

        No filtering, because there is nothing coherent to filter — see the field's own note.
        It is kept solely so a diagnostics report still carries what the cloud sent, which is
        the evidence for the open question about what this field actually is.
        """
        try:
            reading = float(str(value))
        except (TypeError, ValueError):
            return False
        changed = reading != self.total_flow
        self.total_flow = reading
        return changed

    def apply_envelope(self, envelope: Envelope) -> bool:
        """Apply a GCS message. Returns True if Home Assistant should re-render.

        **Every message from the valve advances `last_update`** — whatever its code, whether
        or not this class knows how to decode it, and whether or not it carried anything new.
        That is what the sensor means: the last time the device was heard from, which is a
        liveness signal, not a change feed.

        It used to be set inside three of the four decode handlers, so it only moved for
        `GCS_SOLO_STS`, `GCS_WARM_STS` and `GCS_PRESET_STS`. Everything else the valve emits —
        `READ_GCS_OUTLET_CONFIG_CFG`, `READ_GCS_EXPERIENCE_STS`, `GCS_RECIEVED_STS`,
        `DEVICE_REBOOT_STS`, `READ_GCS_UI_CFG`, the firmware report — left the timestamp
        stale, so a valve that was plainly talking could look silent for hours.
        """
        if envelope.sku != SKU_GCS:
            return False
        # Before dispatch: an undecodable message is still proof of life.
        self.last_update = envelope.received_at
        handler = {
            MSG_GCS_SOLO_STATUS: self._apply_solo,
            MSG_GCS_WARMUP_STATUS: self._apply_warmup,
            MSG_GCS_PRESET_STATUS: self._apply_preset,
            MSG_GCS_OUTLET_CONFIG: self._apply_outlet_config,
        }.get(envelope.code)
        if handler is not None:
            handler(envelope)
        # Always True: `last_update` moved, so the sensor has something new to show even when
        # nothing else did. The handlers' own change flags are subsumed by this.
        return True

    def _apply_outlet_config(self, envelope: Envelope) -> bool:
        """Record the per-outlet flow bounds the valve reports.

        One outlet per message and never on request, so this is opportunistic: it sharpens
        the flow entity's range once the device happens to announce, and the constants stand
        in until then.
        """
        changed = False
        for attribute in envelope.attributes:
            if not isinstance(attribute, dict):
                continue
            try:
                outlet_id = int(str(attribute.get("outLetId")))
                low = int(str(attribute.get("minimumFlowRate")))
                high = int(str(attribute.get("maximumFlowRate")))
            except (TypeError, ValueError):
                continue
            try:
                run_time: int | None = int(str(attribute.get("maximumRunTime")))
            except (TypeError, ValueError):
                run_time = None
            try:
                default_flow: int | None = int(str(attribute.get("defaultFlowRate")))
            except (TypeError, ValueError):
                default_flow = None
            # `outLetType` is spelled identically on both surfaces — it is one of the three
            # keys the read/write spelling trap does *not* touch (docs/gcs/api.md).
            try:
                outlet_type: int | None = int(str(attribute.get("outLetType")))
            except (TypeError, ValueError):
                outlet_type = None
            try:
                # MQTT reports this in **tenths** already (`450`), unlike REST's display °C
                # — the same wire/display split as flow. Stored as read.
                max_temperature: int | None = int(
                    str(attribute.get("maximumOutletTemperature"))
                )
            except (TypeError, ValueError):
                max_temperature = None

            # 🚨 **The other three fields of the write record must be carried too.**
            # This builds a whole `OutletLimits` and *replaces* the stored one, so a field
            # omitted here is not merely absent from this message — it erases what the REST
            # seed read. That is what happened in 0.18.0-0.18.2: a successful write makes the
            # valve announce, the announcement landed without these three, and the *next*
            # write refused because they were suddenly unknown. One write, then never again
            # until a reload. Reported 2026-09-11.
            #
            # MQTT reports all three with the same key spellings as the write body, and
            # temperatures already in tenths — no conversion, unlike REST's display °C.
            def _int(key: str, item: dict = attribute) -> int | None:
                try:
                    return int(str(item.get(key)))
                except (TypeError, ValueError):
                    return None

            limits = OutletLimits(
                outlet_id,
                low,
                high,
                run_time,
                default_flow,
                outlet_type,
                max_temperature,
                _int("minimumOutletTemperature"),
                _int("defaultOutletTemperature"),
                _int("outLetFlags"),
            )
            # **Merge, do not replace.** A field this message did not carry must keep the
            # value already learned: `None` means "not learned", and a write refuses on it,
            # so blanking one here turns a thin announcement into the same
            # one-write-then-never-again failure the three missing fields caused. Every
            # capture carries all ten, and the parser must not depend on that.
            known = self.outlet_limits.get(outlet_id)
            if known is not None:
                limits = replace(
                    limits,
                    **{
                        field: getattr(known, field)
                        for field in (
                            "maximum_run_time",
                            "default_flow_byte",
                            "outlet_type",
                            "maximum_temperature_tenths",
                            "minimum_temperature_tenths",
                            "default_temperature_tenths",
                            "outlet_flags",
                        )
                        if getattr(limits, field) is None
                    },
                )
            if known != limits:
                self.outlet_limits[outlet_id] = limits
                changed = True
        return changed

    def zone_flow_limits(self, zone: int) -> tuple[int, int]:
        """Flow bounds for a zone as **byte** values, falling back to the constants.

        Taken from that zone's **first** outlet, matching what the app does — it bounds the
        slider with `outletConfigurations[0]` of each valve rather than combining outlets.
        """
        limits = self.outlet_limits.get(self.model.outlet_id(zone, 1))
        if limits is None:
            return FLOW_BYTE_MIN, FLOW_BYTE_MAX
        return limits.minimum_flow_byte, limits.maximum_flow_byte

    def _apply_preset(self, envelope: Envelope) -> bool:
        """Apply a pushed preset record.

        The device pushes one of these on every create, edit, rename, and delete, and all
        ten slots after a reboot — so nothing has to poll for preset changes. A delete
        arrives as the same ``presetId`` with an empty name.
        """
        changed = False
        for attribute in envelope.attributes:
            if not isinstance(attribute, dict):
                continue
            raw_id = attribute.get("presetId")
            try:
                preset_id = int(str(raw_id))
            except (TypeError, ValueError):
                continue
            preset = GcsPreset(
                preset_id=preset_id,
                name=str(attribute.get("name") or "").strip(),
                # The push carries no experience flag; the REST list does. Preserve what a
                # previous seed established rather than silently demoting one to a preset.
                is_experience=(
                    self.presets[preset_id].is_experience
                    if preset_id in self.presets
                    else False
                ),
            )
            if self.presets.get(preset_id) != preset:
                self.presets[preset_id] = preset
                changed = True
        return changed

    def _apply_solo(self, envelope: Envelope) -> bool:
        attribute = envelope.attribute(MSG_GCS_SOLO_STATUS) or (
            envelope.attributes[0] if envelope.attributes else None
        )
        if attribute is None:
            return False
        try:
            valve1 = decode_word(str(attribute.get("primaryValve1") or ""))
        except ValveHexError:
            _LOGGER.debug("Undecodable primaryValve1 in %s", envelope.code)
            return False
        valve2 = None
        if self.model.uses_valve2:
            try:
                valve2 = decode_word(str(attribute.get("secondaryValve1") or ""))
            except ValveHexError:
                valve2 = self.valve2

        changed = (valve1, valve2) != (self.valve1, self.valve2)
        self.valve1, self.valve2 = valve1, valve2
        if (volume := attribute.get("totalVolume")) is not None:
            changed |= volume != self.total_volume
            self.total_volume = volume
        if (flow_total := attribute.get("totalFlow")) is not None:
            changed |= self._accept_total_flow_raw(flow_total)
        if (system := attribute.get("currentSystemState")) is not None:
            system = str(system)
            changed |= system != self.system_state
            self.system_state = system
        if (status := attribute.get("warmUpStatus")) is not None:
            in_progress = _is_warmup_in_progress(status)
            changed |= in_progress != self.warmup_in_progress
            self.warmup_in_progress = in_progress
        if "presetOrExperienceId" in attribute:
            active = _preset_id_or_none(attribute.get("presetOrExperienceId"))
            changed |= active != self.active_preset_id
            self.active_preset_id = active
        return changed

    def _apply_warmup(self, envelope: Envelope) -> bool:
        attribute = envelope.attributes[0] if envelope.attributes else None
        if attribute is None:
            return False
        # The MQTT message spells this key **all lowercase** (`warmup`), where the REST
        # `warmUpState` object spells it `warmUp`. Confirmed against 9 captured
        # GCS_WARM_STS messages, whose only keys are `code` and `warmup`. Matching just the
        # REST spelling made this handler a no-op and left the mode to the next poll.
        mode = (
            attribute.get("warmup")
            or attribute.get("warmUp")
            or attribute.get("warmUpMode")
        )
        changed = mode is not None and mode != self.warmup_mode
        if mode is not None:
            self.warmup_mode = str(mode)
        return changed

    def apply_rest_state(self, payload: dict[str, Any]) -> None:
        """Seed from a ``gcs-state`` read, so entities are populated before any event.

        Every container is type-checked before it is walked. ``or {}`` only rescues the
        null case: where the cloud sends a list, a string or a number for ``state`` — a
        schema change, a truncated response, an error body shaped like a success — the
        ``or`` passes it straight through and ``.get`` raises `AttributeError` inside the
        REST seed, which fails setup with a traceback rather than a message. Falling back
        to the pre-seed defaults is the honest response: MQTT is authoritative here and
        will correct anything this seed misses.
        """
        state = payload.get("state") if isinstance(payload, dict) else None
        if not isinstance(state, dict):
            return
        for number, attr in ((1, "valve1"), (2, "valve2")):
            valve = state.get(attr)
            if not isinstance(valve, dict):
                continue
            mask = 0
            for bit, key in enumerate(("out1", "out2", "out3")):
                if str(valve.get(key)) == "1":
                    mask |= 1 << bit
            setpoint = valve.get("temperatureSetpoint")
            flow = valve.get("flowSetpoint")
            try:
                celsius = float(setpoint) if setpoint is not None else 38.0
            except (TypeError, ValueError):
                celsius = 38.0
            try:
                # gcs-state reports flow on the device's own 0-50 scale.
                percent = float(flow) * 2 if flow is not None else 0.0
            except (TypeError, ValueError):
                percent = 0.0
            word = ValveWord(
                prefix=1 if number == 1 else 0x11,
                temperature_celsius=celsius,
                flow_percent=percent,
                outlet_mask=mask,
                paused=str(valve.get("pauseFlag")) == "1",
            )
            if number == 1:
                self.valve1 = word
            elif self.model.uses_valve2:
                self.valve2 = word

        # The one container this method's docstring promises to type-check and did not:
        # `or {}` rescues a null `warmUpState` but hands a list or a string straight
        # through, and the next `.get` raises `AttributeError` inside the REST seed —
        # aborting setup with a traceback, which is the exact failure the guards above exist
        # to prevent. Caught 2026-09-11 by review.
        warm = state.get("warmUpState")
        if not isinstance(warm, dict):
            warm = {}
        self.warmup_mode = warm.get("warmUp") or self.warmup_mode
        progress = warm.get("state")
        if progress is not None:
            self.warmup_in_progress = _is_warmup_in_progress(progress)
        self.total_volume = state.get("totalVolume") or self.total_volume
        if (flow_total := state.get("totalFlow")) is not None:
            self._accept_total_flow_raw(flow_total)
        if (system := state.get("currentSystemState")) is not None:
            self.system_state = str(system)
        if "presetOrExperienceId" in state:
            self.active_preset_id = _preset_id_or_none(
                state.get("presetOrExperienceId")
            )
        self.last_update = time.time()

    def apply_preset_list(self, payload: dict[str, Any]) -> bool:
        """Seed every preset slot from a ``gcs-preset`` read. Returns True if changed.

        Replaces the whole mapping rather than merging, so a preset deleted while Home
        Assistant was not listening disappears instead of lingering.
        """
        details = (
            payload.get("gcsPresetExperienceDetails")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(details, list):
            return False
        presets: dict[int, GcsPreset] = {}
        for entry in details:
            if not isinstance(entry, dict):
                continue
            try:
                preset_id = int(str(entry.get("presetId")))
            except (TypeError, ValueError):
                continue
            presets[preset_id] = GcsPreset(
                preset_id=preset_id,
                name=str(entry.get("title") or entry.get("logicalName") or "").strip(),
                # Kohler sends this as the string "True"/"False", so `bool(value)` would be
                # true for both.
                is_experience=str(entry.get("isExperience")).strip().lower() == "true",
            )
        if presets == self.presets:
            return False
        self.presets = presets
        return True

    def selectable_presets(self, hidden: Container[int] = ()) -> list[GcsPreset]:
        """Presets a user may choose, lowest slot first.

        Empty slots and experiences are dropped, plus anything in ``hidden`` — preset 1 is
        the valve's mandatory default-shower configuration and the Konnect app does not list
        it either.
        """
        return [
            preset
            for _, preset in sorted(self.presets.items())
            if preset.is_selectable and preset.preset_id not in hidden
        ]

    def preset_by_name(
        self, name: str, hidden: Container[int] = ()
    ) -> GcsPreset | None:
        """Resolve a preset by name, case-insensitively.

        Names are resolved **at call time** rather than a remembered id, because a deleted
        slot is reused: a cached id stays valid while pointing at a different scene.

        Takes the same ``hidden`` set as :meth:`selectable_presets` deliberately — a name
        that cannot be offered must not be resolvable either, or a caller can reach a preset
        the UI hides.
        """
        wanted = name.strip().lower()
        for preset in self.selectable_presets(hidden=hidden):
            if preset.name.strip().lower() == wanted:
                return preset
        return None


@dataclass
class HubZone:
    """One HUB water zone, which maps to one valve."""

    status: str | None = None
    outlets: list[bool] = field(default_factory=list)
    temperature: Any = None
    flowrate: Any = None


@dataclass
class HubState:
    """Live state of one Anthem Plus system controller.

    Note the HUB's view of a valve-driven session is unreliable: measured across 95 such
    episodes, 51 were reported immediately, 12 late, and 32 never — with preset-driven
    openings never reported at all (0 of 15). On an account that also has a GCS device,
    read outlets from the valve instead; see :func:`~.models.resolve_outlet_source`.
    """

    model: ValveModel

    zones: dict[int, HubZone] = field(default_factory=dict)
    music_on: bool | None = None
    steam_on: bool | None = None
    light_on: bool | None = None
    active_favorite_id: str | None = None
    # The running favorite's name, as `FAVORITE_STS` reports it. Kept beside the id because
    # the message carries both, and the name is usable before the favorites list has been
    # seeded — see `_apply_favorite`.
    active_favorite_name: str | None = None
    favorites: list[dict[str, Any]] = field(default_factory=list)
    last_update: float | None = None
    # Whether the controller is running a warm-up cycle. Carried on `SHOWER_VALVE_STS` at the
    # `data` level rather than inside `attributes`, so it is per-message, not per-zone.
    #
    # Observed 9 times in 260 captured `SHOWER_VALVE_STS` messages, and **every one of those
    # nine also had both zones ON** — warm-up runs water, exactly as on the valve. That is
    # why anything presenting this must rank it above "running": reporting a warm-up as an
    # ordinary shower hides why the water started with nobody there.
    #
    # None until a message or a read says otherwise; absent from every other HUB message.
    shower_warmup: bool | None = None

    @property
    def outlets(self) -> list[bool]:
        """Per-outlet flags across both zones, in global numbering."""
        flags = list(self.zones.get(1, HubZone()).outlets)
        flags += [False] * max(0, self.model.outlets_valve1 - len(flags))
        if self.model.uses_valve2:
            second = list(self.zones.get(2, HubZone()).outlets)
            second += [False] * max(0, self.model.outlets_valve2 - len(second))
            flags += second
        return flags[: self.model.total_outlets]

    @property
    def is_running(self) -> bool:
        return any(z.status == "ON" for z in self.zones.values())

    def apply_envelope(self, envelope: Envelope) -> bool:
        """Apply a HUB message. True for every HUB message, False for anything else.

        Same contract as :meth:`GcsState.apply_envelope`: every message from the
        controller advances ``last_update`` whether or not this class decodes it, because
        that timestamp means "last heard from", not "last changed" — so there is always
        something new to render and the handlers' own change flags are subsumed.
        """
        if envelope.sku != SKU_HUB:
            return False
        # Before dispatch, for the same reason as the valve: the controller emits plenty this
        # class does not decode — `SYSTEM_STS`, `STATUS_SNAPSHOT`, `LUMIWAVE_STS` and the
        # four `*_EXP_SNAPSHOT` codes — and every one of them is proof it is alive.
        self.last_update = envelope.received_at
        handler = {
            MSG_HUB_SHOWER_VALVE: self._apply_valve,
            MSG_HUB_MUSIC: self._apply_music,
            MSG_HUB_STEAM: self._apply_steam,
            MSG_HUB_LIGHT: self._apply_light,
            MSG_HUB_FAVORITE: self._apply_favorite,
            MSG_HUB_FAVORITES_SNAPSHOT: self._apply_favorites_snapshot,
        }.get(envelope.code)
        if handler is not None:
            handler(envelope)
        return True

    def _apply_valve(self, envelope: Envelope) -> bool:
        changed = False
        # `showerwarmup` sits beside `attributes` under `data`, not within it. Note the
        # casing: MQTT sends `showerwarmup`, the REST read sends `showerWarmUp`.
        data = envelope.raw.get("data")
        warmup = _flag(data.get("showerwarmup") if isinstance(data, dict) else None)
        if warmup is not None and warmup != self.shower_warmup:
            self.shower_warmup = warmup
            changed = True
        for attribute in envelope.attributes:
            number = zone_number(attribute)
            if number is None:
                continue
            count = (
                self.model.outlets_valve1 if number == 1 else self.model.outlets_valve2
            )
            # Same guard as the REST path, and for the same two reasons. `outlet_flags`
            # indexes positionally, so a string `"110"` decomposes into truthy characters
            # and reads as **every outlet running** — wrong state with no error at all. And
            # an int raises `TypeError` on `len()`: not hypothetical, since
            # `docs/hub/cloud_api.md` records Kohler serving `"outlets": 2` as a *count*
            # elsewhere in the same API. The REST sibling had this guard; this one did not.
            outlets = attribute.get("outlets")
            zone = HubZone(
                status=attribute.get("status"),
                outlets=outlet_flags(
                    outlets if isinstance(outlets, list) else None, count
                ),
                temperature=attribute.get("temperature"),
                flowrate=attribute.get("flowrate"),
            )
            # **Merge, do not replace** — the same hazard `_apply_outlet_config` carried
            # until 0.18.3, in the other direction. A message that omits `temperature` or
            # `flowrate` would blank what an earlier one reported, and
            # `ControllerZoneTemperatureSensor` goes unavailable on a None. Every captured
            # `SHOWER_VALVE_STS` carries all four keys, but this stream is documented as
            # coalescing snapshots and skipping windows (`docs/hub/cloud_api.md` §5.1), so
            # depending on that shape is the assumption that just cost a release.
            #
            # `status` and `outlets` are always rebuilt: they are what the message is *for*,
            # and `outlet_flags` already returns a full list rather than None.
            known = self.zones.get(number)
            if known is not None:
                if zone.temperature is None:
                    zone = replace(zone, temperature=known.temperature)
                if zone.flowrate is None:
                    zone = replace(zone, flowrate=known.flowrate)
            if known != zone:
                self.zones[number] = zone
                changed = True
        return changed

    def _status_flag(
        self, envelope: Envelope, component: str | None = None
    ) -> bool | None:
        for attribute in envelope.attributes:
            if component and attribute.get("component") not in (component, None):
                continue
            status = str(attribute.get("status") or "").upper()
            if status in {"ON", "OFF"}:
                return status == "ON"
        return None

    def _apply_music(self, envelope: Envelope) -> bool:
        # Music telemetry is on/off only. Source, volume, and track are not reported on
        # either channel unless a favorite is driving it.
        value = self._status_flag(envelope, "amplifier")
        changed = value is not None and value != self.music_on
        if value is not None:
            self.music_on = value
        return changed

    def _apply_steam(self, envelope: Envelope) -> bool:
        value = self._status_flag(envelope)
        changed = value is not None and value != self.steam_on
        if value is not None:
            self.steam_on = value
        return changed

    def _apply_light(self, envelope: Envelope) -> bool:
        value = self._status_flag(envelope)
        changed = value is not None and value != self.light_on
        if value is not None:
            self.light_on = value
        return changed

    def _apply_favorite(self, envelope: Envelope) -> bool:
        """Track which favorite is running, from `FAVORITE_STS`.

        ⚠️ **This message carries `id` / `name` / `status` inside its attributes, and never
        `favoriteid`.** That key is real, but it belongs to the *accessory* messages —
        `MUSIC_STS`, `LIGHT_STS` and `STEAM_STS` each carry top-level `favoriteid` and
        `experienceid`. An earlier version of this method read `favoriteid` here, so it
        resolved to `None` on every message, `active_favorite_id` was permanently unset, and
        the controller's Favorite dropdown snapped back to `Off` the moment any other
        message arrived. Corrected 2026-08-21 against a live activation.

        ⚠️ **Nor are those a substitute — they answer a different question.** An accessory's
        `favoriteid` is attribution for *that component*: "the music playing right now was
        started by favorite 2". Whether favorite 2 is still running is not the same thing,
        because **a favorite is a composite and its components are optional** — it bundles
        `water`, `steam`, `music` and `light`, and carries only what the owner put in it and
        what the hub is wired to. So:

        * A favorite with no music never appears in `MUSIC_STS` at all. Four of this
          account's six favorites carry no music; watching `favoriteid` would report nothing
          running while the shower is on.
        * Attribution drops before the favorite does. Measured 2026-08-21, `MUSIC_STS` went
          to `favoriteid: "0"` at 07:23:59.150Z, **0.6 s before** `FAVORITE_STS` reported the
          favorite itself `OFF` at 07:23:59.766Z.

        `FAVORITE_STS` is the one message that speaks for the favorite. See
        `docs/hub/cloud_api.md` §5.5 for the component table and the three different ways an
        absent component is spelled.

        **`status` matters as much as `id`.** Start and stop carry the *same* id and differ
        only in `status`, so keying on the id alone would latch the dropdown on forever::

            {"id": "1", "name": "Hair Wash", "status": "ON"}    <- activated
            {"id": "1", "name": "Hair Wash", "status": "OFF"}   <- stopped, 96 s later

        A missing `status` is treated as ON, the same direction of error as `_name_of` and
        the `isExperience` filter in `select.py`: prefer showing a favorite over hiding one.

        The name travels with the message, which is why it is kept — it lets the dropdown
        show a running favorite before the favorites list has been seeded.
        """
        favorite_id: str | None = None
        name: str | None = None
        for attribute in envelope.attributes:
            if not isinstance(attribute, dict):
                continue
            # `favoriteid` accepted only as a fallback, for a firmware that might use the
            # accessory messages' spelling here. Live traffic uses `id`.
            identifier = attribute.get("id") or attribute.get("favoriteid")
            if identifier is None:
                continue
            if str(attribute.get("status") or "").strip().upper() == "OFF":
                # An explicit stop. Break rather than continue, so a trailing attribute
                # cannot resurrect the favorite the controller just turned off.
                favorite_id = name = None
                break
            favorite_id = str(identifier)
            name = str(attribute.get("name") or "").strip() or None
            break

        # "0" means nothing is driving the system.
        if str(favorite_id) in {"0", "None", ""}:
            favorite_id = name = None

        changed = (favorite_id, name) != (
            self.active_favorite_id,
            self.active_favorite_name,
        )
        self.active_favorite_id = favorite_id
        self.active_favorite_name = name
        return changed

    def _apply_favorites_snapshot(self, envelope: Envelope) -> bool:
        # Snapshots carry the whole list and arrive on connect, which is a second, free
        # answer to cold start alongside the REST seed.
        if not envelope.attributes:
            return False
        self.favorites = list(envelope.attributes)
        return True

    def apply_rest_state(self, payload: dict[str, Any]) -> None:
        """Seed from a ``hub-state`` read.

        Type-checked the whole way down, for the reason given on the valve's
        :meth:`GcsState.apply_rest_state`: ``or {}`` rescues null but not a wrong type, and
        an `AttributeError` raised inside the seed fails setup with a traceback. A
        malformed container is skipped and its fields keep their defaults.
        """
        if not isinstance(payload, dict):
            return
        state = payload.get("state")
        if not isinstance(state, dict):
            return
        shower = state.get("shower")
        for entry in shower if isinstance(shower, list) else ():
            # `zone_number` reads five different spellings off the entry, so it needs a
            # mapping; a bare string in the list would raise there rather than here.
            if not isinstance(entry, dict):
                continue
            number = zone_number(entry)
            if number is None:
                continue
            count = (
                self.model.outlets_valve1 if number == 1 else self.model.outlets_valve2
            )
            outlets = entry.get("outlets")
            self.zones[number] = HubZone(
                status=entry.get("status"),
                # `outlet_flags` indexes this positionally, so a string would decompose
                # into truthy characters and read as every outlet running.
                outlets=outlet_flags(
                    outlets if isinstance(outlets, list) else None, count
                ),
                temperature=entry.get("temperature"),
                flowrate=entry.get("flowRate"),
            )
        music_state = state.get("musicStateModel")
        music = music_state.get("status") if isinstance(music_state, dict) else None
        if music is not None:
            self.music_on = str(music).upper() == "ON"
        steam_state = state.get("hubSteamState")
        steam = steam_state.get("status") if isinstance(steam_state, dict) else None
        if steam is not None:
            self.steam_on = str(steam).upper() == "ON"
        lights = state.get("light")
        if isinstance(lights, list):
            self.light_on = any(
                str(light.get("status", "")).upper() == "ON"
                for light in lights
                if isinstance(light, dict)
            )
        # Top level, beside `state` rather than inside it — and camelCase here, against the
        # all-lowercase `showerwarmup` MQTT sends for the same thing.
        warmup = _flag(payload.get("showerWarmUp"))
        if warmup is not None:
            self.shower_warmup = warmup
        self.last_update = time.time()
