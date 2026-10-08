"""Consumer-side raw MQTT capture, keyed to a switch — the "Report Log".

# =====================================================================
# REPORT LOG — off by default, one file per switch-on, survives restarts
# =====================================================================

**Why this exists beside `raw_log.py`, which also captures raw MQTT.** The two serve
different people and different moments:

* `raw_log.py` is the *development* evidence machine: pinned on via `const.py` during
  protocol work, one file per Home Assistant run, written to `/config/kohler_anthem_raw/`
  beside the cutoff and warmup journals it interleaves with.
* This one is the *user's* capture: a switch on the device page ("Report Log") that someone
  flips on to document a bug — or a healthy run — and off when done, producing **one file
  per episode** they can attach to a GitHub issue.

The episode is the unit, not the Home Assistant run:

* **Switch on → a new file**, named for the moment it was enabled.
* **Home Assistant restarting does not split the episode** — the coordinator persists the
  episode name in the config entry options and re-attaches to the *same* file in append
  mode after the restart. A capture of "it breaks when I restart HA" must not lose the
  interesting part to the restart itself.
* **Switch off → the episode ends.** The next switch-on starts a fresh file.

Record format is `raw_log.format_record` — byte-compatible with the development capture, so
every existing tool reads both.

Size is bounded per file, not per episode: a file that reaches ``max_bytes`` rolls to a
continuation part (``<episode>_p2.jsonl``, ``_p3``, …) sharing the episode stem, so one
runaway episode cannot eat the disk in a single unmanageable file and a restart's re-attach
lands on the latest part.

⚠️ **The directory lives inside the integration folder** (``custom_components/
kohler_anthem/reports/``) — the owner's choice, so reports sit with the integration
they describe. Two consequences, both accepted: a HACS update or reinstall **replaces the
integration folder and deletes any reports still in it** (move files out before updating if
they matter), and on the development install the directory is gitignored.

Thread safety: paho calls `write()` on its network thread; `start`/`resume`/`stop` run in
an executor. The lock covers all file state, same pattern as `raw_log.py`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any

from .raw_log import format_record

_LOGGER = logging.getLogger(__name__)

# Episode names this class generates: `report_` plus a UTC stamp. Anything else did not
# come from `start()` — see `resume`.
_SAFE_STEM = re.compile(r"report_\d{8}T\d{6}Z")

DEFAULT_MAX_BYTES = 8 * 1024 * 1024

_PART_RE = re.compile(r"_p(\d+)\.jsonl$")

_README = """\
Kohler Anthem — report captures
====================================

Each report_*.jsonl file here is one capture episode of the raw Kohler MQTT
stream, recorded because the "Report Log" switch on the device page was on.
One file per switch-on; a Home Assistant restart continues the same file; a
file that reaches {max_mb} MB continues in _p2, _p3, ... parts.

One JSON object per line:

    ts           ISO-8601 UTC, when the message arrived
    topic        the MQTT topic
    payload      the payload text exactly as received (undecoded)
    payload_b64  present INSTEAD of payload when the bytes were not UTF-8
    qos, retain  from the MQTT message

Attach these files to a GitHub issue to document a bug — or a healthy run on
hardware the integration has never been verified against.

⚠️ Before sharing, know that these files contain your device identifiers and
show when your shower was used. Review anything you'd rather not publish.

⚠️ This folder is inside the integration itself, so updating or reinstalling
the integration DELETES it. Move files you want to keep somewhere else first.

