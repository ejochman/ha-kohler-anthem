"""How long each valve zone has been running, for the time-left attributes.

The valve stops a zone once it has run for its `maximumRunTime`, and it times the **zone**,
not the outlet: the clock starts when the zone goes from nothing flowing to something
flowing, and moving between outlets within the zone does not reset it (live; see
`docs/protocol/gcs_valve.md`, "Run-time limit"). Nothing on the wire reports the valve's own
clock, so this keeps one from the messages it sees.

Timing starts when *this process* sees the zone start, so a restart or a reconnect
mid-shower loses it. `forget` drops the clocks rather than guessing, and readers get None
until the zone next starts.
"""

from __future__ import annotations

import time

__all__ = ["ZoneClock"]


class ZoneClock:
    """A monotonic start time for each zone that is running water."""

    def __init__(self) -> None:
        self._since: dict[int, float] = {}

    def update(self, flowing: dict[int, bool]) -> None:
        """Apply one valve snapshot: zone -> whether water is running in it.

        A zone counts as running when it has outlets open and is not paused — the same
        reading the valve's own timer uses.
        """
        now = time.monotonic()
        for zone, running in flowing.items():
            if running:
                self._since.setdefault(zone, now)
            else:
                self._since.pop(zone, None)

    def flowing_for(self, zone: int) -> float | None:
        """Seconds the zone has been running, or None if it is idle or not being timed."""
        started = self._since.get(zone)
        return None if started is None else time.monotonic() - started

    def forget(self) -> None:
        """Drop every clock. After a reconnect the gap makes any duration meaningless."""
        self._since.clear()
