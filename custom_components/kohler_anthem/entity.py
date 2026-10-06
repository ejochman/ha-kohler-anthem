"""Shared entity bases.

Two kinds of device are registered, never merged, because they behave differently and their
state arrives on different schedules:

* **Anthem Valve** — a digital valve. Authoritative for outlets, temperature, and flow.
  An account can have several, each its own device bound to its own
  :class:`~.coordinator.Valve`.
* **Anthem Plus** — a system controller. Owns favorites, music, steam, and lighting. An
  account can have several — one per bathroom — and each is its own device, bound to its
  own :class:`~.coordinator.Controller`.

A valve and a controller are usually the same physical shower reached through two different
touchscreens, but presenting them as one device would imply a consistency that does not
exist.

The SKU strings ``GCS`` and ``HUB`` appear nowhere a user can see them. They exist only in
Kohler's API — not in the app, the manual, or on the hardware — so every user-facing string
uses the names Kohler itself shows: "Anthem" and "Anthem Plus".
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_NAME_CONTROLLER,
    DEVICE_NAME_VALVE,
    DOMAIN,
    OUTLET_TYPE_NAMES,
)
from .coordinator import Controller, KohlerAnthemCoordinator, Valve

__all__ = [
    "DEVICE_NAME_CONTROLLER",
    "DEVICE_NAME_VALVE",
    "KohlerControllerEntity",
    "KohlerValveEntity",
    "outlet_name",
    "slug",
    "zone_label",
]


def slug(name: str) -> str:
    """`Zone 2 Rainhead` -> `zone_2_rainhead`, for building a unique id from a name."""
    return "_".join(part.lower() for part in name.split())


def zone_label(device: Valve | Controller, zone: int, label: str) -> str:
    """`Temperature` on a single-zone device, `Temperature 2` on a two-zone one.

    With one zone there is nothing to disambiguate, and a number on every entity of a
    3-outlet valve is noise. A multi-zone valve appends the zone number, because a bare
    `Temperature` would be ambiguous across zones.

    **The number is a suffix, not a `Zone N` prefix.** It sorts the related entities together
    in every Home Assistant list — `Temperature`, `Temperature 2` rather than `Temperature`
    stranded away from `Zone 2 Temperature` — and it reads the way the fixtures do
    (`Showerhead 1`, `Showerhead 2`).

    Takes a valve or a controller — both carry a `model`, and only the zone count is read,
    so the controller's own zone entities are named the same way the valve's are. They were
    not, until 0.8.1: a controller showed `Zone 1 Temperature` beside the valve's plain
    `Temperature`, which read as two different things rather than two views of one shower.
    """
    if len(device.model.zones) <= 1:
        return label
    return f"{label} {zone}"


def outlet_name(valve: Valve, zone: int, outlet: int) -> str:
    """`Rainhead`, `Rainhead 2` on a multi-zone valve, or `Outlet 1` when unknown.

    Lives here rather than on the switch because the outlet's run-time sensor needs the
    same name — `Rainhead Max Run Time` beside `Rainhead` — and a sensor platform reaching
    into a switch platform for it would couple the two for no reason.

    Three rules, in order:

    * **The fixture name wins** where the valve's `outLetType` maps to a confirmed one —
      `Rainhead` says what the entity does in a way `Outlet 1` never can.
    * **The zone number is dropped on a single-zone valve**, per :func:`zone_label`.
    * **An unknown code falls back to the position** — `Zone 1 Outlet 3`. Naming an outlet
      after a code nobody has confirmed would be inventing a fixture; the number is honest.

    Read **once, at construction**. Per-outlet types arrive gradually over MQTT and via the
    REST seed, so a valve that has not announced yet names its outlets by position and picks
    up fixture names on the next restart. Renaming entities live would change their ids
    underneath running automations, which is worse than waiting.
    """

    def fixture_at(position: int) -> str | None:
        """The confirmed fixture name for a 1-based outlet in this zone, or None."""
        flat = valve.model.outlet_id(zone, position)
        limits = valve.gcs_state.outlet_limits.get(flat)
        code = None if limits is None else limits.outlet_type
        return None if code is None else OUTLET_TYPE_NAMES.get(code)

    fixture = fixture_at(outlet)
    if fixture is None:
        # No confirmed fixture: name it by position. `Outlet 1` on a single-zone valve, and
        # `Outlet 1.2` on a multi-zone one, matching the fixture numbering below rather than
        # reintroducing a `Zone N` prefix the rest of the scheme has dropped.
        if len(valve.model.zones) > 1:
            return f"Outlet {zone}.{outlet}"
        return f"Outlet {outlet}"

    # **Two outlets of the same fixture type in one zone is legal** — a pair of body sprays,
    # or the two showerheads a K-28212 can carry. Naming both `Showerhead` would build the
    # same unique id twice, and Home Assistant drops the second silently: one outlet would
    # simply not exist, with no error to explain it. So a repeated fixture keeps its
    # position as a suffix, and only a repeated one does.
    same = [
        position
        for position in range(1, valve.model.outlets_in_zone(zone) + 1)
        if fixture_at(position) == fixture
    ]
    if len(same) > 1:
        # Both this suffix and `zone_label`'s are bare numbers, so applying them together
        # would read `Showerhead 2 1` — two numbers meaning different things, in an order
        # nobody can guess. The zone leads, because that is the coarser grouping: the second
        # showerhead in zone 2 is `Showerhead 2.2`, and in a single-zone valve just
        # `Showerhead 2`.
        position = same.index(outlet) + 1
        if len(valve.model.zones) > 1:
            return f"{fixture} {zone}.{position}"
        return f"{fixture} {position}"
    return zone_label(valve, zone, fixture)


class KohlerValveEntity(CoordinatorEntity[KohlerAnthemCoordinator]):
    """Base for entities belonging to one Anthem digital valve.

    Takes the :class:`~.coordinator.Valve` it belongs to, for the same reason the
    controller base takes a `Controller`: the coordinator holds every valve on the account,
    and an entity reads and commands exactly one. Unique ids are built on that valve's
    device id, so a single-valve install keeps every id it had.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator)
        self._valve = valve
        self._device_id = valve.device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, valve.device_id)},
            # "Anthem Valve" alone with one valve; suffixed with the Konnect name when
            # there are several — see `coordinator.valve_names`.
            name=valve.name,
            manufacturer="Kohler",
            # The valve's own layout — detected from the valve, else the model chosen at
            # setup — which is what is printed on the hardware, far more useful than the
            # API's "GCS".
            model=valve.model.sku,
            model_id=valve.model.name,
            serial_number=valve.gcs_device.serial_number,
        )

    @property
    def _state(self):
        return self._valve.gcs_state

    @property
    def available(self) -> bool:
        return super().available


