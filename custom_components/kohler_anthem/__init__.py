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
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import CONF_VALVES, DOMAIN, ZONE_GROUPING_SUBDEVICES
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
    # Endless Shower's two settings, removed 2026-10-08. These are the flat spellings from
    # before 2026-09-08; the per-valve copies are `_REMOVED_VALVE_KEYS` below.
    "outlet_run_times",
    "restart_on_runtime_cutoff",
)

# ---------------------------------------------------------------------------
# Removed 2026-10-08 — Endless Shower
# ---------------------------------------------------------------------------
# Its settings as stored per valve under `CONF_VALVES`, in data (`outlet_run_times`, the
# learned run-time limits) and options (`restart_on_runtime_cutoff`, the switch).
_REMOVED_VALVE_KEYS = ("outlet_run_times", "restart_on_runtime_cutoff")
# Its Repairs cards: "Endless Shower can't act yet" and "the two Max Shower Durations
# differ". Issues are stored apart from the entry, so they would otherwise outlive it.
_REMOVED_ISSUE_PREFIXES = ("endless_shower_not_set_up", "durations_differ")

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
    # The Endless Shower switch, removed 2026-10-08.
    "_keep_water_running",
)


@callback
def _async_purge_removed_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Strip removed features' stored state from Home Assistant.

    Covers every half of "removed": the config-entry keys they persisted, the entity
    registry rows they own, and any Repairs card they raised. Runs on every setup and is a
    no-op once clean, so a downgrade followed by an upgrade cannot leave orphans behind.
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

    _async_strip_removed_valve_keys(hass, entry)

    registry = er.async_get(hass)
    for row in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if row.unique_id.endswith(_REMOVED_UNIQUE_ID_SUFFIXES):
            registry.async_remove(row.entity_id)
            _LOGGER.info("Removed retired entity %s", row.entity_id)

    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(_REMOVED_ISSUE_PREFIXES):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _async_strip_removed_valve_keys(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop `_REMOVED_VALVE_KEYS` from every valve's settings under `CONF_VALVES`."""

    def stripped(container: Mapping[str, Any]) -> dict[str, Any] | None:
        valves = container.get(CONF_VALVES)
        if not isinstance(valves, dict) or not any(
            key in (settings or {})
            for settings in valves.values()
            for key in _REMOVED_VALVE_KEYS
        ):
            return None
        return {
            **container,
            CONF_VALVES: {
                device_id: {
                    k: v
                    for k, v in (settings or {}).items()
                    if k not in _REMOVED_VALVE_KEYS
                }
                for device_id, settings in valves.items()
            },
        }

    data = stripped(entry.data)
    options = stripped(entry.options)
    if data is None and options is None:
        return
    hass.config_entries.async_update_entry(
        entry,
        data=entry.data if data is None else data,
        options=entry.options if options is None else options,
    )
    _LOGGER.info("Removed the retired Endless Shower settings from the config entry")


@callback
def _async_ensure_parent_valve_devices(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: KohlerAnthemCoordinator
) -> None:
    """Register parent valve devices before platforms add `via_device` sub-devices."""
    if coordinator.zone_grouping != ZONE_GROUPING_SUBDEVICES:
        return
    try:
        dev_reg = dr.async_get(hass)
        for valve in coordinator.valves:
            if len(valve.model.zones) > 1:
                dev_reg.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    identifiers={(DOMAIN, valve.device_id)},
                    manufacturer="Kohler",
                    name=valve.name,
                    model=valve.model.sku,
                    model_id=valve.model.name,
                    serial_number=valve.gcs_device.serial_number,
                )
    except Exception:
        _LOGGER.debug("Device registry unavailable during parent valve setup")


@callback
def _async_cleanup_zone_subdevices(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: KohlerAnthemCoordinator
) -> None:
    """Remove zone sub-devices when sub-device grouping is not active."""
    active_subdevice_ids: set[str] = set()
    if coordinator.zone_grouping == ZONE_GROUPING_SUBDEVICES:
        for valve in coordinator.valves:
            if len(valve.model.zones) > 1:
                for zone in valve.model.zones:
                    active_subdevice_ids.add(f"{valve.device_id}_zone_{zone}")

    valve_prefixes = tuple(f"{valve.device_id}_zone_" for valve in coordinator.valves)
    if not valve_prefixes:
        return

    try:
        dev_reg = dr.async_get(hass)
        devices = list(dr.async_entries_for_config_entry(dev_reg, entry.entry_id))
    except Exception:
        _LOGGER.debug("Device registry unavailable during zone sub-device cleanup")
        return

    for device in devices:
        for domain, identifier in device.identifiers:
            if (
                domain == DOMAIN
                and identifier.startswith(valve_prefixes)
                and identifier not in active_subdevice_ids
            ):
                dev_reg.async_remove_device(device.id)
                _LOGGER.info("Removed unused zone sub-device %s", device.name)
                break


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
    _async_ensure_parent_valve_devices(hass, entry, coordinator)
    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_cleanup_zone_subdevices(hass, entry, coordinator)
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
        coordinator: KohlerAnthemCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown_stream()
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
            # Only once the last entry is gone: the services are shared, so removing them
            # while another entry is still loaded would break it.
            async_unregister_services(hass)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when the entry changed in a way that needs one.

    This integration writes to its own config entry while running — above all the rotating
    refresh token, whenever B2C issues a new one. Every one of those writes fires this
    listener. Reloading on them would flap all entities to ``unavailable`` and drop the MQTT
    connection with its warm-up.

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
