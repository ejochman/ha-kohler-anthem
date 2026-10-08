"""What the Konnect 3.0.6 decompile changed (2026-10-07).

Each test pins one fact recovered from the app, or one fix made because of it, so a later
edit cannot quietly undo it. `docs/protocol/` is the reference these facts are written up in.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from .conftest import make_controller, make_coordinator, make_valve
from .test_entities import collect


def _model(sku: str = "K-28210"):
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    return get_valve_model(sku)


# --------------------------------------------------------------------------- #
# Outlet types
# --------------------------------------------------------------------------- #
def test_the_three_mystery_codes_are_named_from_the_apps_table():
    """38, 39 and 62 were seen in captures and never matched; the app's picker names them."""
    from custom_components.kohler_anthem.const import (
        OUTLET_TYPE_NAMES,
        OUTLET_TYPE_VARIANTS,
    )

    assert OUTLET_TYPE_NAMES[38] == "Rainhead"
    assert OUTLET_TYPE_VARIANTS[38] == "Silk"
    assert OUTLET_TYPE_NAMES[39] == "Rainhead"
    assert OUTLET_TYPE_VARIANTS[39] == "Real Rain"
    assert OUTLET_TYPE_NAMES[62] == "Foot Sprays"
    # The owner-confirmed codes are untouched.
    assert {k: OUTLET_TYPE_NAMES[k] for k in (1, 11, 21, 31, 52)} == {
        1: "Handshower",
        11: "Showerhead",
        21: "Tub Filler",
        31: "Rainhead",
        52: "Body Sprays",
    }
    # Nineteen codes: the app's whole picker.
    assert len(OUTLET_TYPE_NAMES) == 19


def test_an_outlet_switch_publishes_its_variant():
    valve = make_valve(_model(), [39, 11, 1])
    switch = next(
        e for e in collect("switch", make_coordinator([valve])) if e.name == "Rainhead"
    )
    attributes = switch.extra_state_attributes
    assert attributes["outlet_type"] == 39
    assert attributes["outlet_type_name"] == "Rainhead"
    assert attributes["outlet_variant"] == "Real Rain"


def test_a_position_named_outlet_moves_to_its_fixture_id_keeping_its_entity_id(
    monkeypatch,
):
    """`Outlet 1.2` registered before 62 had a name must become `Foot Sprays 1`, same entity."""
    from custom_components.kohler_anthem import switch as module

    model = _model("K-28211")
    valve = make_valve(model, [11, 62, 31, 1], device_id="gcs-x")

    class Registry:
        def __init__(self):
            self.rows = [
                SimpleNamespace(
                    unique_id="gcs-x_outlet_1.2",
                    entity_id="switch.anthem_valve_outlet_1_2",
                    domain="switch",
                ),
                SimpleNamespace(
                    unique_id="gcs-x_showerhead_1",
                    entity_id="switch.anthem_valve_showerhead_1",
                    domain="switch",
                ),
            ]
            self.updates: list = []

        def async_update_entity(self, entity_id, new_unique_id):
            self.updates.append((entity_id, new_unique_id))

    registry = Registry()
    monkeypatch.setattr(module.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        module.er,
        "async_entries_for_config_entry",
        lambda reg, entry_id: reg.rows,
    )
    module._async_migrate_outlet_unique_ids(
        SimpleNamespace(), SimpleNamespace(entry_id="e"), valve
    )
    # Zone 1 of a two-zone valve, so the fixture name carries its zone: `Foot Sprays 1`.
    assert registry.updates == [
        ("switch.anthem_valve_outlet_1_2", "gcs-x_foot_sprays_1")
    ]


# --------------------------------------------------------------------------- #
# Status codes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("code", ["900", 900, " 900 "])
def test_offline_is_recognised_whether_the_code_is_a_string_or_a_number(code):
    """Konnect models `statusCode` as a string; an int-only check missed `"900"`."""
    from custom_components.kohler_anthem.anthem.client import (
        DeviceOffline,
        KohlerClient,
    )

    with pytest.raises(DeviceOffline):
        KohlerClient._raise_for_payload(400, "/x", {"statusCode": code})


