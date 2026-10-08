"""Warm-up mode: writing it, watching it, and putting it back.

Warm-up is a **stored mode**, not an action — an enabled mode means the valve warms the
water by itself at the start of a session — and it is the one setting on this integration
that something else routinely takes away. The hub's web UI writes `warmUpDisabled` on any
signed-in use of it, the Konnect app can, and the fixture itself can. Auto-restore exists
to notice that and undo it, which needs more machinery than a setting normally does: a
memory of the last enabled mode, a record of our own writes so we do not restore over
ourselves, a delay-and-recheck before writing, a counter that gives up rather than fight
forever, and a journal of the whole thing because the cause is still an open question.

That is thirteen methods and six pieces of state, which lived on
:class:`~.coordinator.Valve` until 0.10.0 and made up a fifth of it. They are here now
because they form a closed system: everything that touches the warm-up mode is in this
file, and the rest of `Valve` reaches it through four members
(:attr:`auto_restore`, :attr:`last_mode`, :meth:`async_set_warmup`,
:meth:`handle_mode_change`).

**The valve keeps its public surface unchanged.** `valve.warmup_auto_restore`,
`valve.last_warmup_mode` and `valve.async_set_warmup(...)` still work and still mean the
same thing — they delegate here. Entities, services and diagnostics were not touched by
the move, and neither were their unique ids.

The pure decision logic — *should* this disable be restored, and to what — is not here
either: it is in :mod:`.anthem.warmup`, with no I/O and no Home Assistant, so it can
be tested against captured sequences. This module is the part with the side effects.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from .anthem import (
    WARMUP_DISABLED,
    WARMUP_MODES_CURRENT,
    DeviceOffline,
    KohlerError,
)
from .anthem.warmup import journal_event, restore_target, should_restore_warmup
from .const import (
    CONF_LAST_WARMUP_MODE,
    CONF_WARMUP_AUTO_RESTORE,
    WARMUP_AUTO_RESTORE_DELAY_SECONDS,
    WARMUP_AUTO_RESTORE_GIVING_UP,
    WARMUP_AUTO_RESTORE_MAX_CONSECUTIVE,
    WARMUP_AUTO_RESTORE_NO_TARGET,
    WARMUP_AUTO_RESTORE_SETTLED_SECONDS,
    WARMUP_CONTEXT_AFTER_SECONDS,
    WARMUP_CONTEXT_BEFORE_SECONDS,
    WARMUP_READBACK_DELAYS,
    WARMUP_SELF_WRITE_GRACE_SECONDS,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle; only needed for the annotation
    from .coordinator import Valve

_LOGGER = logging.getLogger(__name__)

__all__ = ["WarmupManager"]


class WarmupManager:
    """The warm-up half of one :class:`~.coordinator.Valve`.

    Holds a back-reference to its valve rather than taking the pieces it needs, because
    almost everything it reads — `gcs_state`, `gcs`, `client`, the option store — is
    replaced or re-seeded during the valve's life, and a snapshot taken at construction
    would go stale. One manager per valve, created in `Valve.__init__` and never shared.
    """

    def __init__(self, valve: Valve) -> None:
        self._valve = valve
        # When we last wrote a mode ourselves, and which — the pair that tells an echo of
        # our own write apart from somebody else disabling warm-up. Read together by
        # `_warmup_write_status`; see `should_restore_warmup`'s self-write grace.
        self._warmup_self_write_at: float | None = None
        self._warmup_self_write_mode: str | None = None
        # One restore in flight at a time; two would race each other over a single field.
        self._warmup_restore_task: asyncio.Task | None = None
        # Consecutive restores that did not stick, and when the last one ran. Together they
        # decide when to stop fighting whatever keeps disabling it.
        self._warmup_restores = 0
        self._warmup_restored_at: float | None = None
        # Latch, so a persistent fight warns once rather than on every recurrence.
        self._warmup_gave_up_reported = False

    def reset_restore_task(self) -> None:
        """Forget the pending restore. Called from `Valve.stop` after cancelling tasks."""
        self._warmup_restore_task = None

    @callback
    def handle_mode_change(
        self, before: str | None, after: str | None, *, announced: bool = False
    ) -> None:
        """Public name for `_handle_warmup_mode_change` — see there."""
        self._handle_warmup_mode_change(before, after, announced=announced)

    def note_seeded_mode(self, before: str | None, after: str | None) -> None:
        """Handle a mode change discovered by the REST seed rather than announced.

        Lifted out of `Valve.async_seed`, where it was thirty lines of warm-up reasoning in
        the middle of a method about seeding everything else.

        `apply_rest_state` writes `warmup_mode` straight in, so a change that happened while
        the stream was down never reaches `_handle_warmup_mode_change` and nothing else
        would record it — the one way a disable can happen and leave no trace in the journal
        at all. That reseed runs on every MQTT reconnect, so the gap it covers is real: any
        signed-in use of the hub web UI writes `warmUpDisabled` (api.md §3h), and a sign-in
        while the stream is down lands exactly here.

        A discovered disable restores through the same machinery as an announced one — same
        decision function, same self-write grace, same single-flight guard — and
        `_async_restore_warmup` waits its delay and re-checks the *live* mode before
        writing, so "the REST read is of unknown age" costs nothing by write time. What a
        discovery still cannot have is a `before_window` (the wire context happened while
        there was no wire), so `source: "rest"` stays on the record and the `restoring`
        field says what was decided.
        """
        if before is not None and after != before:
            write_age, ours = self._warmup_write_status(after)
            restoring = should_restore_warmup(
                before,
                after,
                enabled=self.auto_restore,
                self_write_mode=self._warmup_self_write_mode,
                self_write_age=write_age,
                grace_seconds=WARMUP_SELF_WRITE_GRACE_SECONDS,
            )
            self._warmup_journal(
                "mode",
                before=before,
                after=after,
                ours=ours,
                source="rest",
                restoring=restoring,
            )
            if restoring:
                self._schedule_warmup_restore(before)
        # The third way to learn a mode, and the one `_remember_warmup_mode`'s docstring
        # used to miss. MQTT alone forgets a mode set while the stream was down; our own
        # writes alone forget a mode set from the app or touchscreen; and *both* forget a
        # mode that was simply already in force when we started.
        #
        # That third gap disabled auto-restore for seven hours on 2026-08-20: the valve was
        # in `warmUpAllOutletsWithNoStartDelay`, read correctly over REST at 19:35:32Z, then
        # disabled at 20:36:28Z — and the restore was skipped with "no enabled mode has ever
        # been seen", because no *announcement* had happened in that session. See
        # `_async_restore_warmup`.
        if after is not None and after != WARMUP_DISABLED:
            self._remember_warmup_mode(after)

    async def async_set_warmup(self, mode: str) -> None:
        """Set the valve's warmup mode. **This does not run water now.**

        Warmup is a stored mode, not an action: an enabled mode means the valve warms up by
        itself at the start of a session. The setting persists — it survives a power cycle,
        and the valve re-announces it about 4 s after every boot.

        ``mode`` must be one of ``WARMUP_MODES_CURRENT`` — the three the current Konnect app
        offers. The two legacy delayed-start values are rejected here rather than passed
        through: the valve may still be *holding* one, and `select.py` shows it when it is,
        but nothing knows what their delay does and writing one would be guessing.

        ⚠️ **Refused while water is running.** The Konnect app checks whether any outlet on
        either valve is on and silently reverts its own control rather than calling the API;
        this mirrors that check, but says so instead of reverting. Whether the *device*
        enforces it is untested — the guard is client-side in the app, so a write during a
        shower might land, might be ignored, and there is no way to tell which from the
        response. Raising keeps us on the app's side of a question nobody has answered.
        """
        if self._valve.gcs is None:
            raise HomeAssistantError("No Anthem valve on this account")
        if mode not in WARMUP_MODES_CURRENT:
            raise HomeAssistantError(
                f"{mode!r} is not a warmup mode this integration writes. Expected one of: "
                + ", ".join(WARMUP_MODES_CURRENT)
            )
        state = self._valve.gcs_state
        if state is not None and state.is_running:
            raise HomeAssistantError(
                "Warmup cannot be changed while the shower is running. The Konnect app "
                "blocks this too. Turn the water off and try again."
            )
        # Recorded before the call, not after: the valve's echo can arrive while the POST
        # is still in flight, and a disable we caused must be recognisable by then.
        self._warmup_self_write_at = time.monotonic()
        self._warmup_self_write_mode = mode
        try:
            await self._valve.gcs.async_set_warmup(mode)
        except DeviceOffline as err:
            raise HomeAssistantError(
                "The Anthem valve is offline. Check that it is powered on and connected "
                "to Wi-Fi, then try again."
            ) from err
        except KohlerError as err:
            raise HomeAssistantError(f"Kohler command failed: {err}") from err

        # Read the field back rather than trusting the write. A 200 here means the *cloud*
        # accepted the command; the valve can still ignore it, and does when warmup is
        # disabled on the fixture itself. `warmUpState.warmUp` is the device's own answer.
        #
        # ⚠️ **The first read is too early and will disagree.** Measured live 2026-08-20:
        # `gcs-state` still returned the OLD mode immediately after a successful POST and
        # only caught up by t+3 s, while the valve's own `GCS_WARM_STS` echo landed at
        # +3.42 s. So a single immediate read-back reports a false mismatch on every write.
        # Hence the retries: disagreement only means something after the device has had a
        # few seconds to answer.
        confirmed = None
        for delay in WARMUP_READBACK_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            confirmed = await self.async_read_warmup_mode()
            if confirmed == mode:
                self._remember_warmup_mode(mode)
                _LOGGER.info("Warmup mode set to %s and confirmed by the valve", mode)
                return
        if confirmed is None:
            _LOGGER.info(
                "Warmup mode %s sent to the Anthem valve; the read-back did not answer, so "
                "the stored mode is whatever the valve reports next over MQTT",
                mode,
            )
        else:
            # Past the settle window, so this is a real disagreement rather than lag. The
            # known cause is warmup being disabled on the fixture itself, where the cloud
            # accepts the command and the valve ignores it.
            _LOGGER.warning(
                "Warmup mode was set to %s but the valve still reports %s after %.0f s. The "
                "cloud accepted the command and the valve did not apply it — the usual cause "
                "is warmup being disabled on the fixture. Nothing has been retried.",
                mode,
                confirmed,
                sum(WARMUP_READBACK_DELAYS),
            )

    async def async_read_warmup_mode(self) -> str | None:
        """Read the warmup mode from the REST API and apply it, returning what it said.

        The source is `gcs-state`'s `warmUpState.warmUp` — the same field the Konnect app
        reads, and the same read that seeds this at setup and on every MQTT reconnect. It
        carries the mode axis; `warmUpState.state` beside it carries whether a warm-up is
        running, and `apply_rest_state` takes both.

        ⚠️ **REST reads here are partly cached** — `amplifierSettings.monoVolume` famously
        did not follow a live change (`docs/architecture.md`). So this is authoritative about
        what Kohler's cloud believes, which is not always what the valve did a second ago.
        The device's own push, `GCS_WARM_STS`, remains the final word and arrives by itself.

        Returns `None` if the read failed or carried no warmup field — never a guess, since
        "Off" and "we could not tell" are different answers and only one of them is safe to
        show on a control.
        """
        if self._valve.gcs_device is None or self._valve.gcs_state is None:
            return None
        try:
            payload = await self._valve.client.async_get_gcs_state(
                self._valve.gcs_device.device_id
            )
        except KohlerError as err:
            _LOGGER.debug("Could not read warmup mode: %s", err)
            return None
        if self._valve.cloud_watch is not None:
            # CLOUD CONNECTION WATCH — the fourth free read. A warmup write reads this back
            # up to three times; each one carries `connectionState` and would otherwise
            # discard it.
            self._valve.cloud_watch.note_rest_payload(payload, "warmup read-back")
        self._valve.gcs_state.apply_rest_state(payload)
        # Same notification path as an MQTT update, so the dropdown lands on the confirmed
        # value and drops its optimistic guess exactly as it would on a device push.
        self._valve._push()
        return self._valve.gcs_state.warmup_mode

    @property
    def auto_restore(self) -> bool:
        """Whether to put the warmup mode back after something else disables it.

        Read live from the entry options, so the switch takes effect immediately. Off unless explicitly enabled.
        """
        return bool(self._valve.option(CONF_WARMUP_AUTO_RESTORE, False))

    @property
    def last_mode(self) -> str | None:
        """The last *enabled* warmup mode seen on the valve, or None if we have never seen one.

        Persisted in the entry options so a restore after a Home Assistant restart reinstates
        the mode the fixture actually had. `None` is a real answer and is treated as one: with
        no prior, auto-restore does nothing rather than picking a default, because "all
        outlets" and "selected outlets" are different fixtures' worth of water.
        """
        stored = self._valve.option(CONF_LAST_WARMUP_MODE)
        return stored if stored in WARMUP_MODES_CURRENT else None

    def _message_window(self, since: float, until: float | None = None) -> list[dict]:
        """Messages between two monotonic instants, oldest first, without the clock field."""
        # This valve's own messages and every controller's — never another valve's.
        # The controller ones matter: `SYSTEM_STS: SYSTEM_READY` is the most
        # distinctive marker seen around a disable, and it is a controller message.
        valve = self._valve
        others = {v.device_id for v in valve.coordinator.valves if v is not valve}
        return [
            {k: v for k, v in item.items() if k not in ("at", "device")}
            for item in valve.coordinator._recent_messages
            if item["at"] >= since
            and (until is None or item["at"] <= until)
            and item.get("device") not in others
        ]

    @callback
    def _warmup_journal(self, event: str, **fields: Any) -> None:
        """Append to the warmup journal, and to any active Report Log.

        Mirrors `_journal`, including the deferred open. The Report Log copy is written
        **independently of the standalone journal** — that one has its own enabled flag, and
        a decision must reach an active report whether or not the dedicated warm-up journal
        is switched on. One switch, one attachment; see `report_log.ReportLog.note`.
        """
        tagged = self._valve._tagged(fields)
        report_log = self._valve.coordinator.report_log
        if report_log is not None:
            report_log.note("warmup", event, dict(tagged))
            # `note` never opens a file — it runs on the loop. Same deferred open the
            # warm-up journal below uses.
            if report_log.wants_open:
                self._valve.hass.async_add_executor_job(report_log.prepare)
        if self._valve.warmup_log is None:
            return
        self._valve.warmup_log.note(event, **tagged)
        if self._valve.warmup_log.wants_open:
            self._valve.hass.async_add_executor_job(self._valve.warmup_log.prepare)

    async def _async_journal_warmup_context(self, at: float) -> None:
        """Record what arrived *after* a disable.

        Separate from the disable record because the most distinctive marker seen so far —
        `SYSTEM_STS: SYSTEM_READY` — landed 7 to 9 s afterwards in the two clearest of the
        four known cases. A record written at the moment of the disable cannot contain it.
        """
        await asyncio.sleep(WARMUP_CONTEXT_AFTER_SECONDS)
        state = self._valve.gcs_state
        self._warmup_journal(
            "context",
            after_window=self._message_window(at),
            window_seconds=WARMUP_CONTEXT_AFTER_SECONDS,
            mode_now=None if state is None else state.warmup_mode,
        )

    @callback
    def _remember_warmup_mode(self, mode: str) -> None:
        """Record an enabled mode as what auto-restore should reinstate.

        Called from three directions, because any one of them alone leaves a gap:

        * **The valve announcing a mode over MQTT.** Alone, it forgets a mode chosen while
          the stream was down.
        * **A write of ours confirming.** Alone, it forgets a mode set from the Konnect app
          or the touchscreen — and those are most of them.
        * **The REST seed at setup** (``_async_seed_state``). Alone, neither of the other two
          knows about a mode that was simply already in force when the integration started.
          Added 2026-08-21: its absence cost a seven-hour unrestored disable on 08-20, since
          a mode read but never announced left auto-restore with no target.

        Seeing an enabled mode also ends any fight in progress: the counter that stops an
        endless restore loop is reset here, because the mode staying enabled is exactly the
        outcome that counter exists to wait for.
        """
        if mode == WARMUP_DISABLED:
            return
        self._warmup_restores = 0
        self._warmup_restored_at = None
        # Cleared with the counter, so a fight that stops and later restarts is reported
        # again rather than staying silent for the rest of the run.
        self._warmup_gave_up_reported = False
        if mode != self._valve.option(CONF_LAST_WARMUP_MODE):
            self._valve.set_option(CONF_LAST_WARMUP_MODE, mode)

    def _warmup_write_status(self, after: str | None) -> tuple[float | None, bool]:
        """How long ago we last wrote a warmup mode, and whether ``after`` was that write.

        The pair every warmup observation needs, whichever channel it arrived on:
        ``write_age`` feeds ``should_restore_warmup``'s self-write grace, and ``ours`` is
        the journal's answer to "did we do this?" — true only when the observed mode matches
        the mode we wrote and the write is recent enough to be the cause.
        """
        write_age = (
            None
            if self._warmup_self_write_at is None
            else time.monotonic() - self._warmup_self_write_at
        )
        ours = (
            self._warmup_self_write_mode == after
            and write_age is not None
            and write_age <= WARMUP_SELF_WRITE_GRACE_SECONDS
        )
        return write_age, ours

    def _schedule_warmup_restore(self, taken_away: str | None) -> None:
        """Spawn one restore task, or record why not.

        Shared by both callers — the MQTT announcement path and the reseed discovery path —
        because two restores in flight would race `async_set_warmup` against itself over a
        single field, and the journal should say a second trigger arrived rather than let
        the tasks interleave silently.
        """
        if (
            self._warmup_restore_task is not None
            and not self._warmup_restore_task.done()
        ):
            self._warmup_journal(
                "restore_skipped", reason="a restore is already pending"
            )
            return
        self._warmup_restore_task = self._valve._track(
            self._async_restore_warmup(taken_away)
        )

    @callback
    def _handle_warmup_mode_change(
        self, before: str | None, after: str | None, *, announced: bool = False
    ) -> None:
        """React to the valve announcing a new warmup mode.

        Two jobs: remember any enabled mode as the restore target, and notice a transition
        *into* disabled that this integration did not cause.

        ``announced`` says this envelope was a `GCS_WARM_STS` — the valve volunteering its
        mode — as opposed to any of the other messages that reach this method unchanged.
        It only matters when the mode did **not** move; a change can come from nowhere else,
        since `_apply_warmup` is the only envelope handler that writes `warmup_mode`.
        """
        record = journal_event(before, after, announced=announced)
        if record is None:
            return

        # Computed before the branch because **an announcement needs `ours` just as much as a
        # transition does** — see the note on the `announced` record below.
        write_age, ours = self._warmup_write_status(after)

        if record == "announced":
            # The valve restating a mode it is already in.
            #
            # ⚠️ **Most of these are our own dropdown writes, and the journal has to say so.**
            # Measured live 2026-08-21: `async_set_warmup` reads the mode back over REST at
            # `WARMUP_READBACK_DELAYS = (0.0, 2.0, 4.0)`, and the first of those is immediate,
            # so `apply_rest_state` has already moved `warmup_mode` by the time the valve's
            # own echo lands ~3.4 s later. `before == after`, and what would have been a
            # `mode` record with `ours: true` arrives here instead. Two dropdown changes that
            # evening produced exactly this, at +0.81 s and +0.33 s after their readbacks.
            #
            # Without `ours` the whole class is indistinguishable from the valve volunteering
            # its state — which is the one distinction §3e's open question turns on.
            #
            # Deliberately does not fall through to the disable path: a repeat of
            # `warmUpDisabled` is not a fresh disable, and `should_restore_warmup` would
            # refuse it anyway — but relying on that implicitly is how a restore loop starts.
            self._warmup_journal("announced", mode=after, ours=ours, source="mqtt")
            return

        # Every announcement is journalled, not only the disables. Establishing what a normal
        # week looks like is half of recognising the abnormal event.
        #
        # ⚠️ **That sentence was false from the day it was written until 2026-08-21.** Only
        # *changes* reached this line; a repeat returned above, and the mode a file opened on
        # was never recorded at all. Both are covered now — the `announced` record above and
        # the `baseline` record in `async_setup` — so it is true as stated. Check all three
        # before trusting it again.
        self._warmup_journal(
            "mode", before=before, after=after, ours=ours, source="mqtt"
        )

        if after != WARMUP_DISABLED:
            self._remember_warmup_mode(after)
            return

        restoring = should_restore_warmup(
            before,
            after,
            enabled=self.auto_restore,
            self_write_mode=self._warmup_self_write_mode,
            self_write_age=write_age,
            grace_seconds=WARMUP_SELF_WRITE_GRACE_SECONDS,
        )
        now = time.monotonic()
        state = self._valve.gcs_state
        self._warmup_journal(
            "disabled",
            before=before,
            ours=ours,
            restoring=restoring,
            auto_restore=self.auto_restore,
            restores_to=restore_target(before, self.last_mode),
            water_running=None if state is None else state.is_running,
            before_window=self._message_window(
                now - WARMUP_CONTEXT_BEFORE_SECONDS, now
            ),
            window_seconds=WARMUP_CONTEXT_BEFORE_SECONDS,
        )
        # The after-window is worth having whether or not we restore — an unrestored disable
        # is the cleaner observation of the two, since nothing of ours is in the way.
        self._valve._track(self._async_journal_warmup_context(now))

        if not restoring:
            return
        self._schedule_warmup_restore(before)

    async def _async_restore_warmup(self, taken_away: str | None = None) -> None:
        """Wait out the delay, re-check, and put the mode back.

        Re-checks rather than cancels: during the wait the mode may have been re-enabled by
        hand, the switch turned off, or the shower started. Every one of those means do
        nothing, and asking at the end is simpler than keeping a cancellation path correct
        for each.

        ``taken_away`` is the mode the disable moved *away* from, and it is the restore
        target. ``should_restore_warmup`` refuses unless ``before`` is a known enabled mode,
        so whenever a restore is scheduled that value is present and is the most current
        answer available — more current than ``last_warmup_mode``, which is a persisted
        memory that can be older or, before 2026-08-21, absent entirely.

        ⚠️ **It used to restore to ``last_warmup_mode`` alone, and that had a seven-hour
        failure on 2026-08-20.** The valve was disabled out of
        ``warmUpAllOutletsWithNoStartDelay``; the decision function said restore; the journal
        recorded ``"before": "warmUpAllOutletsWithNoStartDelay"`` and ``"restores_to": null``
        in the same entry — because no enabled mode had been *announced* during that
        integration session, only read over REST at setup. The mode being taken away was in
        hand the whole time and was thrown away. Seeding from REST (see
        ``_async_seed_state``) closes the same gap from the other side.
        """
        target = restore_target(taken_away, self.last_mode)
        if target is None:
            # Now unreachable for a genuine disable — kept because it is cheap, and because
            # a future caller that passes nothing should say so rather than write a default.
            _LOGGER.warning(WARMUP_AUTO_RESTORE_NO_TARGET)
            self._warmup_journal(
                "restore_skipped", reason="no enabled mode has ever been seen"
            )
            return

        if (
            self._warmup_restores >= WARMUP_AUTO_RESTORE_MAX_CONSECUTIVE
            and self._warmup_restored_at is not None
            and time.monotonic() - self._warmup_restored_at
            < WARMUP_AUTO_RESTORE_SETTLED_SECONDS
        ):
            # **This is where giving up actually happens**, and until 0.8.1 it happened
            # silently — the warning lived below, behind a `>` test on a counter this gate
            # stops at `>=`, so it could not fire. Something rewriting the valve's warmup
            # mode is exactly what the owner needs told, and it was only ever written to the
            # journal nobody reads until they already suspect a problem.
            #
            # Latched so a persistent fight logs once rather than every time it recurs; the
            # latch clears in `_remember_warmup_mode` beside the counter, so a fight that
            # genuinely stops and restarts is reported again.
            if not self._warmup_gave_up_reported:
                self._warmup_gave_up_reported = True
                _LOGGER.warning(
                    WARMUP_AUTO_RESTORE_GIVING_UP, WARMUP_AUTO_RESTORE_MAX_CONSECUTIVE
                )
            self._warmup_journal(
                "restore_skipped",
                reason="gave up after %d restores that did not stick"
                % WARMUP_AUTO_RESTORE_MAX_CONSECUTIVE,
            )
            return

        self._warmup_journal(
            "restore_scheduled",
            target=target,
            delay_seconds=WARMUP_AUTO_RESTORE_DELAY_SECONDS,
        )
        await asyncio.sleep(WARMUP_AUTO_RESTORE_DELAY_SECONDS)

        if not self.auto_restore:
            self._warmup_journal(
                "restore_skipped", reason="switched off during the wait"
            )
            return
        state = self._valve.gcs_state
        if state is None or state.warmup_mode != WARMUP_DISABLED:
            # Someone got there first. Nothing to do, and saying so beats a silent return
            # when the owner is watching the log to see whether this feature works.
            _LOGGER.info(
                "Warmup was re-enabled before auto-restore ran; leaving it alone"
            )
            self._warmup_journal(
                "restore_skipped",
                reason="re-enabled during the wait",
                mode_now=None if state is None else state.warmup_mode,
            )
            return

        self._warmup_restores += 1
        self._warmup_restored_at = time.monotonic()
        _LOGGER.warning(
            "Warmup was disabled by something other than Home Assistant. Setting it back "
            "to %s (attempt %d).",
            target,
            self._warmup_restores,
        )
        self._warmup_journal("restore", target=target, attempt=self._warmup_restores)
        try:
            await self.async_set_warmup(target)
        except HomeAssistantError as err:
            # Not retried. The known refusal is water running, and a shower is exactly when
            # nobody wants this fighting the valve; the next disable will schedule another.
            _LOGGER.warning("Warmup auto-restore could not write: %s", err)
            self._warmup_journal("restore_failed", target=target, error=str(err))
            return
        self._warmup_journal(
            "restore_done",
            target=target,
            attempt=self._warmup_restores,
            mode_now=None
            if self._valve.gcs_state is None
            else self._valve.gcs_state.warmup_mode,
        )
