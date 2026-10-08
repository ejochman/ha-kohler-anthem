"""Every entity must construct, and be named and identified as intended.

**This file exists because v0.6.6 shipped with two entities missing.** An edit removed the
body of `ZoneNumberBase` — `__init__`, `_word`, `_attr_mode` — leaving the class declaration
behind. `python -m py_compile` passes on an empty class, so the only signal was a user
installing the release and finding the Temperature and Flow controls gone.

The lesson is narrow: *syntax checking is not verification*. These tests run each platform's
real `async_setup_entry` and assert the resulting entity set, so a deleted method, a broken
constructor, an entity dropped from setup, or a rename that collides with another id fails
here rather than on someone's shower.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.helpers.entity import EntityCategory

from .conftest import make_controller, make_coordinator, make_valve

PLATFORMS = (
    "number",
    "switch",
    "sensor",
    "binary_sensor",
    "select",
    "button",
    "update",
)


def platform(name: str):
    return importlib.import_module(f"custom_components.kohler_anthem.{name}")


def collect(name: str, coordinator) -> list:
    """Run a platform's real `async_setup_entry` and return what it added."""
    added: list = []
    hass = SimpleNamespace(data={"kohler_anthem": {"test": coordinator}})
    entry = SimpleNamespace(entry_id="test", data={}, options={})
    asyncio.run(
        platform(name).async_setup_entry(
            hass, entry, lambda e, *a, **k: added.extend(e)
        )
    )
    return added


# --------------------------------------------------------------------------- #
# The regression that started all this
# --------------------------------------------------------------------------- #
def test_temperature_and_flow_numbers_exist(coordinator):
    """**The 0.6.6 regression test.** Both numbers must be created and usable."""
    entities = collect("number", coordinator)
    assert sorted(e.name for e in entities) == [
        "Default Temperature",
        "Flow",
        "Max Temperature",
        "Temperature",
    ]
    for entity in entities:
        # A constructor that produced an unusable entity would still pass a name check;
        # reading the value proves the inherited plumbing survived.
        assert entity.unique_id
        assert entity.native_value is not None


def test_number_unique_ids_are_stable(coordinator):
    """Renames are display-only — these ids are what existing automations resolve through."""
    entities = {e.name: e for e in collect("number", coordinator)}
    assert entities["Temperature"].unique_id == "gcs-test0001_temperature_zone_1"
    assert entities["Flow"].unique_id == "gcs-test0001_flow_zone_1"


# --------------------------------------------------------------------------- #
# Every platform builds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", PLATFORMS)
def test_platform_constructs(name, coordinator):
    """Each platform's setup runs and every entity it makes exposes a unique id."""
    for entity in collect(name, coordinator):
        assert entity.unique_id, f"{name}: {entity} has no unique id"


def test_no_unique_id_collisions(coordinator):
    """Two entities sharing an id is not an error in Home Assistant — the second is
    dropped silently, indistinguishable from never having been written."""
    seen: dict[str, str] = {}
    for name in PLATFORMS:
        for entity in collect(name, coordinator):
            uid = entity.unique_id
            assert uid not in seen, f"{uid} claimed by both {seen[uid]} and {name}"
            seen[uid] = name


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #
def test_outlets_named_after_their_fixture(coordinator, valve_model):
    names = {e.name for e in collect("switch", coordinator)}
    assert {"Rainhead", "Showerhead", "Handshower"} <= names, sorted(names)
    assert "Shower on" in names

    other_coordinator = make_coordinator([make_valve(valve_model, [52, 21, 1])])
    other_names = {e.name for e in collect("switch", other_coordinator)}
    assert {"Body Sprays", "Tub Filler", "Handshower"} <= other_names, sorted(
        other_names
    )


def test_max_shower_duration_is_unknown_before_it_is_learned(valve_model):
    """`unknown` and zero are different answers; only one of them is safe to show."""
    valve = make_valve(valve_model, [31, 11, 1])
    valve.outlet_run_times = {}
    sensor = _duration_sensor(valve_model, {})
    assert sensor.current_option is None
    assert sensor.extra_state_attributes["reported_minutes"] is None


def _zone_number_names(coordinator):
    """The per-zone numbers only.

    `Max Temperature` and `Default Temperature` are per *valve* — `writeoutletconfig`
    replaces every outlet's record with the same value — so they carry no zone in their
    name and are not what these naming tests are about.
    """
    valve_level = {"Max Temperature", "Default Temperature"}
    return {e.name for e in collect("number", coordinator)} - valve_level


def test_single_zone_valve_drops_the_zone_prefix(coordinator):
    assert _zone_number_names(coordinator) == {"Temperature", "Flow"}


def test_two_zone_valve_numbers_each_zone(valve_model):
    """0.7.3: `Temperature 1`/`Temperature 2`, not `Zone 1 Temperature`.

    The number is a suffix so the pair sorts together in every Home Assistant list.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    model = get_valve_model("K-28212")
    coordinator = make_coordinator([make_valve(model, [31, 11, 1, 11, None, 21])])
    assert _zone_number_names(coordinator) == {
        "Temperature 1",
        "Flow 1",
        "Temperature 2",
        "Flow 2",
    }


def test_unknown_outlet_type_falls_back_to_position(valve_model):
    """An unconfirmed type code must never be given an invented fixture name."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    coordinator = make_coordinator([make_valve(valve_model, [999, 11, 1])])
    names = {e.name for e in collect("switch", coordinator)}
    # Single-zone valve, so no zone number — `Outlet 1`, not `Zone 1 Outlet 1`.
    assert "Outlet 1" in names, sorted(names)

    # Multi-zone valve: unknown/unconfigured outlet falls back to `Outlet <zone>.<outlet>`.
    multi_model = get_valve_model("K-28211")
    multi_coordinator = make_coordinator([make_valve(multi_model, [11, 999, 31, 1])])
    multi_names = {e.name for e in collect("switch", multi_coordinator)}
    assert "Outlet 1.2" in multi_names, sorted(multi_names)


def test_duplicate_fixtures_get_distinct_ids(valve_model):
    """Two outlets of one fixture type in a zone is a legal install."""
    coordinator = make_coordinator([make_valve(valve_model, [11, 11, 1])])
    entities = collect("switch", coordinator)
    ids = [e.unique_id for e in entities]
    assert len(ids) == len(set(ids)), ids


# --------------------------------------------------------------------------- #
# Multiple devices
# --------------------------------------------------------------------------- #
def test_two_valves_do_not_share_ids(valve_model):
    """The account this integration is developed against has two valves."""
    coordinator = make_coordinator(
        [
            make_valve(valve_model, [31, 11, 1], device_id="gcs-left", run_time=1800),
            make_valve(valve_model, [31, 11, 1], device_id="gcs-right", run_time=3600),
        ]
    )
    for name in PLATFORMS:
        ids = [e.unique_id for e in collect(name, coordinator)]
        assert len(ids) == len(set(ids)), f"{name}: {ids}"


# --------------------------------------------------------------------------- #
# Naming: Shower Active
# --------------------------------------------------------------------------- #
def _zone_active(coordinator):
    return [e for e in collect("binary_sensor", coordinator) if "_zone_" in e.unique_id]


def test_zone_active_is_named_shower_active():
    """0.7.2 renamed `Zone 1 Active`, which said nothing a user recognised.

    Asserts the name AND that the unique id still carries `zone_1` — a rename that moved the
    id would silently orphan history and every automation referencing it. Multi-zone now,
    since 0.19.0 is where the entity still exists.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    model = get_valve_model("K-28211")
    coordinator = make_coordinator([make_valve(model, [31, 11, 1, 11])])
    active = sorted(_zone_active(coordinator), key=lambda e: e.unique_id)
    assert [e.unique_id.split("_", 1)[1] for e in active] == [
        "zone_1_active",
        "zone_2_active",
    ]
    assert [e.name for e in active] == ["Shower Active 1", "Shower Active 2"]


def test_single_zone_valve_has_no_shower_active(valve_model):
    """0.19.0: with one zone it duplicated `Status`, which says more.

    The entity a single-zone owner is left with has to carry the cutoff countdown, or
    removing this one loses the only number that matters mid-shower — so that is asserted
    here rather than in a separate test that could pass while this one regressed.
    """
    coordinator = make_coordinator([make_valve(valve_model, [31, 11, 1])])
    assert _zone_active(coordinator) == []

    status = next(
        e for e in collect("sensor", coordinator) if e.unique_id.endswith("_status")
    )
    assert "seconds_remaining" in status.extra_state_attributes
    assert "flowing_for_seconds" in status.extra_state_attributes


def test_status_is_named_system_status_without_moving_its_id():
    """0.19.0 renamed it; the unique id must NOT follow.

    Both devices carry one — they do not collide, being on separate devices — and an id that
    moved with the name would orphan every automation and all recorded history.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    coordinator = make_coordinator(
        [make_valve(get_valve_model("K-28210"), [31, 11, 1])],
        controllers=[make_controller(get_valve_model("K-28210"))],
    )
    named = [e for e in collect("sensor", coordinator) if e.name == "System Status"]
    assert len(named) == 2, [e.name for e in collect("sensor", coordinator)]
    assert all(e.unique_id.endswith("_status") for e in named)


def test_shower_active_is_not_diagnostic():
    """It was diagnostic *with* an enabled-by-default override — a miscategorisation.

    On the multi-zone valve it now exists on, "which shower is running" is primary state.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    model = get_valve_model("K-28211")
    coordinator = make_coordinator([make_valve(model, [31, 11, 1, 11])])
    assert [e.entity_category for e in _zone_active(coordinator)] == [None, None]


# --------------------------------------------------------------------------- #
# Firmware: Kohler reports it in more than one shape
# --------------------------------------------------------------------------- #
def _firmware_holder(configuration):
    """A minimal object carrying the REAL firmware properties, not the test double's.

    `make_valve` returns a `SimpleNamespace`, which cannot carry a property — it holds a
    static `firmware` string instead. Asserting against that would test the fake and pass no
    matter what the integration does, which is how an early draft of this test "passed" one
    case by coincidence. A `SimpleNamespace` also cannot satisfy `firmware`'s reads of
    `about`, so the properties are inherited onto a purpose-built class here.
    """
    from custom_components.kohler_anthem.coordinator import Valve

    class Holder:
        about = Valve.about
        firmware = Valve.firmware
        component_firmware = Valve.component_firmware

        def __init__(self, config):
            self.configuration = config

    return Holder(configuration)


@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        ({"about": {"firmware": "00.74"}}, "00.74"),
        ({"otaReportedProperties": {"currentFirmwareVersion": "01.02"}}, "01.02"),
        ({"otaReportedProperties": {"reported": {"swVersion": "02.10"}}}, "02.10"),
        ({"otaReportedProperties": "03.01"}, "03.01"),
        ({"firmwareUpdate": {"currentVersion": "04.05"}}, "04.05"),
        ({"version": "05.06"}, "05.06"),
        ({"version": 74}, "74"),
        # The owner's real shape before 0.7.2: no `about`, so this read `unknown`.
        ({"createdTime": "x", "deviceId": "y", "sku": "z"}, None),
        # A version the cloud WANTS installed is not the one running. Reporting it would be
        # worse than reporting nothing.
        ({"firmwareUpdate": {"targetVersion": "09.99"}}, None),
        ({"version": "   "}, None),
        ({"version": True}, None),
        ({}, None),
    ],
)
def test_firmware_reads_every_known_shape(configuration, expected):
    """Exercises the REAL `Valve.firmware`, not the stand-in.

    `make_valve` returns a `SimpleNamespace`, which cannot carry a property — it holds a
    static `firmware` attribute instead. Asserting against that would test the fake and pass
    no matter what the integration does, which is how the first draft of this test "passed"
    one case by coincidence. So the real property is bound to a minimal object here.
    """

    assert _firmware_holder(configuration).firmware == expected


# --------------------------------------------------------------------------- #
# Naming: zone numbers and fixture numbers must not blur together
# --------------------------------------------------------------------------- #
def test_duplicate_fixture_in_a_multi_zone_valve_reads_zone_dot_position():
    """`Showerhead 1.2`, never `Showerhead 2 1`.

    Both the zone suffix and the duplicate-fixture suffix are bare numbers, so a valve with
    two zones AND a repeated fixture would otherwise emit two numbers in an order nobody can
    read. The zone leads, separated by a dot.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.entity import outlet_name

    model = get_valve_model("K-28211")
    valve = make_valve(model, [11, 11, 11, 11])
    assert outlet_name(valve, 1, 1) == "Showerhead 1.1"
    assert outlet_name(valve, 1, 2) == "Showerhead 1.2"
    assert outlet_name(valve, 2, 1) == "Showerhead 2.1"


def test_duplicate_fixture_in_a_single_zone_valve_has_no_zone_number(valve_model):
    """One zone means the number can only mean the fixture — `Showerhead 1`, `Showerhead 2`."""
    from custom_components.kohler_anthem.entity import outlet_name

    valve = make_valve(valve_model, [11, 11, 1])
    assert outlet_name(valve, 1, 1) == "Showerhead 1"
    assert outlet_name(valve, 1, 2) == "Showerhead 2"
    assert outlet_name(valve, 1, 3) == "Handshower"


def test_multi_zone_names_stay_unique_across_every_platform():
    """A name collision builds the same unique id twice and HA drops one entity silently."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    model = get_valve_model("K-28212")
    coordinator = make_coordinator([make_valve(model, [11, 11, 11, 11, 11, 11])])
    for platform in PLATFORMS:
        entities = collect(platform, coordinator)
        ids = [e.unique_id for e in entities]
        assert len(ids) == len(set(ids)), f"{platform}: {sorted(ids)}"
        names = [e.name for e in entities if e.name]
        assert len(names) == len(set(names)), f"{platform}: {sorted(names)}"


# --------------------------------------------------------------------------- #
# Water total: published exactly as the device reports it
# --------------------------------------------------------------------------- #
def test_the_total_water_sensor_is_gone():
    """`Total Water Used` was retired in 0.14.0 — `totalFlow` is not a meter.

    Across the reference corpus that field took three distinct values and cycled among them
    with no water running, in pairs exactly 4x apart. As a `total_increasing` sensor every
    shift read as a meter replacement. The 0.7.3 divide-by-four bug came from reading that
    same 4x as a unit conversion.
    """
    import custom_components.kohler_anthem.sensor as sensor_module

    assert not hasattr(sensor_module, "ValveTotalWaterSensor")


def test_no_entity_publishes_total_flow(valve_model):
    """The field is kept for diagnostics only; nothing may publish it again."""
    coordinator = make_coordinator([make_valve(valve_model, [31, 11, 1])])
    ids = [e.unique_id for e in collect("sensor", coordinator)]
    assert not any(i.endswith("_total_water") for i in ids), sorted(ids)


def test_the_retired_sensor_is_purged_from_the_registry():
    """A removed entity leaves a permanently unavailable registry row unless purged.

    `_async_purge_removed_diagnostics` clears them by unique-id suffix; without the suffix
    listed there, every existing install keeps a dead `Total Water Used` row for ever.
    """
    from custom_components.kohler_anthem import _REMOVED_UNIQUE_ID_SUFFIXES

    assert "_total_water" in _REMOVED_UNIQUE_ID_SUFFIXES


def test_total_flow_is_still_recorded_raw_for_diagnostics(valve_model):
    """It is the evidence for the open question, so the raw value must survive."""
    from custom_components.kohler_anthem.anthem.state import GcsState

    state = GcsState(valve_model, "Fahrenheit")
    assert state._accept_total_flow_raw("8224.0") is True
    assert state.total_flow == 8224.0
    # Unfiltered now: a value the old glitch filter would have held is taken verbatim.
    assert state._accept_total_flow_raw("2.0") is True
    assert state.total_flow == 2.0
    assert state._accept_total_flow_raw("rubbish") is False
    assert state.total_flow == 2.0


# --------------------------------------------------------------------------- #
# Flow percentage: the byte is 2 units per percent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The owner's two valves, verbatim from diagnostics captured 2026-09-10. Both carry
        # ODD flow bytes, which is the case an integer-truncating decode gets wrong.
        ("0195310000000001", 24.5),
        ("0189350000000001", 26.5),
    ],
)
def test_flow_decodes_from_real_hardware_words(raw, expected):
    from custom_components.kohler_anthem.anthem.valve_hex import decode_word

    assert decode_word(raw).flow_percent == expected


def test_every_legal_flow_byte_round_trips():
    """All 185 bytes in [16, 200] must survive decode -> encode unchanged.

    Half-percent values are ordinary on this hardware, so a decode that truncated or an
    encode that rounded to whole percents would quietly move the valve.
    """
    from custom_components.kohler_anthem.anthem.valve_hex import (
        FLOW_BYTE_MAX,
        FLOW_BYTE_MIN,
        FLOW_PER_PERCENT,
        decode_word,
    )

    for byte in range(FLOW_BYTE_MIN, FLOW_BYTE_MAX + 1):
        percent = decode_word(f"0195{byte:02x}0000000001").flow_percent
        assert round(percent * FLOW_PER_PERCENT) == byte, (byte, percent)


def test_flow_slider_is_whole_percent():
    """Whole-number percentages, by the owner's decision — step and both bounds.

    The wire resolves to 0.5 %, but the control deliberately does not: see `ZoneFlowNumber`.
    The bounds matter as much as the step, because a half-valued bound would put every
    position on the slider on a half and defeat the whole thing.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    coordinator = make_coordinator(
        [make_valve(get_valve_model("K-28210"), [31, 11, 1])]
    )
    flows = [e for e in collect("number", coordinator) if e.name == "Flow"]
    assert len(flows) == 1
    flow = flows[0]
    assert flow.native_step == 1
    assert float(flow.native_min_value).is_integer(), flow.native_min_value
    assert float(flow.native_max_value).is_integer(), flow.native_max_value