def test_901_is_running_too_and_the_rest_of_the_table_explains_itself():
    from custom_components.kohler_anthem.anthem.client import (
        DeviceRunning,
        KohlerClient,
        KohlerError,
    )

    with pytest.raises(DeviceRunning):
        KohlerClient._raise_for_payload(400, "/x", {"statusCode": "901"})
    with pytest.raises(KohlerError, match="maximum number of favorites"):
        KohlerClient._raise_for_payload(400, "/x", {"statusCode": "915"})


def test_firmware_paths_do_not_leak_device_ids():
    from custom_components.kohler_anthem.anthem.client import KohlerClient

    path = "/platform/api/v1/firmware/gcs/gateway/gcs-secret?releasetarget=Public"
    assert "gcs-secret" not in KohlerClient.safe_path(path)


# --------------------------------------------------------------------------- #
# Valve state
# --------------------------------------------------------------------------- #
def _gcs_state():
    from custom_components.kohler_anthem.anthem.state import GcsState

    return GcsState(model=_model())


def test_system_state_error_is_a_fault_as_the_app_reads_it():
    state = _gcs_state()
    state.system_state = "ERROR"
    assert state.has_fault is True
    state.system_state = "FirmwareUpdate"
    assert state.firmware_updating is True


def test_the_system_state_sensor_knows_the_apps_two_extra_states():
    valve = make_valve(_model(), [31, 11, 1])
    sensor = next(
        e
        for e in collect("sensor", make_coordinator([valve]))
        if e.name == "System State"
    )
    valve.gcs_state.system_state = "Error"
    assert sensor.native_value == "error"
    valve.gcs_state.system_state = "FirmwareUpdate"
    assert sensor.native_value == "FirmwareUpdate"


def test_the_bath_fill_volume_counter_is_recorded():
    from custom_components.kohler_anthem.anthem.mqtt import Envelope

    state = _gcs_state()
    state.apply_envelope(
        Envelope(
            "GCS",
            "gcs-x",
            "READ_DISPENSED_WATER_VOLUME_STS",
            [{"code": "READ_DISPENSED_WATER_VOLUME_STS", "volume": "1234.5"}],
            1.0,
        )
    )
    assert state.dispensed_volume == "1234.5"


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #
def test_unused_preset_valves_are_sent_as_the_apps_zero_word():
    from custom_components.kohler_anthem.anthem.gcs import GcsDevice

    fields = GcsDevice._valve_fields({1: "1190C8"})
    assert fields["valve1"] == "1190c8"
    assert all(fields[f"valve{n}"] == "000000" for n in range(2, 9))


def test_a_120f_favorite_from_the_app_survives_a_timer_sync():
    """The app stores 120 °F as 48.9 °C (489); the echo ceiling must admit it, not 49.0."""
    from custom_components.kohler_anthem.anthem.valve_hex import (
        ValveHexError,
        check_preset_word,
    )

    # 489 = 0x1E9 -> byte0 0x01 (temp high bit), byte1 0xE9.
    assert check_preset_word("01E9C8") == "01e9c8"
    with pytest.raises(ValveHexError):
        check_preset_word("01EAC8")  # 490


# --------------------------------------------------------------------------- #
# Connection state
# --------------------------------------------------------------------------- #
def _watch():
    from custom_components.kohler_anthem.cloud_watch import CloudConnectionWatch

    watch = CloudConnectionWatch.__new__(CloudConnectionWatch)
    watch._connected = None
    watch._reported = None
    watch._last_connected_epoch = None
    watch._checked_at = None
    watch._trigger = None
    watch._checks = 0
    watch._last_error = None
    watch._unfamiliar = None
    return watch


def test_disconnected_is_the_negative_and_anything_else_reads_reachable():
    watch = _watch()
    watch.note_rest_payload({"connectionState": "Disconnected"}, "t", notify=False)
    assert watch._connected is False
    watch.note_rest_payload({"connectionState": "Connected"}, "t", notify=False)
    assert watch._connected is True
    watch.note_rest_payload({"connectionState": "Sleeping"}, "t", notify=False)
    assert watch._connected is True


