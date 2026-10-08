"""Encode and decode the Anthem GCS 4-byte valve command word.

Single source of truth for the valve word. This replaces three implementations that
previously had to agree by hand: a Jinja encoder in ``scripts.yaml``, a decoder in the
standalone ``mqtt_capture.py``, and a subtly wrong ``ValveMode`` reading in the
``kohler-anthem`` library.

Layout, identical for reads and writes::

     0 1 | 2 3 | 4 5 | 6 7
      01 | 84  | C8  | 07
      ^     ^     ^     ^
      |     |     |     └─ outlet mask
      |     |     └─────── flow
      |     └───────────── temperature
      └─────────────────── prefix

Only the first 8 of the field's 16 hex characters carry the command; the trailing 8 were
``00000001`` in every captured message.

Every constant here is validated against 315 ``GCS_SOLO_STS`` messages correlated with the
HUB's ``SHOWER_VALVE_STS``. See ``docs/gcs/valve_hex.md`` for the evidence, and its
"Superseded readings" section for two decompile-derived formulas that this contradicts.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .models import ValveModel

# Sent for a valve the model does not populate. The firmware ignores it: prefix 0x00
# addresses no valve. It must still be present in the payload.
UNUSED_VALVE_WORD = "00000000"

# Byte 0 layout (resolved from the app decompile, `p315jj/h.java`, and verified against 363
# captured messages). READ and WRITE differ:
#
#     status:  [valve index : 4][atFlow : 1][atTemp : 1][temperature high : 2]
#     write:   [valve index : 4][   0       0        ][temperature high : 2]
#
# `atFlow` (0x08) and `atTemp` (0x04) are read-only status the device asserts; the client
# always writes them as zero, which is why they never appear in a command word. They land in
# `ValveStatusModel.atFlow` / `.atTemp` in the app.
#
# `atTemp` is what made 0x05 look mysterious: it means "this valve has reached its
# temperature setpoint", which is sticky, uncorrelated with outlets, and appears about half
# the time. It was never set during warmup in the captures — correct, since the valve is
# still climbing to temperature then.
#
# `atFlow` was never observed set on the test system — but that system has flow control
# DISABLED at the fixture (a workaround for it being broken on HUB firmware 2.88), so the
# flat zero probably reflects the setting rather than the firmware. The measured-flow byte
# is also zero there, which fits. Unconfirmed: needs a system with flow control enabled.
VALVE_INDEX_SHIFT = 4
VALVE1_INDEX = 0
VALVE2_INDEX = 1
AT_FLOW_BIT = 0x08
AT_TEMP_BIT = 0x04
# Kept as the values actually written for a temperature at or above 25.6 °C.
VALVE1_PREFIX = 0x01
VALVE2_PREFIX = 0x11

# Temperature is a 16-BIT value spanning bytes 0 and 1: tenths of a degree Celsius, with
# the high byte in bit 0 of byte 0 and the low byte in byte 1.
#
#     °C = ((byte0 & 0x01) << 8 | byte1) / 10
#
# This supersedes the earlier reading of "25.6 + byte1/10", which was the same arithmetic
# in disguise — 256/10 is 25.6, so that formula silently assumed the high bit was always
# set. It agrees on all 357 captured words where the bit IS set, and is wrong where it is
# not: the words 0000C800 and 1000C800 are 0.0 °C, which the old formula reported as
# 25.6 °C.
#
# It also explains the mysterious "base": there is no base. 25.6 °C is simply the smallest
# temperature whose high byte is 1.
TEMPERATURE_TENTHS_PER_DEGREE = 10
# TWO bits of byte 0, not one: temperature is 10-bit, so up to 102.3 C is representable.
# Bit 0x02 was never set across 363 captures (no temperature reached 51.2 C), which is why a
# one-bit reading agreed with every sample while still being the narrower model.
TEMPERATURE_HIGH_BITS = 0x03
# The Konnect app never sends above 48.8 °C (488 tenths), so writes clamp there rather than
# at the 51.1 °C the encoding could carry.
TEMPERATURE_MAX_TENTHS = 488
# Kept as documentation of the encoding, not because anything here reads them: the wire
# format is `base + tenths * step`, and these name the two halves of that so the tenths
# arithmetic below is checkable against the reference decoder in `docs/gcs/valve_hex.md`
# (which calls them `VALVE_TEMPERATURE_BASE_C` / `VALVE_TEMPERATURE_STEP_C`). Derived, not
# magic. The comment here used to claim callers still used them — none have since the codec
# moved to tenths.
TEMPERATURE_BASE_C = 25.6
TEMPERATURE_STEP_C = 0.1

# ---------------------------------------------------------------------------
# ⚠️ Fahrenheit is a LOOKUP TABLE, not arithmetic — Konnect 3.0.1 `p315jj/h.java:1971`,
#    unchanged in 3.0.6 (`db0/c.java` `A()`, duplicated in `mc0/n.java`)
# ---------------------------------------------------------------------------
# `h.z()` maps a displayed whole °F to tenths of a °C directly, and it is **not**
# `round((f - 32) * 50 / 9)`. Above 86 °F it sits exactly one tenth *below* that formula at
# sixteen entries — 87, 89, 91, 93, 96, 98, 100, 102, 105, 107, 109, 111, 114, 116, 118, 120 —
# precisely the set where naive rounding would round up.
#
# **That low bias is the mechanism, not a rounding artefact.** It is what makes the app's
# round trip idempotent: with `h.j(c) = round(c * 1.8 + 32)` for display, `z(j(t)) == t` holds
# for all 64 entries. Naive arithmetic breaks it on 12 of the 34 values this integration's
# slider can produce, every one by +1 tenth:
#
#     102 °F -> ours 0x185 (389), Kohler 0x184 (388)
#     100 °F -> ours 0x17A (378), Kohler 0x179 (377)
#      98 °F -> ours 0x16F (367), Kohler 0x16E (366)
#
# Measured consequence, 2026-08-17: the owner reported the temperature coming back "one more"
# after the shower restarted itself, and 0x185 (389) appears 11 times in this system's capture
# corpus. `z()` cannot produce it, and the Celsius path writes whole degrees (380/390/400), so
# **no Kohler client emits it in a direct write** — which is what makes this table right for
# `solowritesystem`.
#
# ⚠️ **Correction, 2026-10-07 (Konnect 3.0.6): the app does emit 389 — in favorites.** Its
# favorite path converts a °F default temperature arithmetically, `(F - 32) * 0.5555`
# formatted to one decimal (`db0/c.java` `J()`), so a favorite saved at 102 °F stores 38.9 °C
# = 0x185, and a running favorite's temperature is what the valve word then reports. Those
# 11 captured words could therefore be favorites as much as this integration's writes; the
# earlier note calling 389 "a value no Kohler client can emit" was wrong. The conclusion for
# direct writes stands. See `PRESET_ECHO_MAX_TENTHS` for the one place this changes code.
#
# The valve accepts off-ladder values perfectly well — nothing here is a protocol requirement.
# What it costs is that a setpoint written from Home Assistant no longer sits where the
# touchscreen would put it, so the next panel adjustment starts from a value one tenth off.
#
# Outside 59–122 °F the app returns 0, which for a device that opens water valves would mean
# "full cold". `unit_to_celsius` falls back to the arithmetic rather than doing that.
FAHRENHEIT_TO_TENTHS_C = {
    59: 150,
    60: 156,
    61: 161,
    62: 167,
    63: 172,
    64: 178,
    65: 183,
    66: 189,
    67: 194,
    68: 200,
    69: 206,
    70: 211,
    71: 217,
    72: 222,
    73: 228,
    74: 233,
    75: 239,
    76: 244,
    77: 250,
    78: 256,
    79: 261,
    80: 267,
    81: 272,
    82: 278,
    83: 283,
    84: 289,
    85: 294,
    86: 300,
    87: 305,
    88: 311,
    89: 316,
    90: 322,
    91: 327,
    92: 333,
    93: 338,
    94: 344,
    95: 350,
    96: 355,
    97: 361,
    98: 366,
    99: 372,
    100: 377,
    101: 383,
    102: 388,
    103: 394,
    104: 400,
    105: 405,
    106: 411,
    107: 416,
    108: 422,
    109: 427,
    110: 433,
    111: 438,
    112: 444,
    113: 450,
    114: 455,
    115: 461,
    116: 466,
    117: 472,
    118: 477,
    119: 483,
    120: 488,
    121: 494,
    122: 500,
}
# The byte accepts up to 0xFF, but the Konnect app never sends above 0xE8 (48.8 °C /
# 119.8 °F). Whether the firmware enforces that cap or only the app does is untested, so
# writes clamp to the app's limit rather than the byte's.
TEMPERATURE_BYTE_MAX = 0xE8

# Byte 2 — flow. The SAME byte is expressed on three different scales depending on where
# you read it, which is a standing source of 2x and 4x errors:
#
#   byte            0x00-0xC8   the wire format, here and in MQTT
#   flowSetpoint    0-50        the GCS device's own native unit (gcs-state), byte / 4
#   percent         0-100       HUB favorite `flowrate` and HA entities, byte / 2
#
# Verified live: with the shower idle, gcs-state reports flowSetpoint "50" on both valves,
# and 50 * 4 = 0xC8 = the maximum flow byte. The outlet configuration's documented
# "flow 16-200" range is in BYTE units (0x10-0xC8), not either of the other two.
#
# The HUB has no independent flow of its own — its favorite `flowrate` is just read and
# written through to the GCS valve, so the valve is always the real source.
# **Confirmed 2026-09-10 against the owner's two valves**: every one of their six outlets
# reports `maximumFlowRate: 200`, so `byte / 2` is exactly right on this hardware, and the
# live words decode to the percentages the diagnostics show (byte 49 -> 24.5 %, byte 53 ->
# 26.5 %). The reference integration hardcodes the same 2 and additionally truncates with
# `byte // 2`, losing the half-percent on the odd bytes both of these valves actually carry.
#
# **Only a default, since 0.8.2.** Percent is a ratio against the outlet's own
# `maximumFlowRate` — see `flow_byte_to_percent` below — and the Flow entity uses this zone's
# real ceiling. This constant remains for callers with no per-outlet context (`decode_word`
# and `encode_word` have 28 call sites and no per-valve context), where it reproduces exactly
# what every release before 0.8.2 did.
FLOW_PER_PERCENT = 2
FLOW_PER_SETPOINT = 4

# The flow byte is the valve's own flow rate, and its scale **does not start at zero**.
# Every outlet reports `minimumFlowRate: 16` / `maximumFlowRate: 200` in
# `READ_GCS_OUTLET_CONFIG_CFG`, and across every capture the byte has never once fallen
# outside 16-200 (31 distinct values). So the usable percent range is 8-100, not 0-100.
#
# **The valve honours a directly written flow byte.** Verified against hardware: 74 and 100
# commanded with one, two, and three outlets open in a zone, every echo matching exactly, and
# the other zone untouched.
#
# That needs saying because touchscreen-driven captures look nothing like this — the byte
# moves on its own when outlets change, and the same outlet set tops out at 200 in one
# session and 69 in another. That is the **touchscreen** computing linked-zone scaling and
# its own ceiling before it sends; the valve just obeys whoever wrote last. The Konnect app
# is a third actor and has removed flow control entirely. Writing directly, none of the
# touchscreen's behaviour applies to us.
#
# Consequence for a client: encode against [16, 200] and expect it to stick — but keep
# treating the echo as truth, since the touchscreen can overwrite at any time.
#
# The byte is continuous over that range, which is why it is frequently **not** a multiple
# of 4: values like 17, 19, and 165 are ordinary. `flowSetpoint` (byte/4, the 0-50 figure
# `gcs-state` reports) is a derived display value, not the underlying quantity, so treating
# it as an integer scale invents a precision the device does not use.
FLOW_BYTE_MIN = 0x10
FLOW_SETPOINT_MAX = 50
FLOW_BYTE_MAX = 0xC8


def flow_byte_to_percent(byte: int, max_flow_byte: int = FLOW_BYTE_MAX) -> float:
    """Flow byte to percent, as a ratio against the outlet's own ceiling.

    **This is what the Konnect app does**, confirmed from its bytecode: `jj.h$a.X(value, max)`
    computes `value * 100 / max`, where `max` is that outlet's `maximumFlowrate` read at
    runtime from the device's own settings. There is no divisor of 2 anywhere in the app's
    flow path — `byte / 2` is only correct because a ceiling of 200 makes the two agree.

    `max_flow_byte` defaults to 200 so a caller without per-outlet limits behaves exactly as
    every release before 0.8.2 did.
    """
    if max_flow_byte <= 0:  # pragma: no cover - defensive
        return 0.0
    return round(byte * 100 / max_flow_byte, 1)


def flow_percent_to_byte(percent: float, max_flow_byte: int = FLOW_BYTE_MAX) -> int:
    """Percent to flow byte — the exact inverse, matching `jj.h$a.Y(percent, max)`.

    The app computes `percent * max / 100` and rounds; so does this.
    """
    if max_flow_byte <= 0:  # pragma: no cover - defensive
        return FLOW_BYTE_MIN
    return round(percent * max_flow_byte / 100)


# Byte 3 = [0x80][pause 0x40][0 0 0][outlet3 0x04][outlet2 0x02][outlet1 0x01]
#
# Bit 0x80 differs by direction, like byte 0: on READ it is `errorFlag`, paired with the
# error code in byte 7; on WRITE it is `skipWarmUp` (start without triggering warmup).
# Corroborated by gcs-state, which reports errorFlag "0" / errorCode "1" while captures show
# byte3 & 0x80 clear and byte7 = 0x01. Never observed set on hardware.
#
# Konnect 3.0.6 sets it in exactly one place: **bath fill** (`qb0/m0.java`). Starting a fill
# writes bit 7 plus the tub filler's outlet bit (fixture type 21) on top of whatever is open,
# at the current temperature and flow, and stopping one writes `0xC0`. Every other app write
# leaves it clear. So the write meaning is app-confirmed; what the valve does with it is not.
#
# The outlet mask is ONLY the low three bits. 0x40 is an INDEPENDENT pause bit that
# round-trips the device's pauseFlag (write: `Pi/r.java` getPauseFlag(); read: decodes into
# ValveStatusModel.pauseFlag) — it is not a mask value, and it coexists with outlet bits:
#
#     00  idle, nothing assigned          01/02/04  running to outlet 1/2/3
#     40  paused, nothing assigned        41/42/44  paused, outlet 1/2/3 still assigned
#
# So a paused valve retains which outlet it will resume to. Treating 0x40 as a whole-mask
# sentinel makes 0x41 unrepresentable and misreads a paused-with-assignment valve as running.
# The library's "0x40 = preset-mode" and "0x01 = SHOWER mode" were both misreads of this byte.
OUTLET_MASK_BITS = 0x07
VALVE_PAUSE_FLAG = 0x40
VALVE_SKIP_WARMUP_FLAG = 0x80  # write meaning of bit 0x80
VALVE_ERROR_FLAG = 0x80  # read meaning of the same bit
VALVE_STOP_MASK = 0x00
OUTLETS_PER_VALVE = 3

VALVE_WORD = re.compile(r"^[0-9A-Fa-f]{8}$")

# A preset's hexString is 3 bytes — [byte0][temp low][flow] — and byte0 carries BOTH the
# temperature high bit and the outlet flags, at DIFFERENT bit positions from a command word:
#
#     preset  byte0:  0x04 outlet1   0x08 outlet2   0x10 outlet3   0x01 temp high bit
#     command byte3:  0x01 outlet1   0x02 outlet2   0x04 outlet3
#
# There is no valve-index nibble in a preset: the valve is identified by field position.
# Confirmed on all four valve entries of two live presets:
#
#     018448  byte0 0x01 -> no outlets, 38.8 C      (Default shower Valve1)
#     05849C  byte0 0x05 -> outlet1,    38.8 C      (Default shower Valve2)
#     1190C8  byte0 0x11 -> outlet3,    40.0 C      (Test favorite Valve1)
#     0589C8  byte0 0x05 -> outlet1,    39.3 C      (Test favorite Valve2)
#
# An earlier revision concluded presets carried no outlet mask at all. That was wrong: it
# tested the command word's bit positions (0x01/0x02/0x04) against preset bytes.
PRESET_OUTLET_BITS = (0x04, 0x08, 0x10)
PRESET_WORD = re.compile(r"^[0-9A-Fa-f]{6}$")


def encode_preset_word(
    temperature_celsius: float, flow_percent: float, outlet_mask: int
) -> str:
    """Build a 3-byte preset hexString for one valve.

    Used by ``writepreset`` and ``createpreset``, which take this format — NOT the 4-byte
    command word. Sending a command word where a preset word is expected is accepted by the
    backend and then silently ignored.
    """
    if outlet_mask & ~OUTLET_MASK_BITS:
        raise ValveHexError(f"Outlet mask 0x{outlet_mask:02X} sets unknown bits")
    tenths = min(
        max(round(temperature_celsius * TEMPERATURE_TENTHS_PER_DEGREE), 0),
        TEMPERATURE_MAX_TENTHS,
    )
    byte0 = (tenths >> 8) & TEMPERATURE_HIGH_BITS
    for index, bit in enumerate(PRESET_OUTLET_BITS):
        if outlet_mask >> index & 1:
            byte0 |= bit
    # Clamp to the device's own range, not 0x00. Confirmed against the Konnect
    # decompile: the app's encoder is `hex(round(setpoint_0_50 * 4))` with no clamp of
    # its own — all clamping happens upstream at the slider, bounded by the per-outlet
    # `minimumFlowRate`/`maximumFlowRate`. Our `* 2` on percent is arithmetically the
    # same value, since percent = 2 x setpoint.
    flow_byte = min(
        max(round(flow_percent * FLOW_PER_PERCENT), FLOW_BYTE_MIN), FLOW_BYTE_MAX
    )
    return f"{byte0:02X}{tenths & 0xFF:02X}{flow_byte:02X}"


def preset_word_temperature(word: str) -> float:
    """Read the commanded temperature, in °C, out of a 3-byte preset hexString.

    The inverse of :func:`encode_preset_word`'s temperature half, and the only part of a
    preset word worth reading back: it is the one field that can hurt someone. Raises
    :class:`ValveHexError` if the word is not six hex characters.
    """
    text = str(word or "").strip()
    if not PRESET_WORD.match(text):
        raise ValveHexError(f"Preset word must be 6 hex characters, got {word!r}")
    byte0 = int(text[0:2], 16)
    tenths = ((byte0 & TEMPERATURE_HIGH_BITS) << 8) | int(text[2:4], 16)
    return round(tenths / TEMPERATURE_TENTHS_PER_DEGREE, 1)


#: The ceiling for a preset word **read back** and echoed into a write: 48.9 °C.
#:
#: One tenth above `TEMPERATURE_MAX_TENTHS`, because that is the highest value the Konnect
#: app itself stores in a favorite: its favorite path converts °F arithmetically (see the
#: correction above the Fahrenheit table), and 120 °F — the top of the older app's
#: Max Temperature range — becomes `48.884` -> `"48.9"` -> 489. With the old ceiling a timer
#: sync dropped that valve's word from a favorite the owner made in the app, silently
#: changing the favorite. The valve still clamps any outlet at its own
#: `maximumOutletTemperature`, which the app never writes above 48.8 °C, so admitting the
#: app's own value admits nothing hotter than the valve would run.
PRESET_ECHO_MAX_TENTHS = 489


def check_preset_word(word: str) -> str:
    """Return ``word`` normalised, or raise if it is malformed or commands a scald.

    Used on words **read back from the cloud** before they are echoed into a write.
    ``writepreset`` replaces a preset record whole, so every field not being changed has to
    be sent back exactly as it was found — which means a value the integration never
    computed passes through it to the valve. That is a narrow trust boundary, but a real
    one, and it is cheap to close: a preset word carries the same 10-bit temperature the
    command word does, so a corrupted or hostile record could set a preset to 102.3 °C and
    this integration would be the thing that wrote it there.

    The ceiling is :data:`PRESET_ECHO_MAX_TENTHS` — one tenth above what
    :func:`encode_preset_word` clamps to, so a word this integration wrote always passes,
    and so does anything the Konnect app can store; only a foreign value can fail.
    """
    text = str(word or "").strip().lower()
    ceiling = PRESET_ECHO_MAX_TENTHS / TEMPERATURE_TENTHS_PER_DEGREE
    commanded = preset_word_temperature(text)
    if commanded > ceiling:
        raise ValveHexError(
            f"Preset word {text!r} commands {commanded:.1f} °C, above the "
            f"{ceiling:.1f} °C ceiling"
        )
    return text


class ValveHexError(ValueError):
    """Raised when a valve word is malformed or a value is out of range."""


@dataclass(frozen=True)
class ValveWord:
    """A decoded valve command word."""

    prefix: int
    temperature_celsius: float
    flow_percent: float
    outlet_mask: int
    paused: bool
    # Read-only status the device reports; always zero in a word we send.
    at_temperature: bool = False
    at_flow: bool = False
    error_flag: bool = False
    # Live sensor feedback from the second half of a 16-character status word. None when
    # only the 8-character command half was available.
    measured_temperature_celsius: float | None = None
    measured_flow_percent: float | None = None
    error_code: int | None = None
    # The word exactly as it arrived, so anything wanting to *show* the wire value never
    # re-encodes it from the decoded fields — a reconstruction silently goes stale whenever
    # the codec is corrected, and it cannot represent bits this dataclass does not model.
    # Empty for a word built from a REST read, which carries no wire word.
    #
    # Excluded from equality: two words that decode identically are the same state, and
    # letting a reserved-bit difference count as a change would wake every entity.
    raw: str = field(default="", compare=False)

    @property
    def measured_flow_setpoint(self) -> float | None:
        """Measured flow on the device's own 0-50 scale."""
        if self.measured_flow_percent is None:
            return None
        return round(
            self.measured_flow_percent * FLOW_PER_PERCENT / FLOW_PER_SETPOINT, 1
        )

    @property
    def stopped(self) -> bool:
        """True when no outlet is open and the valve is not merely paused."""
        return self.outlet_mask == VALVE_STOP_MASK and not self.paused

    @property
    def flow_setpoint(self) -> float:
        """Flow on the GCS device's own 0-50 scale, as ``gcs-state`` reports it."""
        return round(self.flow_percent * FLOW_PER_PERCENT / FLOW_PER_SETPOINT, 1)

    def outlet(self, index: int) -> bool:
        """Whether this valve's outlet ``index`` (0-2) is open."""
        return bool(self.outlet_mask >> index & 1)


