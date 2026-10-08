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
    DEFAULT_ZONE_GROUPING,
    DEVICE_NAME_CONTROLLER,
    DEVICE_NAME_VALVE,
    DOMAIN,
    OUTLET_TYPE_NAMES,
    ZONE_GROUPING_NUMBERED,
    ZONE_GROUPING_OUTLET_LABELS,
    ZONE_GROUPING_SUBDEVICES,
)
from .coordinator import Controller, KohlerAnthemCoordinator, Valve

__all__ = [
    "DEVICE_NAME_CONTROLLER",
    "DEVICE_NAME_VALVE",
    "KohlerControllerEntity",
    "KohlerValveEntity",
    "outlet_name",
    "outlet_unique_id",
    "slug",
    "valve_device_info",
    "zone_device_id",
    "zone_device_info",
    "zone_label",
]


def slug(name: str) -> str:
    """`Zone 2 Rainhead` -> `zone_2_rainhead`, for building a unique id from a name."""
    return "_".join(part.lower() for part in name.split())


def valve_device_info(valve: Valve) -> DeviceInfo:
    """The valve's own device.

    One definition, because two places register it: every valve entity, and setup, which
    registers it ahead of the platforms whenever zone sub-devices need it as their parent.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, valve.device_id)},
        # "Anthem Valve" alone with one valve; suffixed with the Konnect name when there
        # are several — see `coordinator.valve_names`.
        name=valve.name,
        manufacturer="Kohler",
        # The valve's own layout — detected from the valve, else the model chosen at
        # setup — which is what is printed on the hardware, far more useful than the API's
        # "GCS".
        model=valve.model.sku,
        model_id=valve.model.name,
        serial_number=valve.gcs_device.serial_number,
    )


def zone_device_id(valve: Valve, zone: int) -> str:
    """The identifier of a zone's sub-device in `ZONE_GROUPING_SUBDEVICES` mode."""
    return f"{valve.device_id}_zone_{zone}"


def zone_device_info(valve: Valve, zone: int) -> DeviceInfo:
    """One zone of a multi-zone valve as its own device, linked to the valve's."""
    return DeviceInfo(
        identifiers={(DOMAIN, zone_device_id(valve, zone))},
        via_device=(DOMAIN, valve.device_id),
        name=f"{valve.name} Zone {zone}",
        manufacturer="Kohler",
        model=valve.model.sku,
        model_id=f"{valve.model.name} (Zone {zone})",
        serial_number=valve.gcs_device.serial_number,
    )


def _fixture_at(valve: Valve, zone: int, position: int) -> str | None:
    """The confirmed fixture name for a 1-based outlet in this zone, or None."""
    flat = valve.model.outlet_id(zone, position)
    limits = valve.gcs_state.outlet_limits.get(flat)
    code = None if limits is None else limits.outlet_type
    return None if code is None else OUTLET_TYPE_NAMES.get(code)


def zone_label(
    device: Valve | Controller,
    zone: int,
    label: str,
    grouping: str | None = None,
) -> str:
    """`Temperature` on a single-zone device, or disambiguated by the grouping mode.

    With one zone there is nothing to disambiguate, and a number on every entity of a
    3-outlet valve is noise. On a multi-zone valve, three modes are offered:

    * **Numbered (default)** — appends the zone number (`Temperature 1`, `Temperature 2`).
    * **Separate zone sub-devices** — each zone is its own device (`Anthem Valve Zone 1`),
      so the entity inside that device needs no suffix (`Temperature`).
    * **Outlet-labelled controls** — stays on one device and names the zone's outlets on
      the control or sensor (`Temperature (Showerhead, Body Sprays)`), falling back to
      the zone number until at least one fixture in the zone is known.

    Takes a valve or a controller — both carry a `model`. A controller has no per-outlet
    fixture codes, so on a multi-zone controller it always appends the zone number.
    """
    if len(device.model.zones) <= 1:
        return label
    if hasattr(device, "gcs_state"):
        mode = (
            grouping
            if grouping is not None
            else getattr(device, "zone_grouping", DEFAULT_ZONE_GROUPING)
        )
        if mode == ZONE_GROUPING_SUBDEVICES:
            return label
        if mode == ZONE_GROUPING_OUTLET_LABELS:
            count = device.model.outlets_in_zone(zone)
            if any(
                _fixture_at(device, zone, pos) is not None
                for pos in range(1, count + 1)
            ):
                outlets = ", ".join(
                    outlet_name(device, zone, pos, grouping=ZONE_GROUPING_SUBDEVICES)
                    for pos in range(1, count + 1)
                )
                return f"{label} ({outlets})"
    return f"{label} {zone}"


