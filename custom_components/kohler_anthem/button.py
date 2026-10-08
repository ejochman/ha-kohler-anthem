"""Buttons for the Kohler Anthem integration.

Two: restart a valve (below), and start a new raw MQTT capture file. That exists because the
natural way to get a fresh capture — restart Home Assistant — costs a full reload and drops
the MQTT connection, which nobody wants in the middle of a sequence of shower experiments.

Pressing this rolls the file instead: the current one is closed and a new one opened, so each
experiment lands in its own file rather than being separated by timestamp afterwards.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import Controller, KohlerAnthemCoordinator, Valve
from .entity import KohlerControllerEntity, KohlerValveEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the capture button on whichever device this account has."""
    coordinator: KohlerAnthemCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [ValveRestartButton(coordinator, valve) for valve in coordinator.valves]
    )

    # The capture covers the whole account rather than one device, so it only needs to live
    # somewhere findable. The first valve the cloud lists is the primary device where one
    # exists; a controller-only account gets it on the first controller — one button,
    # because it is one capture, and a copy per device would be several rows for the
    # same action.
    if coordinator.valves:
        async_add_entities([ValveNewCaptureButton(coordinator, coordinator.valves[0])])
    elif coordinator.controllers:
        async_add_entities(
            [ControllerNewCaptureButton(coordinator, coordinator.controllers[0])]
        )


class ValveRestartButton(KohlerValveEntity, ButtonEntity):
    """Reboot the valve — the Konnect app's Settings → Restart Product.

    ``valvereset {reset: "productRestart"}``, the body Konnect 3.0.6 sends. The app asks
    "Are you sure you want to restart this product?" first and shows "Rebooting Product";
    a Home Assistant button cannot ask, which is why this one is **disabled by default** —
    enable it on purpose, from the device page.

    🚿 **A restart stops any running water.** It **cannot** bring back a valve that has
    dropped off the cloud: that valve never receives the command — power-cycle it at the
    breaker instead.

    Added 2026-10-07; app-confirmed, not yet pressed against hardware by this integration.
    """

    _attr_name = "Restart"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_restart"

    async def async_press(self) -> None:
        await self._valve.async_restart()


class _NewCaptureMixin:
    """Roll the raw MQTT capture file. Shared so both device variants behave identically."""

    _attr_name = "Start new MQTT capture"
    _attr_icon = "mdi:file-restore-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    async def async_press(self) -> None:
        raw_log = self.coordinator.raw_log
        if raw_log is None:
            _LOGGER.warning("No diagnostic capture is set up; nothing to roll")
            return
        # Opens a file and creates a directory — off the event loop.
        raw_path = await self.hass.async_add_executor_job(raw_log.roll)
        if raw_path is None:
            _LOGGER.warning(
                "The raw MQTT capture is OFF, so there is nothing to roll. Turn it on with "
                "ENABLE_RAW_MQTT_LOG in const.py, or the logger.set_level action on "
                "custom_components.kohler_anthem.anthem.raw_log"
            )
            return
        _LOGGER.warning("Started a new raw MQTT capture file: %s", raw_path)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        raw_log = self.coordinator.raw_log
        return {
            "capture_enabled": bool(raw_log and raw_log.enabled),
            "current_file": (raw_log.path if raw_log else None),
        }


class ValveNewCaptureButton(_NewCaptureMixin, KohlerValveEntity, ButtonEntity):
    """Capture button on the Anthem Valve device."""

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_new_mqtt_capture"

    @property
    def available(self) -> bool:
        """A diagnostic action, usable whenever the entry is loaded."""
        return self.coordinator.last_update_success


class ControllerNewCaptureButton(
    _NewCaptureMixin, KohlerControllerEntity, ButtonEntity
):
    """Capture button on a controller-only account. See :class:`ValveNewCaptureButton`."""

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_new_mqtt_capture"

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success