def normalize_word(value: str | None) -> str:
    """Return the uppercased 8-character command half of a valve field."""
    word = str(value or "")[:8].upper()
    if not VALVE_WORD.fullmatch(word):
        raise ValveHexError(f"Not an 8-character valve command word: {value!r}")
    return word


def decode_word(value: str) -> ValveWord:
    """Decode a valve word.

    Accepts both forms. The 8-character word is what ``solowritesystem`` sends: setpoints,
    flags, and the outlet mask. The 16-character word the device *reports* appends four
    more bytes of **live sensor feedback** — so the full status word is symmetric,
    "what was commanded" followed by "what the valve is actually doing":

    ===== ============================================================
    byte  meaning
    ===== ============================================================
    4-5   measured temperature, ``((byte4 & 3) << 8 | byte5) / 10`` °C
    6     measured flow, same scale as the byte-2 setpoint
    7     error code, pairing with ``errorFlag`` in byte 3
    ===== ============================================================

    On the hardware tested these read `00000001` — zero measurement, error code 1 — even
    across 239 messages with an outlet open, so this valve does not appear to report
    measurements over MQTT. The mapping is corroborated by ``gcs-state``, which reports the
    matching ``errorFlag: "0"`` / ``errorCode: "1"``.

    Byte 4's upper six bits are unused, and the app writes the same measurement to all three
    per-outlet sub-objects: it is one per-valve reading, stored redundantly.
    """
    full = str(value or "").strip().upper()
    word = normalize_word(full)
    mask_byte = int(word[6:8], 16)
    byte0 = int(word[0:2], 16)
    tenths = ((byte0 & TEMPERATURE_HIGH_BITS) << 8) | int(word[2:4], 16)
    return ValveWord(
        prefix=byte0,
        temperature_celsius=round(tenths / TEMPERATURE_TENTHS_PER_DEGREE, 1),
        flow_percent=round(int(word[4:6], 16) / FLOW_PER_PERCENT, 1),
        outlet_mask=mask_byte & OUTLET_MASK_BITS,
        paused=bool(mask_byte & VALVE_PAUSE_FLAG),
        at_temperature=bool(byte0 & AT_TEMP_BIT),
        at_flow=bool(byte0 & AT_FLOW_BIT),
        error_flag=bool(mask_byte & VALVE_ERROR_FLAG),
        raw=full,
        **_decode_measurements(full),
    )