def test_flow_rounds_a_half_percent_reading_for_display():
    """The valve reports halves; the control shows whole numbers.

    Both of the owner's valves sit on half-percent bytes, so an unrounded display would show
    a value the slider cannot return to.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.anthem.valve_hex import decode_word

    valve = make_valve(get_valve_model("K-28210"), [31, 11, 1])
    flow = next(
        e for e in collect("number", make_coordinator([valve])) if e.name == "Flow"
    )

    # Byte 49 = 24.5 %, with outlet 1 open so the valve's own reading is the one shown.
    valve.gcs_state.valve1 = decode_word("0195310100000001")
    assert valve.gcs_state.flow_is_live
    assert float(flow.native_value).is_integer(), flow.native_value

    # A whole reading is untouched.
    valve.gcs_state.valve1 = decode_word("0195320100000001")  # byte 50 = 25.0 %
    assert flow.native_value == 25


# --------------------------------------------------------------------------- #
# The gcs-usage contract
# --------------------------------------------------------------------------- #
def test_usage_probe_is_gone_and_the_contract_is_recorded():
    """`probe_usage` existed to learn `gcs-usage`'s contract; Konnect 3.0.6 settled it.

    The action is gone, and the endpoint's documentation must say what the app actually
    sends — `Day`/`Month` with `MM-dd-yyyy` — and that `WEEK` is not an interval at all,
    so nobody reopens the question by probing again.
    """
    from custom_components.kohler_anthem import const, services
    from custom_components.kohler_anthem.anthem import client
    from custom_components.kohler_anthem.anthem import const as protocol

    assert not hasattr(client.KohlerClient, "async_probe_usage")
    assert not hasattr(const, "SERVICE_PROBE_USAGE")
    assert not hasattr(services, "_USAGE_ATTEMPTS")
    import pathlib

    source = pathlib.Path(protocol.__file__).read_text(encoding="utf-8")
    block = source[source.index("Water usage history") : source.index("GCS_USAGE =")]
    assert "MM-dd-yyyy" in block and "never sends `WEEK`" in block


# --------------------------------------------------------------------------- #
# Monthly water usage, from Kohler's own history endpoint
# --------------------------------------------------------------------------- #
#: A real `gcs-usage` response, trimmed. Captured 2026-09-10 from the owner's Shower Left.
_REAL_USAGE = {
    "deviceId": "gcs-test0001",
    "interval": "Month",
    "gcsUsageDataDetailsList": [
        {"intervalKey": "2025-08", "volume": 1319, "onDuration": 14676},
        {"intervalKey": "2026-08", "volume": 1811, "onDuration": 24371},
        {"intervalKey": "2026-09", "volume": 415, "onDuration": 5575},
    ],
}


def _monthly_sensor(usage, *, units="Standard"):
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    valve = make_valve(get_valve_model("K-28210"), [31, 11, 1])
    valve.usage = usage
    coordinator = make_coordinator([valve])
    coordinator.water_units = units
    return next(
        e for e in collect("sensor", coordinator) if e.name == "Water Used This Month"
    )


def test_monthly_water_converts_litres_to_gallons():
    """`volume` is litres on the wire whatever the account's unit — verified from the app.

    1811 L is August 2026 on the owner's valve, and 478.4 gal is what the Konnect app shows
    for it. Matching the app to the tenth is the whole point of using its exact constant.
    """
    sensor = _monthly_sensor(_REAL_USAGE)
    assert sensor.extra_state_attributes["history"]["2026-08"] == 478.4


def test_monthly_water_leaves_litres_alone_on_a_metric_account():
    sensor = _monthly_sensor(_REAL_USAGE, units="Liters")
    assert sensor.extra_state_attributes["history"]["2026-08"] == 1811.0


def test_monthly_water_matches_the_month_rather_than_taking_the_last_entry():
    """The series can end on a month with no data; position is not identity."""
    from datetime import UTC, datetime

    key = datetime.now(UTC).strftime("%Y-%m")
    usage = {
        "gcsUsageDataDetailsList": [
            {"intervalKey": key, "volume": 100, "onDuration": 600},
            {"intervalKey": "1999-01", "volume": 9999, "onDuration": 60},
        ]
    }
    sensor = _monthly_sensor(usage)
    assert sensor.extra_state_attributes["month"] == key
    assert sensor.native_value == round(100 * 0.264172, 1)
    assert sensor.extra_state_attributes["running_minutes"] == 10.0


def test_monthly_water_is_none_without_a_reading():
    """A failed read and a month with no entry must both be `unknown`, never a stale number."""
    assert _monthly_sensor({}).native_value is None
    assert _monthly_sensor({"gcsUsageDataDetailsList": []}).native_value is None
    assert _monthly_sensor({"gcsUsageDataDetailsList": "nonsense"}).native_value is None


# --------------------------------------------------------------------------- #
# Controller (Anthem Plus) entities
# --------------------------------------------------------------------------- #
def _hub(entities, controller_id="hub-test0001"):
    return [e for e in entities if controller_id in (e.unique_id or "")]


def test_every_controller_platform_constructs():
    """Roughly half the entity classes are controller-side and none was ever built here.

    The 0.6.6 regression — a deleted base-class body, `py_compile` clean, two entities
    silently missing — was a valve-side class. Nothing would have caught the same mistake on
    the controller side until this test.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    model = get_valve_model("K-28210")
    coordinator = make_coordinator(
        [make_valve(model, [31, 11, 1])], [make_controller(model)]
    )
    built = {name: _hub(collect(name, coordinator)) for name in PLATFORMS}
    # Every platform that has controller entities must produce them; the two that have none
    # are asserted empty so this notices if that ever changes silently.
    assert {name for name, entities in built.items() if entities} == {
        "switch",
        "sensor",
        "binary_sensor",
        "select",
        "update",
    }, {name: len(entities) for name, entities in built.items()}
    total = sum(len(entities) for entities in built.values())
    assert total >= 10, built


def test_controller_and_valve_entities_never_share_an_id():
    """Both devices carry a Shower switch and a temperature; only the device id separates."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    model = get_valve_model("K-28210")
    coordinator = make_coordinator(
        [make_valve(model, [31, 11, 1])], [make_controller(model)]
    )
    for name in PLATFORMS:
        ids = [e.unique_id for e in collect(name, coordinator)]
        assert len(ids) == len(set(ids)), f"{name}: {sorted(ids)}"


def test_two_controllers_do_not_share_ids():
    """One controller per bathroom is the ordinary case on a large account."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    model = get_valve_model("K-28210")
    coordinator = make_coordinator(
        [make_valve(model, [31, 11, 1])],
        [
            make_controller(model, device_id="hub-left", name="Anthem Plus Left"),
            make_controller(model, device_id="hub-right", name="Anthem Plus Right"),
        ],
    )
    for name in PLATFORMS:
        ids = [e.unique_id for e in collect(name, coordinator)]
        assert len(ids) == len(set(ids)), f"{name}: {sorted(ids)}"


def test_controller_zone_names_match_the_valve_scheme():
    """0.8.1: a controller said `Zone 1 Temperature` beside the valve's plain `Temperature`.

    Two views of one shower should not read as two different things.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    single = get_valve_model("K-28210")
    coordinator = make_coordinator(
        [make_valve(single, [31, 11, 1])], [make_controller(single)]
    )
    assert "Temperature" in {e.name for e in _hub(collect("sensor", coordinator))}

    double = get_valve_model("K-28211")
    coordinator = make_coordinator(
        [make_valve(double, [31, 11, 1, 31])],
        [make_controller(double, zones=tuple(double.zones))],
    )
    names = {e.name for e in _hub(collect("sensor", coordinator))}
    assert {"Temperature 1", "Temperature 2"} <= names, sorted(names)


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
def test_every_registered_service_is_also_unregistered():
    """0.7.7 added a service (`probe_usage`, since retired) and forgot the unload path.

    The action then outlived the last unload with nothing behind it, and calling it reported
    "no Anthem valve on this account" rather than simply not existing. Asserted by reading
    both functions' source, so a future service that is registered and never removed fails
    here rather than becoming a ghost in someone's UI.
    """
    import inspect
    import re

    from custom_components.kohler_anthem import services

    registered = set(
        re.findall(
            r"SERVICE_[A-Z_]+", inspect.getsource(services.async_register_services)
        )
    )
    removed = set(
        re.findall(
            r"SERVICE_[A-Z_]+", inspect.getsource(services.async_unregister_services)
        )
    )
    assert registered, "no services found — the regex or the function shape changed"
    assert registered <= removed, sorted(registered - removed)


def test_services_yaml_describes_every_registered_service():
    """A service with no YAML entry appears in the UI with no name, description or fields."""
    import inspect
    import re

    import yaml

    from custom_components.kohler_anthem import const, services

    registered = {
        getattr(const, name)
        for name in re.findall(
            r"SERVICE_[A-Z_]+", inspect.getsource(services.async_register_services)
        )
        if hasattr(const, name)
    }
    path = Path(services.__file__).parent / "services.yaml"
    described = set(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert registered <= described, sorted(registered - described)


# --------------------------------------------------------------------------- #
# Flow percent is a ratio against the outlet's ceiling, not a fixed divisor
# --------------------------------------------------------------------------- #
def test_flow_conversion_matches_the_app_formula():
    """`percent = byte * 100 / max` — what `jj.h$a.X` does in the Konnect app.

    A hardcoded `/2` agrees with this only where the ceiling is 200. Both of the owner's
    valves report 200, so their hardware cannot tell the two apart; the app's bytecode can,
    and does.
    """
    from custom_components.kohler_anthem.anthem.valve_hex import (
        flow_byte_to_percent,
        flow_percent_to_byte,
    )

    # Ceiling 200: must be indistinguishable from the old divisor, including the odd bytes
    # the owner's valves actually carry.
    for byte in (16, 49, 53, 100, 200):
        assert flow_byte_to_percent(byte, 200) == byte / 2

    # A lower ceiling is where they diverge — and where the old formula sent double.
    assert flow_byte_to_percent(100, 100) == 100.0
    assert flow_percent_to_byte(100, 100) == 100
    assert flow_percent_to_byte(100, 200) == 200

    # Exact inverses across every legal byte, at both ceilings.
    for ceiling in (100, 200):
        for byte in range(16, ceiling + 1):
            assert (
                flow_percent_to_byte(flow_byte_to_percent(byte, ceiling), ceiling)
                == byte
            )


def test_flow_slider_bounds_come_from_the_ceiling(valve_model):
    """The maximum is 100 % by definition — percent is a ratio against the ceiling."""
    coordinator = make_coordinator([make_valve(valve_model, [31, 11, 1])])
    flow = next(e for e in collect("number", coordinator) if e.name == "Flow")
    assert flow.native_max_value == 100
    assert flow.native_min_value == 8
    assert flow.extra_state_attributes["maximum_flow_byte"] == 200


def test_flow_display_is_unchanged_on_a_200_ceiling(valve_model):
    """The owner's hardware must read exactly as it did before 0.8.2."""
    from custom_components.kohler_anthem.anthem.valve_hex import decode_word

    valve = make_valve(valve_model, [31, 11, 1])
    flow = next(
        e for e in collect("number", make_coordinator([valve])) if e.name == "Flow"
    )
    valve.gcs_state.valve1 = decode_word("0195310100000001")  # byte 49 = 24.5 %
    assert valve.gcs_state.flow_is_live
    assert flow.native_value == 24


# --------------------------------------------------------------------------- #
# Safety and credentials
# --------------------------------------------------------------------------- #
def test_send_valve_hex_refuses_a_scalding_word():
    """`send_valve_hex` is the one path that does not go through `encode_word`'s clamp.

    The word carries a 10-bit temperature, so a typo or a script can encode 102.3 °C — 216 °F
    — and it used to be sent verbatim. Outlet, flow and pause bits stay unrestricted: those
    are what the escape hatch is for, and none of them can scald.
    """
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.kohler_anthem.coordinator import _command_half

    assert _command_half("0195C801", "zone1_hex") == "0195C801"  # 40.5 C, ordinary
    # 16-character words pasted from the Hex sensor must still work.
    assert _command_half("0195310100000001", "zone1_hex") == "01953101"

    for word in ("01FFC801", "03FFC801"):  # 51.1 C and 102.3 C
        with pytest.raises(HomeAssistantError, match="above the"):
            _command_half(word, "zone1_hex")


def test_rotated_refresh_token_is_persisted_at_rotation():
    """B2C retires the old token the instant it issues a new one.

    Persistence used to be the caller's job, and on a push-only install (`SCAN_INTERVAL` is
    None) those callers ran once at startup — so hours of rotations went unpersisted and a
    restart loaded a token Kohler had already retired.
    """
    from custom_components.kohler_anthem.anthem.auth import KohlerAuth

    auth = KohlerAuth(None, "token-1")
    seen: list[str] = []
    auth.on_token_rotated = seen.append
    assert auth.on_token_rotated is not None

    # The 401 path must invalidate the access token without discarding the refresh token,
    # so the retry goes back through the lock rather than around it.
    auth.invalidate_access_token()
    assert auth._tokens is None
    assert auth.refresh_token == "token-1"


def test_error_messages_carry_no_device_id():
    """Device ids double as cloud addresses, and error text reaches logs and report files."""
    from custom_components.kohler_anthem.anthem.client import KohlerClient

    for path, ident in (
        ("/devices/api/v1/device-management/gcs-state/gcs-secret01", "gcs-secret01"),
        (
            "/devices/api/v1/device-management/gcs-usage/gcs-secret01?Interval=MONTH",
            "gcs-secret01",
        ),
        ("/devices/api/v1/device-management/hub-state/hub-secret02", "hub-secret02"),
        (
            "/devices/api/v1/device-management/customer-device/tenant-secret03",
            "tenant-secret03",
        ),
    ):
        safe = KohlerClient.safe_path(path)
        assert ident not in safe, safe
        assert "<id>" in safe, safe

    # A command path has no id to redact and must be left intact.
    command = "/platform/api/v1/commands/gcs/solowritesystem"
    assert KohlerClient.safe_path(command) == command


def test_device_names_never_contain_a_device_id():
    """A device name reaches entity ids and the dashboard — permanently."""
    from custom_components.kohler_anthem.coordinator import valve_names

    devices = [
        SimpleNamespace(device_id="gcs-secret01", name="Shower"),
        SimpleNamespace(device_id="gcs-secret02", name="Shower"),
    ]
    names = valve_names(devices)
    for name in names.values():
        assert "gcs-secret" not in name, names


# --------------------------------------------------------------------------- #
# Malformed cloud payloads (0.10.0)
# --------------------------------------------------------------------------- #
#
# `or {}` rescues null but not a wrong type: where the cloud sends a list, a string or a
# number, the `or` passes it straight through and the next `.get` raises inside the REST
# seed — which fails setup with a traceback rather than a message. Every seed entry point
# is checked against the shapes a schema change, a truncated response, or an error body
# shaped like a success could actually produce.
MALFORMED = ({}, None, [], "", "nonsense", 0, 7, [1, 2], {"state": []}, {"state": "x"})


@pytest.mark.parametrize("payload", MALFORMED)
def test_valve_seed_survives_a_malformed_payload(payload, valve_model):
    """A wrong-typed `gcs-state` must leave defaults in place, not raise."""
    from custom_components.kohler_anthem.anthem.state import GcsState

    state = GcsState(model=valve_model)
    state.apply_rest_state(payload)  # must not raise
    assert state.warmup_mode is None or isinstance(state.warmup_mode, str)


@pytest.mark.parametrize("payload", MALFORMED)
def test_hub_seed_survives_a_malformed_payload(payload, valve_model):
    """A wrong-typed `hub-state` must leave defaults in place, not raise."""
    from custom_components.kohler_anthem.anthem.state import HubState

    state = HubState(model=valve_model)
    state.apply_rest_state(payload)  # must not raise
    assert state.zones == {}


def test_hub_seed_skips_entries_that_are_not_objects(valve_model):
    """`zone_number` reads five spellings off the entry, so a bare string would raise."""
    from custom_components.kohler_anthem.anthem.state import HubState

    state = HubState(model=valve_model)
    state.apply_rest_state(
        {"state": {"shower": ["not-an-object", None, 5, {"zone": "1", "status": "ON"}]}}
    )
    assert list(state.zones) == [1]


def test_hub_seed_ignores_a_string_outlet_array(valve_model):
    """`outlet_flags` indexes positionally: a string would read as every outlet running."""
    from custom_components.kohler_anthem.anthem.state import HubState

    state = HubState(model=valve_model)
    state.apply_rest_state(
        {"state": {"shower": [{"zone": "1", "status": "ON", "outlets": "111"}]}}
    )
    assert not any(state.zones[1].outlets)


def test_preset_seed_survives_a_malformed_payload(valve_model):
    from custom_components.kohler_anthem.anthem.state import GcsState

    state = GcsState(model=valve_model)
    for payload in MALFORMED:
        assert state.apply_preset_list(payload) is False


# --------------------------------------------------------------------------- #
# Preset words read back from the cloud (0.10.0)
# --------------------------------------------------------------------------- #


def test_preset_word_temperature_inverts_the_encoder():
    """Every temperature the encoder can produce must read back as itself."""
    from custom_components.kohler_anthem.anthem.valve_hex import (
        encode_preset_word,
        preset_word_temperature,
    )

    for tenths in range(0, 489):
        celsius = tenths / 10
        word = encode_preset_word(celsius, 50.0, 0b001)
        assert preset_word_temperature(word) == pytest.approx(celsius)


def test_check_preset_word_accepts_anything_we_wrote():
    """The ceiling is the encoder's own clamp, so our own words always pass."""
    from custom_components.kohler_anthem.anthem.valve_hex import (
        check_preset_word,
        encode_preset_word,
    )

    for celsius in (0.0, 20.0, 38.8, 48.8, 60.0, 120.0):
        for mask in (0b000, 0b001, 0b111):
            word = encode_preset_word(celsius, 50.0, mask)
            assert check_preset_word(word) == word.lower()