def outlet_name(
    valve: Valve,
    zone: int,
    outlet: int,
    grouping: str | None = None,
) -> str:
    """`Rainhead`, `Rainhead 2` on a multi-zone valve, or `Outlet 1` when unknown.

    Lives here rather than on the switch because the outlet's run-time sensor needs the
    same name — `Rainhead Max Run Time` beside `Rainhead` — and a sensor platform reaching
    into a switch platform for it would couple the two for no reason.

    Three rules, in order:

    * **The fixture name wins** where the valve's `outLetType` maps to a confirmed one —
      `Rainhead` says what the entity does in a way `Outlet 1` never can.
    * **The zone suffix follows the grouping mode:**
      - Single-zone valves and `subdevices` mode drop the zone number (`Showerhead`),
        since the device itself scopes the zone.
      - `outlet_labels` mode drops the zone number whenever the fixture type is unique
        across the valve (`Showerhead`, `Body Sprays`, `Rainhead`, `Handshower`),
        keeping it only if both zones carry the same fixture type on the single device.
      - `numbered` mode appends the zone number on multi-zone valves (`Showerhead 1`).
    * **An unknown code falls back to the position** — `Outlet 1` (or `Outlet 1.2` when
      multiple zones share one device). Naming an outlet after a code nobody has
      confirmed would be inventing a fixture; the number is honest.

    Read **once, at construction**. Per-outlet types arrive gradually over MQTT and via the
    REST seed, so a valve that has not announced yet names its outlets by position and picks
    up fixture names on the next restart. Renaming entities live would change their ids
    underneath running automations, which is worse than waiting.
    """
    mode = (
        grouping
        if grouping is not None
        else getattr(valve, "zone_grouping", DEFAULT_ZONE_GROUPING)
    )
    single_zone_scope = len(valve.model.zones) <= 1 or mode == ZONE_GROUPING_SUBDEVICES

    fixture = _fixture_at(valve, zone, outlet)
    if fixture is None:
        # No confirmed fixture: name it by position. `Outlet 1` when scoped to one zone,
        # and `Outlet 1.2` on a multi-zone single device, matching the numbering below.
        if not single_zone_scope:
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
        if _fixture_at(valve, zone, position) == fixture
    ]
    if len(same) > 1:
        # Both this suffix and `zone_label`'s are bare numbers, so applying them together
        # would read `Showerhead 2 1` — two numbers meaning different things, in an order
        # nobody can guess. The zone leads, because that is the coarser grouping: the second
        # showerhead in zone 2 is `Showerhead 2.2`, and in a single-zone scope just
        # `Showerhead 2`.
        position = same.index(outlet) + 1
        if not single_zone_scope:
            return f"{fixture} {zone}.{position}"
        return f"{fixture} {position}"

    if single_zone_scope:
        return fixture

    if mode == ZONE_GROUPING_OUTLET_LABELS:
        # Omit the zone number when this fixture type appears in only one zone.
        zones_with_fixture = [
            z
            for z in valve.model.zones
            for pos in range(1, valve.model.outlets_in_zone(z) + 1)
            if _fixture_at(valve, z, pos) == fixture
        ]
        if len(zones_with_fixture) == 1:
            return fixture
        return f"{fixture} {zone}"

    return zone_label(valve, zone, fixture, grouping=ZONE_GROUPING_NUMBERED)


def outlet_unique_id(valve: Valve, zone: int, outlet: int) -> str:
    """An outlet switch's unique id: the valve's id plus the outlet's **numbered** name.

    Fixed whatever `zone_grouping` says, so changing the mode renames an outlet switch in
    place rather than orphaning it. Everything that builds or migrates an outlet id must
    come through here: computing it from the *displayed* name instead gave `..._foot_sprays`
    in the sub-device and outlet-label modes, where the switch itself registers
    `..._foot_sprays_1`.
    """
    canonical = outlet_name(valve, zone, outlet, grouping=ZONE_GROUPING_NUMBERED)
    return f"{valve.device_id}_{slug(canonical)}"


class KohlerValveEntity(CoordinatorEntity[KohlerAnthemCoordinator]):
    """Base for entities belonging to one Anthem digital valve.

    Takes the :class:`~.coordinator.Valve` it belongs to, for the same reason the
    controller base takes a `Controller`: the coordinator holds every valve on the account,
    and an entity reads and commands exactly one. Unique ids are built on that valve's
    device id, so a single-valve install keeps every id it had.

    When `zone` is supplied on a multi-zone valve and the entry is configured for
    per-zone sub-devices (`ZONE_GROUPING_SUBDEVICES`), the entity attaches to that
    zone's sub-device (`Anthem Valve Zone 1` / `Zone 2`) linked via `via_device` to the
    parent valve device while keeping `self._device_id` (and therefore `unique_id`)
    unchanged.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: KohlerAnthemCoordinator,
        valve: Valve,
        *,
        zone: int | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._valve = valve
        self._device_id = valve.device_id
        grouping = getattr(valve, "zone_grouping", DEFAULT_ZONE_GROUPING)
        if (
            zone is not None
            and len(valve.model.zones) > 1
            and grouping == ZONE_GROUPING_SUBDEVICES
        ):
            self._attr_device_info = zone_device_info(valve, zone)
        else:
            self._attr_device_info = valve_device_info(valve)

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
        super().__init__(coordinator, valve, zone=zone)
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
            # The controller's web settings page, from the LAN address in
            # `hub-configuration`. Kept current by the coordinator after each seed.
            configuration_url=controller.settings.web_url,
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
