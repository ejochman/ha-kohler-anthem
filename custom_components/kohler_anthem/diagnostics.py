"""Diagnostics for Kohler Anthem — the "Download diagnostics" button.

One report, many buttons: the config entry and every device page (the Anthem Valve and
each Anthem Plus controller) all describe the **whole installation**, by design — a
hardware report should cover everything, because the products are one plumbing system and
half a picture has repeatedly misled this project (see ``docs/architecture.md``). Every
device appears in ``valves`` and ``controllers`` whichever button was pressed.

Two things do follow the button: ``requested_for``, naming it, and the singular ``valve``
/ ``controller`` blocks, which describe **that** device. Those singular keys exist only so
a report from a single-device account reads as it always has; on an account with several,
pressing the second valve's button and getting the first valve's limits under a
``valve_1`` label is precisely the half-picture this module is meant to prevent.

What this is for: **hardware validation reports.** Every claim in this integration is
verified against exactly one installation (a K-28212 + controller), and the support matrix
in the README only moves on evidence. This file is the evidence: model and outlet split as
detected, which devices exist, what the valve and controller are reporting, whether limits
arrived. A user on unverified hardware attaches this JSON to a "hardware report" issue and
that model's row can be marked verified.

What deliberately stays out: credentials (refresh token), account identity (username,
tenant id), and device identity (device ids, serial numbers, the mobile registration id).
Kohler device serials double as cloud addresses, so they are redacted the same way tokens
are — presence and SKU are enough for validation. Preset and favorite *names* are the
owner's own words and stay out too; counts carry the signal.

**Two mechanisms, because they answer different questions.** `TO_REDACT` names the keys whose
values must never appear, which works for the record shapes this module reproduces
deliberately. `_version_fields` is the exception: it walks fields no capture has covered, on
accounts unlike the owner's, so it cannot be driven by a key list — it takes only
version-shaped keys and then checks the *value* as well, because a device id
(`gcs-sio32343h7`) is short enough to pass any sane length ceiling.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_MOBILE_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_TENANT_ID,
    CONF_VALVES,
    DOMAIN,
    OUTLET_TYPE_NAMES,
    PRESET_HIDDEN_IDS,
)
from .coordinator import Controller, KohlerAnthemCoordinator, Valve

# Entry.data/options keys whose VALUES are secrets or identity. Everything else in the
# entry is validation-relevant configuration (model choice, outlet split, units, learned
# run times) and passes through.
TO_REDACT = {
    CONF_USERNAME,
    CONF_REFRESH_TOKEN,
    CONF_TENANT_ID,
    CONF_MOBILE_DEVICE_ID,
}


def _redact_entry(
    coordinator: KohlerAnthemCoordinator, section: Mapping[str, Any]
) -> dict[str, Any]:
    """Redact an entry section, **including the device ids used as dictionary keys.**

    `async_redact_data` replaces the *values* at listed keys and recurses into nested
    dicts. It never touches dictionary **keys** — and `CONF_VALVES` is keyed by device id,
    so a plain redaction published every valve's id verbatim while this module's own
    docstring promised they were held back. Kohler device ids double as cloud addresses,
    and these reports are written to be attached to public issues.

    The ids are replaced with the same `valve_0`, `valve_1` labels `requested_for` uses, in
    the cloud's order, so a report stays readable and the per-valve settings can still be
    matched against the `valves` list further down. An id belonging to no known valve — a
    device removed from the account since it was written — is replaced with a placeholder
    rather than passed through, because "unknown to us" is not "safe to publish".
    """
    labels = {
        valve.device_id: f"valve_{index}"
        for index, valve in enumerate(coordinator.valves)
    }
    redacted = async_redact_data(dict(section), TO_REDACT)
    valves = redacted.get(CONF_VALVES)
    if isinstance(valves, Mapping):
        redacted[CONF_VALVES] = {
            labels.get(str(device_id), "valve_unknown"): settings
            for device_id, settings in valves.items()
        }
    return redacted


def _word(word: Any) -> dict[str, Any] | None:
    """One zone's valve word: the wire hex verbatim, then the decoded reading.

    ``raw`` is the word exactly as it arrived (empty for a word seeded from REST, which
    carries no wire form) — included first because the raw word is what settles disputes
    when a decode is questioned on foreign hardware.
    """
    if word is None:
        return None
    return {
        "raw": word.raw or None,
        "temperature_celsius": word.temperature_celsius,
        "flow_percent": word.flow_percent,
        "outlet_mask": word.outlet_mask,
        "paused": word.paused,
        "at_temperature": word.at_temperature,
        "at_flow": word.at_flow,
        "error_flag": word.error_flag,
        "measured_temperature_celsius": word.measured_temperature_celsius,
        "measured_flow_percent": word.measured_flow_percent,
    }


# Keys whose *values* are version-shaped, wherever they appear in the record. Matched on
# the key name rather than the value, because a version can be a string ("2.20"), an int
# (10) or a float — the app shows the valves as bare `10` — and matching on shape alone
# would sweep up timestamps, ids and counts.
_VERSION_KEY_HINTS = ("firmware", "version", "swrev", "revision", "build")

# Keys that contain one of the hints above but are NOT a version: update bookkeeping,
# feature flags, and the type/status labels that sit beside a version. Excluded by exact
# name so a genuinely new version key is never silently dropped.
_VERSION_KEY_SKIP = {
    "firmwaretype",
    "firmwareupdate",
    "isfirmwareupdateavailable",
    "issinglefirmwareupdate",
    "versionblocks",
}

# Values that would carry identity rather than a version. A version is short; a device id,
# serial or GUID is not, and none of them belong in a report that redacts those elsewhere.
_VERSION_VALUE_MAX_LEN = 24

# ⚠️ **Length alone is not enough.** A Kohler device id is `gcs-sio32343h7` — fourteen
# characters, well under the ceiling above — so a device id sitting under a version-shaped
# key (`deviceVersionId`, say) would have been copied out verbatim. No captured payload has
# such a key today, and this scanner exists precisely to walk fields no capture has covered
# on accounts unlike the owner's, so the shape is checked as well as the length.
#
# Matched on the value, not the key: any string carrying a device prefix, a long hex or
# GUID run, or an Azure connection-string fragment is reported as its type and length.
_IDENTIFIER_VALUE = re.compile(
    r"(?:^|[^a-z0-9])(?:gcs|hub)-[a-z0-9]{4,}"  # device ids
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # GUID
    r"|[0-9a-f]{16,}"  # long hex run: serials, keys
    r"|(?:hostname|deviceid|sharedaccesskey|accountkey)=",  # connection strings
    re.IGNORECASE,
)


def _identity_shaped(value: str) -> bool:
    """True when a value looks like identity rather than a version number."""
    return bool(_IDENTIFIER_VALUE.search(value))


def _version_fields(
    node: Any, path: str = "", found: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Every version-shaped field in a record, by dotted path.

    **Why this exists.** `gcs-configuration` carries at least three different firmwares —
    the touchscreen interface, the valve itself, and the gateway — and the Konnect app shows
    all three as different numbers (2.2, 10, 00.74 on the owner's system). Only the
    interface's lives in the three blocks `_configuration_report` reproduces in full, so the
    other two were invisible in every report, and a single `Firmware` entity was silently
    picking whichever it found first. See `docs/user_guide.md`.

    **Why by path rather than in full.** The blocks these live in (`configuration`, `iot`,
    `applicationSource`) also describe someone's plumbing and their cloud addressing, which
    this module redacts everywhere else. Reproducing them whole to find a version would
    trade the report's discretion for one field. So this walks the record and takes only
    values whose *key* names a version, leaving every sibling behind.

    Values longer than `_VERSION_VALUE_MAX_LEN` are reported as their type and length rather
    than their content: a real version is short, and anything long enough to be a device id,
    a serial or a connection string is exactly what must not be copied out.
    """
    if found is None:
        found = {}
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            lowered = str(key).lower()
            if isinstance(value, (Mapping, list)):
                _version_fields(value, here, found)
                continue
            if lowered in _VERSION_KEY_SKIP:
                continue
            if not any(hint in lowered for hint in _VERSION_KEY_HINTS):
                continue
            if isinstance(value, str) and (
                len(value) > _VERSION_VALUE_MAX_LEN or _identity_shaped(value)
            ):
                found[here] = f"<{type(value).__name__}, {len(value)} chars>"
            else:
                found[here] = value
    elif isinstance(node, list):
        # Indexed, so two valves' entries stay distinguishable in the report.
        for index, item in enumerate(node[:8]):
            _version_fields(item, f"{path}[{index}]", found)
    return found