def test_check_preset_word_refuses_a_scalding_word():
    """A 10-bit temperature reaches 102.3 C, and this word would be echoed to the valve."""
    from custom_components.kohler_anthem.anthem.valve_hex import (
        ValveHexError,
        check_preset_word,
    )

    # byte0 low bits 0b11 -> tenths |= 0x300; 0x3FF tenths = 102.3 C.
    with pytest.raises(ValveHexError, match=r"102\.3"):
        check_preset_word("03ffc8")


@pytest.mark.parametrize(
    "word", ["", "zz", "01", "0189c", "0189c88", "01 89c8", "gg89c8"]
)
def test_check_preset_word_refuses_a_malformed_word(word):
    from custom_components.kohler_anthem.anthem.valve_hex import (
        ValveHexError,
        check_preset_word,
    )

    with pytest.raises(ValveHexError):
        check_preset_word(word)


def test_preset_timer_plan_drops_a_scalding_stored_word():
    """`writepreset` replaces the record whole, so a stored word is echoed back verbatim.

    Dropping it sends an empty field for that valve — exactly what an unused valve already
    gets — so the failure mode is a preset that stops driving one valve, not one that runs
    it too hot.
    """
    from custom_components.kohler_anthem.anthem.gcs import plan_preset_timer

    payload = {
        "gcsPresetExperienceDetails": [
            {
                "presetId": "1",
                "title": "Default shower",
                "time": "0",
                "valveDetails": [
                    {"valveIndex": "Valve1", "hexString": "03FFC8"},
                    {"valveIndex": "Valve2", "hexString": "0589C8"},
                ],
            }
        ]
    }
    plan = plan_preset_timer(payload, 1, 600)
    assert 1 not in plan.valves, "a 102.3 C word must not be echoed back"
    assert plan.valves[2] == "0589c8", "the sound word is preserved byte for byte"


def test_preset_timer_plan_preserves_normal_words():
    """The guard must be invisible on every real record."""
    from custom_components.kohler_anthem.anthem.gcs import plan_preset_timer

    payload = {
        "gcsPresetExperienceDetails": [
            {
                "presetId": "1",
                "title": "Default shower",
                "time": "0",
                "valveDetails": [
                    {"valveIndex": "Valve1", "hexString": "018448"},
                    {"valveIndex": "Valve2", "hexString": "05849C"},
                ],
            }
        ]
    }
    plan = plan_preset_timer(payload, 1, 600)
    assert plan.valves == {1: "018448", 2: "05849c"}


# --------------------------------------------------------------------------- #
# The warm-up extraction (0.10.0)
# --------------------------------------------------------------------------- #


def test_valve_still_exposes_the_whole_warmup_surface():
    """The move must be invisible: entities, services and diagnostics call these names."""
    from custom_components.kohler_anthem.coordinator import Valve

    for name in (
        "async_set_warmup",
        "async_read_warmup_mode",
        "warmup_auto_restore",
        "last_warmup_mode",
        "_handle_warmup_mode_change",
        "_message_window",
    ):
        assert hasattr(Valve, name), f"Valve lost {name} in the warm-up extraction"


def test_warmup_manager_owns_every_moved_member():
    """The other half of the same check: nothing was left behind on `Valve`."""
    from custom_components.kohler_anthem.coordinator import Valve
    from custom_components.kohler_anthem.warmup_manager import WarmupManager

    moved = (
        "_remember_warmup_mode",
        "_schedule_warmup_restore",
        "_async_restore_warmup",
        "_async_journal_warmup_context",
        "_warmup_write_status",
        "_warmup_journal",
    )
    for name in moved:
        assert hasattr(WarmupManager, name), f"WarmupManager is missing {name}"
        assert not hasattr(Valve, name), f"Valve kept a moved member: {name}"


def test_valve_never_calls_a_member_it_no_longer_has():
    """The extraction's real hazard: a leftover `self._warmup_*` call site.

    `journal_baseline` still called `self._warmup_journal` after the move, which would have
    raised `AttributeError` on every warm-up log open — a path no other test exercises,
    because it needs a log file to exist. Checked statically instead: every `self.<name>`
    inside `Valve` must resolve to something `Valve` actually has.
    """
    import ast
    import inspect

    from custom_components.kohler_anthem import coordinator as module
    from custom_components.kohler_anthem.coordinator import Valve

    tree = ast.parse(inspect.getsource(module))
    valve = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "Valve"
    )
    # Names bound by `self.x = ...` anywhere in the class, plus everything on the type.
    assigned = {
        target.attr
        for node in ast.walk(valve)
        for target in getattr(node, "targets", [])
        + ([node.target] if isinstance(node, ast.AnnAssign) else [])
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }
    known = assigned | set(dir(Valve))
    used = {
        node.attr
        for node in ast.walk(valve)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }
    missing = sorted(name for name in used - known if not name.startswith("__"))
    assert not missing, f"Valve calls members it does not have: {missing}"


# --------------------------------------------------------------------------- #
# The diagnostics version scan (0.10.1)
# --------------------------------------------------------------------------- #


def test_version_scan_finds_every_firmware_without_leaking_identity():
    """The scan exists to find the valve and gateway versions the three named blocks miss.

    It walks blocks this module otherwise refuses to reproduce (`configuration`, `iot`,
    `applicationSource`) because they carry plumbing and cloud addressing — so the property
    that matters is that it takes version *values* and leaves every sibling behind.
    """
    from custom_components.kohler_anthem.diagnostics import _version_fields

    record = {
        "deviceId": "gcs-secret01",
        "tenantId": "tenant-secret02",
        "serialNumber": "SN-secret03",
        "iot": {
            "connectionString": "HostName=x.azure-devices.net;DeviceId=gcs-secret01;Key=AAAA",
            "firmwareVersion": "00.74",
            "hubName": "kohler-prod",
        },
        "configuration": {
            "valves": [
                {"valveIndex": "Valve1", "firmwareVersion": 10, "serial": "V-secret04"},
                {"valveIndex": "Valve2", "firmwareVersion": 10},
            ],
            "systemConfiguration": {"pipeLayout": "left-riser", "swRevision": "1.4"},
        },
        "applicationSource": {"version": "3.0.1", "buildId": "a" * 40},
        # Labels that sit beside a version and are not one.
        "firmwareType": "Application",
        "isSingleFirmwareUpdate": False,
    }
    found = _version_fields(record)

    # The versions the report exists to surface, each at a path that says where it came from.
    assert found["iot.firmwareVersion"] == "00.74"
    assert found["configuration.valves[0].firmwareVersion"] == 10
    assert found["configuration.valves[1].firmwareVersion"] == 10
    assert found["configuration.systemConfiguration.swRevision"] == "1.4"

    # A value too long to be a version is described, not copied.
    assert found["applicationSource.buildId"] == "<str, 40 chars>"

    # Type and update-bookkeeping labels are not versions.
    assert "firmwareType" not in found
    assert "isSingleFirmwareUpdate" not in found

    blob = __import__("json").dumps(found)
    for secret in (
        "secret01",
        "secret02",
        "secret03",
        "secret04",
        "azure-devices",
        "left-riser",
        "kohler-prod",
    ):
        assert secret not in blob, f"{secret} leaked into the version scan"


def test_version_scan_reads_the_real_block_shapes():
    """Both interfaces' real payloads, as captured 2026-09-10."""
    from custom_components.kohler_anthem.diagnostics import _version_fields

    # Shower Left: an Application block beside the Assets one.
    left = {
        "firmwareUpdate": {"firmwareType": "Assets", "version": "2.00"},
        "otaReportedProperties": {
            "firmwareType": "Application",
            "initialVersion": "2.20",
            "updatedVersion": "2.20",
        },
        "version": None,
    }
    assert _version_fields(left)["otaReportedProperties.updatedVersion"] == "2.20"

    # Shower Right: no Application block anywhere — the reason its entity read 2.00.
    right = {
        "firmwareUpdate": {"firmwareType": "Assets", "version": "2.00"},
        "otaReportedProperties": {
            "firmwareType": "Assets",
            "initialVersion": "2.00",
            "updatedVersion": "2.00",
        },
        "version": None,
    }
    found = _version_fields(right)
    assert found["otaReportedProperties.updatedVersion"] == "2.00"
    assert all(value in ("2.00", None) for value in found.values())


# --------------------------------------------------------------------------- #
# Three firmwares, not one (0.11.0)
# --------------------------------------------------------------------------- #
#
# The `about` block as both of the owner's K-28210 valves report it, captured 2026-09-10.
# Nested inside the record's own `configuration` key — the depth bug that made every earlier
# report say `about_keys: []` on hardware that populates it in full.
def _real_about(valve_firmware: str) -> dict:
    return {
        "configuration": {
            "about": {
                "firmware": {"version": "00.74", "latestVersion": "00.74"},
                "gateway": {"firmware": "00.74", "assetsFirmware": None},
                "primaryValve": {"firmware": valve_firmware, "assetsFirmware": None},
                "secondaryValve1": {"firmware": "0", "assetsFirmware": None},
                "uI2": {"firmware": "2.2", "assetsFirmware": "2.0"},
                "ui": {"firmware": "0.0", "assetsFirmware": "0.0"},
                "controllerFirmwareVersion": None,
            }
        },
        # The OTA blocks that sat beside it and were being read instead.
        "otaReportedProperties": {
            "firmwareType": "Assets",
            "updatedVersion": "2.00",
        },
        "firmwareUpdate": {"firmwareType": "Assets", "version": "2.00"},
    }


def test_interface_firmware_matches_the_app_not_the_artwork():
    """The bug this release exists for.

    Shower Right's `otaReportedProperties` describes **Assets** — the artwork bundle — and
    carries no Application block anywhere, so `Firmware` reported `2.00` where the Konnect
    app showed 2.2. The interface version was in `about.uI2.firmware` the whole time, one
    nesting level deeper than the code looked.
    """
    holder = _firmware_holder(_real_about("11"))
    assert holder.firmware == "2.2"
    assert holder.firmware != "2.00", "the Assets version is not the interface version"


def test_each_component_reports_its_own_firmware():
    """Three different numbers for one shower, which is why there are three entities."""
    holder = _firmware_holder(_real_about("10"))
    assert holder.component_firmware("uI2") == "2.2"
    assert holder.component_firmware("primaryValve") == "10"
    assert holder.component_firmware("gateway") == "00.74"


def test_two_valves_can_be_on_different_firmware():
    """The app shows 10 for both; the record says 10 and 11.

    Two showers that should be identical are not, and this is the only surface that says so.
    """
    left = _firmware_holder(_real_about("10"))
    right = _firmware_holder(_real_about("11"))
    assert left.component_firmware("primaryValve") == "10"
    assert right.component_firmware("primaryValve") == "11"
    # Everything else matches, which is what makes the valve difference meaningful.
    for component in ("uI2", "gateway"):
        assert left.component_firmware(component) == right.component_firmware(component)


def test_an_assets_only_record_reports_no_firmware():
    """With no `about` and only Assets blocks, `unknown` is the honest answer.

    A blank is better than a confidently wrong version in a bug report — and reporting the
    artwork version here is exactly what this release fixes.
    """
    holder = _firmware_holder(
        {
            "otaReportedProperties": {
                "firmwareType": "Assets",
                "updatedVersion": "2.00",
            },
            "firmwareUpdate": {"firmwareType": "Assets", "version": "2.00"},
        }
    )
    assert holder.firmware is None


def test_an_untyped_ota_block_is_still_read():
    """An untyped block is not an Assets block.

    The reference install's `otaReportedProperties` carries no `firmwareType` at all.
    Refusing it would trade one wrong answer for a blank on hardware that reads correctly
    today, so only a block explicitly naming a non-Application type is skipped.
    """
    holder = _firmware_holder(
        {"otaReportedProperties": {"currentFirmwareVersion": "01.02"}}
    )
    assert holder.firmware == "01.02"


def test_a_top_level_about_still_works():
    """The reference install's shape must keep reading as it always has."""
    assert _firmware_holder({"about": {"firmware": "00.74"}}).firmware == "00.74"


def test_component_firmware_is_none_for_an_absent_part():
    holder = _firmware_holder(_real_about("10"))
    assert holder.component_firmware("nosuchpart") is None
    assert _firmware_holder({}).component_firmware("gateway") is None


def test_firmware_entities_keep_their_ids_and_do_not_collide(valve_model):
    """The interface entity keeps `_firmware`, so history and automations survive."""
    coordinator = make_coordinator([make_valve(valve_model, [31, 11, 1])])
    ids = [e.unique_id for e in collect("sensor", coordinator)]
    assert len(ids) == len(set(ids)), "duplicate unique ids"
    firmware_ids = sorted(i for i in ids if "firmware" in i)
    assert any(i.endswith("_firmware") for i in firmware_ids), firmware_ids
    assert any(i.endswith("_firmware_valve") for i in firmware_ids), firmware_ids
    assert any(i.endswith("_firmware_gateway") for i in firmware_ids), firmware_ids


# --------------------------------------------------------------------------- #
# The scald limit (0.11.1)
# --------------------------------------------------------------------------- #


def _max_temperature_sensor(valve_model, tenths, unit):
    """The Max Temperature **control**.

    Was a diagnostic sensor until 0.18.1, when the read-only copy was retired — the
    configuration entity reports the same value and can also change it. These tests moved
    with it rather than being deleted: what they protect is how the valve's tenths are read
    and converted, which is unchanged.
    """
    from custom_components.kohler_anthem.anthem.state import OutletLimits
    from custom_components.kohler_anthem.number import OutletMaxTemperatureNumber

    valve = make_valve(valve_model, [31, 11, 1])
    # Replace every outlet: the control reads the lowest-numbered one, and a valve's
    # outlets agree unless a write was lost.
    for outlet_id in list(valve.gcs_state.outlet_limits):
        valve.gcs_state.outlet_limits[outlet_id] = OutletLimits(
            outlet_id, 16, 200, 1800, 200, 11, tenths, 150, 388, 1
        )
    coordinator = make_coordinator([valve])
    coordinator.temperature_unit = unit
    return OutletMaxTemperatureNumber(coordinator, valve)


def test_max_temperature_reads_118f_on_a_fahrenheit_account(valve_model):
    """The reference system's real setting: 47.8 C is the 118 F the Konnect app shows."""
    from homeassistant.const import UnitOfTemperature

    sensor = _max_temperature_sensor(valve_model, 478, "Fahrenheit")
    assert sensor.native_value == pytest.approx(118.04, abs=0.05)
    assert sensor.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT


def test_max_temperature_stays_celsius_on_a_metric_account(valve_model):
    from homeassistant.const import UnitOfTemperature

    sensor = _max_temperature_sensor(valve_model, 478, "Celsius")
    # A whole-degree control: 47.8 °C is reported as 48, the nearest settable value.
    assert sensor.native_value == 48
    assert sensor.native_unit_of_measurement == UnitOfTemperature.CELSIUS


def test_max_temperature_is_unknown_before_it_is_reported(valve_model):
    """`unknown` is not the same as "no limit", and must not read as a number."""
    sensor = _max_temperature_sensor(valve_model, None, "Fahrenheit")
    assert sensor.native_value is None


def test_rest_and_mqtt_agree_on_the_scald_limit():
    """REST reports display C (`45`), MQTT reports tenths (`450`). Both mean 45.0 C.

    The same wire/display split as flow, and getting it wrong here would silently misreport
    a safety setting by a factor of ten.
    """
    from custom_components.kohler_anthem.anthem.state import (
        outlet_limits_from_settings,
    )

    rest = outlet_limits_from_settings(
        {
            "valveSettings": [
                {
                    "outletConfigurations": [
                        {
                            "outLetId": "1",
                            "minimumFlowrate": "4",
                            "maximumFlowrate": "50",
                            "maximumRuntime": "1800",
                            "maximumOutletTemperature": "45",
                        }
                    ]
                }
            ]
        }
    )
    assert rest[1].maximum_temperature_tenths == 450

    # And the documented decimal case, which must not round to 478 vs 477.
    decimal = outlet_limits_from_settings(
        {
            "valveSettings": [
                {
                    "outletConfigurations": [
                        {
                            "outLetId": "1",
                            "minimumFlowrate": "4",
                            "maximumFlowrate": "50",
                            "maximumOutletTemperature": "47.8",
                        }
                    ]
                }
            ]
        }
    )
    assert decimal[1].maximum_temperature_tenths == 478


def test_cloud_connection_is_visible_and_enabled(valve_model):
    """0.11.2 unhid it.

    It has to keep *running* whether or not anyone is looking — its value is the record it
    builds while nobody is watching — so it was enabled but hidden. Hiding put "(Hidden)"
    beside the name everywhere it appeared, which reads as a broken entity, and the moment it
    matters is the moment every other entity has silently frozen. Both defaults are asserted
    because enabling without visibility is the state this test exists to prevent recurring.
    """
    coordinator = make_coordinator([make_valve(valve_model, [31, 11, 1])])
    sensor = next(
        e
        for e in collect("binary_sensor", coordinator)
        if e.unique_id.endswith("_cloud_connection")
    )
    assert sensor.entity_registry_visible_default is True
    assert sensor.entity_registry_enabled_default is True
    assert sensor.name == "Cloud Connection"


def _duration_sensor(valve_model, run_times):
    """`run_times` is keyed the way `Valve.outlet_run_times` is: **1-based**.

    These tests used to pass 0-based keys, which the attribute builder's `outlet + 1`
    silently absorbed — two mistakes cancelling out, and the reason a `ValueError` on real
    hardware went uncaught.
    """
    from custom_components.kohler_anthem.select import OutletRunTimeSelect

    valve = make_valve(valve_model, [31, 11, 1])
    valve.outlet_run_times = run_times
    return OutletRunTimeSelect(make_coordinator([valve]), valve)