# --------------------------------------------------------------------------- #
# Anthem Plus
# --------------------------------------------------------------------------- #
def test_max_shower_duration_and_steam_defaults_come_from_hub_configuration():
    from custom_components.kohler_anthem.anthem.hub import HubSettings

    settings = HubSettings.from_configuration(
        {
            "systemSettings": {
                "maxShowerDuration": "60",
                "showerMaxTemperature": "116",
                "flowRateEnable": "0",
            },
            "steamSettings": {"defaultTemperature": 110, "defaultTime": 15},
            "about": {"hub": {"wlan": {"ip": "192.168.1.50"}}},
        }
    )
    assert settings.max_shower_duration_minutes == 60
    assert settings.shower_max_temperature == 116
    assert settings.flow_rate_enabled is False
    assert settings.steam_default_time == 15
    assert settings.steam_ready
    assert settings.lan_ip == "192.168.1.50"


def test_a_single_body_valve_is_not_reported_as_a_disconnected_second_valve():
    """K-28212: `parts.valve2` NotConnected is normal — the app checks the serial first."""
    from custom_components.kohler_anthem.anthem.hub import HubSettings

    zeros = "0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0"
    settings = HubSettings.from_configuration(
        {
            "parts": {"valve1": "Connected", "valve2": "NotConnected"},
            "about": {
                "valve1": {"serialNumber": "1:2:3"},
                "valve2": {"serialNumber": zeros},
            },
            "steamSettings": {"defaultTime": 0},
        }
    )
    assert settings.disconnected == ()


def test_a_fitted_accessory_that_dropped_off_is_reported():
    from custom_components.kohler_anthem.anthem.hub import HubSettings

    settings = HubSettings.from_configuration(
        {
            "parts": {"valve1": "Connected", "steam": "NotConnected"},
            "about": {"valve1": {"serialNumber": "1:2:3"}},
            "steamSettings": {"defaultTime": 20},
            "amplifierSettings": {"stereoVolume": 40, "sdCard": "notpresent"},
            "lightSettings": [{"name": "groupA", "connectivity": "No"}],
        }
    )
    assert set(settings.disconnected) == {"steam", "amplifier", "light", "sd_card"}


def _hub_state():
    from custom_components.kohler_anthem.anthem.state import HubState

    return HubState(model=_model())


def _hub_envelope(code: str, attributes: list, **data):
    from custom_components.kohler_anthem.anthem.mqtt import Envelope

    return Envelope(
        "HUB", "hub-x", code, attributes, 1.0, raw={"data": {"code": code, **data}}
    )


def test_one_light_group_turning_off_does_not_hide_another_still_on():
    state = _hub_state()
    state.apply_envelope(
        _hub_envelope("LIGHT_STS", [{"component": "lightgroupA", "status": "ON"}])
    )
    state.apply_envelope(
        _hub_envelope("LIGHT_STS", [{"component": "lightgroupB", "status": "OFF"}])
    )
    assert state.light_on is True
    assert state.lights == {"a": True, "b": False}
    # REST names the same group differently; it must land on the same key.
    state.apply_rest_state({"state": {"light": [{"name": "groupA", "status": "OFF"}]}})
    assert state.light_on is False


def test_power_clean_is_kept_and_reads_as_not_steaming():
    state = _hub_state()
    state.apply_envelope(
        _hub_envelope(
            "STEAM_STS",
            [{"status": "POWERCLEAN", "temperature": "110", "totaltime": "20"}],
        )
    )
    assert state.steam_powerclean is True
    assert state.steam_on is False
    assert state.steam_temperature == "110"


def test_experience_run_state_follows_the_exp_sts_messages():
    state = _hub_state()
    state.apply_envelope(
        _hub_envelope("SHOWER_EXP_STS", [{"name": "Breathe", "status": "ON"}])
    )
    assert state.active_experience == "Breathe"
    # A late stop for a different program must not clear the one now running.
    state.apply_envelope(
        _hub_envelope("STEAM_EXP_STS", [{"name": "Detox", "status": "OFF"}])
    )
    assert state.active_experience == "Breathe"
    state.apply_envelope(
        _hub_envelope("SHOWER_EXP_STS", [{"name": "Breathe", "status": "OFF"}])
    )
    assert state.active_experience is None