def _decode_measurements(full: str) -> dict[str, object]:
    """Pull the live-feedback half out of a 16-character status word."""
    if len(full) < 16 or not VALVE_WORD.fullmatch(full[8:16]):
        return {}
    byte4, byte5, byte6, byte7 = (int(full[i : i + 2], 16) for i in range(8, 16, 2))
    return {
        "measured_temperature_celsius": round(
            (((byte4 & TEMPERATURE_HIGH_BITS) << 8) | byte5)
            / TEMPERATURE_TENTHS_PER_DEGREE,
            1,
        ),
        "measured_flow_percent": round(byte6 / FLOW_PER_PERCENT, 1),
        "error_code": byte7,
    }


def encode_word(
    prefix: int,
    temperature_celsius: float,
    flow_percent: float,
    outlet_mask: int,
    *,
    paused: bool = False,
    skip_warmup: bool = False,
) -> str:
    """Build one valve command word.

    Temperature and flow are clamped to what the device accepts rather than rejected,
    matching the Jinja encoder this replaces. The outlet mask is not clamped: a caller
    passing something outside 0x00-0x07 or the 0x40 PAUSE sentinel has a bug worth
    surfacing rather than silently reinterpreting as a different set of open outlets.
    """
    if outlet_mask & ~OUTLET_MASK_BITS:
        raise ValveHexError(
            f"Outlet mask 0x{outlet_mask:02X} sets bits outside the low three. "
            "Pause is a separate flag — pass paused=True rather than folding 0x40 in."
        )
    tenths = min(
        max(round(temperature_celsius * TEMPERATURE_TENTHS_PER_DEGREE), 0),
        TEMPERATURE_MAX_TENTHS,
    )
    # The temperature's high bit lives in byte 0 alongside the valve index. Callers pass
    # the already-composed byte (0x01 / 0x11), so keep its nibble and set the bit from the
    # temperature rather than trusting what was handed in.
    # Only the valve index and the temperature high bits are ours to set; atFlow/atTemp
    # stay zero because they are status the device reports, not something we command.
    byte0 = (prefix & 0xF0) | ((tenths >> 8) & TEMPERATURE_HIGH_BITS)
    # Clamp to 0xC8, not 0xFF: 0xC8 is 100% / flowSetpoint 50, the device's maximum.
    # Clamp to the device's own range, not 0x00. Confirmed against the Konnect
    # decompile: the app's encoder is `hex(round(setpoint_0_50 * 4))` with no clamp of
    # its own — all clamping happens upstream at the slider, bounded by the per-outlet
    # `minimumFlowRate`/`maximumFlowRate`. Our `* 2` on percent is arithmetically the
    # same value, since percent = 2 x setpoint.
    flow_byte = min(
        max(round(flow_percent * FLOW_PER_PERCENT), FLOW_BYTE_MIN), FLOW_BYTE_MAX
    )
    byte3 = outlet_mask
    if paused:
        byte3 |= VALVE_PAUSE_FLAG
    if skip_warmup:
        byte3 |= VALVE_SKIP_WARMUP_FLAG
    return f"{byte0:02X}{tenths & 0xFF:02X}{flow_byte:02X}{byte3:02X}"