def test_max_shower_duration_reports_the_shortest_outlet(valve_model):
    """Shower Right, as captured 2026-09-10: Showerhead 3600 s, the other two 1800 s.

    **A lost write, not a per-outlet setting.** There is one master duration; the app writes
    it one outlet at a time and stops at the first failure, so a 60->30 minute change left
    one outlet holding the old value. The shortest is both the value the setting was moving
    to and the soonest the water can stop.
    """
    sensor = _duration_sensor(valve_model, {1: 1800, 2: 3600, 3: 1800})
    # The control reports the shortest, same as the retired sensor did.
    assert sensor.extra_state_attributes["reported_minutes"] == 30.0
    assert sensor.current_option == "30 minutes"
    assert sensor.extra_state_attributes["outlets_agree"] is False
    assert sensor.extra_state_attributes["per_outlet"] == {
        "Rainhead": 30,
        "Showerhead": 60,
        "Handshower": 30,
    }


def test_max_shower_duration_says_so_when_outlets_agree(valve_model):
    """Shower Left: all three at 1800 s — the healthy state, and the normal one."""
    sensor = _duration_sensor(valve_model, {1: 1800, 2: 1800, 3: 1800})
    assert sensor.current_option == "30 minutes"
    assert sensor.extra_state_attributes["outlets_agree"] is True


def test_max_shower_duration_has_no_attributes_before_it_is_learned(valve_model):
    sensor = _duration_sensor(valve_model, {})
    assert sensor.current_option is None
    # No run times learned: no per-outlet breakdown to publish.
    assert "per_outlet" not in sensor.extra_state_attributes
    assert sensor.extra_state_attributes["reported_minutes"] is None


def test_a_longer_first_outlet_does_not_hide_a_shorter_one(valve_model):
    """The failure this replaces, in the direction that actually matters.

    Reading outlet 1 alone would report 60 minutes here while the Rainhead stops at 30 —
    telling somebody they have twice the shower they have. Same lost-write cause, with the
    surviving stale value on a different outlet.
    """
    sensor = _duration_sensor(valve_model, {1: 3600, 2: 1800, 3: 1800})
    assert sensor.extra_state_attributes["reported_minutes"] == 30.0
    assert sensor.extra_state_attributes["outlets_agree"] is False


# --------------------------------------------------------------------------- #
# The temperature slider's ceiling (0.12.0)
# --------------------------------------------------------------------------- #


def _temperature_number(valve_model, unit):
    valve = make_valve(valve_model, [31, 11, 1])
    coordinator = make_coordinator([valve])
    coordinator.temperature_unit = unit
    return next(
        e for e in collect("number", coordinator) if "temperature" in e.unique_id
    )


def test_the_temperature_slider_matches_the_app():
    """Cold, then 59 °F up to the valve's own scald limit — the Konnect app's zone slider.

    Konnect 3.0.6 bounds it from a `COLD` stop one below `minimumOutletTemperature` to the
    valve's *current* `maximumOutletTemperature`. The fixture valve reports 150 and 477
    tenths, so 58 (Cold) to 118 °F. It was a fixed 92-118 °F until 2026-10-07 — the Max
    Temperature *setting's* range, mistaken for this control's.
    """
    from homeassistant.const import UnitOfTemperature

    from custom_components.kohler_anthem.anthem.models import get_valve_model

    number = _temperature_number(get_valve_model("K-28210"), "Fahrenheit")
    assert number.native_min_value == 58
    assert number.native_max_value == 118
    assert number.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT


def test_the_temperature_slider_follows_the_valves_scald_limit():
    """Raise Max Temperature in the app and the zone slider widens, as the app's does."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    number = _temperature_number(get_valve_model("K-28210"), "Fahrenheit")
    for limits in number._valve.gcs_state.outlet_limits.values():
        limits.maximum_temperature_tenths = 450
    assert number.native_max_value == 113


def test_the_cold_stop_sends_full_cold_and_reads_back_as_cold():
    """The bottom step is the app's COLD: it writes 0 °C, and a 0 °C valve shows on it."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    number = _temperature_number(get_valve_model("K-28210"), "Fahrenheit")
    sent: dict = {}

    async def apply(**kwargs):
        sent.update(kwargs)

    number._valve.async_apply_valve = apply
    asyncio.run(number.async_set_native_value(number.native_min_value))
    assert sent == {"zone1_temperature": 32.0}

    number._valve.gcs_state.valve1.temperature_celsius = 0.0
    assert number.native_value == number.native_min_value
    assert number.extra_state_attributes["cold"] is True


