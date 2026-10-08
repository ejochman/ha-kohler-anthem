"""Service actions for Kohler Anthem.

Two, both writing the valve through ``solowritesystem`` — the same endpoint every other
control path uses:

* ``custom_shower`` — the form. Tick outlets, pick a temperature, optionally a flow, and it
  goes to the valve as **one** complete command. Added 2026-09-06 after GitHub issue #1 showed
  what every automation built in the UI turns into — an outlet action, then a temperature
  action — and that the valve cannot take two commands back to back: the second is built from
  a report that predates the first, and closes what the first opened. The words sent here come
  from the form and from nothing else; see `anthem.valve_hex.encode_shower`.
* ``send_valve_hex`` — the escape hatch, a raw command word for anything neither the entities
  nor the form can express.

**Only registered when the account has an Anthem valve.** ``solowritesystem`` is a GCS
endpoint — an Anthem Plus controller on its own has no valve to write to, and control there
goes through favorites instead. So on a HUB-only account neither service appears at all,
rather than appearing and failing.

The **device field is optional**. With one Anthem valve on the account — every install
before 2026-09-08 — it can be left empty and the action finds the valve itself; asking
would be friction for nothing. With several, it says which one, and an action that does
not say is refused rather than sent to whichever valve happened to load first.

**These services can run water.** Input is validated before sending and the effect is logged,
but they are deliberately unrestricted otherwise: the point is to reach states the UI does
not model.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.service import async_set_service_schema

from .anthem.models import ValveModel
from .anthem.valve_hex import (
    FLOW_BYTE_MAX,
    FLOW_BYTE_MIN,
    FLOW_PER_PERCENT,
    ValveHexError,
    encode_shower,
    unit_to_celsius,
)
from .const import (
    DEFAULT_FLOW_PERCENT,
    DOMAIN,
    SERVICE_CUSTOM_SHOWER,
    SERVICE_SEND_VALVE_HEX,
    UI_TEMPERATURE_MAX_F,
    ZONE_TEMPERATURE_MIN_F,
)
from .coordinator import KohlerAnthemCoordinator, Valve

_LOGGER = logging.getLogger(__name__)

# Home Assistant's device-registry id for the valve, as the device selector yields it.
ATTR_DEVICE_ID = "device_id"
ATTR_ZONE1_HEX = "zone1_hex"
ATTR_ZONE2_HEX = "zone2_hex"

ATTR_ZONE1_TEMPERATURE = "zone1_temperature"
ATTR_ZONE2_TEMPERATURE = "zone2_temperature"
ATTR_FLOW = "flow"
ATTR_KEEP_ON = "keep_on_after_warmup"
# Flat data keys, zone by zone. The form groups them into one section per zone for display
# only; the call data is flat. Zone 2's temperature is optional and follows zone 1's.
_ZONE_TEMPERATURE_FIELDS = {1: ATTR_ZONE1_TEMPERATURE, 2: ATTR_ZONE2_TEMPERATURE}
_ZONE_OUTLET_FIELDS: dict[int, tuple[str, ...]] = {
    1: ("zone1_outlet_1", "zone1_outlet_2", "zone1_outlet_3"),
    2: ("zone2_outlet_1", "zone2_outlet_2", "zone2_outlet_3"),
}
_ZONE_SECTIONS = {1: "zone_1", 2: "zone_2"}
# The flow byte's own range, as a percentage — 0x10 (16) to 0xC8 (200) is 8 % to 100 %.
_FLOW_MIN_PERCENT = FLOW_BYTE_MIN // FLOW_PER_PERCENT
_FLOW_MAX_PERCENT = FLOW_BYTE_MAX // FLOW_PER_PERCENT

# Either length the system itself shows: 8 for a command word, 16 for what the Zone Hex
# sensor displays — its second half is sensor feedback, which `_command_half` discards. Both
# are accepted so a value can be pasted straight out of that sensor without being edited.
# Any other length is a typo, and `async_send_valve_hex` re-checks it there too: this layer
# only exists so the UI can reject one without a round trip.
_HEX_WORD = vol.All(
    cv.string, cv.matches_regex(r"^(?:[0-9A-Fa-f]{8}|[0-9A-Fa-f]{16})$")
)

SEND_VALVE_HEX_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): vol.Any(None, cv.string),
        vol.Required(ATTR_ZONE1_HEX): _HEX_WORD,
        # `vol.Maybe` because the UI submits "" for a touched-then-cleared optional text
        # field, which would otherwise fail the regex instead of meaning "closed".
        vol.Optional(ATTR_ZONE2_HEX): vol.Any("", None, _HEX_WORD),
    }
)

# Temperatures are range-checked in the handler, not here: the bounds depend on the account's
# unit, which the schema does not know. The booleans default to off so a YAML call may leave
# them out; the form marks them required for a display reason explained at `_FIELD_OUTLETS`.
CUSTOM_SHOWER_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_DEVICE_ID): vol.Any(None, cv.string),
        vol.Required(ATTR_ZONE1_TEMPERATURE): vol.Coerce(float),
        vol.Optional(ATTR_ZONE2_TEMPERATURE): vol.Any(None, vol.Coerce(float)),
        vol.Optional(ATTR_FLOW): vol.All(
            vol.Coerce(float), vol.Range(min=_FLOW_MIN_PERCENT, max=_FLOW_MAX_PERCENT)
        ),
        vol.Optional(ATTR_KEEP_ON, default=False): cv.boolean,
        **{
            vol.Optional(key, default=False): cv.boolean
            for keys in _ZONE_OUTLET_FIELDS.values()
            for key in keys
        },
    }
)


# The forms as the UI renders them. `services.yaml` carries the same thing statically, as the
# fallback if the runtime override below cannot be applied; keep the two in step —
# Nothing in this repository checks that they stay in step — an external harness did, and
# `tests/` here does not cover it. Treat the duplication as hand-maintained.
_FIELD_DEVICE = {
    "name": "Valve",
    "required": False,
    "description": "Which Anthem valve, when the account has more than one.",
    "selector": {"device": {"filter": {"integration": DOMAIN}}},
}
_FIELD_ZONE1 = {
    "name": "Zone 1 Hex",
    "required": True,
    "description": (
        'The 8-character code from the "Zone 1 Hex" sensor on the Anthem Valve '
        "device. Set the outlet switches and temperature the way you want them "
        "first, then copy the code."
    ),
    "example": "0184C801",
    "selector": {"text": None},
}
_FIELD_ZONE2 = {
    "name": "Zone 2 Hex",
    "required": False,
    "description": (
        'The 8-character code from the "Zone 2 Hex" sensor on the Anthem Valve '
        "device. Set the outlet switches and temperature the way you want them "
        "first, then copy the code."
    ),
    "example": "1184C801",
    "selector": {"text": None},
}
_SERVICE_DESCRIPTION = (
    "Send a command code straight to the Anthem valve, for anything the normal "
    "controls cannot do. Set the shower up how you want it with the outlet switches "
    "and temperature controls, then copy the code from the Zone Hex diagnostic "
    "sensor and paste it below. WARNING: this can start water."
)

_CUSTOM_SHOWER_DESCRIPTION = (
    "Start the shower with the outlets and temperature you choose, sent to the valve as "
    "one command. This is the reliable way to open an outlet and set its temperature from "
    "a single automation step. Outlets you leave off are closed, and leaving every outlet "
    "off stops the shower. On a valve with warm-up enabled the valve warms up first and "
    'then pauses for two minutes, just as it always does; turn on "No pausing warm-up" '
    "(beta) to have it carry on with your outlets and temperature the moment "
    "that pause begins. WARNING: this can start water."
)
# The static form shows Fahrenheit and the K-28212's outlets; the runtime override swaps in
# the account's unit and range and drops the zones and outlets the valve does not have.
_FIELD_ZONE1_TEMPERATURE = {
    "name": "Zone 1 temperature",
    "required": True,
    "description": "Target temperature for zone 1, in the unit your account uses.",
    "example": 104,
    "selector": {
        "number": {
            "min": 59,
            "max": 118,
            "step": 1,
            "unit_of_measurement": "°F",
            "mode": "slider",
        }
    },
}
_FIELD_ZONE2_TEMPERATURE = {
    "name": "Zone 2 temperature",
    "required": False,
    "description": "Target temperature for zone 2. Leave it unset to use the zone 1 temperature.",
    "example": 104,
    "selector": {
        "number": {
            "min": 59,
            "max": 118,
            "step": 1,
            "unit_of_measurement": "°F",
            "mode": "slider",
        }
    },
}
# `required: True` on every boolean is a display decision, not validation. The automation
# editor puts an "include this field" checkbox in front of each optional field, which next to
# a toggle is two switches for one thing (owner, 2026-09-06, after the first live test). A
# required boolean is shown as the toggle alone and pre-filled off — frontend
# `ha-service-control`: `showOptionalToggle` is false for a required field, and a required
# boolean with no default is set to `false` when the action is chosen. The schema above still
# defaults them, so a YAML call may leave them out.
_FIELD_OUTLETS = {
    "zone1_outlet_1": {
        "name": "Zone 1 outlet 1",
        "required": True,
        "selector": {"boolean": None},
    },
    "zone1_outlet_2": {
        "name": "Zone 1 outlet 2",
        "required": True,
        "selector": {"boolean": None},
    },
    "zone1_outlet_3": {
        "name": "Zone 1 outlet 3",
        "required": True,
        "selector": {"boolean": None},
    },
    "zone2_outlet_1": {
        "name": "Zone 2 outlet 1",
        "required": True,
        "selector": {"boolean": None},
    },
    "zone2_outlet_2": {
        "name": "Zone 2 outlet 2",
        "required": True,
        "selector": {"boolean": None},
    },
    "zone2_outlet_3": {
        "name": "Zone 2 outlet 3",
        "required": True,
        "selector": {"boolean": None},
    },
}
# "(beta)" in the name and "Beta" in the description are deliberate and mirrored in the docs
# and release notes (owner, 2026-09-06): the resume has run on one valve, so the form says so.
_FIELD_KEEP_ON = {
    "name": "No pausing warm-up (beta)",
    "required": True,
    "description": (
        "Keeps the shower on after the valve's warm-up is finished. Beta, tested on one "
        "valve so far. Only matters when the valve's warm-up is enabled. After warming "
        "up, the valve pauses for two minutes, just as it always "
        "does. With this on, the shower carries on with your outlets and temperature the "
        "moment that pause begins. Off, the valve's two-minute pause runs as usual."
    ),
    "selector": {"boolean": None},
}
_FIELD_FLOW = {
    "name": "Flow",
    "required": False,
    "description": (
        "Percentage of full flow, from 8 to 100. Leave it unset for full flow. The flow "
        "set here holds only until someone presses the flow button on the touchscreen, "
        "which takes over from then on."
    ),
    "example": 100,
    "selector": {
        "number": {
            "min": 8,
            "max": 100,
            "step": 1,
            "unit_of_measurement": "%",
            "mode": "box",
        }
    },
}
_SECTION_NAMES = {
    "zone_1": "Zone 1",
    "zone_2": "Zone 2",
    "advanced_fields": "Advanced",
}


def _temperature_bounds(unit: str) -> tuple[float, float, str]:
    """The form's range in the account's unit: 59-118 °F.

    The zone slider's range less its Cold stop. The floor is `ZONE_TEMPERATURE_MIN_F`, the
    minimum every outlet carries; the top is `UI_TEMPERATURE_MAX_F`, the highest the app lets
    Max Temperature go — fixed here because one form serves every valve, and each valve
    clamps to its own scald limit anyway. **Cold is deliberately not reachable from here**:
    a YAML caller typing 0 should get an error, not a cold shower. `send_valve_hex` and the
    zone slider's Cold stop are the ways to ask for it.
    """
    if unit.lower().startswith("f"):
        return float(ZONE_TEMPERATURE_MIN_F), float(UI_TEMPERATURE_MAX_F), "°F"
    return (
        float(round(unit_to_celsius(ZONE_TEMPERATURE_MIN_F, "Fahrenheit"))),
        float(round(unit_to_celsius(UI_TEMPERATURE_MAX_F, "Fahrenheit"))),
        "°C",
    )


def _async_describe_service(
    hass: HomeAssistant, two_zones: bool, several_valves: bool
) -> None:
    """Publish the `send_valve_hex` form, showing the Zone 2 field only on a two-zone system
    and the Valve picker only when the account has more than one valve.

    `services.yaml` is static and cannot vary per installation, so a single-zone owner would
    otherwise be shown a Zone 2 box for a zone they do not have — with a sensor named in its
    description that does not exist on their device. `async_set_service_schema` overrides
    that description at runtime, which is the supported way to vary it.

    Best-effort: if this cannot be applied the static `services.yaml` still stands, so the
    action keeps working with one redundant field rather than not working at all.
    """
    fields: dict[str, Any] = {}
    if several_valves:
        # Same rule as Zone 2 (2026-09-08): a field for a choice the owner does not have is
        # noise. With one valve the handler uses it unasked, so the picker is not shown.
        fields[ATTR_DEVICE_ID] = _FIELD_DEVICE
    fields["zone1_hex"] = _FIELD_ZONE1
    if two_zones:
        fields["zone2_hex"] = _FIELD_ZONE2
    try:
        async_set_service_schema(
            hass,
            DOMAIN,
            SERVICE_SEND_VALVE_HEX,
            {
                "name": "Send valve hex",
                "description": _SERVICE_DESCRIPTION,
                "fields": fields,
            },
        )
    except Exception:
        _LOGGER.debug("Could not override the service description", exc_info=True)


def _async_describe_custom_shower(
    hass: HomeAssistant, models: list[ValveModel], temperature_unit: str
) -> None:
    """Publish the `custom_shower` form for this installation.

    Three things the static `services.yaml` cannot know: the account's temperature unit and
    therefore the slider's range, which zones the valve has, and how many outlets each zone
    has (`ValveModel.outlets_in_zone`). Same best-effort rule as `_async_describe_service`.

    With several valves the form is the union of their layouts — a zone or outlet any of
    them has is shown — and the handler checks the chosen valve's own model, so an outlet
    the target does not have is an error there rather than a silent no-op. The Valve picker
    itself appears only then: with one valve there is no choice to make, so — like Zone 2 on
    a single-zone valve — it is left out (2026-09-08).
    """
    low, high, symbol = _temperature_bounds(temperature_unit)

    def temperature_field(static: dict[str, Any]) -> dict[str, Any]:
        return {
            **static,
            "selector": {
                "number": {
                    **static["selector"]["number"],
                    "min": int(low),
                    "max": int(high),
                    "unit_of_measurement": symbol,
                }
            },
        }

    statics = {1: _FIELD_ZONE1_TEMPERATURE, 2: _FIELD_ZONE2_TEMPERATURE}
    fields: dict[str, Any] = {}
    if len(models) > 1:
        fields[ATTR_DEVICE_ID] = _FIELD_DEVICE
    zones = [1, 2] if any(model.uses_valve2 for model in models) else [1]
    for zone in zones:
        widest = max(
            model.outlets_in_zone(zone) for model in models if zone in model.zones
        )
        keys = _ZONE_OUTLET_FIELDS[zone][:widest]
        section = _ZONE_SECTIONS[zone]
        fields[section] = {
            "name": _SECTION_NAMES[section],
            "collapsed": False,
            "fields": {
                _ZONE_TEMPERATURE_FIELDS[zone]: temperature_field(statics[zone]),
                **{key: _FIELD_OUTLETS[key] for key in keys},
            },
        }
    fields[ATTR_KEEP_ON] = _FIELD_KEEP_ON
    fields["advanced_fields"] = {
        "name": _SECTION_NAMES["advanced_fields"],
        "collapsed": True,
        "fields": {ATTR_FLOW: _FIELD_FLOW},
    }
    try:
        async_set_service_schema(
            hass,
            DOMAIN,
            SERVICE_CUSTOM_SHOWER,
            {
                "name": "Custom shower",
                "description": _CUSTOM_SHOWER_DESCRIPTION,
                "fields": fields,
            },
        )
    except Exception:
        _LOGGER.debug("Could not override the custom_shower description", exc_info=True)


def _resolve_valve(hass: HomeAssistant, device_id: str | None) -> Valve:
    """Find the valve an action is for.

    ``device_id`` is Home Assistant's device-registry id — what the device selector yields —
    resolved to a valve through the identifiers this integration registers. Without one,
    the single valve across every loaded entry is used. With several and no id the action
    refuses, because writing a command word to whichever one happened to load first is
    worse than saying so.
    """
    entries: dict[str, KohlerAnthemCoordinator] = hass.data.get(DOMAIN, {})
    valves = [valve for coordinator in entries.values() for valve in coordinator.valves]
    if not valves:
        raise HomeAssistantError(
            "No Anthem valve on this account — solowritesystem is a valve endpoint, and an "
            "Anthem Plus controller is driven through favorites instead"
        )
    if device_id:
        device = dr.async_get(hass).async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"No device with id {device_id!r}")
        wanted = {
            identifier for domain, identifier in device.identifiers if domain == DOMAIN
        }
        for valve in valves:
            if valve.device_id in wanted:
                return valve
        raise ServiceValidationError(
            f"{device.name_by_user or device.name} is not an Anthem valve; choose the "
            "valve device, not the controller"
        )
    if len(valves) > 1:
        raise ServiceValidationError(
            "More than one Anthem valve is set up ("
            + ", ".join(valve.name for valve in valves)
            + "); say which one with the Valve field (device_id)"
        )
    return valves[0]


async def _async_send_valve_hex(call: ServiceCall) -> ServiceResponse:
    """Handle `kohler_anthem.send_valve_hex`."""
    valve = _resolve_valve(call.hass, call.data.get(ATTR_DEVICE_ID))
    result: dict[str, Any] = await valve.async_send_valve_hex(
        call.data[ATTR_ZONE1_HEX], call.data.get(ATTR_ZONE2_HEX) or None
    )
    return result


async def _async_custom_shower(call: ServiceCall) -> ServiceResponse:
    """Handle `kohler_anthem.custom_shower`.

    Builds both words from the form and hands them to the coordinator as one write. Each
    temperature is checked against `_temperature_bounds`, in the account's unit, so a YAML
    caller cannot send the valve full cold by typing 0. Zone 2's temperature follows zone
    1's when it is not given.
    """
    valve = _resolve_valve(call.hass, call.data.get(ATTR_DEVICE_ID))
    unit = valve.temperature_unit
    low, high, symbol = _temperature_bounds(unit)
    zone1 = float(call.data[ATTR_ZONE1_TEMPERATURE])
    zone2_raw = call.data.get(ATTR_ZONE2_TEMPERATURE)
    zone2 = zone1 if zone2_raw is None else float(zone2_raw)
    for zone, temperature in ((1, zone1), (2, zone2)):
        if not low <= temperature <= high:
            raise ServiceValidationError(
                f"Zone {zone} temperature must be between {low:g} and {high:g} {symbol}; "
                f"got {temperature:g}"
            )
    zone_flags = {
        zone: [bool(call.data.get(key, False)) for key in keys]
        for zone, keys in _ZONE_OUTLET_FIELDS.items()
    }
    flow = call.data.get(ATTR_FLOW)
    try:
        word1, word2 = encode_shower(
            valve.model,
            {1: unit_to_celsius(zone1, unit), 2: unit_to_celsius(zone2, unit)},
            DEFAULT_FLOW_PERCENT if flow is None else float(flow),
            zone_flags,
        )
    except ValveHexError as err:
        raise ServiceValidationError(str(err)) from err
    keep_on = bool(call.data.get(ATTR_KEEP_ON, False))
    result: dict[str, Any] = await valve.async_custom_shower(
        word1, word2, keep_on_after_warmup=keep_on
    )
    return {**result, ATTR_KEEP_ON: keep_on}


# `probe_usage` lived here until 2026-10-07: an exploratory action that tried candidate
# query strings against `gcs-usage` because its contract was unknown. Konnect 3.0.6 settled
# the contract (`anthem/const.py` `GCS_USAGE`), the integration already reads it, and the
# action went with the question it existed to answer.


def async_register_services(
    hass: HomeAssistant, coordinator: KohlerAnthemCoordinator
) -> None:
    """Register the integration's services, once, if this entry has a valve.

    Idempotent: `async_setup_entry` runs per entry and on every reload, and re-registering
    would otherwise stack handlers. A HUB-only entry registers nothing, so the actions do
    not appear in the UI on an account that could never use them.
    """
    if not coordinator.valves:
        return
    # The forms describe every valve loaded so far, across entries, so the Valve picker
    # appears only once there is more than one to choose from. Registration is once, but
    # the description is re-published on every entry setup so a second entry's valve turns
    # the picker on. (An unload does not re-describe; a stale picker is cosmetic, and the
    # handler checks the target valve either way.) `hass.data[DOMAIN]` already holds this
    # entry's coordinator — `async_setup_entry` stores it before calling here.
    models = [
        valve.model
        for other in hass.data.get(DOMAIN, {}).values()
        for valve in other.valves
    ]
    if not hass.services.has_service(DOMAIN, SERVICE_SEND_VALVE_HEX):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_VALVE_HEX,
            _async_send_valve_hex,
            schema=SEND_VALVE_HEX_SCHEMA,
            # Returns the decoded reading of both words, so a caller can confirm the word
            # meant what they thought without going to the log.
            supports_response=SupportsResponse.OPTIONAL,
        )
    # After registering, not before: the description attaches to a service that exists.
    # Whether the Zone 2 field is shown follows the topology detected at setup; whether the
    # Valve picker is shown follows how many valves are loaded.
    _async_describe_service(
        hass, any(model.uses_valve2 for model in models), len(models) > 1
    )
    if not hass.services.has_service(DOMAIN, SERVICE_CUSTOM_SHOWER):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CUSTOM_SHOWER,
            _async_custom_shower,
            schema=CUSTOM_SHOWER_SCHEMA,
            # Same response as `send_valve_hex`, so the form doubles as a way to learn the
            # command word for the escape hatch.
            supports_response=SupportsResponse.OPTIONAL,
        )
    _async_describe_custom_shower(hass, models, coordinator.temperature_unit)


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the services when the last entry unloads.

    Tolerates never having been registered — a HUB-only account gets here having skipped
    registration entirely.
    """
    # Every service registered above belongs here. One that is missed outlives the last
    # unload — as `probe_usage` did in 0.7.7 — and stays in the registry with nothing
    # behind it, failing with an error about the wrong thing instead of simply not existing.
    for service in (
        SERVICE_SEND_VALVE_HEX,
        SERVICE_CUSTOM_SHOWER,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
