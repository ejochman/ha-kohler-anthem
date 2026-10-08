"""Decision log for the run-time cutoff detector — why it fired, and why it didn't.

# =====================================================================
# CUTOFF DEBUG LOG — diagnostic, OFF BY DEFAULT, safe to delete wholesale
# =====================================================================
#
# Searching for this later? The markers are:
#
#   grep -rn "CUTOFF DEBUG LOG" custom_components/kohler_anthem/
#
# That finds this module, the constants in `const.py`, the call sites in
# `anthem/runtime_cutoff.py` and `coordinator.py`, and the roll button.
# Removing those blocks removes the feature completely.
#
# =====================================================================

The cutoff detector is the one piece of this integration that turns water back **on** by
itself, and its inputs are invisible after the fact: durations are measured against a
monotonic clock in memory, and the valve destroys the outlet mask in the same message that
reports the close. When it misbehaves, `home-assistant.log` shows only the outcome — a
WARNING if it fired, and *nothing at all* if it should have fired and didn't. Silence is the
failure mode that matters, and silence is exactly what a normal log cannot record.

So this writes the detector's whole decision trail: every zone that starts or stops flowing,
every close it evaluated, the duration and limits it compared, and the verdict with a reason.

## Reading it alongside the raw MQTT capture

Both logs live in the **same directory** and stamp `ts` in the **same format** — ISO-8601
UTC with a `Z` suffix, taken from the same clock. So the two interleave directly:

    cd /config/kohler_anthem_raw
    jq -c '{ts, src:"mqtt", code:(.payload|fromjson|.data.code)}' mqtt_raw_*.jsonl \
      > /tmp/a.jsonl
    jq -c '{ts, src:"cutoff", event, zone, verdict, reason}' cutoff_*.jsonl > /tmp/b.jsonl
    sort -m -t'"' -k4 /tmp/a.jsonl /tmp/b.jsonl | less

The pairing to look for is a `GCS_SOLO_STS` in the raw log whose valve word carries `0x40`,
and the `flow_end` record written in the same instant. If the raw log shows the pause and
the cutoff log shows `verdict: "ignored"`, the `reason` and `duration` fields say precisely
why — which is the question that took a full capture corpus to answer the first time.

Switching it on works exactly like the raw capture, and independently of it:

* **From the UI, no restart** — Developer Tools → Actions → `logger.set_level`, YAML mode:

      action: logger.set_level
      data:
        custom_components.kohler_anthem.anthem.cutoff_log: debug

* **Permanently** — set `ENABLE_CUTOFF_DEBUG_LOG = True` in `const.py`.

Volume is low — a handful of lines per shower, versus one per MQTT message — so leaving it
on across days costs almost nothing. Files are one per Home Assistant run, pruned to the
newest `keep_files`.

Thread safety: the detector runs on the event loop, but `note()` is cheap and the lock makes
it safe from the paho thread too, matching `RawMqttLog`.
"""

from __future__ import annotations

import binascii
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# This module's own logger doubles as the runtime switch, read as a flag rather than used
# for output — see `RawMqttLog.enabled` for why `.level` and not `isEnabledFor()`.
_SWITCH_LOGGER = _LOGGER

#: None means no limit — every log file is kept forever. This is the default; the
#: directory is diagnostic output the owner wants to keep, not a rotating buffer.
# Matches `RAW_MQTT_LOG_MAX_BYTES`. See `_max_bytes` for why a cap exists at all.
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_KEEP_FILES: int | None = None

