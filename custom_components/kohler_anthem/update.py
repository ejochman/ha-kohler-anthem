"""Firmware update entities — installed version, latest version, and whether to update.

**Read-only.** Each entity shows what Kohler's firmware endpoint reports for one part —
``/platform/api/v1/firmware/{gcs|gcs/gateway|hub}/{id}?releasetarget=Public`` — and offers
no Install button. Installing stays with the Konnect app, which refuses while water runs or
the device is ``Disconnected`` and walks the owner through an install that can take up to
two hours; a Home Assistant button that skipped all of that would be the wrong trade for a
once-a-year action.

Recovered from Konnect 3.0.6 on 2026-10-07: the response is ``{currentFirmware, firmware
(the latest), firmwareUpdateAvailable, mandatoryUpdate, otaStatus, skip, ...}``, and the app
decides "update available" from ``firmwareUpdateAvailable`` alone, which is what this does.
Read twice a day (`FIRMWARE_CHECK_INTERVAL`) — nothing pushes a release.

Never seen reporting an available update on the reference system, which has been current
throughout.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import Controller, KohlerAnthemCoordinator, Valve
from .entity import KohlerControllerEntity, KohlerValveEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """One update entity per valve, per gateway, and per controller."""
    coordinator: KohlerAnthemCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[UpdateEntity] = []
    for valve in coordinator.valves:
        entities.append(ValveFirmwareUpdate(coordinator, valve, "gcs"))
        entities.append(ValveFirmwareUpdate(coordinator, valve, "gateway"))
    for controller in coordinator.controllers:
        entities.append(ControllerFirmwareUpdate(coordinator, controller))
    async_add_entities(entities)


def _text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


class _FirmwareMixin:
    """Shared reading of one firmware response."""

    def _info(self) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def installed_version(self) -> str | None:
        return _text(self._info().get("currentFirmware"))

    @property
    def latest_version(self) -> str | None:
        """The release on offer when one is, else the installed version.

        Home Assistant shows "update available" whenever the two differ, so the latest
        version is reported only when the cloud says an update is available — a newer
        number on a release the cloud is not offering to this device is not an update.
        """
        info = self._info()
        installed = self.installed_version
        if str(info.get("firmwareUpdateAvailable")).strip().lower() == "true":
            return _text(info.get("firmware")) or installed
        return installed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._info()
        return {
            "mandatory_update": info.get("mandatoryUpdate"),
            "ota_status": info.get("otaStatus"),
            "skipped": info.get("skip"),
        }


class ValveFirmwareUpdate(_FirmwareMixin, KohlerValveEntity, UpdateEntity):
    """Firmware for the valve (``gcs``) or its Wi-Fi gateway (``gateway``)."""

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, valve: Valve, part: str
    ) -> None:
        super().__init__(coordinator, valve)
        self._part = part
        if part == "gateway":
            self._attr_name = "Gateway Firmware"
            self._attr_unique_id = f"{self._device_id}_gateway_firmware_update"
        else:
            self._attr_name = "Firmware"
            self._attr_unique_id = f"{self._device_id}_firmware_update"

    def _info(self) -> dict[str, Any]:
        return self._valve.firmware_info.get(self._part) or {}

    @property
    def in_progress(self) -> bool:
        """The valve's own `currentSystemState == "FirmwareUpdate"`, for its main firmware."""
        if self._part != "gcs":
            return False
        state = self._state
        return bool(state is not None and state.firmware_updating)


class ControllerFirmwareUpdate(_FirmwareMixin, KohlerControllerEntity, UpdateEntity):
    """Firmware for the Anthem Plus controller (``hub``)."""

    _attr_name = "Firmware"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_firmware_update"

    def _info(self) -> dict[str, Any]:
        return self._controller.firmware_info or {}