def test_accessory_errorstate_and_rest_error_flags_make_a_fault():
    state = _hub_state()
    assert state.has_fault is None
    state.apply_envelope(
        _hub_envelope(
            "SHOWER_VALVE_STS", [{"zone": "2", "status": "OFF", "errorstate": "1"}]
        )
    )
    assert state.error_components == {"valve2": True}
    assert state.has_fault is True
    fresh = _hub_state()
    fresh.apply_rest_state(
        {"state": {}, "errorState": False, "errorComponent": {"steam": True}}
    )
    assert fresh.has_fault is True


def test_favorite_temperatures_go_out_in_fahrenheit_for_a_celsius_account():
    from custom_components.kohler_anthem.anthem.hub import HubDevice

    hub = HubDevice.__new__(HubDevice)
    hub.temperature_unit = "Celsius"
    assert hub.to_wire_temperature(38) == 100
    hub.temperature_unit = "Fahrenheit"
    assert hub.to_wire_temperature(100) == 100


def test_favorite_bodies_omit_unused_components_and_refuse_shower_with_steam():
    from custom_components.kohler_anthem.anthem.hub import HubDevice

    hub = HubDevice.__new__(HubDevice)
    hub._client = SimpleNamespace(tenant_id="t")
    hub.device_id = "hub-x"
    body = hub._favorite_body(
        "Rinse",
        zone1=HubDevice.zone(100, [True, False, False]),
        zone2=None,
        steam=None,
        music=None,
        light=None,
    )
    assert body["water"] == {
        "zone1": {"temperature": 100, "flowrate": 100, "outlets": [0]}
    }
    assert "steam" not in body and "light" not in body and "music" not in body
    with pytest.raises(ValueError):
        hub._favorite_body(
            "Both",
            zone1=HubDevice.zone(100, [True]),
            zone2=None,
            steam={"temperature": 110, "time": 10},
            music=None,
            light=None,
        )


def test_steam_will_not_start_while_the_controller_runs_the_shower():
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.kohler_anthem.coordinator import KohlerAnthemCoordinator

    sent: list = []

    async def set_steam(on):
        sent.append(on)

    controller = SimpleNamespace(
        name="Anthem Plus",
        water_is_running=True,
        hub=SimpleNamespace(async_set_steam=set_steam),
    )
    coordinator = KohlerAnthemCoordinator.__new__(KohlerAnthemCoordinator)
    coordinator._note_local_write = lambda: None
    with pytest.raises(HomeAssistantError, match="shower"):
        asyncio.run(coordinator.async_set_hub_steam(controller, True))
    assert sent == []
    # Turning steam OFF is always allowed.
    asyncio.run(coordinator.async_set_hub_steam(controller, False))
    assert sent == [False]


# --------------------------------------------------------------------------- #
# New entities
# --------------------------------------------------------------------------- #
def test_the_update_entity_offers_a_release_only_when_the_cloud_does():
    valve = make_valve(_model(), [31, 11, 1])
    update = next(
        e
        for e in collect("update", make_coordinator([valve]))
        if e.unique_id.endswith("_firmware_update")
        and not e.unique_id.endswith("gateway_firmware_update")
    )
    valve.firmware_info = {
        "gcs": {
            "currentFirmware": "2.2",
            "firmware": "2.3",
            "firmwareUpdateAvailable": False,
        }
    }
    assert update.installed_version == update.latest_version == "2.2"
    valve.firmware_info["gcs"]["firmwareUpdateAvailable"] = True
    assert update.latest_version == "2.3"


def test_the_restart_button_exists_but_starts_disabled():
    valve = make_valve(_model(), [31, 11, 1])
    button = next(
        e
        for e in collect("button", make_coordinator([valve]))
        if e.unique_id.endswith("_restart")
    )
    assert button.entity_registry_enabled_default is False


