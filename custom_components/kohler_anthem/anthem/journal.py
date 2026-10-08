"""A JSONL debug journal written beside the raw MQTT capture. Off by default.

One writer, parameterised by file prefix and README, so a journal is a few lines to add.
Today it serves one: the **warmup journal** (`warmup_*.jsonl`), which records every change
to the valve's warmup mode with the traffic around it — see `WARMUP_README` below. Until
2026-10-08 it also wrote the run-time cutoff decision log for Endless Shower, which has
been removed.

Records stamp `ts` as ISO-8601 UTC with a `Z` suffix, the same clock and format as
`mqtt_raw_*.jsonl` in the same directory, so the two interleave by sorting on it.

Switching it on:

* **Permanently** — the journal's `ENABLE_*` constant in `const.py`
  (`ENABLE_WARMUP_DEBUG_LOG`).
* **From the UI, no restart** — Developer Tools → Actions → `logger.set_level`:

      action: logger.set_level
      data:
        custom_components.kohler_anthem.anthem.journal: debug

Volume is a handful of lines per shower. Files are one per Home Assistant run, each capped
at `max_bytes`, and pruned to the newest `keep_files`.

Thread safety: callers run on the event loop, but `note()` is cheap and the lock makes it
safe from the paho thread too, matching `RawMqttLog`.
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


class DebugJournal:
    """Append diagnostic records to a JSONL file, when switched on."""

    def __init__(
        self,
        directory: str,
        *,
        forced: bool = False,
        keep_files: int | None = DEFAULT_KEEP_FILES,
        prefix: str,
        readme: str,
        readme_fields: dict[str, Any] | None = None,
        label: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """`prefix` names the files and scopes pruning; `readme` is the note left beside them.

        Pruning matches on the prefix, so two journals in one directory never delete each
        other's files. `readme` is a `str.format` template; it may use `{keep_desc}` and
        any key in `readme_fields`.
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

        Unlike `RawMqttLog`, this log is written from the **event loop**, where its callers
        run. Opening a file and creating a directory are blocking calls that must not
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
        # gain.
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
                    self._readme.format(
                        **self._readme_fields,
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
                _LOGGER.warning("%s could not start a new file: %s", self._label, err)
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