def outlet_mask(*outlets: bool) -> int:
    """Combine up to three outlet flags into a mask (bit 0 is the first)."""
    if len(outlets) > OUTLETS_PER_VALVE:
        raise ValveHexError(
            f"A valve has {OUTLETS_PER_VALVE} outlets, not {len(outlets)}"
        )
    return sum(1 << index for index, is_on in enumerate(outlets) if is_on)


def encode_pair(
    model: ValveModel,
    temperature_celsius: float,
    flow_percent: float,
    outlets: list[bool],
) -> tuple[str, str]:
    """Build both valve words from this model's outlet flags.

    ``outlets`` has one flag per physical outlet, so its length depends on the model. The
    split between valve1 and valve2 is the model's, NOT a fixed 3+3 — on a 4-outlet
    K-28211 it is 2+2, so outlet 3 is valve2's first outlet.

    When the model has no second valve, ``secondaryValve1`` is the all-zero ignore word.
    """
    valve1_flags, valve2_flags = model.split_outlets(outlets)
    valve1 = encode_word(
        VALVE1_PREFIX, temperature_celsius, flow_percent, outlet_mask(*valve1_flags)
    )
    if not model.uses_valve2:
        return valve1, UNUSED_VALVE_WORD
    return valve1, encode_word(
        VALVE2_PREFIX, temperature_celsius, flow_percent, outlet_mask(*valve2_flags)
    )