_README = """\
Run-time cutoff decision log — written by the kohler_anthem integration.

Each .jsonl file is one Home Assistant run, one JSON object per line. Every
record has:

    ts       ISO-8601 UTC — the SAME clock and format as mqtt_raw_*.jsonl in
             this directory, so the two files interleave by sorting on it
    event    flow_start | flow_end | restore | arm | anchor

`anchor` (since 2026-08-22) is a restore starting a zone's clock itself: the valve
does not reliably republish a restored zone (176.77 s of silence in the measured
case), so the clock now starts at the restore write rather than waiting for a
message. A flow_start for that zone will NOT follow — the anchor took its place.

`flow_end` is the interesting one. It carries the detector's full reasoning:

    zone       the valve zone that stopped flowing
    duration   seconds it had been flowing, monotonic
    limits     the maximumRunTime values it was compared against
    mask       the outlet mask that was flowing just before it stopped
    paused     whether the zone carried the 0x40 pause flag
    verdict    "cutoff" or "ignored"
    reason     why, when ignored

`flow_start`, `mask_change`, `setting_change` and `flow_end` also carry what the
shower was actually delivering at that moment:

    flow_percent     0-100, from the valve word
    temperature_f    degrees FAHRENHEIT always, whatever the account displays,
                     so captures from different accounts stay comparable

`setting_change` fires when flow or temperature moved while the outlets did not —
which is what the touchscreen adjusting a dial mid-shower looks like.

On a `restore`, compare `was_flow_percent` against `writing_flow_percent`.
`flow_preserved: false` means at least one cut zone had no captured reading, so it
came back at `DEFAULT_FLOW_PERCENT` instead of its own prior value — the fallback,
not the normal case. `true` means every cut zone's flow was replayed exactly as it
was running before the cut.

To correlate with the raw MQTT capture, look for the GCS_SOLO_STS message
whose valve word has 0x40 in byte 3 at the same `ts` as a flow_end record.

This log is OFF by default. Right now it is on because:

{why}

{keep_desc} Delete them freely — pure diagnostics.
"""

_WHY_FORCED = """\
    ENABLE_CUTOFF_DEBUG_LOG = True

in the integration's const.py, which pins it on across restarts.

TO STOP IT: set that constant back to False and restart Home Assistant.
`logger.set_level` will NOT turn it off while the constant is True."""

_WHY_LOGGER = """\
    custom_components.kohler_anthem.anthem.cutoff_log

is set to debug. To stop it, call `logger.set_level` with that same logger
name set to `info` — no restart needed. This does not survive a restart; set
ENABLE_CUTOFF_DEBUG_LOG = True in const.py to keep it on."""


WARMUP_README = """\
Kohler Anthem — warmup journal
==================================

These `warmup_*.jsonl` files were built to answer what was once this project's oldest open
question: **what keeps setting the Anthem valve's warmup mode back to `warmUpDisabled`?**

SOLVED, 2026-08-21 — it is the Anthem Plus hub's web UI. Ordinary signed-in use of it (a
PIN sign-in alone is enough; so is an SD-card music scan) runs a fixed routine that writes
the valve's warmup mode, and the value written is `warmUpDisabled` every time — a constant
in the hub's login/UI routine, not a stored setting: no hub surface, local or cloud, holds
the literal, and `get_valve_settings.warmupmode` read `on` during the very logins that
pushed the disable. Reproduced live six times in one day (four UI actions, three with
deliberately empty 120 s before-windows, plus two PIN-probe logins); auto-restore recovered
every one in 63-69 s. The durable record with both evidence tables is `docs/gcs/api.md`
§3h.

The journal stays on as the watchdog: it is what proved the mechanism, it verifies every
auto-restore end to end, and it is what would first notice a second, different writer.

{keep_desc}

Records
-------
  baseline          the first line of every file: the mode in force when the journal opened,
                    read over REST at setup, plus whether auto-restore is armed and what it
                    would restore to. The valve never volunteers its mode on connect — over
                    all 74 raw captures the first `GCS_WARM_STS` in a file lands between
                    137 s and 7 h in — so without this line a file has no idea what it
                    started from, and cannot say how long the mode had been in force.
  mode              the mode moved. `before` -> `after`, `ours` (did we write it), and
                    `source`: `mqtt` if the valve announced it, `rest` if a reseed found it
                    already changed. A `rest` one means the move happened while the stream
                    was down, so it can never carry a `before_window` — but since 2026-08-22
                    it carries `restoring` and a discovered disable is restored through the
                    same machinery as an announced one (a hub sign-in during an outage was
                    the hole). Journals before that date carry `restored: false` here
                    instead: recorded, deliberately never restored.
  announced         the valve restated a mode it was already in. Carries `mode` and `ours`.
                    No decision attached — 28 of the 43 announcements in the raw corpus are
                    these. ⚠️ **Check `ours` before reading one as the valve volunteering.**
                    Setting the mode from the dropdown lands here rather than on a `mode`
                    record: the write reads itself back over REST immediately, so our state
                    has already moved by the time the valve's echo arrives ~3.4 s later.
  disabled          the mode went to `warmUpDisabled`. Carries `ours` (did we write it),
                    `restoring` (is auto-restore acting), and `before_window`: every MQTT
                    message seen in the {before}s leading up to it.
  context           written {after}s later, holding `after_window` — the messages that
                    followed. `SYSTEM_STS: SYSTEM_READY` appearing here is the signature seen
                    7-9 s after two of the four known disables. The window deliberately
                    closes before auto-restore could act, so this record never contains our
                    own write.
  restore*          what auto-restore did: scheduled, skipped, done, or failed.

Reading them
------------
Every record has an ISO-8601 UTC `ts`, the same clock as the raw capture beside it, so the two
interleave:

    jq -c '{{ts, src:"warmup", event, mode, before, after, ours}}' warmup_*.jsonl > /tmp/a.jsonl
    jq -c '{{ts, src:"raw", topic}}' mqtt_raw_*.jsonl > /tmp/b.jsonl
    sort -m /tmp/a.jsonl /tmp/b.jsonl

What a hub-UI disable looks like
--------------------------------
The machine fingerprint, constant to the tenth of a second across eight days of instances:
the `disabled` record, then the hub's five-snapshot burst at +2.6-3.2 s, then
`READ_GCS_EXPERIENCE_STS` at +4.7-5.0 s. The write itself never appears on this MQTT
channel — only the valve's echo does. A `disabled` record WITHOUT that follower pattern
would be news: a different writer than the one identified.

⚠️ Absence of a message means "nothing was pushed", never "nothing happened" — MQTT here is
the Konnect app's UI channel, not device-to-device traffic. See docs/case_studies/intro.md.

Auto-restore and this journal
-----------------------------
A single disable is recorded identically whether the Warmup Auto-Restore switch is on or off:
both windows close before a restore could fire. The disable cannot be prevented from outside
the hub's firmware, so **auto-restore on is the standing mitigation** — every hub web UI
sign-in will disable warmup, and the restore puts it back a minute later. Our own writes stay
identifiable in the records (`ours: true`, and the `restore*` events carry timestamps).


Turning it off
--------------
Set ENABLE_WARMUP_DEBUG_LOG = False in const.py and restart Home Assistant Core.
"""