def test_the_celsius_slider_stays_inside_the_codec_ceiling():
    """14 (Cold) to 48 °C, and the top must not exceed what `encode_word` accepts."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.anthem.valve_hex import (
        TEMPERATURE_MAX_TENTHS,
        TEMPERATURE_TENTHS_PER_DEGREE,
    )

    number = _temperature_number(get_valve_model("K-28210"), "Celsius")
    assert number.native_min_value == 14
    assert number.native_max_value == 48
    ceiling = TEMPERATURE_MAX_TENTHS / TEMPERATURE_TENTHS_PER_DEGREE
    assert number.native_max_value <= ceiling, (
        "the slider must not offer a refused value"
    )


def test_the_service_schema_matches_the_slider():
    """The `custom_shower` form and the number entity must not disagree on the range.

    They are two ways to set the same thing, and a form that accepts what the slider refuses
    (or the reverse) is the kind of inconsistency nobody finds until it bites.
    """
    from custom_components.kohler_anthem.const import (
        UI_TEMPERATURE_MAX_F,
        ZONE_TEMPERATURE_MIN_F,
    )
    from custom_components.kohler_anthem.services import (
        _FIELD_ZONE1_TEMPERATURE,
        _FIELD_ZONE2_TEMPERATURE,
        _temperature_bounds,
    )

    # The slider less its Cold stop, which a typed form deliberately cannot reach.
    for field in (_FIELD_ZONE1_TEMPERATURE, _FIELD_ZONE2_TEMPERATURE):
        selector = field["selector"]["number"]
        assert selector["max"] == UI_TEMPERATURE_MAX_F, field["name"]
        assert selector["min"] == ZONE_TEMPERATURE_MIN_F, field["name"]
    assert _temperature_bounds("Fahrenheit")[:2] == (59.0, 118.0)

    import yaml

    with open(
        "custom_components/kohler_anthem/services.yaml", encoding="utf-8"
    ) as handle:
        described = yaml.safe_load(handle)
    seen = 0
    for key, section in described["custom_shower"]["fields"].items():
        nested = section.get("fields") or {key: section}
        for name, field in nested.items():
            if name.endswith("_temperature"):
                seen += 1
                assert field["selector"]["number"]["min"] == ZONE_TEMPERATURE_MIN_F
                assert field["selector"]["number"]["max"] == UI_TEMPERATURE_MAX_F
    assert seen, "no temperature fields found in services.yaml"


def test_118f_encodes_to_the_valve_without_being_clamped():
    """The whole point of raising the ceiling: 118 °F must survive the codec intact."""
    from custom_components.kohler_anthem.anthem.valve_hex import (
        decode_word,
        encode_word,
        unit_to_celsius,
    )

    celsius = unit_to_celsius(118, "Fahrenheit")
    word = encode_word(1, celsius, 50.0, 0b001)
    assert decode_word(word).temperature_celsius == pytest.approx(celsius, abs=0.05)


def test_max_temperature_is_a_setting_not_a_hardware_ceiling(valve_model):
    """Observed live 2026-09-10: 450 tenths at 16:48, 477 at 16:55, on one valve.

    The owner changed it from 113 °F to 118 °F in the Konnect app and the valve took it on all
    three outlets. This entity therefore reports the ceiling **in force now**, which can move
    at any time — nothing may treat it as a fixed device property, and in particular the
    temperature slider's bounds must not be derived from it.
    """
    before = _max_temperature_sensor(valve_model, 450, "Fahrenheit")
    after = _max_temperature_sensor(valve_model, 477, "Fahrenheit")
    assert before.native_value == 113
    # 477 tenths is 47.7 °C = 117.86 °F, reported as a whole 118 because this is a
    # whole-degree control — 117.86 is not a value anyone can set. The round trip is exact
    # in both directions: `unit_to_celsius(118, "Fahrenheit")` produces 47.7 °C = 477
    # tenths, so Kohler's stored value and this integration agree on what "118 °F" means.
    assert after.native_value == 118


def test_the_slider_does_not_follow_the_scald_limit(valve_model):
    """The slider matches the app's range, not the valve's current setting.

    Deriving it from `maximumOutletTemperature` would have been the intuitive fix and is the
    wrong one: the limit is user-configurable, so the slider would silently reshape itself
    whenever somebody changed a setting in the app — and would have to be rebuilt to widen,
    since Home Assistant caches an entity's bounds.
    """
    from custom_components.kohler_anthem.anthem.state import OutletLimits

    valve = make_valve(valve_model, [31, 11, 1])
    # A valve set well below the slider's ceiling.
    valve.gcs_state.outlet_limits[1] = OutletLimits(1, 16, 200, 1800, 200, 11, 450)
    coordinator = make_coordinator([valve])
    coordinator.temperature_unit = "Fahrenheit"
    number = next(
        e for e in collect("number", coordinator) if "temperature" in e.unique_id
    )
    assert number.native_max_value == 118, "the slider follows the app, not the valve"


def test_favorite_is_us_spelled_but_its_id_is_not(valve_model):
    """0.21.0 renamed the entity to `Favorite`, matching the Konnect app.

    The unique id and the attribute keys deliberately keep `favourite`: they are
    identifiers, and moving them would orphan history and break anything reading them.
    """
    coordinator = make_coordinator(
        [make_valve(valve_model, [31, 11, 1])],
        controllers=(make_controller(valve_model, device_id="hub-1"),),
    )
    selects = [e for e in collect("select", coordinator) if "favourite" in e.unique_id]
    assert len(selects) == 2, [e.unique_id for e in collect("select", coordinator)]
    for entity in selects:
        assert entity.name == "Favorite"
        assert entity.unique_id.endswith("_favourite")
        assert "favourite_count" in entity.extra_state_attributes


def test_auto_restore_says_whether_the_fault_can_even_occur(valve_model):
    """The only identified cause of a spontaneous warm-up disable lives in the HUB.

    `docs/gcs/api.md` §3h: the Anthem Plus controller's web UI writes `warmUpDisabled` to the
    valve as a fixed step of its signed-in routine. The write originates in the hub's
    firmware; the valve is only the recipient. So on a controller-free account this switch
    defends against nothing observed, and `hub_present` is what says so.
    """
    valve = make_valve(valve_model, [31, 11, 1])

    # 0.20.0: the switch is no longer created at all where the fault cannot occur. That is
    # the same judgement `hub_present` used to report, acted on rather than described.
    alone = make_coordinator([valve])
    assert [
        e
        for e in collect("switch", alone)
        if e.unique_id.endswith("_warmup_auto_restore")
    ] == []

    with_hub = make_coordinator(
        [valve], controllers=(make_controller(valve_model, device_id="hub-1"),)
    )
    switch = next(
        e
        for e in collect("switch", with_hub)
        if e.unique_id.endswith("_warmup_auto_restore")
    )
    assert switch.extra_state_attributes["hub_present"] is True


# --------------------------------------------------------------------------- #
# Water used this year (0.13.0)
# --------------------------------------------------------------------------- #


def _yearly_sensor(
    valve_model, monkeypatch, series, units="Standard", now_month="2026-09"
):
    from datetime import datetime

    from custom_components.kohler_anthem import sensor as module

    # Pin the clock: the sensor excludes whatever month `datetime.now` says it is, so on
    # the real clock these tests start failing the moment `now_month` is in the past.
    class _PinnedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.strptime(f"{now_month}-15", "%Y-%m-%d").replace(tzinfo=tz)

    monkeypatch.setattr(module, "datetime", _PinnedDateTime)
    valve = make_valve(valve_model, [31, 11, 1])
    valve.usage = {"gcsUsageDataDetailsList": series}
    coordinator = make_coordinator([valve])
    coordinator.water_units = units
    sensor = next(
        e
        for e in collect("sensor", coordinator)
        if e.unique_id.endswith("_water_this_year")
    )
    return sensor


def test_yearly_water_sums_twelve_complete_months(valve_model, monkeypatch):
    """500 gallons a month for a year, in the litres the API actually returns."""
    litres_per_month = 500 / 0.264172
    series = [
        {"intervalKey": f"2025-{m:02d}", "volume": litres_per_month}
        for m in range(9, 13)
    ] + [
        {"intervalKey": f"2026-{m:02d}", "volume": litres_per_month}
        for m in range(1, 9)
    ]
    sensor = _yearly_sensor(valve_model, monkeypatch, series)
    assert sensor.native_value == pytest.approx(6000, abs=1)
    assert sensor.extra_state_attributes["months_counted"] == 12


def test_yearly_water_excludes_the_current_month(valve_model, monkeypatch):
    """A rolling window that crept up through the month would not be a `TOTAL`.

    The partial month belongs to `Water Used This Month`; including it here would make the
    value climb daily and then fall at every month boundary.
    """
    litres = 100 / 0.264172
    series = [
        {"intervalKey": "2026-08", "volume": litres},
        {"intervalKey": "2026-09", "volume": litres * 99},  # the current, partial month
    ]
    sensor = _yearly_sensor(valve_model, monkeypatch, series)
    assert sensor.native_value == pytest.approx(100, abs=1)
    assert sensor.extra_state_attributes["excludes_current_month"] == "2026-09"


def test_yearly_water_takes_only_the_twelve_most_recent(valve_model, monkeypatch):
    """A 400-day fetch returns thirteen months; the thirteenth must not inflate the year."""
    litres = 100 / 0.264172
    series = [{"intervalKey": f"2025-{m:02d}", "volume": litres} for m in range(1, 13)]
    series += [{"intervalKey": f"2026-{m:02d}", "volume": litres} for m in range(1, 9)]
    sensor = _yearly_sensor(valve_model, monkeypatch, series)
    assert sensor.extra_state_attributes["months_counted"] == 12
    assert sensor.native_value == pytest.approx(1200, abs=1)
    assert sensor.extra_state_attributes["last_month"] == "2026-08"


def test_yearly_water_reports_a_short_series_honestly(valve_model, monkeypatch):
    """A young account has fewer than twelve months, and must not read as a dry year."""
    litres = 100 / 0.264172
    series = [{"intervalKey": "2026-07", "volume": litres}]
    sensor = _yearly_sensor(valve_model, monkeypatch, series)
    assert sensor.native_value == pytest.approx(100, abs=1)
    assert sensor.extra_state_attributes["months_counted"] == 1


def test_yearly_water_stays_in_litres_on_a_metric_account(valve_model, monkeypatch):
    series = [{"intervalKey": "2026-08", "volume": 1000.0}]
    sensor = _yearly_sensor(valve_model, monkeypatch, series, units="Liters")
    assert sensor.native_value == pytest.approx(1000.0)


def test_yearly_water_is_none_without_a_series(valve_model, monkeypatch):
    assert _yearly_sensor(valve_model, monkeypatch, []).native_value is None


# --------------------------------------------------------------------------- #
# Upstream parity (0.15.0)
# --------------------------------------------------------------------------- #


def test_topology_latches_only_on_an_answer(valve_model):
    """A read with no layout in it must not pin the entry's model for ever.

    `_topology_checked` was set to True *before* `_apply_topology` ran, so a first
    `gcsadvancestate` read carrying no layout latched anyway — and on an account whose
    valves differ, that leaves the wrong layout on every valve but the first, permanently.
    Found by GitHub Copilot's review of upstream #3.
    """
    from custom_components.kohler_anthem.coordinator import Valve

    valve = make_valve(valve_model, [31, 11, 1])
    # An empty settings payload says nothing about the layout.
    assert Valve._apply_topology(valve, {}) is False
    assert Valve._apply_topology(valve, {"valveSettings": []}) is False


def test_topology_latches_when_the_read_agrees(valve_model):
    """An answer that matches the entry is still an answer — it must not re-read for ever."""
    from custom_components.kohler_anthem.coordinator import Valve

    valve = make_valve(valve_model, [31, 11, 1])
    # The real shape: `valve` names the slot, `noOfOutlets` is the count.
    settings = {"valveSettings": [{"valve": "valve1", "noOfOutlets": 3}]}
    assert Valve._apply_topology(valve, settings) is True


def test_a_zone2_word_is_refused_on_a_single_zone_valve(valve_model):
    """The form offers Zone 2 whenever *any* valve has one — including for one that has not.

    The word used to be encoded and sent to a valve with nothing to receive it. All zeroes
    stays legal: that is the sentinel this valve genuinely uses for "no second valve".

    Drives the real coroutine so the guard itself is exercised, not a restatement of its
    condition — the valve double raises on any actual write, so reaching the send is a
    failure too.
    """
    import asyncio

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.kohler_anthem.coordinator import Valve

    assert valve_model.uses_valve2 is False
    valve = make_valve(valve_model, [31, 11, 1])
    # The guard runs after the "is there a valve at all" check, so both must be present.
    valve.gcs = object()
    valve.name = "Shower Left"

    with pytest.raises(HomeAssistantError, match="one zone"):
        asyncio.run(Valve.async_send_valve_hex(valve, "0184C801", "1184C801"))


def test_the_report_log_records_decisions_beside_messages(tmp_path):
    """One switch, one attachment: the wire traffic and the reasoning, on one clock.

    A report that shows a decision was *seen and skipped* answers "why didn't it act"
    without asking the reader to line two files up by timestamp. The journal names are
    labels; ReportLog accepts any.
    """
    import json

    from custom_components.kohler_anthem.anthem.report_log import ReportLog

    log = ReportLog(str(tmp_path))
    log.start()
    log.write("$iothub/twin/PATCH", b'{"data":{"code":"GCS_SOLO_STS"}}')
    log.note("cutoff", "zone_start", {"zone": 1})
    log.note(
        "warmup", "mode", {"before": "warmUpDisabled", "after": "warmUpAllOutlets"}
    )
    log.stop()
    log.close()

    lines = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    messages = [r for r in lines if "topic" in r]
    decisions = [r for r in lines if "journal" in r]
    assert len(messages) == 1
    assert {r["journal"] for r in decisions} == {"cutoff", "warmup"}
    # The two vocabularies reuse event names, which is why `journal` has to be there.
    assert all("event" in r and "ts" in r for r in decisions)
    # A decision must never be mistakable for a message, or `jq select(.topic)` breaks.
    assert not any("topic" in r for r in decisions)


def test_the_report_log_ignores_decisions_when_no_episode_is_active(tmp_path):
    """Off means off — a note outside an episode must not create a file."""
    from custom_components.kohler_anthem.anthem.report_log import ReportLog

    log = ReportLog(str(tmp_path))
    log.note("cutoff", "zone_start", {"zone": 1})
    assert list(tmp_path.glob("*.jsonl")) == []


def test_the_report_log_never_opens_a_file_from_the_event_loop(tmp_path):
    """`note()` runs on the loop; opening a file there is a blocking-call error in HA.

    Shipped doing exactly that in 0.15.0 — `note()` fell through to `_write_line_locked`,
    which calls `_open_locked`, which does `os.makedirs`, a README write and an `open()`.
    `write()` is fine because paho calls it on its own network thread; `note()` is not,
    because the cutoff detector and the warm-up watcher both live on the loop.
    """
    import builtins

    from custom_components.kohler_anthem.anthem.report_log import ReportLog

    log = ReportLog(str(tmp_path))
    log.start()
    # The state `note()` must tolerate: an episode is live but no handle is open.
    log._close_locked(quiet=True)

    opened: list[str] = []
    real_open = builtins.open

    def tracking_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    builtins.open = tracking_open
    try:
        log.note("cutoff", "zone_start", {"zone": 1})
    finally:
        builtins.open = real_open

    assert opened == [], f"note() opened files on the event loop: {opened}"
    # It must ask for the open instead, so the caller can schedule it in an executor.
    assert log.wants_open is True

    log.prepare()
    log.note("cutoff", "zone_stop", {"zone": 1})
    log.close()
    written = [
        line for p in tmp_path.glob("*.jsonl") for line in p.read_text().splitlines()
    ]
    assert len(written) == 1, "the record after prepare() must land"


# --------------------------------------------------------------------------- #
# Lifecycle bugs found by review (0.15.1)
# --------------------------------------------------------------------------- #


def test_the_quiet_timer_survives_a_valve_that_has_never_spoken():
    """`_last_gcs_at` is None until the first MQTT message — which can be hours away.

    Unguarded, the subtraction raised `TypeError` **after** `_quiet_cancel` was nulled and
    **before** either re-arm, so trigger B died for the life of the coordinator. A
    push-only integration on a quiet shower is the normal case, not the edge case: the
    module records benign silences of 12 h and 35 h.
    """
    from custom_components.kohler_anthem.cloud_watch import CloudConnectionWatch

    watch = CloudConnectionWatch.__new__(CloudConnectionWatch)
    watch._last_gcs_at = None
    watch._quiet_cancel = object()
    armed: list = []
    checked: list = []
    watch._arm_quiet_timer = lambda *a: armed.append(a)
    watch._request_check = lambda reason: checked.append(reason)

    CloudConnectionWatch._quiet_elapsed(watch, None)

    assert checked, "silence since setup is exactly what trigger B asks about"
    assert armed, "the timer must be re-armed or the trigger is dead for ever"


def test_valves_are_seeded_once_not_twice():
    """A serial loop was left in place when the concurrent gather was added (0.9.0).

    Every valve was seeded twice on every setup, reconnect and manual refresh — double the
    REST traffic the change existed to reduce, and two `async_seed` coroutines for one valve
    interleaving over the same state objects.
    """
    import ast
    import inspect
    import textwrap

    from custom_components.kohler_anthem import coordinator as module

    source = textwrap.dedent(
        inspect.getsource(module.KohlerAnthemCoordinator._async_seed_state)
    )
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_seed"
    ]
    assert len(calls) == 1, f"async_seed is called {len(calls)} times per seed"


def test_the_reseed_task_is_held_so_unload_can_cancel_it():
    """It ends in a config-entry write and a push into entities, and outlived an unload.

    `_async_seed_state` is a dozen REST round trips; a reload inside that window left the
    task awaiting HTTP against a coordinator Home Assistant had already discarded.
    """
    import inspect

    from custom_components.kohler_anthem import coordinator as module

    spawn = inspect.getsource(module.KohlerAnthemCoordinator._handle_connected)
    assert "_reseed_task = self.hass.async_create_task" in spawn, spawn

    shutdown = inspect.getsource(module.KohlerAnthemCoordinator.async_shutdown_stream)
    assert "_reseed_task.cancel()" in shutdown, shutdown


@pytest.mark.parametrize("warm", [[], "x", 5, 0, True])
def test_a_malformed_warmupstate_does_not_abort_the_seed(warm, valve_model):
    """The one container `apply_rest_state` promised to type-check and did not.

    `or {}` rescues a null but hands a list or a string through, and the next `.get` raises
    `AttributeError` inside the REST seed — aborting setup with a traceback. Both call sites
    catch only `KohlerError`, so nothing downstream would have absorbed it.
    """
    from custom_components.kohler_anthem.anthem.state import GcsState

    state = GcsState(valve_model, "Fahrenheit")
    state.apply_rest_state({"state": {"warmUpState": warm}})  # must not raise
    assert state.warmup_mode is None


def test_a_good_warmupstate_still_parses(valve_model):
    """The guard must not cost the normal case."""
    from custom_components.kohler_anthem.anthem.state import GcsState

    state = GcsState(valve_model, "Fahrenheit")
    state.apply_rest_state(
        {"state": {"warmUpState": {"warmUp": "warmUpDisabled", "state": "x"}}}
    )
    assert state.warmup_mode == "warmUpDisabled"


@pytest.mark.parametrize(
    ("outlets", "expected"),
    [
        # A string decomposes into truthy characters and read as EVERY outlet running —
        # wrong state, silently, with no error anywhere.
        ("110", [False, False, False]),
        # Kohler serves `"outlets": 2` as a *count* elsewhere in the same API
        # (docs/hub/cloud_api.md), and `len()` on an int raises.
        (2, [False, False, False]),
        (None, [False, False, False]),
        # The legitimate shape must still work.
        ([1, 0, 1], [True, False, True]),
    ],
)
def test_the_mqtt_outlet_array_is_guarded_like_the_rest_one(
    outlets, expected, valve_model
):
    """The REST path guarded this; the MQTT path did not."""
    from types import SimpleNamespace

    from custom_components.kohler_anthem.anthem.state import HubState

    state = HubState(valve_model)
    state._apply_valve(
        SimpleNamespace(
            attributes=[{"zone": "1", "status": "ON", "outlets": outlets}], raw={}
        )
    )
    assert state.zones[1].outlets == expected


# --------------------------------------------------------------------------- #
# Seeding concurrency (0.16.0)
# --------------------------------------------------------------------------- #


class _SeedRecorder:
    """A stand-in client that records call order and sleeps like a round trip."""

    def __init__(self, delay: float = 0.02) -> None:
        self.delay = delay
        self.started: list[str] = []
        self.fail: set[str] = set()

    async def _read(self, name: str) -> dict:
        self.started.append(name)
        await asyncio.sleep(self.delay)
        if name in self.fail:
            from custom_components.kohler_anthem.anthem.client import (
                KohlerError,
            )

            raise KohlerError(f"{name} failed")
        return {}

    async def async_get_gcs_settings(self, device_id):
        return await self._read("settings")

    async def async_get_gcs_state(self, device_id):
        return await self._read("state")

    async def async_get_gcs_configuration(self, device_id):
        return await self._read("configuration")

    async def async_get_gcs_about(self, device_id):
        # Mirrors the real client, which answers {} rather than raising.
        try:
            return await self._read("about")
        except Exception:
            return {}

    async def async_get_gcs_presets(self, device_id):
        return await self._read("presets")

    async def async_get_usage(self, device_id, *, from_date, to_date, interval="MONTH"):
        # Mirrors the real client, which answers {} rather than raising.
        try:
            return await self._read("usage")
        except Exception:
            return {}


def _seed_valve(client):
    """A Valve with just enough wired up to run `async_seed`."""
    from custom_components.kohler_anthem import coordinator as module

    valve = object.__new__(module.Valve)
    # `client` is a read-only property reading through the coordinator.
    valve.coordinator = SimpleNamespace(client=client)
    valve.gcs_device = SimpleNamespace(device_id="dev-1")
    valve.configuration = None
    valve.usage = {}
    valve._seeded_presets = None
    valve._topology_checked = True
    valve.cloud_watch = None
    valve.gcs_state = SimpleNamespace(
        outlet_limits={},
        warmup_mode=None,
        apply_rest_state=lambda payload: None,
        apply_preset_list=lambda payload: False,
    )
    valve.warmup = SimpleNamespace(note_seeded_mode=lambda before, after: None)
    return valve


@pytest.mark.asyncio
async def test_independent_seed_reads_overlap_the_ordered_pair():
    """Three of the five seed reads depend on nothing and must not wait their turn.

    Only `gcs-settings` → `gcs-state` is a real ordering (topology decodes the state word).
    Run serially, a cold start paid five round trips deep per valve; the configuration,
    usage and preset reads now overlap the pair, making it two deep.
    """
    client = _SeedRecorder()
    valve = _seed_valve(client)

    await valve.async_seed()

    # The independent reads must have been *issued* before the ordered pair finished —
    # which is what proves they overlap rather than merely being reordered.
    assert client.started.index("configuration") < client.started.index("state")
    assert client.started.index("presets") < client.started.index("state")
    # And the dependency that is real still holds.
    assert client.started.index("settings") < client.started.index("state")


@pytest.mark.asyncio
async def test_seed_is_two_round_trips_deep_not_five():
    """Measured, not asserted from the source: the whole seed is two sleeps deep."""
    client = _SeedRecorder(delay=0.05)
    valve = _seed_valve(client)

    start = time.monotonic()
    await valve.async_seed()
    elapsed = time.monotonic() - start

    # Seven: settings, state, configuration, presets, the two usage series (monthly and
    # daily) and the per-part `about`, which overlap each other as well as the ordered pair.
    assert len(client.started) == 7, client.started
    # Still two round trips deep (~0.10s) plus slack; six serial would be ~0.30s.
    assert elapsed < 0.20, f"seed took {elapsed:.3f}s — reads went serial again"


@pytest.mark.asyncio
async def test_one_failed_seed_read_does_not_blank_the_others():
    """Each read is guarded individually; a gather must not let one cancel its siblings."""
    client = _SeedRecorder()
    client.fail = {"configuration", "settings"}
    valve = _seed_valve(client)

    await valve.async_seed()

    # Every read was still attempted, and the failures were absorbed.
    assert set(client.started) == {
        "settings",
        "state",
        "configuration",
        "presets",
        "usage",
        "about",
    }
    # A failed configuration read latches {} so a reconnect does not retry static data.
    assert valve.configuration == {}


@pytest.mark.asyncio
async def test_cancelling_a_seed_leaves_no_orphaned_reads():
    """A reload mid-seed must not leave reads running against a discarded entry.

    The same class of bug 0.15.1 fixed for the reconnect reseed: awaiting the background
    task in a plain `finally` would not do it, because that await is cancelled too.
    """
    client = _SeedRecorder(delay=0.05)
    valve = _seed_valve(client)

    task = asyncio.ensure_future(valve.async_seed())
    await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Give anything orphaned a chance to still be pending.
    await asyncio.sleep(0.12)
    pending = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    assert not pending, f"{len(pending)} seed read(s) outlived the cancelled seed"


def test_zone_word_entity_reads_the_right_zone_everywhere():
    """Zone→word was duplicated in two platforms; reading the wrong zone is silent.

    A two-zone shower would report and command the other half with no error anywhere, so
    the shared accessor is asserted against both zones on both platforms that use it.
    """
    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )
    from custom_components.kohler_anthem.entity import ZoneWordEntity
    from custom_components.kohler_anthem.number import ZoneNumberBase
    from custom_components.kohler_anthem.sensor import ValveHexSensor

    # Both platforms must share the one implementation, not re-declare it.
    assert issubclass(ValveHexSensor, ZoneWordEntity)
    assert issubclass(ZoneNumberBase, ZoneWordEntity)

    model = model_for_topology(3, 3)
    valve = make_valve(model, [31, 11, 1, 11, None, 21])
    coordinator = make_coordinator([valve])

    assert ValveHexSensor(coordinator, valve, 1)._word is valve.gcs_state.valve1
    assert ValveHexSensor(coordinator, valve, 2)._word is valve.gcs_state.valve2

    # The diagnostic defaults and the unique id must survive the shared base.
    hex2 = ValveHexSensor(coordinator, valve, 2)
    assert hex2.unique_id == "gcs-test0001_zone_2_hex"
    assert hex2.entity_registry_enabled_default is False
    assert hex2.entity_category is not None


# --------------------------------------------------------------------------- #
# Security (0.16.0)
# --------------------------------------------------------------------------- #


def test_version_scanner_does_not_copy_out_identifiers():
    """A device id is short enough to pass the length ceiling — shape must be checked too.

    `_version_fields` walks whatever the cloud returns, so it meets keys no capture has
    covered. A real device id is `gcs-sio32343h7` — fourteen characters, well under
    `_VERSION_VALUE_MAX_LEN` — so length alone could never have caught one sitting under a
    version-shaped key.
    """
    from custom_components.kohler_anthem.diagnostics import _version_fields

    found = _version_fields(
        {
            "configuration": {
                "about": {
                    "interface": {"firmware": "2.20"},
                    "valve": {"firmware": 10},
                    "gateway": {"firmware": "00.74"},
                }
            },
            "shortDeviceVersion": "gcs-sio32343h7",
            "hubVersionId": "hub-ab12cd34",
            "swVersion": "00112233-4455-6677-8899-001122334455",
            "iotVersion": "HostName=x;DeviceId=gcs-1122;SharedAccessKey=k==",
            "serialVersion": "0011223344556677aabb",
            "version": "2.88",
        }
    )

    # Every real firmware still comes through, including the bare integer.
    assert found["configuration.about.interface.firmware"] == "2.20"
    assert found["configuration.about.valve.firmware"] == 10
    assert found["configuration.about.gateway.firmware"] == "00.74"
    assert found["version"] == "2.88"

    # Nothing identifier-shaped survives as its own value.
    for key in (
        "shortDeviceVersion",
        "hubVersionId",
        "swVersion",
        "iotVersion",
        "serialVersion",
    ):
        assert found[key].startswith("<str,"), f"{key} leaked as {found[key]!r}"
    assert "gcs-sio32343h7" not in repr(found)
    assert "SharedAccessKey" not in repr(found)


def test_a_secret_hidden_in_a_value_is_redacted():
    """`_redact_payload` matched key names only; an Azure connection string hides in one.

    It runs on whatever the cloud returns — including `probe_usage`, which deliberately
    calls undocumented endpoints — so a credential under a neutral key was copied out whole.
    """
    from custom_components.kohler_anthem.anthem.client import _redact_payload

    out = _redact_payload(
        {
            "connectionString": (
                "HostName=k.azure-devices.net;DeviceId=gcs-x;SharedAccessKey=SECRET=="
            ),
            "sasUri": "https://x/?sig=ABC123&se=1",
            "password": "hunter2",
            "ioTHub": "kohler.azure-devices.net",
            "firmware": "2.20",
        }
    )

    assert out["connectionString"] == "**REDACTED**"
    assert out["sasUri"] == "**REDACTED**"
    assert out["password"] == "**REDACTED**"
    assert "SECRET" not in repr(out)
    assert "ABC123" not in repr(out)
    # Still useful: non-secret context survives.
    assert out["ioTHub"] == "kohler.azure-devices.net"
    assert out["firmware"] == "2.20"


def test_report_log_will_not_resume_outside_its_directory(tmp_path):
    """`_part_path` joins the stem straight onto the directory.

    Only `start()` ever writes the stem, so a hostile value means the config entry was
    already edited — but it round-trips through a file an operator may hand-edit, and the
    guard is one comparison.
    """
    from custom_components.kohler_anthem.anthem.report_log import ReportLog

    log = ReportLog(str(tmp_path / "reports"), max_bytes=10_000)
    try:
        log.resume("../../../../../../tmp/kohler_pwned_report")
        # Rejected, and a fresh legitimate episode started in its place.
        assert log._stem is not None
        assert log._stem.startswith("report_")
        assert not Path("/tmp/kohler_pwned_report.jsonl").exists()
        for written in (tmp_path / "reports").glob("*.jsonl"):
            assert written.parent == tmp_path / "reports"
    finally:
        log.close()

    # A name this class could have written is still honoured.
    log2 = ReportLog(str(tmp_path / "reports"), max_bytes=10_000)
    try:
        log2.resume("report_20260911T120000Z")
        assert log2._stem == "report_20260911T120000Z"
    finally:
        log2.close()


def test_no_device_id_reaches_a_non_debug_log():
    """Device ids are cloud addresses and must not reach the log people paste into issues.

    0.9.0 removed them from the startup line and from read errors; three INFO messages
    added since then had reintroduced them — two topology-mismatch messages (which a
    multi-valve account triggers) and the per-valve settings migration.

    DEBUG is exempt: it is opt-in, and the raw captures already carry ids with their own
    warning. `mqtt.py` is allowed the last 8 characters of the *mobile* identity, which is
    this integration's own registration, not a device address.
    """
    import ast

    base = Path(__file__).resolve().parents[1] / "custom_components" / "kohler_anthem"
    risky = (
        "device_id",
        "serial_number",
        "refresh_token",
        "access_token",
        "tenant_id",
        "password",
    )
    offenders = []
    for path in sorted([*base.glob("*.py"), *base.glob("anthem/*.py")]):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"info", "warning", "error", "critical"}
                and isinstance(node.func.value, ast.Name)
                and "LOGGER" in node.func.value.id.upper()
            ):
                continue
            for arg in node.args[1:]:
                source = ast.unparse(arg)
                if any(name in source for name in risky):
                    offenders.append(f"{path.name}:{node.lineno} {source}")

    # Matched on content, not on a line number, so this does not break when mqtt.py moves.
    def _allowed(entry: str) -> bool:
        return "_mobile_device_id" in entry and "[-8:]" in entry

    assert not [o for o in offenders if not _allowed(o)], offenders


def test_max_shower_duration_publishes_attributes_without_raising():
    """It read `unknown` on real hardware while `native_value` was returning 30 the whole time.

    `outlet_run_times` is 1-based; the attribute builder passed `outlet + 1` on top of that,
    so every outlet was looked up one place too high and the last ran off the end of the
    model. `outlet_location` raises `ValueError` there, and an exception while Home Assistant
    reads a property fails the entity — so the value never reached the state machine.

    Reproduced on the owner's K-28210 (3 outlets, one zone) 2026-09-11, where a diagnostics
    download taken minutes earlier showed all three run times learned at 1800 s.
    """
    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )
    from custom_components.kohler_anthem.select import OutletRunTimeSelect

    for topology in ((3, 0), (2, 0), (2, 2), (3, 3)):
        model = model_for_topology(*topology)
        types = [31, 11, 1, 11, None, 21][: model.total_outlets]
        valve = make_valve(model, types)
        coordinator = make_coordinator([valve])
        sensor = OutletRunTimeSelect(coordinator, valve)

        # The value was always right; the attributes are what failed.
        assert sensor.current_option == "30 minutes", model.sku
        attributes = sensor.extra_state_attributes
        assert attributes["outlets_agree"] is True, model.sku
        # One entry per outlet, each named for the fixture actually at that position.
        assert len(attributes["per_outlet"]) == model.total_outlets, model.sku
        assert all(v == 30.0 for v in attributes["per_outlet"].values()), model.sku

    # The first outlet must map to the first fixture, not the second — the off-by-one was
    # invisible in the count and only showed in the names.
    model = model_for_topology(3, 0)
    valve = make_valve(model, [31, 11, 1])
    sensor = OutletRunTimeSelect(make_coordinator([valve]), valve)
    assert list(sensor.extra_state_attributes["per_outlet"]) == [
        "Rainhead",
        "Showerhead",
        "Handshower",
    ]


# --------------------------------------------------------------------------- #
# Daily and weekly water (0.17.0)
# --------------------------------------------------------------------------- #

# The owner's real `Interval=DAY` response, 2026-09-11 — the probe that established DAY
# works where WEEK does not. Litres, as every `gcs-usage` volume is.
_REAL_DAY_SERIES = {
    "gcsUsageDataDetailsList": [
        {"intervalKey": key, "volume": litres}
        for key, litres in (
            ("2026-08-28", 112),
            ("2026-08-29", 59),
            ("2026-08-30", 75),
            ("2026-08-31", 75),
            ("2026-09-01", 46),
            ("2026-09-02", 39),
            ("2026-09-03", 31),
            ("2026-09-04", 47),
            ("2026-09-05", 38),
            ("2026-09-06", 39),
            ("2026-09-07", 42),
            ("2026-09-08", 37),
            ("2026-09-09", 49),
            ("2026-09-10", 56),
            ("2026-09-11", 41),
        )
    ]
}


def _water_sensors(monkeypatch, *, metric=False):
    """The day and week sensors over the real series, with "today" pinned to its last day."""
    from datetime import date

    from custom_components.kohler_anthem import sensor as module
    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )

    class _PinnedNow:
        @staticmethod
        def now():
            class _Today:
                @staticmethod
                def date():
                    return date(2026, 9, 11)

            return _Today()

    monkeypatch.setattr(module, "dt_util", _PinnedNow)
    model = model_for_topology(3, 0)
    valve = make_valve(model, [31, 11, 1])
    valve.usage_daily = _REAL_DAY_SERIES
    coordinator = make_coordinator([valve])
    if metric:
        coordinator.water_units = "Liters"
    return (
        module.ValveDailyWaterSensor(coordinator, valve),
        module.ValveWeeklyWaterSensor(coordinator, valve),
    )


def test_water_used_today_is_the_last_bucket(monkeypatch):
    """41 L on 2026-09-11 -> 10.8 US gallons, from the owner's own probe response."""
    today, _ = _water_sensors(monkeypatch)
    assert today.native_value == 10.8
    assert today.extra_state_attributes["days_counted"] == 1


