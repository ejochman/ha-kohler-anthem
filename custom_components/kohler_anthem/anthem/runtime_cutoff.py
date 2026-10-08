"""Detect the valve closing a zone because it hit its own maximum run time.

The valve enforces a `maximumRunTime` — 900 s on the reference install, 3600 s before it was
reconfigured — and shuts the water off once it has been running that long. Nothing on the
wire says "I did this because of the timer": `currentSystemState` reads `normalOperation` in
every captured status message, and a timed-out close looks exactly like someone pressing
stop.

**The timer is per ZONE, not per outlet.** This is the thing that took two attempts to get
right, so it is worth stating precisely:

* The clock starts when a zone goes from *nothing flowing* to *something flowing*.
* It **does not reset when outlets change within the zone.** Opening a second head, closing
  the first, swapping between them — none of it touches the timer.
* At the limit the valve pauses that zone (`0x40`) and clears its mask.

An earlier version of this module timed each outlet from when *that outlet* opened. It fired
correctly only when a zone happened to run one outlet, unchanged, for the whole session, and
silently missed everything else — any mid-shower outlet change resets the per-outlet clock
while the valve's own clock keeps running. Replayed over the corpus it finds 8 of the 11 real
cutoffs; in the one logged session where the owner actually moved between shower heads, it
found 1 of 4.

# ---------------------------------------------------------------------------
# Every decision this module makes is written to the cutoff debug log — including
# the ones where it decides NOT to fire, which nothing else records. When this
# feature misbehaves, read `cutoff_*.jsonl` alongside `mqtt_raw_*.jsonl` in the
# same directory; see `cutoff_log.py`.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ⚠️ ANNOUNCED LIMITS ONLY. NOTHING HERE INFERS A LIMIT FROM BEHAVIOUR.
# ---------------------------------------------------------------------------
# The only limits this detector will ever fire on are the `maximumRunTime` values the valve
# itself announced, as persisted in `CONF_OUTLET_RUN_TIMES`. A duration that matches nothing
# announced is recorded and left alone, however suggestive it looks.
#
# A `MissedCutoffWatcher` used to live here and guess: it collected declined pauses and, on
# three landing within 2 s of each other, offered the duration as a "suspected limit" for the
# caller to promote. **Removed 2026-08-17, deliberately, and it should not come back.**
#
# It was built when it was believed the valve had one run-time limit and the only way to miss
# one was not being told. That turned out to be wrong: a **preset carries its own `time`**, a
# second limit independent of `maximumRunTime`, and it is the lower of the two that stops the
# shower (`docs/gcs/api.md`, "two independent timers"). So repeated identical durations are
# not evidence of an unannounced hardware limit at all — they are the normal signature of a
# preset the owner configured. Presets 2-10 are user settings; restarting a shower that ended
# on one would override a deliberate choice.
#
# The mechanism is understood now and there is nothing left to infer. `test_no_limit_guessing.py`
# in the offline suite asserts this module has no source of limits but its caller's.
# ---------------------------------------------------------------------------

## Why duration alone is enough to discriminate

Across **156** completed zone-open periods in the capture corpus (2026-08-07 to 08-14,
spanning the reconfiguration from a 3600 s limit to 900 s):

| | |
|---|---|
| Zone **paused** within 10 s of a limit | **11** — 899.84, 899.93, 899.95, 899.96, 900.01, 900.07, 3598.68, 3599.68, 3599.74, 3599.85, 3599.85 |
| Every other pause (54) | 1.9 s to 1831 s, none closer than **334 s** to a limit |
| Zones ended by a **stop** (`0x00`) rather than a pause (91) | none closer than **123 s** to a limit |

Every cutoff lands within **1.32 s** of its limit; the nearest pause that was not a cutoff is
over five minutes away. The two groups do not come close to touching.

Note what the third row *used to* buy: requiring the pause flag removed 91 of the 156 periods
from consideration outright, including every stop issued from Home Assistant or the Anthem
Plus controller. Duration then only had to separate 11 cutoffs from 54 other pauses.

### ⚠️ REQUIRED AGAIN — 2026-08-18, owner's decision, after five case studies

**A cutoff must carry `0x40`.** The 2026-08-17 change below is reverted; the section is kept
because its measurement is still the best description of the corpus, and because the reason it
was made turned out to be a configuration problem rather than a protocol one.

**What we did not know on 2026-08-17:** the Anthem Plus controller runs its own maximum
shower duration, independently of the valve's `maximumRunTime`, and the two were set to
different values — 900 s on the valve, 3600 s on the controller. The controller's longer clock
expired mid-leg and stopped a shower still in use. That looked like a valve behaviour the
detector was missing. It was two timers disagreeing.

**What five case studies established** (`docs/case_studies/`):

* Only the **GCS valve** cuts with `0x40`. Only the **controller** cuts with `0x00`.
* The valve fires slightly **early** — measured −0.08 to −0.23 s against its limit.
* The controller fires slightly **late** — measured +0.20, +0.557 and +1.004 s.
* So **with both durations set to the same value the valve's pause always arrives first**,
  by ~1.1 s in the one session where both were at 900 s, and it is always the actionable
  signal. The controller's `0x00` then lands on a zone already restored.
* Across the whole corpus, **not one genuine max-duration cutoff took the shape "both zones
  transitioned from flowing to zero"** — all 16 such transitions are `stopall`, test scripts
  or somebody ending a shower.

**So the rule is: match the two Max Shower Durations, and require the pause flag.** That
restores the strong property the 2026-08-17 change gave up — a deliberate stop is never
undone, whoever issued it — and costs nothing as long as the durations agree.

⚠️ **If they do not agree**, the controller can fire alone with `0x00` and the shower will not
come back. That is the safe failure direction (water off, not water on), and it is logged at
WARNING naming the likely cause, plus journalled with its own verdict so it is visible in
analysis. Do not re-fix it by accepting `0x00` again; fix the configuration.

⚠️ **Known accepted risk, unchanged:** pressing off on the **first-generation touchscreen**
writes `0x40` on **both zones** (case study 4), which is byte-identical to a preset-driven
cutoff. Ending a shower from that screen within `CUTOFF_TOLERANCE_SECONDS` of the limit will
restart the water. There is no discriminator in the data — requiring both zones to match would
break the preset case, which is exactly what `also_paused` exists for. The owner has weighed
this: a real installation sets the longest available duration (60 min), so the window is ten
seconds in an hour, and 15 min was only ever used to make experiments run faster.

**The Konnect app's Stop is in the same class** — established 2026-10-07 from Konnect 3.0.6,
whose stop writes byte 3 = `0x40` with no outlets, not `0x00` (`valve_hex.stop_pair` has the
evidence). So pressing Stop in the app within the tolerance of the limit restarts the water
too, exactly as the first-gen screen does, and for the same reason it is accepted. This
integration's own stop stays `0x00` precisely so that Home Assistant is *not* in this class.

**The duration-matching rule is now checked, not assumed.** The controller's Max Shower
Duration is readable from the cloud (`hub-configuration` `systemSettings.maxShowerDuration`,
minutes — also from Konnect 3.0.6), so a mismatch raises a Repairs card rather than relying on
the owner having read this; see `KohlerAnthemCoordinator.async_refresh_duration_issues`.

### The 2026-08-17 reasoning, superseded but kept for the record

**A `0x00` stop is now treated exactly like a `0x40` pause.** The rule above described the
corpus accurately and still does; what it missed is that the valve has a *second* way to end
a session, and it does not pause.

Measured 2026-08-18 (local 2026-08-17 evening), a shower with Endless Shower on and
`maximumRunTime` 900 s on all six outlets:

```text
17:43:07  zone 1 opens                                     <- the session clock starts here
17:58:07  paused 0x40 at 899.88s  -> restarted (1.45s off)
18:13:08  paused 0x40 at 899.91s  -> restarted (2.19s off)
18:28:10  paused 0x40 at 899.76s  -> restarted (1.54s off)
18:43:07  STOPPED 0x00 at 895.47s -> ignored, water stayed off
          3595.02s of flow + 5.18s of restart gaps = 3600.20s wall clock
```

The final leg came up short of its own 900 s limit by exactly the time the water had spent
off during the three restarts. **Mechanism confirmed by the owner: the two devices each run
their own maximum-duration timer, and they signal differently.**

    GCS valve   maximumRunTime, per outlet   900 s here    writes 0x40  (pause)
    Anthem Plus HUB   its own max duration   60 min here   writes 0x00  (stop)

So `0x00` is not a strange valve behaviour — it is the controller stopping the shower with
the same plain stop every HUB-issued stop uses. The HUB's clock keeps running through our
1.2-2.2 s restore gaps, which is why the ceiling arrives mid-leg. The owner was still
showering — zone 2 ran on for another six minutes, untouched, because the HUB times per zone.

**What this costs, stated plainly:** a deliberate stop that lands within
`CUTOFF_TOLERANCE_SECONDS` of an announced limit will now be restarted. Somebody who ends a
shower at 14:50-15:10 by the wall panel gets the water back. Two things still stand between
that and a restart: `LOCAL_WRITE_GRACE_SECONDS` covers every stop Home Assistant itself
issued, and the duration window remains narrow — of the 91 stops in the corpus, none came
closer than 123 s to a limit, and the one that did was this genuine cutoff at 4.53 s. The
journal keeps recording `paused` on every `flow_end`, so the two populations stay separable
in analysis even though the detector no longer separates them.

⚠️ **This does not on its own catch the ceiling at other Max Shower Duration settings.** A
duration must still match an announced `maximumRunTime` within tolerance, and 3600 s only
lands near one because the limit is currently 900 s. At 20 or 25 minutes the ceiling falls
mid-leg and is logged as an ordinary unexplained stop. Closing that needs the session clock
itself — see `docs/gcs/api.md`, "the 60-minute session ceiling".

This module only *reports*. It never sends anything, holds no Home Assistant imports, and is
deliberately separable from whatever a caller chooses to do with the answer.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

_LOGGER = logging.getLogger(__name__)


class Journal(Protocol):
    """The bit of `cutoff_log.CutoffDebugLog` this module needs.

    Declared structurally so this module keeps no import of it — the detector stays
    dependency-free and unit-testable with a list-backed stub.
    """

    def note(self, event: str, **fields: Any) -> None:  # pragma: no cover - protocol
        ...


class _NullJournal:
    """Default journal: discards everything."""

    def note(self, event: str, **fields: Any) -> None:
        return


# How far from `maximumRunTime` a pause may land and still count as the timer firing.
#
# Every cutoff ever measured landed within **1.32 s** of the limit, and all but one within
# 0.35 s. The nearest pause that was *not* a cutoff was 334 s away. So anything from about
# 2 s to 300 s "works" on the evidence, and the number is a judgement about which way to fail.
#
# It fails asymmetrically, so this is deliberately tight rather than generous:
#
# * **Too tight** — a real cutoff is missed, the water stays off, somebody turns it back on.
# * **Too loose** — a deliberate stop is mistaken for a cutoff and the water comes back on by
#   itself, which is the outcome this whole feature exists to avoid causing accidentally.
#
# **Tightened from 10 s to 2 s on 2026-08-19.** The 10 s figure was set against a worst
# observed deviation of 1.32 s — a number from the 2026-08-13/14 flow-experiment and
# valve-reboot era, which is now excluded from the corpus (see `pause_resolution.py`).
#
# Every valve cutoff measured since the corpus floor lands within **0.232 s** of its limit:
#
#     3599.768 s vs 3600   -0.232      case study 1
#      899.918 s vs  900   -0.082      case study 3
#      899.85 / 899.92 / 899.84        case study 5
#      899.79 / 899.78                 case study 6
#
# 2 s is still **8.6x the worst of those**, and these are the detector's own measurements, so
# MQTT delivery jitter is already inside them.
#
# It buys a fivefold reduction in the one accepted risk: pressing off on the first-generation
# touchscreen writes `0x40` on both zones, byte-identical to a preset-driven cutoff, so ending
# a shower within this window of the limit restarts the water. That exposure is now 2 s in
# whatever the duration is, not 10.
#
# The case that keeps it from being looser: **a GCS-only install has no Anthem Plus**, so the
# touchscreen is the primary control surface — and a touchscreen pause writes `0x40`, exactly
# like a cutoff, with no `note_local_write()` guard because Home Assistant did not send it.
# Somebody who knows their shower cuts out at 15 minutes and pauses it at 14:55 is a
# plausible user, not a contrived one, and they must not have the water restarted on them.
#
# ⚠️ Tightening fails in the SAFE direction — a missed cutoff leaves the water off. Loosening
# does not. Do not raise this without measurements from hardware that needs it.
CUTOFF_TOLERANCE_SECONDS = 2.0

# ---------------------------------------------------------------------------
# DIAGNOSIS ONLY — naming the Anthem Plus controller's ceiling in the log
# ---------------------------------------------------------------------------
# ⚠️ Used **exclusively to write a clearer WARNING**. Never a source of limits to fire on, and
# it cannot become one: it is read only inside the branch that has already decided no announced
# limit matched, and that branch always declines. `test_no_limit_guessing.py` and
# `test_controller_limit_hint.py` assert the property from both sides.
#
# Why this is not the removed `MissedCutoffWatcher` in disguise: that inferred a *valve* limit
# from repeated durations and offered to promote it into the firing set. This holds no state,
# looks at one duration, and promotes nothing.
#
# **The rule: a whole number of MINUTES, overshot by a fraction of a second.**
#
# `maxshowerduration` is stored in **minutes** (`docs/hub/local_api.md` §3a, and the cloud's
# `systemSettings.maxShowerDuration` agrees), so the controller's ceiling always lands on a
# minute boundary whatever it is set to. And the
# controller always fires **late, never early** — every measurement, across every setting:
#
#     900 s setting   +0.30 .. +1.25 s   thirteen cutoffs, case studies 2, 3, 7
#     300 s setting   +0.57, +0.78 s     2026-08-19
#     180 s setting   +0.46, +0.64 s     2026-08-19
#
# The valve, by contrast, fires **early** (-0.08 to -0.23 s), so a late-only window separates
# the two devices cleanly. It also excludes the 1800 s preset timer, which fires early at
# 1799.9 s.
#
# ⚠️ **Superseded 2026-08-20: the four-value dropdown is NOT the candidate set.** This started
# life as a fixed `(900, 1800, 2700, 3600)` because the vendor UI only offers 15/30/45/60 min.
# The local API accepts **anything** — 3, 5, 75 and 120 minutes were all written and enforced
# on firmware 2.88 (`docs/hub/local_api.md` §3b) — so enumerating the UI's values would miss
# every custom setting, including the 3- and 5-minute cutoffs measured above.
#
# Measured against the clean corpus: **15 of 15** journalled controller cutoffs match, with
# **one** false positive in 62 stops (a 420.86 s stop, 0.86 s past 7 minutes). A false positive
# costs a log line and nothing else.
CONTROLLER_LATE_WINDOW = (0.2, 2.0)


def suspected_controller_limit(duration: float) -> int | None:
    """The controller Max Shower Duration this `0x00` stop looks like, in seconds, or None.

    Diagnosis only — see the note above. Nothing acts on the result.
    """
    minutes = round(duration / 60)
    if minutes < 1:
        return None
    early, late = CONTROLLER_LATE_WINDOW
    limit = minutes * 60
    return limit if early <= duration - limit <= late else None


# A close is ignored if the integration itself **closed that zone** within this window.
# Without it, stopping the shower from Home Assistant at the limit would be read as the timer
# firing and immediately undone — the integration fighting its own user.
#
# Note "closed that zone", not "wrote to the valve". Both qualifiers were learned the hard
# way on 2026-08-14, from a live shower where the second of two cutoffs was swallowed:
#
#     22:23:11  zone 1 cut at 899.8s -> restored outlets 1, 5      (correct)
#     22:23:57  Home Assistant opens outlet 4 -> zone 2 mask 2->3
#     22:24:11  zone 2 cut at 899.9s -> IGNORED, "we wrote moments ago"   (wrong)
#
# The write at 22:23:57 *opened* an outlet. An opening write cannot cause a close, so it must
# not license ignoring one. Scoping by zone matters for the same reason — adjusting zone 2
# should never blind zone 1. See `note_local_write`.
LOCAL_WRITE_GRACE_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Self-diagnosis: noticing a limit we do not know about
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ZoneReading:
    """What a zone was actually delivering, beyond which outlets were open.

    Captured so the log can answer "what was the shower like before the cut", which the mask
    alone cannot. **Recorded, not yet acted on** — see :attr:`ZoneCutoff.flow_percent`.
    """

    #: 0-100, decoded from the flow byte (byte 2 / 2).
    flow_percent: float
    #: Degrees Fahrenheit. Always °F in this log regardless of the account's display unit,
    #: so two captures from different accounts can be compared without unit archaeology.
    temperature_f: float


@dataclass(frozen=True)
class ZoneCutoff:
    """One zone that the valve just closed on its own timer."""

    zone: int
    #: Seconds the zone had been flowing. Within `CUTOFF_TOLERANCE_SECONDS` of `limit`.
    duration: float
    #: The `maximumRunTime` this matched against.
    limit: int
    #: The outlet mask that was flowing in the instant before the valve cleared it. This is
    #: the only surviving record of what the shower was doing — the cutoff message itself
    #: reports an empty mask.
    mask: int
    #: Flow and temperature in that same instant, or None if the caller did not supply them.
    #:
    #: **Captured for the log only; the restore does not use them yet.** Measured 2026-08-14:
    #: a preset-driven shower running at 82.5% flow was cut and restored at 100%, because the
    #: restore rebuilds its command through `async_apply_valve`, which deliberately does not
    #: inherit flow. Recording the value is step one — it makes the gap visible in
    #: `cutoff_*.jsonl` before any behaviour changes.
    reading: ZoneReading | None = None


@dataclass
class ZoneCutoffDetector:
    """Tracks how long each zone has been flowing and reports timer-driven closes.

    Feed it every valve snapshot with :meth:`update`; it returns the zones that just hit
    their limit. Timing comes from when *this process* saw the zone start flowing, so a
    restart mid-shower loses the start time and that session cannot be judged — which is the
    right failure, since guessing would mean re-opening a valve on no evidence.
    """

    #: Where the decision trail goes. See `cutoff_log.py` for why this is worth having: a
    #: cutoff that *fails to fire* produces no log line anywhere else, and that is the
    #: failure mode this feature actually has.
    journal: Journal = field(default_factory=_NullJournal)
    # zone -> monotonic time it was first seen flowing, or absent if not flowing
    _flowing_since: dict[int, float] = field(default_factory=dict)
    # zone -> the last mask seen while flowing, so a cutoff knows what to put back
    _last_mask: dict[int, int] = field(default_factory=dict)
    # zone -> the last flow/temperature seen while flowing. Same idea as `_last_mask`: the
    # cutoff message has already overwritten them by the time the close is visible.
    _last_reading: dict[int, ZoneReading] = field(default_factory=dict)
    # zone -> monotonic time we last CLOSED it ourselves, to suppress self-inflicted
    # detections. Only closes are recorded; see `note_local_write`.
    _local_close: dict[int, float] = field(default_factory=dict)

    def note_local_write(self, masks: dict[int, int] | None = None) -> None:
        """Record a command we just sent, so a close it causes is not ours to undo.

        `masks` is what was written, per zone. **Only zones written closed (mask 0) are
        recorded**, because only those can explain a close arriving moments later. A write
        that opens outlets cannot, and treating it as though it could is what swallowed a
        real cutoff on 2026-08-14 — the restore after one zone's cutoff, and then the owner
        opening another head, each blinded the detector to the *other* zone's timer.

        Omitting `masks` records every zone, which is the cautious reading: a caller that has
        not said what it wrote might have stopped the shower, and wrongly suppressing means
        the water stays off, while wrongly firing means it comes back on by itself.
        """
        now = time.monotonic()
        if masks is None:
            # Unknown write: assume the worst, which here means assume it was a stop.
            for zone in set(self._flowing_since) | set(self._local_close) | {1, 2}:
                self._local_close[zone] = now
            return
        for zone, mask in masks.items():
            if mask:
                # An opening write. Explicitly drop any earlier suppression for this zone —
                # whatever we closed before, this zone is open again now.
                self._local_close.pop(zone, None)
            else:
                self._local_close[zone] = now

    def note_restore(
        self, masks: dict[int, int], readings: dict[int, ZoneReading] | None = None
    ) -> None:
        """Anchor zones a restore just reopened, so valve silence cannot blind the timer.

        ``masks`` is what the restore wrote, per zone. Only non-zero masks anchor, and a
        zone already being timed is left alone — when the valve's own announcement wins the
        race, that anchor stands. ``readings`` carries the flow/temperature the restore
        replayed, so a later cutoff's record is as complete as the first one's.

        Why this exists: the valve does not reliably announce a restored zone. Across the
        18 restores in the clean corpus, 17 drew a ``GCS_SOLO_STS`` within 0.06–1.08 s —
        and one drew nothing for **176.77 s** while the water demonstrably ran (2026-08-19
        23:48, owner-confirmed; `docs/architecture.md` "Silence is not state"). Anchoring
        in :meth:`update` alone put that zone's ``flow_start`` 176.77 s late, which at the
        next cutoff would have measured ~723 s against the 900 s limit and, at
        ``CUTOFF_TOLERANCE_SECONDS = 2``, ignored it: no restore, water off, nothing in the
        log saying why.

        Restores only — deliberately not every opening write. A restore reopens water the
        valve was demonstrably running a second earlier, so the write taking effect is the
        overwhelmingly probable outcome; an ordinary open can be accepted by the cloud and
        dropped by the valve, and anchoring one that never ran would hold a false clock —
        against which a *manual* pause landing within tolerance of a limit would read as a
        cutoff and restart water nobody is running. :meth:`update` still corrects both
        directions: any snapshot showing the zone empty pops the anchor (the `0x40` pause
        resolution guarantees such a snapshot within ~120 s of a failed restore), and one
        showing it flowing keeps whichever anchor came first.
        """
        now = time.monotonic()
        readings = readings or {}
        for zone, mask in masks.items():
            if not mask or zone in self._flowing_since:
                continue
            self._flowing_since[zone] = now
            self._last_mask[zone] = mask
            reading = readings.get(zone)
            if reading is not None:
                self._last_reading[zone] = reading
            self.journal.note(
                "anchor",
                zone=zone,
                mask=mask,
                reason="restore write; the valve may not republish this zone",
            )

    def flowing_for(self, zone: int) -> float | None:
        """Seconds this zone has been flowing, or None if it is not, or is not being timed.

        The same clock the cutoff decision uses, so anything displaying it is showing what
        the detector actually believes rather than a second, parallel measurement that could
        disagree. None after a reconnect until the zone next starts — see :meth:`forget`.
        """
        started = self._flowing_since.get(zone)
        return None if started is None else time.monotonic() - started

    def forget(self) -> None:
        """Drop all tracking — for a reconnect, where the gap makes durations meaningless."""
        if self._flowing_since:
            self.journal.note(
                "forget",
                zones=sorted(self._flowing_since),
                reason="reconnect — durations across the gap are meaningless",
            )
        self._flowing_since.clear()
        self._last_mask.clear()
        self._last_reading.clear()

    def update(
        self,
        masks: dict[int, int],
        paused: dict[int, bool],
        limits: dict[int, tuple[int, ...]],
        readings: dict[int, ZoneReading] | None = None,
    ) -> list[ZoneCutoff]:
        """Apply a valve snapshot. Returns the zones whose run-time limit just fired.

        `masks` maps zone number to its current outlet mask, `paused` to whether that zone
        carries the `0x40` pause flag, and `limits` to the distinct `maximumRunTime` values
        configured for the outlets in that zone (empty where the valve has not said).

        A zone counts as *flowing* when it has a non-empty mask and is not paused. A cutoff
        is a flowing zone that stops flowing **with the `0x40` pause flag set**, having
        flowed for one of its limits.

        **The pause flag is required**, restored 2026-08-18 at the owner's instruction after
        five case studies showed the `0x00` that had motivated dropping it was the Anthem
        Plus controller's own ceiling, fired because the two Max Shower Durations were set to
        different values. Only the valve cuts with `0x40`, and it fires marginally early, so
        with the durations matched its pause always arrives first and is always the
        actionable signal. A `0x00` at a matching duration is declined, logged at WARNING
        naming the likely cause, and journalled with its own `reason`. The full reasoning and
        what it costs are in this module's docstring; `paused` reaches the journal on every
        `flow_end` either way, so the two populations stay separable in analysis.
        """
        now = time.monotonic()
        fired: list[ZoneCutoff] = []
        readings = readings or {}

        def described(zone: int) -> dict[str, Any]:
            """Flow and temperature as journal fields, or nothing if unknown."""
            reading = self._last_reading.get(zone) or readings.get(zone)
            if reading is None:
                return {}
            return {
                "flow_percent": reading.flow_percent,
                "temperature_f": reading.temperature_f,
            }

        def suppressed(zone: int) -> bool:
            """Did *we* close this zone recently enough to explain its close?"""
            closed_at = self._local_close.get(zone)
            return (
                closed_at is not None and (now - closed_at) < LOCAL_WRITE_GRACE_SECONDS
            )

        for zone, mask in masks.items():
            is_paused = paused.get(zone, False)
            flowing = bool(mask) and not is_paused

            if flowing:
                reading = readings.get(zone)
                if zone not in self._flowing_since:
                    self._flowing_since[zone] = now
                    if reading is not None:
                        self._last_reading[zone] = reading
                    self.journal.note(
                        "flow_start",
                        zone=zone,
                        mask=mask,
                        limits=list(limits.get(zone, ())),
                        **described(zone),
                    )
                else:
                    was_reading = self._last_reading.get(zone)
                    if reading is not None:
                        self._last_reading[zone] = reading
                    if self._last_mask.get(zone) != mask:
                        # Worth a line of its own: an outlet change mid-session is exactly
                        # what the old per-outlet detector reset its clock on, and the whole
                        # point of this one is that it does not.
                        self.journal.note(
                            "mask_change",
                            zone=zone,
                            mask=mask,
                            was=self._last_mask.get(zone),
                            flowing_for=now - self._flowing_since[zone],
                            **described(zone),
                        )
                    elif reading is not None and reading != was_reading:
                        # Flow or temperature moved without the outlets changing — the
                        # touchscreen adjusting a dial mid-shower looks exactly like this,
                        # and it is what decides whether a restore should replay the
                        # original settings or the latest ones.
                        self.journal.note(
                            "setting_change",
                            zone=zone,
                            mask=mask,
                            flowing_for=now - self._flowing_since[zone],
                            was_flow_percent=(
                                was_reading.flow_percent if was_reading else None
                            ),
                            was_temperature_f=(
                                was_reading.temperature_f if was_reading else None
                            ),
                            **described(zone),
                        )
                self._last_mask[zone] = mask
                continue

            started = self._flowing_since.pop(zone, None)
            was_running = self._last_mask.pop(zone, 0)
            # Popped last, so `described()` above still sees it while building the record.
            was_reading = self._last_reading.get(zone)
            if started is None:
                self._last_reading.pop(zone, None)
                continue

            duration = now - started
            candidates = limits.get(zone, ())
            record: dict[str, Any] = {
                "zone": zone,
                "duration": duration,
                "limits": list(candidates),
                "mask": was_running,
                "paused": is_paused,
                **described(zone),
            }
            self._last_reading.pop(zone, None)

            def stop(reason: str, _record: dict = record, **extra: Any) -> None:
                """Record a close this detector is not claiming, and why.

                Recording only. A close that matches no announced `maximumRunTime` is left
                alone — see the module docstring on why nothing is inferred from durations.

                `record` is bound as a default argument rather than captured from the
                enclosing scope. Every call happens inside the same loop iteration that
                defines it, so closing over it would be correct today — but a closure over a
                loop variable is one refactor away from reading the wrong zone's record, and
                the failure would be a mislabelled journal entry rather than a crash. Bound
                explicitly so it cannot drift.
                """
                self.journal.note(
                    "flow_end", **_record, verdict="ignored", reason=reason, **extra
                )

            if not candidates:
                # The valve has never announced a limit for this zone's outlets, so there is
                # nothing to compare against. Silence beats a guess when the consequence is
                # running water.
                stop("no maximumRunTime known for this zone's outlets")
                continue
            match = next(
                (
                    limit
                    for limit in candidates
                    if abs(duration - limit) <= CUTOFF_TOLERANCE_SECONDS
                ),
                None,
            )
            if match is None:
                # Nothing announced matches. Before writing this off as an ordinary stop,
                # check whether it looks like the *other* product's ceiling — the one this
                # feature cannot act on and which, until 2026-08-19, ended showers with no
                # explanation anywhere in the log. See `docs/case_studies/conclusions.md` B4.
                suspected = None if is_paused else suspected_controller_limit(duration)
                # WARNING only when the controller PREEMPTED the valve — suspected below
                # every valve limit means the hub's Max Shower Duration is set lower than
                # the valve's, Endless Shower is silently defeated, and a fix exists on the
                # side this integration can actually write (the valve's `maximumRunTime`).
                # The other direction — a minute-boundary stop past the valve's limit, the
                # controller sweeping a restored session — is journalled below but not
                # warned: nothing HA-side can change the hub's number, so a warning would
                # only advise the impossible. Owner's decision 2026-08-22; the unconditional
                # "match the durations" nags at startup and toggle time went with it.
                if suspected is not None and suspected < min(candidates):
                    _LOGGER.warning(
                        "Zone %s STOPPED (0x00) after %.1f s. That matches no limit the "
                        "Anthem valve announced, but it is %.2f s past %d minutes — a whole "
                        "number of minutes, which is the shape of the Anthem Plus "
                        "controller's Max Shower Duration (settable to any minute value), "
                        "and the controller ends a shower exactly like this, a fraction of "
                        "a second late. Endless Shower cannot restart it: only the valve's "
                        "cutoff carries the pause flag. Set the controller's Max Shower "
                        "Duration to the same value as the valve's (%s) so the valve cuts "
                        "first",
                        zone,
                        duration,
                        duration - suspected,
                        suspected // 60,
                        ", ".join(f"{c} s" for c in candidates),
                    )
                stop(
                    "duration is not within %.0fs of any limit"
                    % CUTOFF_TOLERANCE_SECONDS,
                    off_by=min(abs(duration - limit) for limit in candidates),
                    **(
                        {"controller_limit_suspected": suspected}
                        if suspected is not None
                        else {}
                    ),
                )
                continue
            if not is_paused:
                # **The pause flag is required again, 2026-08-18.** See the module docstring
                # for the full reasoning; in short, the GCS valve is the only device that
                # cuts with `0x40`, it fires a fraction of a second EARLY, and the Anthem
                # Plus controller fires a fraction of a second LATE — so when the two maximum
                # durations are set to the same value the valve's pause always arrives first
                # and is always the signal worth acting on. A `0x00` at a matching duration
                # is then either the controller's redundant follow-up to a cut we have
                # already restored, or somebody deliberately ending their shower.
                #
                # Logged at WARNING rather than swallowed: if the two durations are ever set
                # to DIFFERENT values, the controller can fire alone and this line is the
                # only thing that will say why the shower did not come back.
                _LOGGER.warning(
                    "Zone %s STOPPED (0x00) after %.0f s, which matches its %s s limit, but a "
                    "cutoff must carry the pause flag (0x40) and this did not. Not restarting. "
                    "If the shower ended by itself, check that the Anthem valve's Max Shower "
                    "Duration and the Anthem Plus controller's are set to the SAME value",
                    zone,
                    duration,
                    match,
                )
                stop(
                    "stopped (0x00) at a limit rather than paused (0x40) — a cutoff must "
                    "carry the pause flag",
                    matched=match,
                )
                continue
            if suppressed(zone):
                _LOGGER.debug(
                    "Zone %s closed at its %ss limit, but Home Assistant closed that zone "
                    "moments ago — treating it as our own stop",
                    zone,
                    match,
                )
                stop(
                    "Home Assistant closed this zone within the %.0fs grace window"
                    % LOCAL_WRITE_GRACE_SECONDS
                )
                continue
            self.journal.note("flow_end", **record, verdict="cutoff", matched=match)
            fired.append(
                ZoneCutoff(
                    zone=zone,
                    duration=duration,
                    limit=match,
                    mask=was_running,
                    reading=was_reading,
                )
            )

        return fired