def encode_shower(
    model: ValveModel,
    temperatures_celsius: Mapping[int, float],
    flow_percent: float,
    zone_flags: Mapping[int, Sequence[bool]],
) -> tuple[str, str]:
    """Build both words for a whole-shower command from per-zone flags and temperatures.

    The stateless sibling of `encode_pair`, written for the ``custom_shower`` action: the
    words say everything, and nothing in them comes from what the valve last reported. A
    zone missing from ``zone_flags``, or with no flag set, is written **closed** (mask
    ``0x00``) — never "left as it was", because "as it was" is exactly the report that lags
    a write (GitHub issue #1, 2026-09-03). ``temperatures_celsius`` is per zone, like the
    valve's own setpoints; zone 2 falls back to zone 1's when not given.

    Flags are ZONE-LOCAL — index 0 is that zone's outlet 1 — unlike `encode_pair`'s flat
    list, because the global numbering is model-dependent (`ValveModel.outlet_location`)
    and a per-zone form cannot get it wrong. A flag set beyond the outlets the zone has
    raises `ValveHexError` rather than being dropped or shifted onto another outlet; on a
    single-zone model that includes any zone 2 flag at all.
    """

    def mask_for(zone: int) -> int:
        flags = list(zone_flags.get(zone, ()))
        capacity = model.outlets_in_zone(zone)
        if any(flags[capacity:]):
            raise ValveHexError(
                f"{model.name} has {capacity} outlet(s) in zone {zone}; "
                f"outlet {capacity + 1} or beyond was requested"
            )
        return outlet_mask(*flags[:capacity])

    # Both masks are checked before anything is built, so a zone 2 flag on a single-zone
    # model is an error rather than something silently swallowed by the sentinel below.
    mask1 = mask_for(1)
    mask2 = mask_for(2)
    celsius1 = temperatures_celsius[1]
    celsius2 = temperatures_celsius.get(2, celsius1)
    valve1 = encode_word(VALVE1_PREFIX, celsius1, flow_percent, mask1)
    if not model.uses_valve2:
        return valve1, UNUSED_VALVE_WORD
    return valve1, encode_word(VALVE2_PREFIX, celsius2, flow_percent, mask2)


