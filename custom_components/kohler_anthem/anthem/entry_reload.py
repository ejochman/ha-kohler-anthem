"""Decide whether a config-entry update actually needs the integration reloaded.

Home Assistant fires an entry's update listener on **every** ``async_update_entry`` call,
and this integration writes to its own entry while running — above all the refresh token,
because Azure B2C rotates it and issues a new one on every refresh (``anthem/auth.py``), so
it is rewritten constantly. Per-valve bookkeeping such as the remembered warm-up mode is
written too.

Reloading on any of those would tear down and rebuild every platform, flap all entities to
``unavailable``, drop the MQTT connection with its warm-up, and reset the zone clocks behind
the time-left attributes while the valve's own timer keeps counting.

So the listener has to tell a real configuration change from the integration's own
bookkeeping, and it does that by comparing a **signature taken at setup** against the live
entry.

**Why "taken at setup" is the whole point.** The version this module replaces compared
``entry.data`` against ``coordinator.entry.data``. But the coordinator stores a *reference*
to the same ``ConfigEntry`` object Home Assistant mutates in place, so both sides were the
same attribute of the same object read one line apart. The comparison was an object against
itself, always equal, and the listener returned early every single time — nothing ever
reloaded. :func:`reload_signature` returns an immutable, fully-frozen value for exactly that
reason: a snapshot that cannot quietly follow the thing it is meant to be compared against.

Kept free of Home Assistant imports so the offline suite exercises the shipped decision
rather than a mirror of it. That distinction is not academic here — the defect above survived
two sessions because the surrounding logic was only ever re-implemented in tests, never run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["reload_signature"]


def _frozen(value: Any) -> Any:
    """Return an immutable, comparable copy of a config value.

    Deep rather than shallow on purpose. Home Assistant replaces whole mappings rather than
    editing them in place, so a shallow copy would be correct today — but the bug this
    module exists to fix *was* an aliasing bug, and a snapshot that can be changed from
    underneath is not a snapshot.
    """
    if isinstance(value, Mapping):
        # Sorted so two equal mappings with different insertion order compare equal. Keys
        # are unique, so the sort never has to fall through to comparing values.
        return tuple(sorted((str(key), _frozen(val)) for key, val in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(_frozen(item) for item in value))
    return value


def reload_signature(
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    *,
    ignore_data: Iterable[str] = (),
    ignore_options: Iterable[str] = (),
) -> tuple[Any, ...]:
    """Fingerprint everything about an entry that, if changed, means "reload".

    ``ignore_data`` holds the keys the running integration writes to its own entry, and
    ``ignore_options`` the options it reads live rather than caching. Everything else counts
    — including keys that appear or disappear, and including keys nobody thought about when
    this was written.

    That default is deliberate. An unrecognised key produces a reload, so a setting added
    later takes effect without anyone remembering to wire it up; the opposite default is
    what let the old listener be silently dead for two sessions. Suppressing a reload has to
    be a decision someone writes down, in ``RELOAD_IGNORED_DATA_KEYS`` or
    ``RELOAD_IGNORED_OPTION_KEYS``.
    """
    skip_data = frozenset(ignore_data)
    skip_options = frozenset(ignore_options)
    return (
        tuple(
            sorted(
                (key, _frozen(value))
                for key, value in data.items()
                if key not in skip_data
            )
        ),
        tuple(
            sorted(
                (key, _frozen(value))
                for key, value in options.items()
                if key not in skip_options
            )
        ),
    )