class CutoffDebugLog:
    """Append cutoff-detector decisions to a JSONL file, when switched on."""

    def __init__(
        self,
        directory: str,
        *,
        forced: bool = False,
        keep_files: int | None = DEFAULT_KEEP_FILES,
        prefix: str = "cutoff",
        readme: str | None = None,
        readme_fields: dict[str, Any] | None = None,
        label: str = "Cutoff debug log",
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """`prefix` names the files and scopes pruning; `readme` is the note left beside them.

        Parameterised 2026-08-20 so the warmup journal can reuse this writer rather than
        copy it. Pruning matches on the prefix, so two journals in one directory never
        delete each other's files.
        """
        self._directory = directory
        self._forced = forced
        self._keep_files = keep_files
        self._prefix = prefix
        self._readme = readme
        self._readme_fields = readme_fields or {}
        self._label = label
        # **A single file must not grow without bound.** This writer had no size cap at all
        # until 2026-09-10, while `RawMqttLog` beside it has always rolled at 8 MB — so a
        # busy or misbehaving valve could grow one journal indefinitely on what is usually
        # an SD card. Volume is normally a handful of lines per shower; the cap is a
        # backstop, not an expected path.
        self._max_bytes = max_bytes
        self._written = 0
        self._lock = threading.Lock()
        self._handle: Any = None
        self._path: str | None = None
        self._announced = False
        # Set when a record arrives with no file open. See `wants_open`.
        self._wants_open = False

    @property
    def enabled(self) -> bool:
        """True when the log is switched on, by either mechanism."""
        return self._forced or _SWITCH_LOGGER.level == logging.DEBUG

    @property
    def path(self) -> str | None:
        """The file currently being written, or None when not logging."""
        return self._path

    @property
    def wants_open(self) -> bool:
        """True when a record arrived with no file open, so :meth:`prepare` should be called.

        Unlike `RawMqttLog`, this log is written from the **event loop** — the detector runs
        there. Opening a file and creating a directory are blocking calls that must not
        happen on it, so `note()` never opens one; it raises this flag instead and the caller
        schedules `prepare()` in an executor. The cost is that the first record after
        switching capture on mid-session is dropped, which matters far less than the flag it
        replaces.
        """
        return self._wants_open

    def prepare(self) -> None:
        """Open an empty file now, if switched on, so it is visibly working.

        Blocking file I/O: call it from an executor, not the event loop.
        """
        if not self.enabled:
            return
        with self._lock:
            self._wants_open = False
            if self._handle is None:
                try:
                    self._open_locked()
                except OSError as err:
                    _LOGGER.warning("%s could not open a file: %s", self._label, err)

    def note(self, event: str, **fields: Any) -> None:
        """Record one decision or transition. Cheap no-op when switched off."""
        if not self.enabled:
            if self._handle is not None:
                self.close()
            return

        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "event": event,
        }
        # Rounded on the way in: these are seconds measured off a monotonic clock, and
        # sixteen significant figures of float noise makes the log harder to read for no
        # gain. Two decimals still resolves the 0.2 s jitter the tolerance is sized against.
        for key, value in fields.items():
            record[key] = round(value, 2) if isinstance(value, float) else value

        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return

        with self._lock:
            if self._handle is None:
                # No file yet, and opening one here would block the event loop. Ask for a
                # prepare() instead; this record is lost and the next one lands.
                self._wants_open = True
                return
            encoded = line + "\n"
            if self._written >= self._max_bytes:
                # Roll rather than truncate: the older records are the ones a bug report
                # needs, and `_open_locked` prunes by the same prefix.
                self._open_locked()
                if self._handle is None:
                    return
            try:
                self._handle.write(encoded)
                self._handle.flush()
                self._written += len(encoded.encode("utf-8"))
            except OSError as err:
                # A diagnostic must never take the integration down with it.
                _LOGGER.warning("%s write failed, disabling: %s", self._label, err)
                self._close_locked()
                self._forced = False

    def _open_locked(self) -> None:
        self._close_locked()
        self._written = 0
        os.makedirs(self._directory, exist_ok=True)
        self._write_readme()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        suffix = binascii.hexlify(os.urandom(4)).decode("ascii")
        self._path = os.path.join(
            self._directory, f"{self._prefix}_{stamp}Z_{os.getpid()}_{suffix}.jsonl"
        )
        self._handle = open(self._path, "a", encoding="utf-8")  # noqa: SIM115 - handle outlives this call; closed by close()
        self._prune()
        if not self._announced:
            self._announced = True
            _LOGGER.info("%s is ON, writing to %s", self._label, self._path)

    def _write_readme(self) -> None:
        """Leave a note saying what these files are and how to stop them."""
        try:
            with open(
                os.path.join(self._directory, f"README-{self._prefix}.txt"),
                "w",
                encoding="utf-8",
            ) as fh:
                fh.write(
                    (self._readme or _README).format(
                        **self._readme_fields,
                        why=_WHY_FORCED if self._forced else _WHY_LOGGER,
                        keep_desc=(
                            "No limit on the number of files — every one is kept."
                            if self._keep_files is None
                            else f"Only the newest {self._keep_files} are kept."
                        ),
                    )
                )
        except OSError:  # pragma: no cover - the log still works without it
            pass

    def _prune(self) -> None:
        """Keep the newest `keep_files` logs so the directory stays bounded.

        A no-op when `keep_files` is None — unlimited is the default, and the owner wants
        this directory to hold everything.
        """
        if self._keep_files is None:
            return
        try:
            logs = sorted(
                (
                    os.path.join(self._directory, name)
                    for name in os.listdir(self._directory)
                    if name.startswith(f"{self._prefix}_") and name.endswith(".jsonl")
                ),
                key=os.path.getmtime,
            )
        except OSError:  # pragma: no cover
            return
        for stale in logs[: max(0, len(logs) - self._keep_files)]:
            try:
                os.remove(stale)
            except OSError:  # pragma: no cover
                pass

    def roll(self) -> str | None:
        """Start a new file immediately. Returns its path, or None if the log is off.

        Rolled together with the raw capture so a pair of files always covers the same
        experiment — matching them up afterwards by timestamp is the bookkeeping this
        avoids.

        Blocking file I/O — call it from an executor, not the event loop.
        """
        if not self.enabled:
            return None
        with self._lock:
            self._close_locked()
            try:
                self._open_locked()
            except OSError as err:
                _LOGGER.warning("Could not start a new cutoff debug log: %s", err)
                return None
            return self._path

    def close(self) -> None:
        """Close the current file, if one is open."""
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:  # pragma: no cover
                pass
            if self._announced:
                _LOGGER.info("%s is OFF (%s)", self._label, self._path)
                self._announced = False
        self._handle = None
        self._path = None