def test_water_used_today_accepts_utc_next_day_bucket(monkeypatch):
    """US evenings can label local today's usage with tomorrow's UTC date."""
    from datetime import UTC, date

    from custom_components.kohler_anthem import sensor as module
    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )

    class _PinnedLocalNow:
        @staticmethod
        def now():
            class _Today:
                @staticmethod
                def date():
                    return date(2026, 9, 15)

            return _Today()

    class _PinnedUtcDateTime:
        @staticmethod
        def now(tz=None):
            assert tz is UTC

            class _Today:
                @staticmethod
                def date():
                    return date(2026, 9, 16)

            return _Today()

    monkeypatch.setattr(module, "dt_util", _PinnedLocalNow)
    monkeypatch.setattr(module, "datetime", _PinnedUtcDateTime)
    model = model_for_topology(3, 0)
    valve = make_valve(model, [31, 11, 1])
    valve.usage_daily = {
        "gcsUsageDataDetailsList": [{"intervalKey": "2026-09-16", "volume": 41}]
    }
    coordinator = make_coordinator([valve])

    today = module.ValveDailyWaterSensor(coordinator, valve)

    assert today.native_value == 10.8
    assert today.extra_state_attributes == {
        "days_counted": 1,
        "bucket_date": "2026-09-16",
    }


def test_water_used_this_week_sums_seven_days(monkeypatch):
    """A rolling seven days, today included — `Interval=WEEK` is refused by this endpoint."""
    _, week = _water_sensors(monkeypatch)
    # 38+39+42+37+49+56+41 = 302 L -> 79.8 gal.
    assert week.native_value == 79.8
    attributes = week.extra_state_attributes
    assert attributes["days_counted"] == 7
    assert attributes["window_days"] == 7
    # The breakdown must cover exactly the window, in order, and stop at today.
    assert list(attributes["per_day"]) == [
        "2026-09-05",
        "2026-09-06",
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
    ]


def test_daily_water_stays_in_litres_on_a_metric_account(monkeypatch):
    """`volume` is litres on the wire whatever the account; only the display converts."""
    today, week = _water_sensors(monkeypatch, metric=True)
    assert today.native_value == 41.0
    assert week.native_value == 302.0


def test_the_daily_series_agrees_with_the_monthly_one():
    """The check that proves the DAY series is real and not a different unit or window.

    The owner's daily buckets for 2026-09 sum to 465 L — exactly what the `MONTH` call
    reported for that month in the same probe run.
    """
    september = sum(
        entry["volume"]
        for entry in _REAL_DAY_SERIES["gcsUsageDataDetailsList"]
        if entry["intervalKey"].startswith("2026-09")
    )
    assert september == 465


def test_water_sensors_are_empty_without_a_series(monkeypatch):
    """No series is `unknown`, not zero — zero would read as "no water used today"."""
    from custom_components.kohler_anthem import sensor as module
    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )

    model = model_for_topology(3, 0)
    valve = make_valve(model, [31, 11, 1])
    valve.usage_daily = {}
    coordinator = make_coordinator([valve])
    for sensor in (
        module.ValveDailyWaterSensor(coordinator, valve),
        module.ValveWeeklyWaterSensor(coordinator, valve),
    ):
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {}


@pytest.mark.asyncio
async def test_daily_usage_refreshes_when_a_shower_ends():
    """The only moment the figure can change, and the only read this adds.

    `SCAN_INTERVAL` is None — there is no clock — so without this the day's total would hold
    whatever it read at startup. Tying it to the running -> stopped edge keeps the push-only
    design: no timer, and no call at all on a day nobody showered.
    """
    from custom_components.kohler_anthem import coordinator as module

    calls: list[str] = []

    class _Holder:
        """Enough of a Valve to exercise the edge detector."""

        _track = module.Valve._track
        _note_running_for_usage = module.Valve._note_running_for_usage
        _async_refresh_daily_usage_soon = module.Valve._async_refresh_daily_usage_soon

        def __init__(self):
            self.gcs_state = SimpleNamespace(is_running=False)
            self._was_running = False
            self._daily_usage_task = None
            self._background_tasks = set()
            self.hass = SimpleNamespace(async_create_task=asyncio.ensure_future)
            self.coordinator = SimpleNamespace(
                async_refresh_entities=lambda: calls.append("rendered")
            )

        async def async_refresh_daily_usage(self):
            calls.append("read")

    valve = _Holder()

    # Running: nothing to do — the total cannot be final while water is flowing.
    valve.gcs_state.is_running = True
    valve._note_running_for_usage()
    assert calls == []

    # Stopped: one read, after the delay that lets Kohler aggregate the session.
    with patch.object(module.asyncio, "sleep", new=_noop_sleep):
        valve.gcs_state.is_running = False
        valve._note_running_for_usage()
        assert valve._daily_usage_task is not None
        await valve._daily_usage_task

    assert calls == ["read", "rendered"]

    # A second stop message in the same wind-down must not become a second cloud read.
    calls.clear()
    valve._note_running_for_usage()
    assert calls == []


async def _noop_sleep(_seconds):
    """`asyncio.sleep` with the wait removed, so the delay is not paid in tests."""
    return None


def test_outlet_limits_capture_every_writeoutletconfig_field():
    """`writeoutletconfig` replaces the whole record, so every field must be readable.

    Three of its eleven keys were never parsed — `outLetFlags`,
    `minimumOutletTemperature` and `defaultOutletTemperature` — so a write would have had to
    invent them, and one sits beside the scald limit. See `docs/gcs/api.md` §1c.

    The minimum landing on 59 °F is the corroboration that matters: it is exactly the lower
    bound the Konnect app offers for Default Temperature, which is what identifies
    `defaultOutletTemperature` as the field behind that setting.
    """
    from custom_components.kohler_anthem.anthem.state import (
        outlet_limits_from_settings,
    )

    # REST display units, the shape `gcsadvancestate` returns.
    limits = outlet_limits_from_settings(
        {
            "setting": {
                "valveSettings": [
                    {
                        "outletConfigurations": [
                            {
                                "outLetId": "0",
                                "outLetType": "31",
                                "outLetFlags": "1",
                                "minimumOutletTemperature": "15",
                                "defaultOutletTemperature": "38.8",
                                "maximumOutletTemperature": "47.7",
                                "minimumFlowrate": "4",
                                "defaultFlowrate": "50",
                                "maximumFlowrate": "50",
                                "maximumRuntime": "1800",
                            }
                        ]
                    }
                ]
            }
        }
    )

    limit = limits[0]
    # Tenths of °C, normalised from REST's display °C like the maximum already was.
    assert limit.minimum_temperature_tenths == 150  # 59.0 °F
    assert limit.default_temperature_tenths == 388  # 101.8 °F
    assert limit.maximum_temperature_tenths == 477  # 117.9 °F
    # Read only so a write can echo it back unchanged; never interpreted.
    assert limit.outlet_flags == 1
    # The fields that already worked must not have regressed.
    assert limit.maximum_run_time == 1800
    assert limit.outlet_type == 31


def test_missing_write_fields_are_none_not_zero():
    """An absent key must read as "not learned", never as a real 0 °C or flag 0.

    Zero is a legal-looking value for all three, and a write that echoed it back would
    silently reset the record it was meant to preserve.
    """
    from custom_components.kohler_anthem.anthem.state import (
        outlet_limits_from_settings,
    )

    limits = outlet_limits_from_settings(
        {
            "setting": {
                "valveSettings": [
                    {
                        "outletConfigurations": [
                            {
                                "outLetId": "0",
                                "minimumFlowrate": "4",
                                "maximumFlowrate": "50",
                            }
                        ]
                    }
                ]
            }
        }
    )

    limit = limits[0]
    assert limit.minimum_temperature_tenths is None
    assert limit.default_temperature_tenths is None
    assert limit.outlet_flags is None


# --------------------------------------------------------------------------- #
# Writable outlet configuration (0.18.0)
# --------------------------------------------------------------------------- #


def _config_entities(coordinator, valve):
    from custom_components.kohler_anthem.number import (
        OutletDefaultTemperatureNumber,
        OutletMaxTemperatureNumber,
    )
    from custom_components.kohler_anthem.select import OutletRunTimeSelect

    return (
        OutletMaxTemperatureNumber(coordinator, valve),
        OutletDefaultTemperatureNumber(coordinator, valve),
        OutletRunTimeSelect(coordinator, valve),
    )


def test_the_three_settings_match_the_konnect_app(valve_model):
    """Ranges and options taken from the app, against the owner's own valve values."""
    valve = make_valve(valve_model, [31, 11, 1])
    coordinator = make_coordinator([valve])
    maximum, default, duration = _config_entities(coordinator, valve)

    # 92-118 °F, the app's Max Temperature range.
    assert (maximum.native_min_value, maximum.native_max_value) == (92.0, 118.0)
    assert maximum.native_value == 118  # 477 tenths
    # 59 °F up to whatever the scald limit is — not a constant.
    assert default.native_min_value == 59.0
    assert default.native_value == 102  # 388 tenths
    # The app's six, in order.
    assert duration.options == [
        "15 minutes",
        "20 minutes",
        "25 minutes",
        "30 minutes",
        "45 minutes",
        "60 minutes",
    ]
    assert duration.current_option == "30 minutes"
    # All three are settings, not diagnostics.
    for entity in (maximum, default, duration):
        assert entity.entity_category == EntityCategory.CONFIG


@pytest.mark.asyncio
async def test_default_temperature_refuses_to_exceed_the_scald_limit(valve_model):
    """A fixed 92-118 slider, with the scald limit enforced on the way in.

    0.18.2: the ceiling used to move with `Max Temperature`, which was faithful to the app
    and worse to use — Home Assistant caches an entity's bounds, so the slider's range
    changed shape underneath the user and could show a stale limit. A fixed range with a
    clear refusal is the more predictable trade.
    """
    from homeassistant.exceptions import HomeAssistantError

    valve = make_valve(valve_model, [31, 11, 1])
    written: list[int] = []
    valve.async_write_outlet_setting = lambda **kw: written.append(kw) or _done()
    _, default, _ = _config_entities(make_coordinator([valve]), valve)
    # Not what this test is about; it needs a live `hass` the fixture has no reason to build.
    default.async_write_ha_state = lambda: None

    # The slider's range never moves.
    assert (default.native_min_value, default.native_max_value) == (59.0, 118.0)

    for limit in valve.gcs_state.outlet_limits.values():
        limit.maximum_temperature_tenths = 450  # 113 °F

    # The ceiling is still visible, just not as the slider's bound.
    assert default.extra_state_attributes["scald_limit"] == 113

    # Above it: refused, naming both numbers and the setting to change.
    with pytest.raises(HomeAssistantError, match="Max Temperature"):
        await default.async_set_native_value(118)
    assert written == []

    # At or below it: written.
    await default.async_set_native_value(110)
    assert len(written) == 1


async def _done():
    """An already-finished awaitable, for stubbing a write."""
    return None


def test_duration_reports_a_value_the_app_cannot_offer_honestly(valve_model):
    """A valve holding 2100 s is not an error, and must not be shown as one of the six."""
    valve = make_valve(valve_model, [31, 11, 1])
    valve.outlet_run_times = {1: 2100, 2: 2100, 3: 2100}
    _, _, duration = _config_entities(make_coordinator([valve]), valve)

    assert duration.current_option is None
    attributes = duration.extra_state_attributes
    assert attributes["reported_minutes"] == 35.0
    assert attributes["in_app_picker"] is False
    # 2100 is above 1800, so an out-of-date Konnect build would misread it.
    assert attributes["long_duration_app_warning"] is True


def test_the_write_body_is_the_whole_record_in_wire_units():
    """Twelve string keys, write-side spellings, tenths and flow bytes — never the read keys."""
    from custom_components.kohler_anthem.anthem.gcs import GcsDevice
    from custom_components.kohler_anthem.anthem.state import OutletLimits

    sent: dict = {}

    class _Client:
        tenant_id = "tenant-guid"

        async def async_request(self, method, path, json_body=None):
            sent.update(json_body)
            return {"correlationId": "x"}

    device = GcsDevice.__new__(GcsDevice)
    device._client = _Client()
    device.device_id = "gcs-x"
    limits = OutletLimits(0, 16, 200, 1800, 200, 31, 477, 150, 388, 1)

    asyncio.run(
        device.async_write_outlet_config(limits, maximum_temperature_tenths=460)
    )
    model = sent["gcsOutletConfigControlModel"]

    # Konnect 3.0.6 sends twelve (`GcsOutletConfigurationsModel`): the ten read back plus
    # `maxVolume` and `purge`, which no captured read carries, so they go as the app sends
    # them when unread.
    assert len(model) == 12
    assert model["maxVolume"] == "0"
    assert model["purge"] == ""
    assert all(isinstance(v, str) for v in model.values())
    # Write-side spellings: lowercase t/r. The read side capitalises them, and Gson drops
    # unmatched keys silently while still returning 201.
    for key in (
        "maximumRuntime",
        "maximumFlowrate",
        "minimumFlowrate",
        "defaultFlowrate",
    ):
        assert key in model
    # Only the named field changed; everything else echoes what was read.
    assert model["maximumOutletTemperature"] == "460"
    assert model["defaultOutletTemperature"] == "388"
    assert model["minimumOutletTemperature"] == "150"
    assert model["maximumRuntime"] == "1800"
    assert model["outLetFlags"] == "1"


