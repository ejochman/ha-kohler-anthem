"""Cloud reachability for the Anthem valve — the one fact MQTT cannot carry.

# =====================================================================
# CLOUD CONNECTION WATCH
# =====================================================================
#
#     grep -rn "CLOUD CONNECTION WATCH" custom_components/kohler_anthem/
#
# That finds this module, the constants in `const.py`, the wiring in `coordinator.py`, and
# the entity in `binary_sensor.py`.
#
# =====================================================================

## The problem

The GCS valve drops off Kohler's cloud on its own, at random, and comes back only when it is
physically power-cycled. While it is gone the Konnect app shows it as cut off, Home Assistant
shows nothing at all, and the valve keeps working perfectly from the wall.

**MQTT cannot report this, ever.** Every message on that stream is published *by* the valve,
so a disconnect is silence — and the valve is silent most of the time anyway, because the
channel is event-driven. `GCS_SOLO_STS.IoTActive` looks purpose-built for the job and is
useless for the same reason: it read `Active` in 1 020 of 1 020 captured samples, because a
disconnected device cannot publish a message saying so.

## Why there is no silence threshold, and why this module does not use one

Measured across a 19-day capture corpus (2026-08-07 → 08-26):

===========================================================  ===========
Longest GCS silence provably benign (capture never stopped,
no reboot, valve answered a command at the end of it)          12 h 02 m
Longest benign silence observed at all                         35 h 49 m
The one real outage                                            12 h 22 m
===========================================================  ===========

The outage is **20 minutes longer than a known-good idle period**. Any threshold low enough
to catch it fires constantly on healthy quiet, and any threshold high enough to be quiet
misses it. So neither trigger below decides anything from silence. **Silence only decides
when to ask.** The answer always comes from `connectionState`, which is ground truth.

## The two triggers

**A — contradiction (needs a HUB).** The controller reports a zone `ON` while the valve says
nothing. This is the only signal in the corpus that separated the outage from 18 healthy
days: of 437 zone-`ON` `SHOWER_VALVE_STS` messages, 435 had a valve message within 60 s, and
**the only 2 that did not are the outage itself** — zero false positives over 435 healthy
samples. It fires within a minute rather than hours, and costs no network traffic to detect.

⚠️ **Zone `ON` only.** An all-`OFF` card gets republished with no valve action at all: the
controller emits its whole card set (`MUSIC_STS` + `SHOWER_VALVE_STS` + `STEAM_STS` +
`FAVORITE_STS` + `LIGHT_STS`) in one second during favorite activity, and every
`SHOWER_VALVE_STS` in the captured favorite bursts reads `z1:OFF z2:OFF`. The valve owes no
reply to an unchanged OFF card, so a missing valve message there means nothing. The `OFF`
variant is not merely noisier — it is evidentially empty.

**B — prolonged quiet (any account with a valve).** Trigger A can only see an outage the
controller happens to be awake for; measured against the corpus that is 36 % of all silence
time, and a GCS-only account has no controller at all. So after
:data:`CLOUD_CHECK_QUIET_SECONDS` with no valve message, ask once, then keep asking on that
interval while the quiet continues.

## What this is not

**It is not a polling loop.** `SCAN_INTERVAL` stays `None`; nothing here runs on a clock while
the valve is talking. Trigger A is driven by an arriving message. Trigger B's timer is reset
by every valve message, so on a normal day it never reaches its deadline — and when it does,
it is because the thing it watches has actually stopped.

## The earlier `Connection` sensor, and why this is not a repeat of it

An entity reporting `connectionState` existed once and was **deliberately removed** (see
`binary_sensor.py`). Its stated faults were that it was a fact about the plumbing rather than
the integration, that it *had no push source so it went stale as soon as polling was removed*,
and that a valve dropping off the cloud is fixed in the Konnect app rather than here.

The middle one was the real objection and it is the one this module answers: the field still
has no push source, so instead of polling it, **two push-driven events decide when to read
it**. The other two did not survive contact with 2026-08-26 — the owner needed exactly this
fact, and the app is where the problem *appeared*, not where it got fixed. It took a power
cycle.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from .anthem import AuthError, KohlerError
from .anthem.const import MSG_HUB_SHOWER_VALVE
from .const import (
    CLOUD_CHECK_COOLDOWN_SECONDS,
    CLOUD_CHECK_PAIR_WINDOW_SECONDS,
    CLOUD_CHECK_QUIET_SECONDS,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .anthem.mqtt import Envelope
    from .coordinator import KohlerAnthemCoordinator, Valve

_LOGGER = logging.getLogger(__name__)

#: The values Kohler's cloud reports, compared case-insensitively. Only ``"Connected"`` has
#: ever been captured; ``"Disconnected"`` is the negative Konnect 3.0.6 tests for
#: (`mc0/n.java`, `ui/ota/f.java`), and the app treats a device as online **unless** it reads
#: exactly that. This module matched only the positive until 2026-10-07, because the
#: negative was unconfirmed; it now follows the app's rule, so an unfamiliar value no longer
#: reads as an outage — it is logged once instead.
CONNECTED = "connected"
DISCONNECTED = "disconnected"


def _utc_iso(stamp: float | None) -> str | None:
    """Wall-clock seconds to the ISO-8601 Z form the journals and raw capture use."""
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, tz=UTC).isoformat().replace("+00:00", "Z")


class CloudConnectionWatch:
    """Decides when to ask Kohler whether the valve is still reachable, and remembers.

    One instance per valve, owned by its :class:`~.coordinator.Valve` — it is that valve's
    reachability it reports. Trigger A is wired only when the account also has a
    controller.

    **Nothing here ever changes valve state.** The only network call is a GET of
    ``gcs-state``, and its payload is deliberately *not* fed to :class:`GcsState` — see
    :meth:`_async_check`.
    """

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        self._coordinator = coordinator
        self._valve = valve
        self._hass = coordinator.hass

        # Answer state. `_connected` is None until the first successful read: "we have not
        # asked" and "it said no" are different answers and must not render the same.
        self._connected: bool | None = None
        self._reported: str | None = None
        self._checked_at: float | None = None
        self._trigger: str | None = None
        self._checks = 0
        self._last_error: str | None = None
        # The last unfamiliar `connectionState` logged, so each new one is said once.
        self._unfamiliar: str | None = None
        # Present only if Kohler returns it for the valve. `hub-state` carries it; whether
        # `gcs-state` does is an open question, so this surfaces the answer the first time a
        # real read happens rather than waiting for someone to probe it by hand.
        self._last_connected_epoch: Any = None

        # Clocks. Monotonic for every interval decision, so a system clock step cannot make
        # the valve look absent or suppress a check.
        self._last_gcs_at: float | None = None
        self._last_check_at: float | None = None

        self._pair_cancel: Any = None
        self._quiet_cancel: Any = None
        self._task: Any = None
        self._stopped = False

    # ------------------------------------------------------------------ #
    # What entities read
    # ------------------------------------------------------------------ #
    @property
    def connected(self) -> bool | None:
        """True/False from the last successful read, None before there has been one.

        A failed read does **not** move this. "We could not reach Kohler" is a different
        fault from "Kohler cannot reach the valve", and collapsing them would report the
        valve offline every time the WAN hiccups.
        """
        return self._connected

    @property
    def attributes(self) -> dict[str, Any]:
        """Everything needed to judge how much the boolean above is worth."""
        now = time.monotonic()
        quiet_for = (
            None if self._last_gcs_at is None else round(now - self._last_gcs_at, 1)
        )
        return {
            "connection_state": self._reported,
            "last_checked": _utc_iso(self._checked_at),
            "checked_because": self._trigger,
            "checks": self._checks,
            "last_error": self._last_error,
            "cloud_last_connected": self._last_connected_epoch,
            "seconds_since_valve_message": quiet_for,
            "contradiction_watch": bool(self._coordinator.controllers),
        }

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def async_start(self) -> None:
        """Arm the quiet timer. Idempotent — safe to call again on every reconnect."""
        self._stopped = False
        self._arm_quiet_timer()

    def async_stop(self) -> None:
        """Cancel every pending timer and in-flight read. Called from entry unload."""
        self._stopped = True
        for cancel in (self._pair_cancel, self._quiet_cancel):
            if cancel is not None:
                cancel()
        self._pair_cancel = None
        self._quiet_cancel = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    # ------------------------------------------------------------------ #
    # Push — fed from the coordinator's envelope handler
    # ------------------------------------------------------------------ #
    def note_gcs_message(self) -> None:
        """A valve message arrived: it is reachable, by definition.

        This is the cheapest possible confirmation and it is why the module costs nothing on
        an active day. It also settles any pending contradiction — a valve message inside the
        pair window is exactly what trigger A was waiting to see.
        """
        self._last_gcs_at = time.monotonic()
        if self._pair_cancel is not None:
            self._pair_cancel()
            self._pair_cancel = None
        # **Armed once, not re-armed per message.** The timestamp above is what decides
        # whether the valve has gone quiet; the timer only needs to wake up and compare.
        # Cancelling and rebuilding a three-hour deadline on every message allocated a
        # `HassJob` and a loop timer handle thousands of times a day to move a deadline that
        # is checked once — see `_quiet_elapsed`, which now re-arms for the remaining time
        # rather than firing early.
        if self._quiet_cancel is None:
            self._arm_quiet_timer()

    def note_hub_envelope(self, envelope: Envelope) -> None:
        """Trigger A. Only ``SHOWER_VALVE_STS`` with a zone ``ON`` is evidence."""
        if envelope.code != MSG_HUB_SHOWER_VALVE:
            return
        if not any(
            isinstance(item, dict) and item.get("status") == "ON"
            for item in envelope.attributes
        ):
            # An all-OFF card is republished without the valve doing anything. See the module
            # docstring: this is not a weaker signal, it is not a signal.
            return
        now = time.monotonic()
        if self._last_gcs_at is not None and now - self._last_gcs_at <= (
            CLOUD_CHECK_PAIR_WINDOW_SECONDS
        ):
            # Already paired by a valve message in the window's trailing half.
            return
        if self._pair_cancel is not None:
            # A check is already pending for an earlier report in the same shower.
            return
        _LOGGER.debug(
            "Controller reports a zone ON with no valve message in %.0fs; watching for %.0fs",
            CLOUD_CHECK_PAIR_WINDOW_SECONDS,
            CLOUD_CHECK_PAIR_WINDOW_SECONDS,
        )
        self._pair_cancel = async_call_later(
            self._hass, CLOUD_CHECK_PAIR_WINDOW_SECONDS, self._pair_window_elapsed
        )

    # ------------------------------------------------------------------ #
    # Timers
    # ------------------------------------------------------------------ #
    #
    # ⚠️ **Both handlers below MUST carry `@callback`, and it is load-bearing, not style.**
    #
    # `async_call_later` wraps a bare callable in a `HassJob`, and `HassJob` infers its type
    # from the callable: a coroutine function runs on the loop, a function marked `@callback`
    # runs on the loop, and **anything else is classified `HassJobType.Executor` and dispatched
    # to a worker thread** (`homeassistant/core.py`, `get_hassjob_callable_job_type`). Nothing
    # warns about it — the handler simply runs on the wrong thread.
    #
    # That is fatal here rather than merely untidy, because `_request_check` ends in
    # `hass.async_create_task`, which raises for a custom integration the moment it sees a
    # foreign thread id. The raise escapes `_quiet_elapsed` **before** its closing
    # `_arm_quiet_timer()`, so trigger B does not just miss one check — it fires once and then
    # never re-arms for the life of the coordinator.
    #
    # Shipped that way in v0.2.6 and found in session 22: across 45 h and an 11 h 44 m valve
    # silence, `checked_because` was "REST seed" in 120 of 120 recorded states. Neither trigger
    # had ever fired. `select.py` had the decorator on its own `async_call_later` handler all
    # along; this module was written without it.
    @callback
    def _pair_window_elapsed(self, _now: Any) -> None:
        """The full ±window passed with no valve message. Ask the cloud."""
        self._pair_cancel = None
        self._request_check("controller reported a zone ON, valve silent")

    def _arm_quiet_timer(self, seconds: float | None = None) -> None:
        """Start trigger B's countdown, for the full window or the remainder of one."""
        if self._stopped:
            return
        if self._quiet_cancel is not None:
            self._quiet_cancel()
        self._quiet_cancel = async_call_later(
            self._hass,
            CLOUD_CHECK_QUIET_SECONDS if seconds is None else max(seconds, 1.0),
            self._quiet_elapsed,
        )

    @callback
    def _quiet_elapsed(self, _now: Any) -> None:
        """Trigger B. Ask if the valve really has been quiet; otherwise wait out the rest.

        The timer is armed once and left alone, so it can fire while the valve has been
        talking — `note_gcs_message` moves the timestamp without touching the deadline. That
        is the case this checks: if the last message is more recent than the quiet window,
        nothing is wrong and the timer is simply re-armed for the time that remains.
        """
        self._quiet_cancel = None
        # ⚠️ **`_last_gcs_at` is None until the valve's first MQTT message**, and on a
        # push-only integration that can be hours — the module header records benign
        # silences of 12 h and 35 h. Unguarded, this raised `TypeError` **after**
        # `_quiet_cancel` was nulled and **before** either re-arm below, so trigger B died
        # for the life of the coordinator: exactly the "fires once and never re-arms"
        # failure the `@callback` note above describes, through a different door.
        #
        # No timestamp means nothing has been heard since setup, which is precisely what
        # this trigger exists to ask about — so treat it as the full quiet window rather
        # than skipping the check.
        if self._last_gcs_at is None:
            self._request_check(
                f"no valve message since setup ({CLOUD_CHECK_QUIET_SECONDS / 3600:.0f}h)"
            )
            self._arm_quiet_timer()
            return
        quiet_for = time.monotonic() - self._last_gcs_at
        if quiet_for >= CLOUD_CHECK_QUIET_SECONDS:
            self._request_check(
                f"no valve message for {CLOUD_CHECK_QUIET_SECONDS / 3600:.0f}h"
            )
            # Re-armed after asking, including when the check was skipped or failed: the
            # point of trigger B is that it keeps asking while the valve stays quiet.
            self._arm_quiet_timer()
            return
        self._arm_quiet_timer(CLOUD_CHECK_QUIET_SECONDS - quiet_for)

    # ------------------------------------------------------------------ #
    # The read
    # ------------------------------------------------------------------ #
    def _request_check(self, trigger: str) -> None:
        """Apply the guards, then spawn the read. Never blocks the caller."""
        if self._stopped:
            return

        stream = self._coordinator.stream
        if stream is None or not stream.connected:
            # Our own stream is down, so the valve's silence is ours, not its. The reconnect
            # path already reseeds from REST; asking here would report our outage as the
            # valve's.
            _LOGGER.debug("Cloud check skipped (%s): our MQTT stream is down", trigger)
            return

        now = time.monotonic()
        if (
            self._last_check_at is not None
            and now - self._last_check_at < CLOUD_CHECK_COOLDOWN_SECONDS
        ):
            _LOGGER.debug(
                "Cloud check skipped (%s): %.0fs since the last one, cooldown is %.0fs",
                trigger,
                now - self._last_check_at,
                CLOUD_CHECK_COOLDOWN_SECONDS,
            )
            return

        if self._task is not None and not self._task.done():
            # A check is already in flight. Return **without stamping the cooldown**: this
            # call did nothing, and burning the next half hour on a check that never ran
            # would let a genuine contradiction be swallowed by an unrelated one already
            # in progress. Trigger A fires on bursty controller traffic, so two arriving
            # together is ordinary rather than exceptional.
            return
        self._last_check_at = now
        self._task = self._hass.async_create_task(self._async_check(trigger))

    async def _async_check(self, trigger: str) -> None:
        """One GET of ``gcs-state``, read for ``connectionState`` and nothing else.

        ⚠️ **The payload is deliberately not applied to :class:`GcsState`.** It would be free
        state, but `apply_rest_state` feeds the warmup-change machinery, and this runs
        unattended at arbitrary hours — including 3 a.m. on trigger B. A reachability check
        must not be able to start a warmup restore as a side effect. The reseed paths that
        *are* meant to apply state still do.
        """
        device = self._valve.gcs_device
        try:
            payload = await self._coordinator.client.async_get_gcs_state(
                device.device_id
            )
        except (AuthError, KohlerError) as err:
            # Explicitly not a verdict about the valve. `_connected` keeps its previous value.
            self._last_error = f"{type(err).__name__}: {err}"
            _LOGGER.debug("Cloud check (%s) could not reach Kohler: %s", trigger, err)
            self._coordinator.async_refresh_entities()
            return

        self.note_rest_payload(payload, trigger)

    def note_rest_payload(
        self, payload: Any, trigger: str, *, notify: bool = True
    ) -> None:
        """Take the answer out of a ``gcs-state`` payload somebody else already fetched.

        **This is the cheap path and it is the important one.** The integration reads
        ``gcs-state`` at setup, on every MQTT reconnect, on a manual ``update_entity``, and on
        every warmup write — four places, all of which had this field in hand and dropped it,
        because `GcsState.apply_rest_state` starts at ``payload["state"]`` and never looks at
        its siblings. Routing those reads through here means the sensor has a real value from
        the moment the integration starts, instead of sitting at ``unknown`` until one of the
        two triggers happens to fire — which on a quiet account is up to three hours.

        These calls cost **nothing**: the request was already made and paid for. They do not
        touch the cooldown, which governs only reads this module initiates — a free answer
        must never be able to suppress an investigation, because the valve can be Connected at
        one moment and gone two minutes later. That is the whole failure mode.

        ``notify`` is False for the setup seed, which pushes its own snapshot immediately
        afterwards and must not notify listeners before the platforms exist.
        """
        reported = (payload or {}).get("connectionState")
        self._reported = None if reported is None else str(reported)
        self._last_connected_epoch = (payload or {}).get("lastConnected")
        self._checked_at = time.time()
        self._trigger = trigger
        self._checks += 1
        self._last_error = None

        if reported is None:
            # The field is documented and observed, so its absence is worth saying out loud
            # rather than silently reading as "not connected".
            self._last_error = "gcs-state carried no connectionState field"
            _LOGGER.warning(
                "Kohler gcs-state returned no connectionState; cannot judge reachability"
            )
            if notify:
                self._coordinator.async_refresh_entities()
            return

        was = self._connected
        value = str(reported).strip().lower()
        if value not in (CONNECTED, DISCONNECTED) and value != self._unfamiliar:
            self._unfamiliar = value
            _LOGGER.info(
                "Kohler gcs-state reported an unfamiliar connectionState %r; treating it "
                "as reachable, as the Konnect app does",
                reported,
            )
        self._connected = value != DISCONNECTED
        if self._connected:
            _LOGGER.debug("Cloud check (%s): valve reachable (%s)", trigger, reported)
        elif was is not False:
            # Once per transition, at WARNING: this is the condition the module exists for and
            # the user cannot see it anywhere else.
            _LOGGER.warning(
                "Kohler's cloud reports the Anthem valve as %s — checked because %s. "
                "This clears on a power cycle of the valve, not from Home Assistant.",
                reported,
                trigger,
            )
        if notify:
            self._coordinator.async_refresh_entities()