def test_the_controller_gets_steam_problem_duration_and_experience_entities():
    model = _model()
    coordinator = make_coordinator(
        [make_valve(model, [31, 11, 1])], [make_controller(model)]
    )
    ids = {
        e.unique_id
        for name in ("switch", "binary_sensor", "sensor", "select")
        for e in collect(name, coordinator)
    }
    for suffix in (
        "_steam_control",
        "_problem",
        "_max_shower_duration",
        "_experience",
    ):
        assert f"hub-test0001{suffix}" in ids, suffix


def test_the_controller_problem_sensor_reports_a_dropped_accessory():
    from custom_components.kohler_anthem.anthem.hub import HubSettings

    model = _model()
    controller = make_controller(model)
    coordinator = make_coordinator([make_valve(model, [31, 11, 1])], [controller])
    problem = next(
        e
        for e in collect("binary_sensor", coordinator)
        if e.unique_id == "hub-test0001_problem"
    )
    controller.settings = HubSettings(disconnected=("steam",))
    assert problem.is_on is True
    assert problem.extra_state_attributes["disconnected"] == ["steam"]


def test_the_controller_experience_select_lists_the_catalogue():
    model = _model()
    controller = make_controller(model)
    controller.experiences = {
        "showerExperiences": [{"title": "Breathe"}, {"title": "Focus"}],
        "steamExperiences": [{"title": "Detox"}],
    }
    coordinator = make_coordinator([make_valve(model, [31, 11, 1])], [controller])
    select = next(
        e
        for e in collect("select", coordinator)
        if e.unique_id == "hub-test0001_experience"
    )
    assert select.options == ["Off", "Breathe", "Focus", "Detox"]
    controller.state.active_experience = "Focus"
    assert select.current_option == "Focus"


def test_the_daily_water_sensor_counts_how_often_the_shower_ran():
    from datetime import date

    from homeassistant.util import dt as dt_util

    valve = make_valve(_model(), [31, 11, 1])
    today = dt_util.now().date()
    valve.usage_daily = {
        "gcsUsageDataDetailsList": [
            {
                "intervalKey": today.isoformat(),
                "volume": 40,
                "numberOfTimesValveSwitchedOn": 3,
            }
        ]
    }
    assert isinstance(today, date)
    sensor = next(
        e
        for e in collect("sensor", make_coordinator([valve]))
        if e.name == "Water Used Today"
    )
    assert sensor.extra_state_attributes["times_turned_on"] == 3


# --------------------------------------------------------------------------- #
# The controller's web settings page
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("hub", "ip", "url"),
    [
        ({"wlan": {"ip": "192.168.1.40"}}, "192.168.1.40", "http://192.168.1.40/"),
        # A wired controller: no Wi-Fi address, so Ethernet's.
        (
            {"wlan": {"ip": ""}, "eth": {"ip": "10.0.0.7"}},
            "10.0.0.7",
            "http://10.0.0.7/",
        ),
        ({"wlan": {"ip": "fd00::5"}}, "fd00::5", "http://[fd00::5]/"),
        ({"wlan": {"ip": "0.0.0.0"}}, None, None),
        ({"wlan": {"ip": "null"}}, None, None),
        ({}, None, None),
    ],
)
def test_the_controller_address_becomes_its_web_page(hub, ip, url):
    from custom_components.kohler_anthem.anthem.hub import HubSettings

    settings = HubSettings.from_configuration({"about": {"hub": hub}})
    assert settings.lan_ip == ip
    assert settings.web_url == url


def test_the_controller_device_links_to_its_web_page():
    from custom_components.kohler_anthem.anthem.hub import HubSettings

    model = _model()
    controller = make_controller(model)
    controller.settings = HubSettings(lan_ip="192.168.1.40")
    coordinator = make_coordinator([make_valve(model, [31, 11, 1])], [controller])
    entities = [
        e
        for e in collect("binary_sensor", coordinator)
        if e.unique_id.startswith("hub")
    ]
    assert entities
    for entity in entities:
        assert entity.device_info["configuration_url"] == "http://192.168.1.40/"