def _configuration_report(valve: Valve) -> dict[str, Any]:
    """What `gcs-configuration` returned, summarised rather than reproduced.

    Deliberately **not** the raw record. The structural blocks describe someone's plumbing
    and, on an account that populates them, could carry more identity than a hardware report
    needs — while the open question only asks *which* fields are populated. So this reports
    the key names and whether each block is null, plus firmware in full.

    `read: false` distinguishes "the call has not run or failed" from "it ran and everything
    was null" — indistinguishable otherwise, and the whole point of the exercise.
    """
    configuration = valve.configuration
    if not configuration:
        return {"read": configuration is not None, "populated": {}, "firmware": None}

    # The structural fields the docs list as null on a controller-attached valve. Reported
    # by name so a GCS-only account's report says plainly which of them arrived.
    structural = (
        "zoneone",
        "zonetwo",
        "parts",
        "valve1Settings",
        "valve2Settings",
        "systemConfiguration",
        "systemSettings",
    )
    # Version-shaped blocks, reported in full. These carry no plumbing structure and no
    # identity — they are version strings and update state — and withholding them is what
    # made 0.7.1's `unknown` firmware unanswerable from a report: the summariser named
    # `firmwareUpdate`, `otaReportedProperties` and `version` without ever saying what was
    # in them, so the one question the report existed to answer needed a second round trip.
    version_blocks = {
        key: configuration.get(key)
        for key in ("firmwareUpdate", "otaReportedProperties", "version")
        if key in configuration
    }

    return {
        "read": True,
        "firmware": valve.firmware,
        "version_blocks": version_blocks,
        # Every version-shaped field anywhere in the record, including the blocks named but
        # not dumped below. The interface firmware is the only one the three blocks above
        # carry; the valve and gateway versions the Konnect app shows live elsewhere, and
        # without this a report cannot say where.
        "version_fields": _version_fields(configuration),
        # True where the key is present AND not null — the distinction the question turns on.
        "populated": {key: configuration.get(key) is not None for key in structural},
        # Every other key the record carried, named but not dumped, so a field nobody has
        # seen before shows up in a report without its contents going with it.
        "other_keys": sorted(
            key for key in configuration if key not in structural and key != "about"
        ),
        # Read through `Valve.about`, which knows both nestings. Reading
        # `configuration["about"]` directly reported `[]` on hardware that populates the
        # block in full, because this account nests it under the record's own
        # `configuration` key — the same depth bug that made `Firmware` report an artwork
        # version. Key names only; the values are versions and reach the report through
        # `version_fields`.
        "about_keys": sorted(valve.about),
        # Timestamps, by value rather than by name. These are the record's own dates —
        # when Kohler's cloud created the device row and when it last changed — and unlike
        # the structural blocks they carry no installation detail, so there is nothing to
        # withhold. `createdTime` is the closest thing to an install date this API has.
        "created_time": configuration.get("createdTime"),
        "updated_timestamp": configuration.get("updatedTimestamp"),
    }