Turning the switch off ends the capture; the files stay until you delete them
(or an update does).
"""


class ReportLog:
    """One raw-capture file per switch-on episode, append-through-restart.

    The coordinator owns the episode lifecycle: `start()` returns the episode name for it
    to persist, `resume(name)` re-attaches after a restart, `stop()` ends the episode, and
    `close()` merely releases the file handle at unload without ending the episode (the
    options key, not this object, is what says an episode is in force).
    """

    def __init__(self, directory: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._directory = directory
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._handle = None
        self._written = 0
        self._path: str | None = None
        self._stem: str | None = None
        # Set when `note()` had a record but no open file. See `wants_open`.
        self._wants_open = False

    @property
    def active(self) -> bool:
        """Whether an episode is being written by this object right now."""
        return self._stem is not None

    @property
    def wants_open(self) -> bool:
        """True when a decision arrived with no file open, so `prepare()` should be called.

        `write()` is called from paho's network thread, where opening a file is fine.
        `note()` is called from the **event loop**, where it is not — so `note()` never
        opens one and raises this instead, exactly as `DebugJournal` does. The caller
        schedules `prepare()` in an executor; the record that raised the flag is lost and
        the next one lands.
        """
        return self._wants_open

    def prepare(self) -> None:
        """Open the episode's file. **Blocking — call from an executor.**"""
        with self._lock:
            self._wants_open = False
            if self._stem is None or self._handle is not None:
                return
            try:
                self._open_locked()
            except OSError as err:  # pragma: no cover - defensive
                _LOGGER.warning("Report log could not open: %s", err)

    @property
    def path(self) -> str | None:
        """The file currently being written, or None."""
        return self._path

    def start(self) -> str:
        """Begin a new episode. Returns the episode name for the caller to persist.

        Opens the file immediately rather than waiting for the first message — this stream
        has been silent for 11.9 hours, and someone who just flipped the switch deserves a
        file they can see. Blocking I/O: call from an executor.
        """
        stem = "report_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        with self._lock:
            self._close_locked()
            self._stem = stem
            try:
                self._open_locked()
            except OSError as err:
                _LOGGER.warning("Report log could not open a file: %s", err)
        _LOGGER.info("Report log ON — new episode %s", stem)
        return stem

    def resume(self, stem: str) -> None:
        """Re-attach to a persisted episode after a restart, appending to its last part.

        Blocking I/O: call from an executor. A vanished file (user deleted it mid-episode)
        is simply recreated by the append open — the episode name is the identity.

        The stem is only ever written by `start()` from its own timestamp, so a hostile
        value means the config entry has already been edited — at which point far more is
        available than this. Validated anyway because the value round-trips through a file
        an operator may hand-edit, and `_part_path` joins it straight onto the directory:
        a stem carrying `..` or a separator would write outside the log folder.
        """
        if not _SAFE_STEM.fullmatch(stem):
            _LOGGER.warning(
                "Ignoring a report-log episode name that is not one we could have "
                "written: %r. Starting a new episode instead.",
                stem,
            )
            self.start()
            return
        with self._lock:
            self._close_locked()
            self._stem = stem
            try:
                self._open_locked()
            except OSError as err:
                _LOGGER.warning("Report log could not reopen %s: %s", stem, err)
        _LOGGER.info("Report log resumed episode %s after restart", stem)

    def stop(self) -> None:
        """End the episode. The next `start()` gets a fresh file."""
        with self._lock:
            path = self._path
            self._close_locked()
            self._stem = None
        _LOGGER.info("Report log OFF (%s)", path)

    def close(self) -> None:
        """Release the file handle without ending the episode — for unload.

        The episode's persistence is the config entry's business; after a reload the
        coordinator calls `resume()` with the persisted name and writing continues in the
        same file.
        """
        with self._lock:
            self._close_locked()

    def write(
        self, topic: str, payload: bytes, *, qos: int = 0, retain: bool = False
    ) -> None:
        """Record one message. Cheap no-op when no episode is active."""
        if self._stem is None:
            return
        line = format_record(topic, payload, qos=qos, retain=retain)
        if line is None:  # pragma: no cover - defensive
            return
        with self._lock:
            if self._stem is None:
                return
            try:
                self._write_line_locked(line)
            except OSError as err:
                # A diagnostic must never take the stream down. Drop the handle but keep
                # the episode: the disk may come back, and the persisted name means a
                # restart re-attaches either way.
                _LOGGER.warning("Report log write failed: %s", err)
                self._close_locked()

    def note(self, journal: str, event: str, fields: dict[str, Any]) -> None:
        """Record one **decision** — what the integration concluded, not what arrived.

        The raw messages say what the valve sent; these say what was made of it. Both in one
        file, in the order they happened, on one clock, so a report answers "why was warmup
        not restored" without asking the reader to line two files up by timestamp. One switch,
        one attachment.

        Told apart from a raw message by their keys: a message has `topic`, a decision has
        `journal` (`warmup` today; reports from before 2026-10-08 can also carry `cutoff`,
        from the removed Endless Shower) and `event`. To read one or the other:

            jq -c 'select(.topic)'    report_*.jsonl   # the wire
            jq -c 'select(.journal)'  report_*.jsonl   # the reasoning

        **Written whether or not the feature is switched on**, exactly as the standalone
        journal is, so a report from an install with Auto-Restore off still shows a disable
        that was seen and deliberately left alone — which is usually the question being
        asked.
        """
        if self._stem is None:
            return
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "journal": journal,
            "event": event,
        }
        record.update(fields)
        try:
            line = json.dumps(record, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return
        with self._lock:
            if self._stem is None:
                return
            # ⚠️ **Never opens a file.** Unlike `write()`, which paho calls on its own network
            # thread, this runs on the **event loop** — the warm-up watcher lives there — and `_open_locked` creates a directory and opens two
            # files. Blocking calls on the loop are an error in Home Assistant, and this
            # shipped doing exactly that in 0.15.0.
            #
            # Same answer as `DebugJournal.note`: raise a flag and let the caller schedule
            # `prepare()` in an executor. The cost is that a decision arriving while no file
            # is open is dropped and the next one lands — acceptable for a diagnostic, and
            # the usual case is a file already open from `start()` or `resume()`.
            if self._handle is None or self._written >= self._max_bytes:
                self._wants_open = True
                return
            try:
                self._write_line_locked(line)
            except OSError as err:
                _LOGGER.warning("Report log write failed: %s", err)
                self._close_locked()

    # ------------------------------------------------------------------ #
    # Locked internals
    # ------------------------------------------------------------------ #
    def _write_line_locked(self, line: str) -> None:
        if self._handle is None or self._written >= self._max_bytes:
            self._open_locked()
        assert self._handle is not None
        encoded = line + "\n"
        self._handle.write(encoded)
        self._handle.flush()
        # Per line, like the development capture: reports are read while the problem is
        # still happening, and buffered lines would not be there yet.
        self._written += len(encoded.encode("utf-8"))

    def _part_path(self, part: int) -> str:
        name = f"{self._stem}.jsonl" if part == 1 else f"{self._stem}_p{part}.jsonl"
        return os.path.join(self._directory, name)

    def _latest_part(self) -> int:
        """The highest existing part number for this episode, or 1 if none exist yet."""
        assert self._stem is not None
        latest = 1
        try:
            for name in os.listdir(self._directory):
                if not name.startswith(self._stem):
                    continue
                if name == f"{self._stem}.jsonl":
                    latest = max(latest, 1)
                else:
                    match = _PART_RE.search(name)
                    if match and name == f"{self._stem}_p{match.group(1)}.jsonl":
                        latest = max(latest, int(match.group(1)))
        except OSError:
            pass
        return latest

    def _open_locked(self) -> None:
        """Open the episode's current part for append, rolling to the next when full.

        Append mode is the whole trick: a restart's `resume()` lands here, finds the
        latest part, and continues it — the file does not restart with Home Assistant.
        """
        self._close_locked(quiet=True)
        os.makedirs(self._directory, exist_ok=True)
        self._write_readme()
        part = self._latest_part()
        path = self._part_path(part)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size >= self._max_bytes:
            part += 1
            path = self._part_path(part)
            size = 0
        self._path = path
        self._handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - handle outlives this call; closed by close()
        self._written = size

    def _write_readme(self) -> None:
        try:
            with open(
                os.path.join(self._directory, "README.txt"), "w", encoding="utf-8"
            ) as fh:
                fh.write(_README.format(max_mb=self._max_bytes // (1024 * 1024)))
        except OSError:  # pragma: no cover - the capture still works without it
            pass

    def _close_locked(self, *, quiet: bool = False) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:  # pragma: no cover
                pass
        self._handle = None
        if not quiet:
            self._path = None
        self._written = 0