def test_the_write_echoes_max_volume_and_purge_when_the_valve_reported_them():
    """Read-back values win over the app's defaults — a whole-record write puts back what it found."""
    from dataclasses import replace

    from custom_components.kohler_anthem.anthem.gcs import GcsDevice
    from custom_components.kohler_anthem.anthem.state import OutletLimits

    sent: dict = {}

    class _Client:
        tenant_id = "tenant-guid"

        async def async_request(self, method, path, json_body=None):
            sent.update(json_body)

    device = GcsDevice.__new__(GcsDevice)
    device._client = _Client()
    device.device_id = "gcs-x"
    limits = replace(
        OutletLimits(0, 16, 200, 1800, 200, 21, 477, 150, 388, 1),
        max_volume="150",
        purge="1",
    )
    asyncio.run(device.async_write_outlet_config(limits, maximum_run_time=900))
    model = sent["gcsOutletConfigControlModel"]
    assert model["maxVolume"] == "150"
    assert model["purge"] == "1"


def test_mqtt_outlet_config_keeps_max_volume_learned_earlier():
    """A thin announcement must not blank a field a write will echo."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.anthem.mqtt import Envelope
    from custom_components.kohler_anthem.anthem.state import GcsState

    state = GcsState(model=get_valve_model("K-28210"))

    def announce(**extra):
        attribute = {
            "outLetId": "0",
            "minimumFlowRate": "16",
            "maximumFlowRate": "200",
            **extra,
        }
        state.apply_envelope(
            Envelope("GCS", "gcs-x", "READ_GCS_OUTLET_CONFIG_CFG", [attribute], 1.0)
        )

    announce(maxVolume="120")
    announce()
    assert state.outlet_limits[0].max_volume == "120"


def test_a_write_refuses_rather_than_inventing_a_field():
    """This endpoint replaces the record, so an unread field cannot be guessed.

    🚨 One of these sits beside the scald limit; a spurious value would change a safety
    setting the caller never asked to touch.
    """
    from custom_components.kohler_anthem.anthem.client import KohlerError
    from custom_components.kohler_anthem.anthem.gcs import GcsDevice
    from custom_components.kohler_anthem.anthem.state import OutletLimits

    class _Client:
        tenant_id = "tenant-guid"

        async def async_request(self, method, path, json_body=None):
            raise AssertionError("must not reach the network")

    device = GcsDevice.__new__(GcsDevice)
    device._client = _Client()
    device.device_id = "gcs-x"

    for limits, missing in (
        (OutletLimits(0, 16, 200, 1800, 200, 31, 477, None, 388, 1), "minimum"),
        (OutletLimits(0, 16, 200, 1800, 200, 31, 477, 150, None, 1), "default"),
        (OutletLimits(0, 16, 200, 1800, 200, 31, 477, 150, 388, None), "outLetFlags"),
        (OutletLimits(0, 16, 200, None, 200, 31, 477, 150, 388, 1), "maximumRuntime"),
    ):
        # The message must name the field that is missing, or it cannot be acted on.
        with pytest.raises(KohlerError, match=missing):
            asyncio.run(device.async_write_outlet_config(limits))

    # And a default above the scald limit is refused before it reaches the valve.
    full = OutletLimits(0, 16, 200, 1800, 200, 31, 477, 150, 388, 1)
    with pytest.raises(KohlerError, match="above the scald limit"):
        asyncio.run(
            device.async_write_outlet_config(full, maximum_temperature_tenths=380)
        )


def _write_valve(monkeypatch, *, fail_after=None, verify_as=None, verify_delay=None):
    """A Valve wired for `async_write_outlet_setting`, with the network faked."""
    from custom_components.kohler_anthem import coordinator as module
    from custom_components.kohler_anthem.anthem.client import KohlerError
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.anthem.state import OutletLimits

    written: list[int] = []

    class _Gcs:
        async def async_write_outlet_config(self, limits, **kwargs):
            if fail_after is not None and len(written) >= fail_after:
                raise KohlerError("device offline")
            written.append(limits.outlet_id)

    valve = object.__new__(module.Valve)
    valve.name = "Anthem Valve"
    valve.gcs = _Gcs()
    valve.gcs_device = SimpleNamespace(device_id="gcs-x")
    valve.gcs_state = SimpleNamespace(
        # Three outlets on one valve, `outLetId`s 0-2. The model is what turns those ids
        # back into outlet numbers for the partial-write and verification messages.
        model=get_valve_model("K-28210"),
        outlet_limits={
            i: OutletLimits(i, 16, 200, 1800, 200, 31, 477, 150, 388, 1)
            for i in range(3)
        },
    )
    valve._note_local_write = lambda: None
    # Verification runs detached (0.18.2); capture the task so a test can await it.
    valve._background_tasks = set()
    issues: list[tuple[str, str]] = []
    valve._raise_write_issue = lambda setting, detail: issues.append((setting, detail))
    valve._clear_write_issue = lambda: None
    valve.raised_issues = issues

    # The verification read returns whatever the caller asked it to.
    async def _read_back(device_id):
        return _settings(verify_as)

    # `hass` and `client` are read-only properties reading through the coordinator.
    valve.coordinator = SimpleNamespace(
        client=SimpleNamespace(async_get_gcs_settings=_read_back),
        hass=SimpleNamespace(async_create_task=asyncio.ensure_future),
    )
    if verify_delay is None:
        monkeypatch.setattr(module.asyncio, "sleep", _noop_sleep)
    else:

        async def _slow_sleep(_seconds):
            await asyncio.sleep(verify_delay)

        monkeypatch.setattr(module.asyncio, "sleep", _slow_sleep)
    return valve, written


def _settings(run_time):
    """A `gcsadvancestate` response for three outlets at `run_time` seconds."""
    return {
        "setting": {
            "valveSettings": [
                {
                    "outletConfigurations": [
                        {
                            "outLetId": str(i),
                            "outLetType": "31",
                            "outLetFlags": "1",
                            "minimumOutletTemperature": "15",
                            "defaultOutletTemperature": "38.8",
                            "maximumOutletTemperature": "47.7",
                            "minimumFlowrate": "4",
                            "maximumFlowrate": "50",
                            "defaultFlowrate": "50",
                            "maximumRuntime": str(run_time),
                        }
                        for i in range(3)
                    ]
                }
            ]
        }
    }


@pytest.mark.asyncio
async def test_a_write_is_verified_against_a_read_back(monkeypatch):
    """A 201 means accepted for delivery, never applied — so the value is read back.

    The endpoint echoes nothing and the Konnect app performs no verification at all.
    """
    valve, written = _write_valve(monkeypatch, verify_as=2700)
    await valve.async_write_outlet_setting(maximum_run_time=2700)
    # One call per outlet, in order — there is no list form.
    assert written == [0, 1, 2]

    await _drain(valve)
    # It verified, so nothing is raised at the user.
    assert valve.raised_issues == []


@pytest.mark.asyncio
async def test_the_write_call_does_not_wait_for_verification(monkeypatch):
    """0.18.2: awaiting the ~30 s read-back froze the slider for its whole duration.

    Home Assistant warns at 10 s and the entity looked broken. The POSTs are still awaited —
    a refused write should fail immediately — but the waiting is detached.
    """
    valve, _ = _write_valve(monkeypatch, verify_as=2700, verify_delay=5.0)

    # Returns without paying the verification delay, which the fake makes deliberately long.
    await asyncio.wait_for(valve.async_write_outlet_setting(maximum_run_time=2700), 0.5)


@pytest.mark.asyncio
async def test_a_write_the_valve_ignored_becomes_a_repair_issue(monkeypatch):
    """The valve took the call and kept its old value: still a failure, surfaced differently.

    Detached verification has no caller to raise at, so it raises a repair issue instead —
    which is also more useful, because it survives the moment the slider was moved.
    """
    valve, written = _write_valve(monkeypatch, verify_as=1800)
    await valve.async_write_outlet_setting(maximum_run_time=2700)
    assert written == [0, 1, 2]

    await _drain(valve)
    assert len(valve.raised_issues) == 1
    setting, detail = valve.raised_issues[0]
    # The message must name the setting and the outlets, or it cannot be acted on.
    assert "Max Shower Duration" in setting
    assert "45 minutes" in setting
    assert "1, 2, 3" in detail


async def _drain(valve):
    """Await whatever `_track` started, so a detached verification finishes.

    Snapshotted first: `_track` registers a done-callback that discards from the same set.
    """
    for task in tuple(valve._background_tasks):
        await asyncio.shield(task)


@pytest.mark.asyncio
async def test_a_partial_write_says_which_outlets_took_it(monkeypatch):
    """A failure part-way leaves outlets in mixed state — the user needs to know that.

    This is not hypothetical: it is what produced the `outlets_agree: false` this
    integration already reports, seen on the owner's own hardware.
    """
    from homeassistant.exceptions import HomeAssistantError

    valve, written = _write_valve(monkeypatch, fail_after=2)
    with pytest.raises(HomeAssistantError, match="mixed state"):
        await valve.async_write_outlet_setting(maximum_run_time=2700)
    # Two landed, the third did not — and the error names how far it got.
    assert written == [0, 1]


def test_the_purge_removes_the_retired_sensors_and_nothing_else():
    """The retired diagnostics go; the controls that replaced them must not.

    🚨 The purge matches on **suffix**, and the controls' ids contain the same words as the
    sensors they replaced. A bare `_max_temperature` suffix would be one careless id away
    from deleting the very entity the sensor was retired in favour of — so the suffixes name
    the outlet, which no control id does.
    """
    from custom_components.kohler_anthem import _REMOVED_UNIQUE_ID_SUFFIXES

    retired = (
        "gcs-x_outlet_1_max_run_time",
        "gcs-x_outlet_1_max_temperature",
        "gcs-x_total_water",
    )
    kept = (
        # The three configuration entities from 0.18.0.
        "gcs-x_max_run_time_setting",
        "gcs-x_max_temperature_setting",
        "gcs-x_default_temperature",
        # And a sample of everything else.
        "gcs-x_water_today",
        "gcs-x_water_this_week",
        "gcs-x_firmware",
        "gcs-x_zone_1_hex",
    )

    for unique_id in retired:
        assert unique_id.endswith(_REMOVED_UNIQUE_ID_SUFFIXES), unique_id
    for unique_id in kept:
        assert not unique_id.endswith(_REMOVED_UNIQUE_ID_SUFFIXES), unique_id


def test_the_duration_control_kept_the_lost_write_diagnosis(valve_model):
    """`outlets_agree` moved to the control rather than being retired with the sensor.

    It is the signal that a write was lost part-way — established on real hardware, where
    one valve held 3600 s on its Showerhead and 1800 s on the other two — and losing it
    with the sensor would have thrown away the only way to see that state.
    """
    disagreeing = _duration_sensor(valve_model, {1: 1800, 2: 3600, 3: 1800})
    assert disagreeing.extra_state_attributes["outlets_agree"] is False
    assert disagreeing.extra_state_attributes["per_outlet"] == {
        "Rainhead": 30.0,
        "Showerhead": 60.0,
        "Handshower": 30.0,
    }

    agreeing = _duration_sensor(valve_model, {1: 1800, 2: 1800, 3: 1800})
    assert agreeing.extra_state_attributes["outlets_agree"] is True


def test_an_mqtt_announcement_keeps_the_write_fields():
    """🚨 One successful write, then never again until a reload — 0.18.0 through 0.18.2.

    `_apply_outlet_config` builds a whole `OutletLimits` and **replaces** the stored one, so
    a field it does not carry is not merely absent from that message: it erases what the REST
    seed read. Three of the write record's fields were missing there.

    The trigger is a *successful* write. The valve announces its new outlet config
    afterwards, the announcement landed without those three, and the next write refused
    because they had become unknown. Reported by the owner 2026-09-11 with exactly this
    message:

        Refusing to write outlet 0: defaultOutletTemperature, minimumOutletTemperature,
        outLetFlags has not been read from the valve

    MQTT carries all three, with the write body's key spellings and temperatures already in
    tenths — they were simply never read.
    """
    from types import SimpleNamespace

    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )
    from custom_components.kohler_anthem.anthem.state import (
        GcsState,
        OutletLimits,
    )

    state = GcsState(model=model_for_topology(3, 0))
    # As the REST seed leaves it: a complete record, which a write needs.
    state.outlet_limits[0] = OutletLimits(0, 16, 200, 1800, 200, 31, 477, 150, 388, 1)

    # The announcement that follows a write, verbatim from `docs/gcs/api.md`.
    state._apply_outlet_config(
        SimpleNamespace(
            attributes=[
                {
                    "outLetId": "0",
                    "outLetType": "31",
                    "outLetFlags": "1",
                    "minimumOutletTemperature": "150",
                    "defaultOutletTemperature": "388",
                    "maximumOutletTemperature": "477",
                    "minimumFlowRate": "16",
                    "defaultFlowRate": "200",
                    "maximumFlowRate": "200",
                    "maximumRunTime": "2700",
                }
            ]
        )
    )

    limits = state.outlet_limits[0]
    # The announcement's own news still lands.
    assert limits.maximum_run_time == 2700
    # And the fields a write cannot proceed without survive it.
    assert limits.minimum_temperature_tenths == 150
    assert limits.default_temperature_tenths == 388
    assert limits.outlet_flags == 1


def test_a_sparse_announcement_does_not_erase_what_is_known():
    """An older or partial message must not blank a field it simply does not mention.

    Every capture carries all ten, but the parser must not depend on that: `None` means
    "not learned", and a write refuses on it — turning a thin message into the same
    one-write-then-never-again failure.
    """
    from types import SimpleNamespace

    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )
    from custom_components.kohler_anthem.anthem.state import (
        GcsState,
        OutletLimits,
    )

    state = GcsState(model=model_for_topology(3, 0))
    state.outlet_limits[0] = OutletLimits(0, 16, 200, 1800, 200, 31, 477, 150, 388, 1)
    before = state.outlet_limits[0]

    state._apply_outlet_config(
        SimpleNamespace(
            attributes=[
                {
                    "outLetId": "0",
                    "minimumFlowRate": "16",
                    "maximumFlowRate": "200",
                    "maximumRunTime": "2700",
                }
            ]
        )
    )

    limits = state.outlet_limits[0]
    assert limits.maximum_run_time == 2700
    for field in (
        "minimum_temperature_tenths",
        "default_temperature_tenths",
        "outlet_flags",
        "maximum_temperature_tenths",
    ):
        assert getattr(limits, field) == getattr(before, field), field


def test_default_temperature_survives_an_announcement_from_another_setting():
    """It vanished from the dashboard after changing Max Shower Duration. 0.18.0-0.18.2.

    The same erasure as `test_an_mqtt_announcement_keeps_the_write_fields`, seen from the
    entity: `native_value` returned `None` once `defaultOutletTemperature` was blanked, and
    Home Assistant renders a number with no value as unavailable — so the entity
    disappeared. `Max Temperature` stayed, because its own field was one of the seven the
    announcement did carry, which is what made it look like an entity-specific fault.

    Reported by the owner 2026-09-11: "Default temperature entity still disappears after an
    update to another entity such as max shower duration."
    """
    from types import SimpleNamespace

    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )
    from custom_components.kohler_anthem.anthem.state import (
        GcsState,
        OutletLimits,
    )
    from custom_components.kohler_anthem.number import (
        OutletDefaultTemperatureNumber,
        OutletMaxTemperatureNumber,
    )

    model = model_for_topology(3, 0)
    valve = make_valve(model, [31, 11, 1])
    # A real state object: the announcement path is what this test is about.
    state = GcsState(model=model)
    for outlet in range(3):
        state.outlet_limits[outlet] = OutletLimits(
            outlet, 16, 200, 1800, 200, 31, 477, 150, 388, 1
        )
    valve.gcs_state = state
    coordinator = make_coordinator([valve])
    default = OutletDefaultTemperatureNumber(coordinator, valve)
    maximum = OutletMaxTemperatureNumber(coordinator, valve)

    assert default.native_value == 102
    assert maximum.native_value == 118

    # What the valve sends after a Max Shower Duration write: one message per outlet.
    state._apply_outlet_config(
        SimpleNamespace(
            attributes=[
                {
                    "outLetId": str(outlet),
                    "outLetType": "31",
                    "outLetFlags": "1",
                    "minimumOutletTemperature": "150",
                    "defaultOutletTemperature": "388",
                    "maximumOutletTemperature": "477",
                    "minimumFlowRate": "16",
                    "defaultFlowRate": "200",
                    "maximumFlowRate": "200",
                    "maximumRunTime": "2700",
                }
                for outlet in range(3)
            ]
        )
    )

    # Both entities still have a value — neither disappears.
    assert default.native_value == 102, "Default Temperature vanished"
    assert maximum.native_value == 118
    assert default.available is True
    # And the duration the announcement was actually reporting did land.
    assert state.outlet_limits[0].maximum_run_time == 2700


def test_a_sparse_zone_message_does_not_blank_the_readings():
    """The same erasure `_apply_outlet_config` carried until 0.18.3, in the hub's state.

    `_apply_valve` rebuilds a whole `HubZone` and replaces the stored one, so a message
    without `temperature` or `flowrate` blanked what an earlier one reported — and
    `ControllerZoneTemperatureSensor` goes unavailable on a None, which is the same
    disappearing-entity symptom the outlet bug produced.

    Every captured `SHOWER_VALVE_STS` carries all four keys, so this is latent rather than
    live. It is fixed anyway because that stream is documented as coalescing snapshots and
    skipping windows, and "every message we have seen carries it" is precisely the
    assumption that cost 0.18.0 through 0.18.2.
    """
    from types import SimpleNamespace

    from custom_components.kohler_anthem.anthem.models import (
        model_for_topology,
    )
    from custom_components.kohler_anthem.anthem.state import HubState, HubZone

    state = HubState(model=model_for_topology(3, 3))
    state.zones[1] = HubZone(
        status="ON", outlets=[True, False, False], temperature=104, flowrate=100
    )

    def _message(attributes):
        return SimpleNamespace(
            attributes=[attributes], code="", raw={}, sku="HUB", device_id="hub-x"
        )

    # Status only: the readings must survive.
    state._apply_valve(_message({"zone": "1", "status": "OFF"}))
    assert state.zones[1].status == "OFF"
    assert state.zones[1].temperature == 104
    assert state.zones[1].flowrate == 100

    # A full message still updates everything it carries.
    state._apply_valve(
        _message({"zone": "1", "status": "ON", "temperature": "106", "flowrate": "80"})
    )
    assert state.zones[1].temperature == "106"
    assert state.zones[1].flowrate == "80"


def test_k28211_hardware_outlet_ids_skip_unused_valve1_slot():
    """Each valve body occupies 3 `outLetId` slots (0-2 on Valve1, 3-5 on Valve2).

    On a 4-outlet K-28211 (2+2), `gcsadvancestate` and `READ_GCS_OUTLET_CONFIG_CFG`
    report `outLetId`s 0 and 1 for Valve1 and **3 and 4** for Valve2 (leaving slot 2
    unused). Treating `outLetId` as `outlet - 1` mapped `outLetId` 4 to global outlet 5,
    crashing `OutletRunTimeSelect.extra_state_attributes` with
    `ValueError: K-28211 has outlets 1-4; got 5` and looking up `outLetId` 2 and 3 for
    zone 2's fixtures, flow bounds, and run-time limits.
    """
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.anthem.state import (
        GcsState,
        outlet_limits_from_settings,
    )
    from custom_components.kohler_anthem.coordinator import Valve
    from custom_components.kohler_anthem.entity import outlet_name
    from custom_components.kohler_anthem.select import OutletRunTimeSelect

    model = get_valve_model("K-28211")
    assert [
        model.outlet_id(zone, outlet)
        for zone in model.zones
        for outlet in range(1, model.outlets_in_zone(zone) + 1)
    ] == [0, 1, 3, 4]
    assert [model.outlet_from_id(i) for i in range(6)] == [1, 2, None, 3, 4, None]
    # Messages must not report the unused slot as outlet 3, which is a real outlet.
    assert Valve._outlet_label(SimpleNamespace(model=model), 2) == "id 2"
    assert Valve._outlet_label(SimpleNamespace(model=model), 3) == "3"

    settings = {
        "valveSettings": [
            {
                "valveIndex": "Valve1",
                "noOfOutlets": "2",
                "outletConfigurations": [
                    {
                        "outLetId": "0",
                        "outLetType": "11",
                        "minimumFlowrate": "12.5",
                        "maximumFlowrate": "50",
                        "maximumRuntime": "1800",
                    },
                    {
                        "outLetId": "1",
                        "outLetType": "99",  # outside the app's outlet table
                        "minimumFlowrate": "12.5",
                        "maximumFlowrate": "50",
                        "maximumRuntime": "1800",
                    },
                ],
            },
            {
                "valveIndex": "Valve2",
                "noOfOutlets": "2",
                "outletConfigurations": [
                    {
                        "outLetId": "3",
                        "outLetType": "31",
                        "minimumFlowrate": "15.0",
                        "maximumFlowrate": "45",
                        "maximumRuntime": "1800",
                    },
                    {
                        "outLetId": "4",
                        "outLetType": "1",
                        "minimumFlowrate": "15.0",
                        "maximumFlowrate": "45",
                        "maximumRuntime": "1800",
                    },
                ],
            },
        ]
    }
    state = GcsState(model=model)
    state.outlet_limits.update(outlet_limits_from_settings(settings))
    assert state.zone_flow_limits(1) == (50, 200)
    assert state.zone_flow_limits(2) == (60, 180)

    class Holder:
        outlet_run_times = Valve.outlet_run_times
        _zone_limits = Valve._zone_limits

        def __init__(self):
            self.model = model
            self.gcs_state = state

    # Read straight from the outlet records, through the K-28211's skipped id 2.
    holder = Holder()
    assert holder.outlet_run_times == {1: 1800, 2: 1800, 3: 1800, 4: 1800}
    assert holder._zone_limits() == {1: (1800,), 2: (1800,)}

    # 99 is outside Konnect's outlet table (62 is a pair of foot sprays since 2026-10-07),
    # to verify the "Outlet 1.2" fallback
    valve = make_valve(model, [11, 99, 31, 1])
    valve.gcs_state = state
    valve.outlet_run_times = holder.outlet_run_times
    assert [
        outlet_name(valve, zone, outlet)
        for zone in model.zones
        for outlet in range(1, model.outlets_in_zone(zone) + 1)
    ] == ["Showerhead 1", "Outlet 1.2", "Rainhead 2", "Handshower 2"]

    select = OutletRunTimeSelect(make_coordinator([valve]), valve)
    assert select.extra_state_attributes["per_outlet"] == {
        "Showerhead 1": 30.0,
        "Outlet 1.2": 30.0,
        "Rainhead 2": 30.0,
        "Handshower 2": 30.0,
    }


# --------------------------------------------------------------------------- #
# Configurable zone grouping: numbered, sub-devices, and outlet-labelled
# --------------------------------------------------------------------------- #
def test_zone_grouping_subdevices_splits_zones_and_drops_zone_numbers():
    """In `subdevices` mode, each zone on a multi-zone valve is a child device."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.const import ZONE_GROUPING_SUBDEVICES

    model = get_valve_model("K-28211")
    valve = make_valve(model, [11, 52, 31, 1], zone_grouping=ZONE_GROUPING_SUBDEVICES)
    coordinator = make_coordinator([valve], zone_grouping=ZONE_GROUPING_SUBDEVICES)

    switches = {e.unique_id: e for e in collect("switch", coordinator)}
    outlet_switches = [
        switches["gcs-test0001_showerhead_1"],
        switches["gcs-test0001_body_sprays_1"],
        switches["gcs-test0001_rainhead_2"],
        switches["gcs-test0001_handshower_2"],
    ]
    assert [e.name for e in outlet_switches] == [
        "Showerhead",
        "Body Sprays",
        "Rainhead",
        "Handshower",
    ]
    assert outlet_switches[0].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001_zone_1")
    }
    assert outlet_switches[0].device_info["via_device"] == (
        "kohler_anthem",
        "gcs-test0001",
    )
    assert outlet_switches[0].device_info["name"] == "Anthem Valve Zone 1"
    assert outlet_switches[2].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001_zone_2")
    }
    assert outlet_switches[2].device_info["name"] == "Anthem Valve Zone 2"

    # Whole-valve switch stays on the parent valve device.
    assert switches["gcs-test0001_shower"].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001")
    }

    numbers = {e.unique_id: e for e in collect("number", coordinator)}
    assert numbers["gcs-test0001_temperature_zone_1"].name == "Temperature"
    assert numbers["gcs-test0001_temperature_zone_2"].name == "Temperature"
    assert numbers["gcs-test0001_flow_zone_1"].name == "Flow"
    assert numbers["gcs-test0001_flow_zone_2"].name == "Flow"
    assert numbers["gcs-test0001_temperature_zone_1"].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001_zone_1")
    }
    assert numbers["gcs-test0001_temperature_zone_2"].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001_zone_2")
    }

    active = {e.unique_id: e for e in _zone_active(coordinator)}
    assert active["gcs-test0001_zone_1_active"].name == "Zone Active"
    assert active["gcs-test0001_zone_2_active"].name == "Zone Active"
    assert active["gcs-test0001_zone_1_active"].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001_zone_1")
    }
    assert active["gcs-test0001_zone_2_active"].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001_zone_2")
    }

    sensors = {e.unique_id: e for e in collect("sensor", coordinator)}
    assert sensors["gcs-test0001_zone_1_hex"].name == "Hex"
    assert sensors["gcs-test0001_zone_2_hex"].name == "Hex"
    assert sensors["gcs-test0001_zone_1_hex"].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001_zone_1")
    }
    assert sensors["gcs-test0001_zone_2_hex"].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001_zone_2")
    }