#: Fields of a firmware read worth reporting. `url` (the image's download address) and
#: anything else is left out — a report needs the versions and the verdict, not the file.
_FIRMWARE_FIELDS = (
    "currentFirmware",
    "firmware",
    "firmwareUpdateAvailable",
    "mandatoryUpdate",
    "otaStatus",
    "skip",
)


def _firmware_report(info: Mapping[str, Any] | None) -> dict[str, Any]:
    info = info or {}
    return {key: info.get(key) for key in _FIRMWARE_FIELDS if key in info}


def _about_report(about: Mapping[str, Any] | None) -> dict[str, Any]:
    """`gcs-configuration/{id}/about`, with every serial reduced to "present or not".

    Serials double as cloud addresses (see the module docstring), so they never appear;
    models, firmware and status do, because they are what a hardware report is read for.
    """
    about = about or {}

    def part(entry: Any) -> dict[str, Any]:
        if not isinstance(entry, Mapping):
            return {}
        return {
            **{
                k: entry.get(k)
                for k in ("model", "name", "firmware", "status")
                if k in entry
            },
            "serial_present": bool(entry.get("serialNo")),
        }

    valves = about.get("valvesConfigInfo")
    interfaces = about.get("interfacesConfigInfo")
    return {
        "gateway": part(about.get("gatewayConfigInfo")),
        "valves": [part(v) for v in valves] if isinstance(valves, list) else [],
        "interfaces": (
            [part(i) for i in interfaces] if isinstance(interfaces, list) else []
        ),
    }


