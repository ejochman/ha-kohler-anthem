"""Shared fixtures.

The integration lives at `custom_components/kohler_anthem/`, the standard Home
Assistant layout, so it imports as an ordinary package and these tests need no staging
tricks. (It used to sit at the repository root under HACS's `content_in_root`, where
`select.py` shadowed the standard library's `select` module and broke `asyncio` — and so
pytest itself — for anything with the root on `sys.path`. Restructuring removed that whole
class of problem.)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DOMAIN = "kohler_anthem"


class FakeWord:
    """One decoded valve word, with the fields entities read."""

    def __init__(
        self, flow: float = 100.0, mask: int = 0, paused: bool = False
    ) -> None:
        self.temperature_celsius = 40.5
        self.flow_percent = flow
        self.outlet_mask = mask
        self.paused = paused
        self.raw = ""
        self.prefix = 1
        self.flow_setpoint = int(flow * 2)
        self.at_temperature = False
        self.at_flow = False
        self.error_flag = False
        self.error_code = 1
        self.measured_temperature_celsius = None
        self.measured_flow_percent = None


class FakeLimits:
    def __init__(self, outlet_type: int | None, run_time: int = 1800) -> None:
        self.outlet_id = 0
        self.minimum_flow_byte = 16
        self.maximum_flow_byte = 200
        self.maximum_run_time = run_time
        self.default_flow_byte = 200
        self.outlet_type = outlet_type
        # The three temperature/flag fields `writeoutletconfig` also replaces. Values from
        # the owner's own hardware: 59 °F floor, 101.8 °F default, 117.9 °F scald limit.
        self.minimum_temperature_tenths = 150
        self.default_temperature_tenths = 388
        self.maximum_temperature_tenths = 477
        self.outlet_flags = 1


class FakeState:
    """Stands in for `GcsState`.

    Hand-built rather than a mock: an auto-speccing mock answers every attribute, which is
    exactly the failure these tests exist to catch. A fake with only the real attributes
    raises when an entity reaches for something that does not exist.
    """

    def __init__(self, model, types: list[int | None]) -> None:
        self.model = model
        self.valve1 = FakeWord()
        self.valve2 = FakeWord() if model.uses_valve2 else None
        self.warmup_mode = "warmUpDisabled"
        self.warmup_in_progress = False
        self.total_volume = "537557808"
        self.total_flow = 2056.0
        self.total_flow_filtered = 2056.0
        self.total_flow_glitches = 0
        self.system_state = "normalOperation"
        self.last_update = 1789005586.0
        self.presets: dict = {}
        self.active_preset_id = None
        outlet_ids = [
            model.outlet_id(zone, outlet)
            for zone in model.zones
            for outlet in range(1, model.outlets_in_zone(zone) + 1)
        ]
        self.outlet_limits = {
            outlet_id: FakeLimits(t)
            for outlet_id, t in zip(outlet_ids, types, strict=False)
        }
        self.temperature_unit = "Fahrenheit"

    @property
    def is_running(self) -> bool:
        return any(
            w.outlet_mask and not w.paused for w in (self.valve1, self.valve2) if w
        )

    @property
    def is_paused(self) -> bool:
        words = [w for w in (self.valve1, self.valve2) if w]
        return any(w.paused for w in words) and not self.is_running

    @property
    def at_temperature(self):
        return False

    @property
    def has_fault(self):
        return False

    @property
    def error_codes(self) -> dict:
        return {}

    @property
    def flow_percent(self):
        return self.valve1.flow_percent

    @property
    def flow_is_live(self) -> bool:
        return bool(self.valve1.outlet_mask) and not self.valve1.paused

    @property
    def total_flow_gallons(self):
        return self.total_flow_filtered

    @property
    def warmup_enabled(self):
        return self.warmup_mode != "warmUpDisabled"

    def zone_word(self, zone: int):
        return self.valve1 if zone == 1 else self.valve2

    def zone_outlets(self, zone: int, *, flowing: bool = True) -> list[bool]:
        word = self.zone_word(zone)
        count = self.model.outlets_in_zone(zone)
        if word is None:
            return [False] * count
        if flowing and word.paused:
            return [False] * count
        return [bool(word.outlet_mask & (1 << i)) for i in range(count)]

    def zone_flow_limits(self, zone: int) -> tuple[int, int]:
        return 16, 200

    def selectable_presets(self, hidden=()) -> list:
        return []

    def experiences(self) -> list:
        return []

    def experience_by_name(self, name):
        return None

    @property
    def firmware_updating(self) -> bool:
        return self.system_state == "FirmwareUpdate"


def make_valve(model, types, *, device_id="gcs-test0001", run_time=1800):
    """A `Valve` stand-in carrying only what entities touch."""
    state = FakeState(model, types)
    return SimpleNamespace(
        device_id=device_id,
        name="Anthem Valve",
        model=model,
        gcs_state=state,
        gcs_device=SimpleNamespace(serial_number="SN-TEST"),
        cloud_watch=SimpleNamespace(connected=True),
        configuration={
            "about": {"firmware": "00.74"},
            "createdTime": "2024-03-11T14:22:31Z",
        },
        created_time="2024-03-11T14:22:31Z",
        firmware="00.74",
        zone_flow={zone: 100.0 for zone in model.zones},
        restart_on_runtime_cutoff=False,
        # **1-based**, matching `Valve.outlet_run_times` — which maps its 0-based internal
        # store up by one. This fixture used `range()` and so handed entities 0-based keys
        # no real valve ever produces, which hid an off-by-one in the Max Shower Duration
        # attributes for as long as it existed.
        outlet_run_times={i + 1: run_time for i in range(model.total_outlets)},
        armed_zones=list(model.zones),
        zones_awaiting_run_time=[],
        warmup_auto_restore=False,
        last_warmup_mode=None,
        zone_flowing_for=lambda zone: None,
        about_parts={},
        firmware_info={},
    )


def make_controller(model, *, device_id="hub-test0001", name="Anthem Plus", zones=(1,)):
    """A `Controller` stand-in carrying what controller entities touch.

    `HubState` and `HubCapabilities` are the **real** classes rather than fakes — they are
    plain dataclasses with no I/O, so constructing them exercises the actual decode paths an
    entity reads through, which is the point of the exercise. Everything a `Controller`
    reaches out to (the cloud command surface) is stubbed.
    """
    from custom_components.kohler_anthem.anthem.hub import HubCapabilities, HubSettings
    from custom_components.kohler_anthem.anthem.state import HubState, HubZone

    state = HubState(model=model)
    for zone in zones:
        state.zones[zone] = HubZone(
            outlets=[False] * model.outlets_in_zone(zone),
            temperature=38.0,
        )
    state.last_update = 1_700_000_000.0

    return SimpleNamespace(
        device=SimpleNamespace(device_id=device_id, serial_number="HUB-SN-TEST"),
        device_id=device_id,
        hub=SimpleNamespace(device_id=device_id),
        state=state,
        name=name,
        model=model,
        # `known=True` so capability-gated entities are actually created; an unknown
        # controller creates almost nothing, which would defeat the point of the test.
        capabilities=HubCapabilities(
            water=True, music=True, light=True, steam=True, known=True
        ),
        favorites=[],
        water_is_running=False,
        report_log=SimpleNamespace(enabled=False, path=None),
        settings=HubSettings(),
        experiences={},
        active_errors=[],
        firmware_info={},
    )


def make_coordinator(valves, controllers=()):
    return SimpleNamespace(
        valves=list(valves),
        controllers=list(controllers),
        temperature_unit="Fahrenheit",
        water_units="Standard",
        model=valves[0].model,
        stream=SimpleNamespace(connected=True),
        entry=SimpleNamespace(data={}, options={}, entry_id="test"),
        last_update_success=True,
        async_add_listener=lambda *a, **k: lambda: None,
    )


@pytest.fixture(scope="session")
def valve_model():
    """The K-28210 — three outlets, one zone. The hardware these tests target."""
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    return get_valve_model("K-28210")


@pytest.fixture
def valve(valve_model):
    """A K-28210 reporting the outlet types confirmed on real hardware."""
    return make_valve(valve_model, [31, 11, 1])


@pytest.fixture
def coordinator(valve):
    return make_coordinator([valve])
