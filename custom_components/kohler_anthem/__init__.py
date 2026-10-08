"""The Kohler Anthem integration.

Supports both products in the Anthem line, and works with either or both on an account —
any number of each, every valve and every controller as its own device:

* **Anthem** (SKU ``GCS``) — the digital valve with built-in Wi-Fi. Full outlet,
  temperature, and flow control.
* **Anthem Plus** (SKU ``HUB``) — the Linux system controller that adds music, lighting,
  and steam. Controlled through favorites.

State is push-only over Azure IoT Hub MQTT — there is no polling interval. REST is read on
events: once at setup and again on every MQTT (re)connect, because the broker replays
nothing on connect. All protocol handling lives in the bundled ``anthem`` package,
which has no Home Assistant imports and can be tested offline.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_NOT_SET_UP
from .coordinator import KohlerAnthemCoordinator, entry_reload_signature
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]

# ---------------------------------------------------------------------------
# Removed 2026-08-15 — valve reboot counter, controller ping, outage counter
# ---------------------------------------------------------------------------
# Config-entry keys the old diagnostics persisted. They are dead weight now, and leaving
# them would make `_async_update_listener` see a spurious difference on the first load.
_REMOVED_ENTRY_KEYS = (
    "gcs_reboot_count",
    "gcs_reboot_last",
    "hub_local_host",
    "hub_outage_count",
    "hub_outage_last",
    "hub_outage_last_seconds",
)

# Unique-ID suffixes of the entities those diagnostics created. Home Assistant keeps a
# registry row for every entity it has ever seen, so without this the three would linger as
# permanently unavailable rows that only a manual delete would clear.
_REMOVED_UNIQUE_ID_SUFFIXES = (
    "_reboot_count",
    "_local_outages",
    "_local_reachable",
    # `Total Water Used`, retired in 0.14.0. It published `totalFlow`, which is not a meter:
    # across the whole reference corpus it took **three distinct values** and shifted between
    # two scales exactly 4x apart, with no water running. As a `total_increasing` sensor every
    # shift read as a meter replacement and injected a phantom spike into long-term
    # statistics. `Water Used This Year` and `Water Used This Month` publish Kohler's own
    # usage series instead, in units that are actually established.
    "_total_water",
    # `Max Shower Duration` and `Max Temperature` as read-only diagnostics, retired in
    # 0.18.1. Both became configuration entities in 0.18.0 — a number and a select that
    # report the same values and can also change them — so the sensors were a second copy
    # of a setting, showing the same figure with no way to act on it.
    #
    # ⚠️ **Outlet-qualified on purpose.** The controls' own ids end `_max_temperature_setting`
    # and `_max_run_time_setting`; a bare `_max_temperature` suffix would not match those
    # today, but naming the outlet makes it impossible for a future id to collide and purge
    # the control along with the sensor it replaced.
    "_outlet_1_max_run_time",
    "_outlet_1_max_temperature",
)


@callback
def _async_purge_removed_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Strip the removed diagnostics' stored state from Home Assistant.

    Covers both halves of "removed": the config-entry keys they persisted, and the entity
    registry rows they own. Runs on every setup and is a no-op once clean, so a downgrade
    followed by an upgrade cannot leave orphans behind.
    """
    stale = {key: entry.data[key] for key in _REMOVED_ENTRY_KEYS if key in entry.data}
    if stale:
        hass.config_entries.async_update_entry(
            entry,
            data={k: v for k, v in entry.data.items() if k not in _REMOVED_ENTRY_KEYS},
            options={
                k: v for k, v in entry.options.items() if k not in _REMOVED_ENTRY_KEYS
            },
        )
        _LOGGER.info(
            "Removed stale diagnostic keys from the config entry: %s",
            ", ".join(sorted(stale)),
        )
    elif any(key in entry.options for key in _REMOVED_ENTRY_KEYS):
        hass.config_entries.async_update_entry(
            entry,
            options={
                k: v for k, v in entry.options.items() if k not in _REMOVED_ENTRY_KEYS
            },
        )

    registry = er.async_get(hass)
    for row in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if row.unique_id.endswith(_REMOVED_UNIQUE_ID_SUFFIXES):
            registry.async_remove(row.entity_id)
            _LOGGER.info("Removed retired diagnostic entity %s", row.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Kohler Anthem from a config entry."""
    _async_purge_removed_diagnostics(hass, entry)
    coordinator = KohlerAnthemCoordinator(hass, entry)
    # **Everything after `async_setup` must be unwound on failure.** By the time it
    # returns, the MQTT stream is connected, four journal files are open, and every valve
    # has armed its cloud-watch timers — but the coordinator is not yet in `hass.data`, so
    # a raise here means Home Assistant discards it without ever calling
    # `async_unload_entry`. Left alone that strands a paho network thread with its own
    # reconnect loop, the open files, and timers that fire into a dead coordinator; and
    # because `ConfigEntryNotReady` is retried, each attempt stacks another set.
    await coordinator.async_setup()
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_shutdown_stream()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    # Services are global, not per entry — `async_register_services` is idempotent so this
    # is safe on every entry and every reload. It registers nothing for a HUB-only account:
    # `send_valve_hex` writes to a valve endpoint that such an account does not have.
    # With several valves the actions take a `device_id` to say which one.
    async_register_services(hass, coordinator)

    # Deliberately no "GCS"/"HUB" here: those strings exist only inside Kohler's API and
    # appear nowhere the owner can see them — not the app, the manual, or the hardware.
    found = ", ".join(
        filter(
            None,
            (
                # Every device, each by the name its device page will carry — and, for a
                # valve, the layout it decodes with, which is its own rather than the entry's.
                #
                # **No device ids here.** They are cloud addresses, this line is INFO, and
                # `home-assistant.log` is what people attach to issues — so printing them
                # here handed over exactly what `diagnostics.py` goes to length to redact.
                # The name and SKU identify the device to its owner, which is all this line
                # is for; anyone needing the id has diagnostics, where it is labelled.
                *(
                    f"{v.name} ({v.model.sku}, {v.model.total_outlets} outlets)"
                    for v in coordinator.valves
                ),
                *(c.name for c in coordinator.controllers),
            ),
        )
    )
    _LOGGER.info("Kohler Anthem ready (%s)", found or "no devices")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # Repairs outlive the config entry, so an entry being removed would otherwise leave a
        # card pointing at an integration that is no longer installed. Deleting a missing
        # issue is a no-op, so this is safe on a plain reload too — setup re-raises it if the
        # condition still holds.
        coordinator: KohlerAnthemCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        # The per-valve ids, plus the entry-only id an install from before 2026-09-08 may
        # still be carrying.
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_NOT_SET_UP}_{entry.entry_id}")
        for valve in coordinator.valves:
            ir.async_delete_issue(hass, DOMAIN, valve.issue_id)
        await coordinator.async_shutdown_stream()
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
            # Only once the last entry is gone: the services are shared, so removing them
            # while another entry is still loaded would break it.
            async_unregister_services(hass)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when the entry changed in a way that needs one.

    This integration writes to its own config entry while running — the rotating refresh
    token whenever B2C issues a new one, and ``maximumRunTime`` whenever the valve announces
    one, which it does unprompted and can do mid-shower. Every one of those writes fires this
    listener. Reloading on them would flap all entities to ``unavailable``, drop the MQTT
    connection with its warm-up, and reset the run-time cutoff's zone clocks while the valve's
    own timer kept running.

    So the decision is a comparison against ``coordinator.reload_signature``, the frozen
    snapshot taken when the coordinator was built. ``RELOAD_IGNORED_DATA_KEYS`` and
    ``RELOAD_IGNORED_OPTION_KEYS`` in ``const.py`` say what is excluded and why; anything
    else — including a key nobody anticipated — reloads.

    ⚠️ **Do not compare against ``coordinator.entry``.** That is the same object Home
    Assistant mutates in place, so it always equals ``entry`` and this listener becomes dead
    code that returns early every time. That was the defect here until 2026-08-17; see
    ``anthem/entry_reload.py``.
    """
    coordinator: KohlerAnthemCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None and entry_reload_signature(entry) == (
        coordinator.reload_signature
    ):
        return
    await hass.config_entries.async_reload(entry.entry_id)