def _fault_log(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """`gcs-diagnostics` entries, ids stripped, the app's own `errorCode "0"` filter applied."""
    details = (payload or {}).get("errorDetails")
    keep = (
        "errorCode",
        "title",
        "description",
        "errorState",
        "isActive",
        "timestamp",
        "valveId",
        "component",
        "area",
    )
    return [
        {k: entry.get(k) for k in keep if k in entry}
        for entry in (details if isinstance(details, list) else [])
        if isinstance(entry, Mapping) and str(entry.get("errorCode") or "0") != "0"
    ]


def _valve_report(valve: Valve) -> dict[str, Any]:
    """One valve's state, limits, and its own Endless Shower and warm-up settings."""
    gcs = valve.gcs_state
    model = valve.model
    return {
        # The layout this valve actually decodes with — its own, which can differ from the
        # entry's `model` when the account has several valves.
        "model": {
            "sku": model.sku,
            "outlets_valve1": model.outlets_valve1,
            "outlets_valve2": model.outlets_valve2,
        },
        "zone_words": {str(zone): _word(gcs.zone_word(zone)) for zone in model.zones},
        "is_running": gcs.is_running,
        "is_paused": gcs.is_paused,
        "warmup_mode": gcs.warmup_mode,
        "warmup_in_progress": gcs.warmup_in_progress,
        "active_preset_id": gcs.active_preset_id,
        # The valve's own session flag, beside the decoded `is_running`/`is_paused` above.
        # A disagreement between them is worth seeing in a bug report.
        "system_state": gcs.system_state,
        "water": {
            # Raw and filtered both, so a report shows whether the glitch filter fired and
            # `totalFlow` exactly as the cloud sent it. **Not a meter** — it takes three
            # distinct values and shifts between two scales 4x apart with no water running
            # (see `GcsState.total_flow`). Reported because it is the evidence for that open
            # question, not because it means anything.
            "total_flow_raw": gcs.total_flow,
            # Undocumented unit; recorded so the question can be settled from real reports.
            "total_volume": gcs.total_volume,
        },
        "presets": {
            "slots_seen": len(gcs.presets),
            # **`selectable` counts before the hidden ids are removed; `offered` counts
            # after.** They differ by the default-shower slot (`PRESET_HIDDEN_IDS`), which
            # is startable but never listed, so a valve with no user favorites reports
            # `selectable: 1` and `offered: 0`. Reading the first as "one usable
            # favorite" and expecting the picker to show it is a mistake this pair of
            # numbers exists to prevent — `offered` is what the dropdown actually holds.
            "selectable": sum(1 for p in gcs.presets.values() if p.is_selectable),
            "offered": len(gcs.selectable_presets(hidden=PRESET_HIDDEN_IDS)),
            "hidden_ids_present": sorted(
                p.preset_id
                for p in gcs.presets.values()
                if p.preset_id in PRESET_HIDDEN_IDS and not p.is_empty
            ),
            "experiences": sum(1 for p in gcs.presets.values() if p.is_experience),
            "empty": sum(1 for p in gcs.presets.values() if p.is_empty),
        },
        # Keyed by the device's own 0-based outLetId. Fills in gradually over MQTT
        # and REST; a missing outlet means "never announced", not zero.
        "outlet_limits": {
            str(outlet_id): {
                "minimum_flow_byte": lim.minimum_flow_byte,
                "maximum_flow_byte": lim.maximum_flow_byte,
                "maximum_run_time": lim.maximum_run_time,
                "default_flow_byte": lim.default_flow_byte,
                # The valve's own type code for this outlet, unmapped. Recorded so the
                # codes seen across real installs can be compared with what the Konnect
                # app shows for the same fixture — only three of them are documented, and
                # a name map has to be built from evidence rather than guessed.
                "outlet_type": lim.outlet_type,
                # None for a code this integration cannot name yet — which is the useful
                # signal in a hardware report, since it says exactly which codes still
                # need an owner to confirm what the fixture is.
                "outlet_type_name": (
                    None
                    if lim.outlet_type is None
                    else OUTLET_TYPE_NAMES.get(lim.outlet_type)
                ),
                # The scald limit, in tenths of °C. Added to `OutletLimits` in 0.11.1 and
                # missing from this report until 0.11.3 — so the first reports carrying the
                # new sensor could not say whether the valve had sent the field or the
                # parser had missed it, which is the one question such a report exists to
                # answer.
                "maximum_temperature_tenths": lim.maximum_temperature_tenths,
                # The other three fields `writeoutletconfig` replaces. Reported so a report
                # can confirm what a write would have to echo back — see `OutletLimits`.
                "minimum_temperature_tenths": lim.minimum_temperature_tenths,
                "default_temperature_tenths": lim.default_temperature_tenths,
                "outlet_flags": lim.outlet_flags,
            }
            for outlet_id, lim in sorted(gcs.outlet_limits.items())
        },
        "last_update": gcs.last_update,
        "cloud_connected": valve.cloud_watch.connected,
        # `gcs-configuration`, read once at the first seed.
        #
        # **This block exists to settle an open question.** On the reference install — a
        # valve wired to an Anthem Plus controller — every structural field comes back
        # null, because such a valve reports its configuration through the controller, and
        # `docs/gcs/api.md` records that whether a **GCS-only** install populates them is
        # unknown and untested. A report from a controller-free account answers it.
        #
        # `field_names` rather than the values: the structural blocks would carry
        # installation detail, and what the question needs is which keys are populated, not
        # what is in them. Firmware is named in full because it is the useful part today.
        "configuration": _configuration_report(valve),
        "endless_shower": {
            "enabled": valve.restart_on_runtime_cutoff,
            "run_times_seconds": {
                str(k): v for k, v in sorted(valve.outlet_run_times.items())
            },
            "armed_zones": valve.armed_zones,
            "zones_awaiting_run_time": valve.zones_awaiting_run_time,
            "flowing_for_seconds": {
                str(zone): valve.zone_flowing_for(zone) for zone in model.zones
            },
        },
        "warmup": {
            "mode": gcs.warmup_mode,
            "auto_restore": valve.warmup_auto_restore,
            "restores_to": valve.last_warmup_mode,
        },
        # `currentSystemState` is reported above; `has_fault` folds in its `error` value,
        # which no capture has carried, so the first one shows in a report.
        "has_fault": gcs.has_fault,
        "dispensed_volume": gcs.dispensed_volume,
        "about": _about_report(valve.about_parts),
        "firmware": {
            part: _firmware_report(info)
            for part, info in (valve.firmware_info or {}).items()
        },
    }


def _controller_report(controller: Controller) -> dict[str, Any]:
    """One controller's state, capabilities and layout. Device id deliberately absent."""
    hub = controller.state
    caps = controller.capabilities
    model = controller.model
    settings = controller.settings
    return {
        # The layout this controller actually decodes with — its own, which can differ
        # from the entry's `model` above when the account has several controllers.
        "model": {
            "sku": model.sku,
            "outlets_valve1": model.outlets_valve1,
            "outlets_valve2": model.outlets_valve2,
        },
        "zones": {
            str(zone): {
                "status": getattr(z, "status", None),
                "outlets": list(getattr(z, "outlets", ()) or ()),
            }
            for zone, z in sorted(hub.zones.items())
        },
        "is_running": hub.is_running,
        "shower_warmup": hub.shower_warmup,
        "music_on": hub.music_on,
        "steam_on": hub.steam_on,
        "light_on": hub.light_on,
        "favorites_count": len(controller.favorites or []),
        "active_favorite": hub.active_favorite_id is not None,
        "capabilities": {
            "known": caps.known,
            "water": caps.water,
            "music": caps.music,
            "light": caps.light,
            "steam": caps.steam,
        },
        "last_update": hub.last_update,
        "steam_status": hub.steam_status,
        "light_groups": dict(hub.lights),
        "active_experience": hub.active_experience,
        "error": hub.error,
        "error_components": dict(hub.error_components),
        "active_errors": [
            {k: e.get(k) for k in ("errorCode", "title", "component", "isActive")}
            for e in controller.active_errors
        ],
        # The controller's own settings. The LAN address is reduced to its presence: it
        # identifies a home network, and a report needs only to know it was published.
        "settings": {
            "max_shower_duration_minutes": settings.max_shower_duration_minutes,
            "shower_max_temperature": settings.shower_max_temperature,
            "temperature_unit": settings.temperature_unit,
            "flow_rate_enabled": settings.flow_rate_enabled,
            "steam_default_temperature": settings.steam_default_temperature,
            "steam_default_time": settings.steam_default_time,
            "light_group_count": len(settings.light_groups),
            "lan_ip_present": settings.lan_ip is not None,
            "disconnected": list(settings.disconnected),
        },
        "experiences": {
            category: len(items) for category, items in controller.experiences.items()
        },
        "firmware": _firmware_report(controller.firmware_info),
    }


def _build(
    coordinator: KohlerAnthemCoordinator,
    requested_for: str,
    valve_index: int = 0,
    controller_index: int = 0,
) -> dict[str, Any]:
    """The whole installation, as this integration currently understands it.

    ``valve_index`` / ``controller_index`` say which device the singular ``valve`` /
    ``controller`` blocks should describe — the one whose Download-diagnostics button was
    pressed. The full ``valves`` / ``controllers`` lists are unaffected and always carry
    every device; see the note beside those blocks for why the singular keys still exist.
    """
    model = coordinator.model

    payload: dict[str, Any] = {
        "requested_for": requested_for,
        "model": {
            "sku": model.sku,
            "name": model.name,
            "outlets_valve1": model.outlets_valve1,
            "outlets_valve2": model.outlets_valve2,
            "total_outlets": model.total_outlets,
            "zones": model.zones,
        },
        "devices": {
            "valve_present": bool(coordinator.valves),
            "valve_count": len(coordinator.valves),
            "controller_present": bool(coordinator.controllers),
            # More than one is a configuration this project has never seen run; the count
            # is what tells a report from such an account apart.
            "controller_count": len(coordinator.controllers),
        },
        "entry": {
            "data": _redact_entry(coordinator, coordinator.entry.data),
            "options": _redact_entry(coordinator, coordinator.entry.options),
        },
        "stream": {
            "mqtt_connected": bool(coordinator.stream and coordinator.stream.connected),
        },
    }

    # One entry per valve, in the cloud's order. `valve`, `endless_shower` and `warmup`
    # (singular) are kept so reports from before 2026-09-08 and after read the same on a
    # single-valve account; `valves` carries all of them, each with its own
    # `endless_shower` and `warmup` nested inside.
    #
    # **The singular block describes the valve whose button was pressed, not always the
    # first.** Until 2026-09-09 it was hardcoded to index 0, so a report downloaded from
    # the second valve's page carried `requested_for: valve_1` above a `valve` block
    # describing valve 0 — two valves on one account can differ in exactly the fields this
    # block is read for (a 30-minute and a 60-minute run-time limit, on the account that
    # found this). The full picture was always present in `valves`; the label was the lie,
    # which is the failure mode this module's header exists to warn about.
    reports = [_valve_report(valve) for valve in coordinator.valves]
    if reports:
        # Defensive: an out-of-range index would be a caller bug, but a diagnostics report
        # that raises is a report nobody can attach to an issue.
        primary = (
            reports[valve_index] if 0 <= valve_index < len(reports) else reports[0]
        )
        payload["valve"] = {
            k: v for k, v in primary.items() if k not in ("endless_shower", "warmup")
        }
        payload["endless_shower"] = primary["endless_shower"]
        payload["warmup"] = primary["warmup"]
        payload["valves"] = reports

    # One entry per controller, in the cloud's order. `controller` (singular) is kept so
    # reports from before 2026-09-08 and after read the same on a single-controller
    # account; `controllers` carries all of them. Follows the pressed device for the same
    # reason as the valve block above.
    reports = [_controller_report(controller) for controller in coordinator.controllers]
    if reports:
        payload["controller"] = (
            reports[controller_index]
            if 0 <= controller_index < len(reports)
            else reports[0]
        )
        payload["controllers"] = reports

    return payload


def _coordinator(hass: HomeAssistant, entry: ConfigEntry) -> KohlerAnthemCoordinator:
    return hass.data[DOMAIN][entry.entry_id]


async def _async_add_fault_logs(
    coordinator: KohlerAnthemCoordinator, payload: dict[str, Any]
) -> dict[str, Any]:
    """Attach each valve's `gcs-diagnostics` fault log, read now.

    Read on demand rather than kept: it is only ever wanted at the moment someone is
    building a report, and the client answers `{}` on failure, so an unreachable endpoint
    costs an empty list rather than the report.
    """
    for index, valve in enumerate(coordinator.valves):
        log = _fault_log(await valve.client.async_get_gcs_diagnostics(valve.device_id))
        payload["valves"][index]["fault_log"] = log
        if index == 0 or payload.get("requested_for") == f"valve_{index}":
            payload.setdefault("valve", {})["fault_log"] = log
    return payload


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Diagnostics from the integration card."""
    coordinator = _coordinator(hass, entry)
    return await _async_add_fault_logs(coordinator, _build(coordinator, "config_entry"))


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Diagnostics from any device page.

    The whole-installation payload is the same whichever button was pressed — that is the
    point of this module. What follows the button is `requested_for` and the singular
    `valve` / `controller` blocks, which describe the device whose page it came from.
    """
    coordinator = _coordinator(hass, entry)
    requested_for = "unknown_device"
    # Default to the first of each, which is what the config-entry button reports and what
    # every single-device account has always produced.
    valve_index = 0
    controller_index = 0
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        for index, valve in enumerate(coordinator.valves):
            if identifier == valve.device_id:
                requested_for = (
                    "valve" if len(coordinator.valves) == 1 else f"valve_{index}"
                )
                valve_index = index
        for index, controller in enumerate(coordinator.controllers):
            if identifier == controller.device_id:
                # Plain "controller" with one, as every report so far has said; an index
                # into `controllers` when there are several, since ids are redacted.
                requested_for = (
                    "controller"
                    if len(coordinator.controllers) == 1
                    else f"controller_{index}"
                )
                controller_index = index
    return await _async_add_fault_logs(
        coordinator, _build(coordinator, requested_for, valve_index, controller_index)
    )