class ZoneWordEntity(KohlerValveEntity):
    """A valve entity scoped to one zone, reading that zone's command word.

    Zone number to word is the same two lines wherever it appears, and getting it wrong is
    not a visible error — it silently reads the *other* zone, so a two-zone shower would
    report and command the wrong half. Kept in one place for that reason rather than for
    the five lines.
    """

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, valve: Valve, zone: int
    ) -> None:
        super().__init__(coordinator, valve)
        self._zone = zone

    @property
    def _word(self):
        """This zone's command word, or None before any state has arrived."""
        state = self._state
        if state is None:
            return None
        return state.valve1 if self._zone == 1 else state.valve2


class KohlerControllerEntity(CoordinatorEntity[KohlerAnthemCoordinator]):
    """Base for entities belonging to one Anthem Plus system controller.

    Takes the :class:`~.coordinator.Controller` it belongs to, not just the coordinator:
    the coordinator holds every controller on the account, and an entity reads and commands
    exactly one of them. Unique ids are built on that controller's device id, so a
    single-controller install keeps every id it had before the list existed.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator)
        self._controller = controller
        self._device_id = controller.device_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, controller.device_id)},
            # "Anthem Plus" alone with one controller; suffixed with the Konnect name when
            # there are several — see `coordinator.controller_names`.
            name=controller.name,
            manufacturer="Kohler",
            model="Anthem+ System Controller",
            serial_number=controller.device.serial_number,
        )

    @property
    def _state(self):
        return self._controller.state

    @property
    def available(self) -> bool:
        """Available whenever the entry is — deliberately no freshness test.

        Session 10 flagged that 18 hours of silence looks healthy here; closed 2026-08-22 as
        designed. This integration is push-only, so silence is the normal state of an unused
        shower — "no messages" means "no changes", not "no data" — and the REST reseed
        refreshes controller state on every reconnect. A staleness timeout would mark a
        healthy-but-quiet system unavailable on every calm day, and the one honest probe (the
        local ping) was removed 2026-08-15 as the integration's only polling loop. The
        controller's Last Update sensor is the freshness surface instead. See
        `docs/hub/cloud_api.md` §5.1.
        """
        return super().available
