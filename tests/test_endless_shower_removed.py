"""Endless Shower was removed on 2026-10-08. These pin the removal and the upgrade cleanup.

What stays: the zone clock behind the `flowing_for_seconds` / `seconds_remaining`
attributes, and `Valve.outlet_run_times`, now read from the outlet records rather than a
learned copy.
"""

from __future__ import annotations

import importlib
import json
import pathlib
from types import SimpleNamespace

import pytest

from .conftest import make_coordinator, make_valve
from .test_entities import collect


def _model(sku: str = "K-28210"):
    from custom_components.kohler_anthem.anthem.models import get_valve_model

    return get_valve_model(sku)


def test_no_endless_shower_switch_is_created():
    valve = make_valve(_model(), [31, 11, 1])
    switches = collect("switch", make_coordinator([valve]))
    assert "Endless Shower" not in {e.name for e in switches}
    assert not any(e.unique_id.endswith("_keep_water_running") for e in switches)


def test_the_detector_module_and_its_constants_are_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("custom_components.kohler_anthem.anthem.runtime_cutoff")
    from custom_components.kohler_anthem import const

    for name in (
        "CONF_RESTART_ON_RUNTIME_CUTOFF",
        "CONF_OUTLET_RUN_TIMES",
        "ISSUE_NOT_SET_UP",
        "ISSUE_DURATION_MISMATCH",
        "ENABLE_CUTOFF_DEBUG_LOG",
    ):
        assert not hasattr(const, name), name


def test_the_retired_repairs_have_no_text_left():
    root = pathlib.Path("custom_components/kohler_anthem")
    for path in [
        root / "strings.json",
        *sorted((root / "translations").glob("*.json")),
    ]:
        issues = json.loads(path.read_text(encoding="utf-8")).get("issues", {})
        assert "endless_shower_not_set_up" not in issues, path
        assert "durations_differ" not in issues, path


# --------------------------------------------------------------------------- #
# Upgrade cleanup
# --------------------------------------------------------------------------- #
def test_the_switch_row_is_purged_and_nothing_else():
    from custom_components.kohler_anthem import _REMOVED_UNIQUE_ID_SUFFIXES

    assert "gcs-x_keep_water_running".endswith(_REMOVED_UNIQUE_ID_SUFFIXES)
    valve = make_valve(_model("K-28212"), [31, 11, 1, 52, 62, 21])
    coordinator = make_coordinator([valve])
    for platform in ("switch", "sensor", "binary_sensor", "select", "number", "button"):
        for entity in collect(platform, coordinator):
            assert not entity.unique_id.endswith(_REMOVED_UNIQUE_ID_SUFFIXES), (
                entity.unique_id
            )


def test_per_valve_settings_lose_only_the_retired_keys():
    from custom_components.kohler_anthem import _async_strip_removed_valve_keys

    updates: list[dict] = []
    entry = SimpleNamespace(
        data={
            "refresh_token": "t",
            "valves": {
                "gcs-a": {"outlet_run_times": {"0": 900}},
                "gcs-b": {"outlet_run_times": {"0": 1800}},
            },
        },
        options={
            "valves": {
                "gcs-a": {
                    "restart_on_runtime_cutoff": True,
                    "warmup_auto_restore": True,
                    "last_warmup_mode": "warmUpAllOutletsWithNoStartDelay",
                }
            }
        },
    )

    def update(_entry, **changes):
        updates.append(changes)
        for key, value in changes.items():
            setattr(entry, key, value)

    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update))
    _async_strip_removed_valve_keys(hass, entry)
    assert entry.data == {"refresh_token": "t", "valves": {"gcs-a": {}, "gcs-b": {}}}
    assert entry.options == {
        "valves": {
            "gcs-a": {
                "warmup_auto_restore": True,
                "last_warmup_mode": "warmUpAllOutletsWithNoStartDelay",
            }
        }
    }
    # Clean now, so a second setup writes nothing.
    _async_strip_removed_valve_keys(hass, entry)
    assert len(updates) == 1


def test_the_old_repairs_cards_are_deleted_and_others_kept(monkeypatch):
    from custom_components import kohler_anthem as module

    deleted: list[str] = []
    issues = {
        ("kohler_anthem", "endless_shower_not_set_up_entry_gcs-a"): None,
        ("kohler_anthem", "endless_shower_not_set_up_entry"): None,
        ("kohler_anthem", "durations_differ_hub-a"): None,
        ("kohler_anthem", "outlet_write_unverified_gcs-a"): None,
        ("other_domain", "durations_differ_x"): None,
    }
    monkeypatch.setattr(
        module.ir, "async_get", lambda hass: SimpleNamespace(issues=issues)
    )
    monkeypatch.setattr(
        module.ir,
        "async_delete_issue",
        lambda hass, domain, issue_id: deleted.append(issue_id),
    )
    monkeypatch.setattr(module.er, "async_get", lambda hass: None)
    monkeypatch.setattr(module.er, "async_entries_for_config_entry", lambda *a: [])
    entry = SimpleNamespace(entry_id="entry", data={}, options={})
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=None))

    module._async_purge_removed_diagnostics(hass, entry)
    assert sorted(deleted) == [
        "durations_differ_hub-a",
        "endless_shower_not_set_up_entry",
        "endless_shower_not_set_up_entry_gcs-a",
    ]


# --------------------------------------------------------------------------- #
# What stays
# --------------------------------------------------------------------------- #
def test_the_zone_clock_times_the_zone_not_the_outlet(monkeypatch):
    from custom_components.kohler_anthem.anthem import zone_clock

    now = [100.0]
    monkeypatch.setattr(zone_clock.time, "monotonic", lambda: now[0])
    clock = zone_clock.ZoneClock()

    clock.update({1: True, 2: False})
    now[0] = 160.0
    # Same zone, still running — an outlet change does not restart the valve's timer.
    clock.update({1: True, 2: False})
    assert clock.flowing_for(1) == 60.0
    assert clock.flowing_for(2) is None

    clock.update({1: False})
    assert clock.flowing_for(1) is None

    clock.update({1: True})
    clock.forget()
    assert clock.flowing_for(1) is None


def test_a_paused_zone_is_not_running():
    from custom_components.kohler_anthem.anthem.zone_clock import ZoneClock
    from custom_components.kohler_anthem.coordinator import Valve

    words = {
        1: SimpleNamespace(outlet_mask=0x01, paused=True),
        2: SimpleNamespace(outlet_mask=0x02, paused=False),
    }
    holder = SimpleNamespace(
        gcs_state=SimpleNamespace(zone_word=words.get),
        model=SimpleNamespace(zones=(1, 2)),
        _zone_clock=ZoneClock(),
    )
    Valve._update_zone_clock(holder)
    assert holder._zone_clock.flowing_for(1) is None
    assert holder._zone_clock.flowing_for(2) is not None


def test_run_times_come_from_the_outlet_records():
    from custom_components.kohler_anthem.anthem.state import GcsState, OutletLimits
    from custom_components.kohler_anthem.coordinator import Valve

    model = _model()
    state = GcsState(model=model)
    state.outlet_limits[0] = OutletLimits(0, 16, 200, 900)
    state.outlet_limits[1] = OutletLimits(1, 16, 200, None)
    holder = SimpleNamespace(model=model, gcs_state=state)
    # Outlet 2 has a record but no run time yet; outlet 3 has no record at all.
    assert Valve.outlet_run_times.fget(holder) == {1: 900}