def test_zone_grouping_outlet_labels_names_controls_after_zone_outlets():
    """In `outlet_labels` mode, one device is kept and controls list their outlets."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.const import ZONE_GROUPING_OUTLET_LABELS

    model = get_valve_model("K-28211")
    valve = make_valve(
        model, [11, 52, 31, 1], zone_grouping=ZONE_GROUPING_OUTLET_LABELS
    )
    coordinator = make_coordinator([valve], zone_grouping=ZONE_GROUPING_OUTLET_LABELS)

    switches = {e.unique_id: e for e in collect("switch", coordinator)}
    assert [
        switches["gcs-test0001_showerhead_1"].name,
        switches["gcs-test0001_body_sprays_1"].name,
        switches["gcs-test0001_rainhead_2"].name,
        switches["gcs-test0001_handshower_2"].name,
    ] == ["Showerhead", "Body Sprays", "Rainhead", "Handshower"]
    assert switches["gcs-test0001_showerhead_1"].device_info["identifiers"] == {
        ("kohler_anthem", "gcs-test0001")
    }

    assert _zone_number_names(coordinator) == {
        "Temperature (Showerhead, Body Sprays)",
        "Flow (Showerhead, Body Sprays)",
        "Temperature (Rainhead, Handshower)",
        "Flow (Rainhead, Handshower)",
    }

    active = sorted(_zone_active(coordinator), key=lambda e: e.unique_id)
    assert [e.name for e in active] == [
        "Zone Active (Showerhead, Body Sprays)",
        "Zone Active (Rainhead, Handshower)",
    ]


def test_unique_ids_stay_identical_across_all_zone_grouping_modes():
    """Switching grouping modes updates entities in place without orphaning them."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.const import ZONE_GROUPING_MODES

    model = get_valve_model("K-28211")
    for types in ([11, 52, 31, 1], [11, 52, 11, 1], [11, 11, 11, 11]):
        by_mode: dict[str, dict[str, list[str]]] = {}
        for mode in ZONE_GROUPING_MODES:
            valve = make_valve(model, types, zone_grouping=mode)
            coordinator = make_coordinator([valve], zone_grouping=mode)
            by_mode[mode] = {
                name: sorted(e.unique_id for e in collect(name, coordinator))
                for name in PLATFORMS
            }
        baseline = by_mode["numbered"]
        for mode, uids in by_mode.items():
            assert uids == baseline, f"unique_ids diverged in {mode} for {types}"


def test_outlet_labels_disambiguates_shared_fixtures_and_falls_back_when_unseeded():
    """Shared fixtures keep zone numbers on one device; unseeded zones use numbers."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.const import (
        ZONE_GROUPING_OUTLET_LABELS,
        ZONE_GROUPING_SUBDEVICES,
    )
    from custom_components.kohler_anthem.entity import outlet_name, zone_label
    from custom_components.kohler_anthem.select import OutletRunTimeSelect

    model = get_valve_model("K-28211")
    shared = make_valve(
        model, [11, 52, 11, 1], zone_grouping=ZONE_GROUPING_OUTLET_LABELS
    )
    assert [
        outlet_name(shared, z, o)
        for z in model.zones
        for o in range(1, model.outlets_in_zone(z) + 1)
    ] == ["Showerhead 1", "Body Sprays", "Showerhead 2", "Handshower"]

    # On the whole-valve Max Shower Duration select, `subdevices` mode still uses
    # disambiguated keys in `per_outlet` so two zones with a Showerhead never collide.
    sub_valve = make_valve(
        model, [11, 52, 11, 1], zone_grouping=ZONE_GROUPING_SUBDEVICES
    )
    select = OutletRunTimeSelect(
        make_coordinator([sub_valve], zone_grouping=ZONE_GROUPING_SUBDEVICES),
        sub_valve,
    )
    assert select.extra_state_attributes["per_outlet"] == {
        "Showerhead 1": 30.0,
        "Body Sprays": 30.0,
        "Showerhead 2": 30.0,
        "Handshower": 30.0,
    }

    unseeded = make_valve(
        model, [None, None, None, None], zone_grouping=ZONE_GROUPING_OUTLET_LABELS
    )
    assert zone_label(unseeded, 1, "Temperature") == "Temperature 1"
    assert zone_label(unseeded, 2, "Temperature") == "Temperature 2"


def test_single_zone_valve_never_creates_zone_subdevices(valve_model):
    """A single-zone valve stays on one device regardless of `zone_grouping`."""
    from custom_components.kohler_anthem.const import ZONE_GROUPING_SUBDEVICES

    valve = make_valve(valve_model, [31, 11, 1], zone_grouping=ZONE_GROUPING_SUBDEVICES)
    coordinator = make_coordinator([valve], zone_grouping=ZONE_GROUPING_SUBDEVICES)
    for name in PLATFORMS:
        for entity in collect(name, coordinator):
            assert entity.device_info["identifiers"] == {
                ("kohler_anthem", "gcs-test0001")
            }


def test_options_flow_merges_existing_options_and_reloads_on_grouping_change():
    """Saving Configure preserves per-valve options and reloads on grouping change."""
    from custom_components.kohler_anthem.config_flow import (
        KohlerAnthemConfigFlow,
        KohlerAnthemOptionsFlow,
    )
    from custom_components.kohler_anthem.const import (
        CONF_REPORT_LOG_FILE,
        CONF_VALVES,
        CONF_ZONE_GROUPING,
        ZONE_GROUPING_NUMBERED,
        ZONE_GROUPING_SUBDEVICES,
    )
    from custom_components.kohler_anthem.coordinator import entry_reload_signature

    existing = {
        CONF_VALVES: {"gcs-test0001": {"warmup_auto_restore": True}},
        CONF_REPORT_LOG_FILE: "report_2026.jsonl",
    }
    entry = SimpleNamespace(
        entry_id="test",
        data={"username": "user@example.com"},
        options=dict(existing),
    )

    flow = KohlerAnthemConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, KohlerAnthemOptionsFlow)

    form = asyncio.run(flow.async_step_init(None))
    assert form["type"] == "form"
    assert form["step_id"] == "init"

    saved = asyncio.run(
        flow.async_step_init({CONF_ZONE_GROUPING: ZONE_GROUPING_SUBDEVICES})
    )
    assert saved["type"] == "create_entry"
    assert saved["data"] == {
        **existing,
        CONF_ZONE_GROUPING: ZONE_GROUPING_SUBDEVICES,
    }

    before = entry_reload_signature(entry)
    after_entry = SimpleNamespace(
        entry_id="test",
        data=entry.data,
        options=saved["data"],
    )
    assert entry_reload_signature(after_entry) != before

    numbered_entry = SimpleNamespace(
        entry_id="test",
        data=entry.data,
        options={**existing, CONF_ZONE_GROUPING: ZONE_GROUPING_NUMBERED},
    )
    assert entry_reload_signature(after_entry) != entry_reload_signature(numbered_entry)


def test_zone_subdevice_cleanup_and_service_resolution(monkeypatch):
    """Stale zone sub-devices are removed on mode switch; services accept sub-devices."""
    from homeassistant.helpers import device_registry as dr

    from custom_components.kohler_anthem import _async_cleanup_zone_subdevices
    from custom_components.kohler_anthem.anthem.models import get_valve_model
    from custom_components.kohler_anthem.const import (
        ZONE_GROUPING_NUMBERED,
        ZONE_GROUPING_SUBDEVICES,
    )
    from custom_components.kohler_anthem.services import _resolve_valve

    model = get_valve_model("K-28211")
    valve = make_valve(model, [11, 52, 31, 1], zone_grouping=ZONE_GROUPING_SUBDEVICES)
    coordinator = make_coordinator([valve], zone_grouping=ZONE_GROUPING_SUBDEVICES)

    parent_dev = SimpleNamespace(
        id="dev-parent",
        name="Anthem Valve",
        name_by_user=None,
        identifiers={("kohler_anthem", "gcs-test0001")},
    )
    zone1_dev = SimpleNamespace(
        id="dev-zone-1",
        name="Anthem Valve Zone 1",
        name_by_user=None,
        identifiers={("kohler_anthem", "gcs-test0001_zone_1")},
    )
    removed: list[str] = []

    class FakeDeviceRegistry:
        def __init__(self) -> None:
            self.devices = {"dev-parent": parent_dev, "dev-zone-1": zone1_dev}

        def async_get(self, dev_id: str):
            return self.devices.get(dev_id)

        def async_remove_device(self, dev_id: str) -> None:
            removed.append(dev_id)
            self.devices.pop(dev_id, None)

    reg = FakeDeviceRegistry()
    hass = SimpleNamespace(data={"kohler_anthem": {"test": coordinator}})
    entry = SimpleNamespace(entry_id="test", data={}, options={})

    monkeypatch.setattr(dr, "async_get", lambda h: reg)
    monkeypatch.setattr(
        dr,
        "async_entries_for_config_entry",
        lambda r, eid: list(r.devices.values()),
    )

    assert _resolve_valve(hass, "dev-zone-1") is valve

    # While `subdevices` is active, zone sub-devices are kept.
    _async_cleanup_zone_subdevices(hass, entry, coordinator)
    assert removed == []

    # Switching back to `numbered` removes the zone sub-device.
    coordinator.zone_grouping = ZONE_GROUPING_NUMBERED
    _async_cleanup_zone_subdevices(hass, entry, coordinator)
    assert removed == ["dev-zone-1"]
    assert "dev-parent" in reg.devices