def _mask_pair(
    model: ValveModel,
    mask: int,
    temperature_celsius: float,
    flow_percent: float,
    *,
    paused: bool = False,
) -> tuple[str, str]:
    """Build both words carrying the same mask, respecting the model's valve count."""
    valve1 = encode_word(
        VALVE1_PREFIX, temperature_celsius, flow_percent, mask, paused=paused
    )
    if not model.uses_valve2:
        return valve1, UNUSED_VALVE_WORD
    return valve1, encode_word(
        VALVE2_PREFIX, temperature_celsius, flow_percent, mask, paused=paused
    )


def stop_pair(
    model: ValveModel,
    temperature_celsius: float = 38.0,
    flow_percent: float = 100,
) -> tuple[str, str]:
    """Build the pair of words that stops every outlet.

    Mask ``0x00`` is STOP. The temperature and flow bytes are ignored by the firmware for a
    stop but still have to be well-formed — which is why the library's ``turn_off()``,
    sending an all-zero ``primaryValve1``, is ignored: prefix ``0x00`` addresses no valve.

    ⚠️ **Deliberately not what the Konnect app sends.** The app's Stop writes byte 3 =
    ``0x40`` — the pause bit with no outlets, exactly :func:`pause_pair` with no mask
    (Konnect 3.0.6 ``db0/c.java``: the solo-write builder sets the per-valve pause bit when
    stopping and zeroes the outlets; the older screens send the same). This integration keeps
    ``0x00`` on purpose: ``runtime_cutoff`` restarts a shower on a ``0x40`` pause that lands
    at the valve's own run-time limit, and a ``0x00`` stop is the one shape it never undoes.
    Sending the app's word would make a Home Assistant stop that happened to coincide with
    the limit indistinguishable from a cutoff. Both words leave the valve idle.
    """
    return _mask_pair(model, VALVE_STOP_MASK, temperature_celsius, flow_percent)


def pause_pair(
    model: ValveModel,
    temperature_celsius: float = 38.0,
    flow_percent: float = 100,
    outlet_mask: int = 0x00,
) -> tuple[str, str]:
    """Build the pair of words that pauses the valves, holding the session open.

    ``outlet_mask`` carries forward which outlets the session will resume to — a paused
    valve keeps its assignment, so passing the running mask preserves it.
    """
    return _mask_pair(
        model, outlet_mask, temperature_celsius, flow_percent, paused=True
    )


# A `preset_word_to_command` used to live here: it built a command word by reading a
# preset hexString's byte 0 as an outlet mask. **That reading is wrong** — byte 0 carries
# the temperature high bit and the PRESET_OUTLET_BITS positions, and live data disproved
# the mask premise (see `preset_valve_to_command`, whose docstring holds the evidence).
# Removed 2026-08-21 along with its only caller, the superseded two-command preset start
# (`controlpresetorexperience` runs the preset by itself, so no valve write follows it).


def celsius_to_unit(value_c: float, temperature_unit: str) -> float:
    """Convert a decoded Celsius value into the account's display unit.

    Kohler's REST API and this valve byte both report Celsius regardless of the account's
    display preference; the mobile app converts locally. Convert at the edge only.
    """
    if temperature_unit.lower().startswith("f"):
        return round(value_c * 9 / 5 + 32, 1)
    return round(value_c, 1)


def unit_to_celsius(value: float, temperature_unit: str) -> float:
    """Convert an account-unit temperature into Celsius for encoding.

    Fahrenheit goes through `FAHRENHEIT_TO_TENTHS_C`, Kohler's own table, so a value written
    from Home Assistant lands exactly where the touchscreen would put it. See that table for
    why arithmetic is wrong here — it drifts +1 tenth on 12 of the 34 values the slider offers.

    The table holds whole degrees only. A fractional °F — reachable through the service, not
    the slider — falls back to the arithmetic, as does anything outside 59-122 °F. Kohler
    returns 0 there; that would be full cold, so it is not copied.
    """
    if not temperature_unit.lower().startswith("f"):
        return value
    whole = round(value)
    if abs(value - whole) < 0.01 and whole in FAHRENHEIT_TO_TENTHS_C:
        return FAHRENHEIT_TO_TENTHS_C[whole] / TEMPERATURE_TENTHS_PER_DEGREE
    return (value - 32) * 5 / 9
