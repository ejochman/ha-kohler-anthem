"""Valve status and diagnostics.

One headline entity, ``Status``, collapses what the valve is doing into a single value so a
dashboard needs one card rather than four booleans.

Everything else here is diagnostic and **disabled by default**: useful when something looks
wrong, noise otherwise. Enable them individually from the device page.

**No measured-temperature or measured-flow entities.** Bytes 4-6 of the status word carry
live sensor feedback, and on this hardware they read zero in every message ever captured —
including 239 with an outlet open. Entities that can only ever report ``unknown`` are noise,
so they were removed. The decode is intact and both values still appear as attributes on the
hex sensor, where a zero reads as data rather than as a broken entity.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .anthem.models import OutletStateSource, resolve_outlet_source
from .anthem.state import usage_series, usage_volume_gallons
from .anthem.valve_hex import encode_word
from .const import DOMAIN, EXPOSE_CONTROLLER_WATER_STATE
from .coordinator import Controller, KohlerAnthemCoordinator, Valve
from .entity import (
    KohlerControllerEntity,
    KohlerValveEntity,
    ZoneWordEntity,
    zone_label,
)

# US liquid gallons to litres. `totalFlow` is reported in US gallons; a `Liters` account
# is converted for display only — the filter and the stored total stay in gallons.
_LITERS_PER_GALLON = 3.785411784

# The four states the valve can be in, in priority order. "Warming Up" outranks "Water
# Running" because warmup does run water — reporting it as an ordinary shower would hide
# why the water started on its own.
STATE_RUNNING = "Water Running"
STATE_PAUSED = "Paused"
STATE_WARMING = "Warming Up"
STATE_IDLE = "Idle"
VALVE_STATES = [STATE_RUNNING, STATE_PAUSED, STATE_WARMING, STATE_IDLE]

# The controller's vocabulary is the valve's minus "Paused" — see `ControllerStatusSensor`.
# Same strings for the three it does have, so the two sensors can be compared directly and
# templated against interchangeably.
CONTROLLER_STATES = [STATE_RUNNING, STATE_WARMING, STATE_IDLE]


def _usage_bucket_date(interval: object) -> date | None:
    """Return the calendar date from a `gcs-usage` interval key."""
    if not isinstance(interval, str):
        return None
    try:
        return date.fromisoformat(interval[:10])
    except ValueError:
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the valve sensors, or the controller's where it is the only water source."""
    coordinator: KohlerAnthemCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    # One set per valve — each is its own device with its own state and layout.
    for valve in coordinator.valves:
        entities += [
            ValveStatusSensor(coordinator, valve),
            ValveSystemStateSensor(coordinator, valve),
            ValveMonthlyWaterSensor(coordinator, valve),
            ValveYearlyWaterSensor(coordinator, valve),
            ValveDailyWaterSensor(coordinator, valve),
            ValveWeeklyWaterSensor(coordinator, valve),
            ValveLastUpdateSensor(coordinator, valve),
            ValveFirmwareSensor(coordinator, valve),
            # The other two firmwares the Konnect app shows. Separate entities rather than
            # attributes: they update independently, and a valve that differs from its
            # sibling is the kind of thing worth being able to graph and alert on.
            ValveComponentFirmwareSensor(
                coordinator, valve, "primaryValve", "Valve Firmware", slug="valve"
            ),
            ValveComponentFirmwareSensor(
                coordinator, valve, "gateway", "Gateway Firmware", slug="gateway"
            ),
            ValveRegisteredSensor(coordinator, valve),
            ValveHexSensor(coordinator, valve, 1),
        ]
        if valve.model.uses_valve2:
            entities.append(ValveHexSensor(coordinator, valve, 2))
            # Only where a second valve exists: `about.secondaryValve1.firmware` reads `0`
            # on a single-valve system, which is a placeholder rather than a version.
            entities.append(
                ValveComponentFirmwareSensor(
                    coordinator,
                    valve,
                    "secondaryValve1",
                    "Second Valve Firmware",
                    slug="valve2",
                )
            )

    # Controller-only accounts get the zone temperature from SHOWER_VALVE_STS. Not created
    # where a valve exists: the valve reports its own setpoint per zone, and the controller
    # goes stale the moment the valve is driven directly.
    source = resolve_outlet_source(
        bool(coordinator.valves), bool(coordinator.controllers)
    )
    controller_water = source is OutletStateSource.HUB_MQTT or (
        bool(coordinator.controllers) and EXPOSE_CONTROLLER_WATER_STATE
    )
    # One set per controller: each is its own device with its own state and — since a
    # second bathroom need not have the same valve — its own outlet layout.
    for controller in coordinator.controllers:
        # Diagnostic, and about the controller's *reporting* rather than the water, so it is
        # created for every controller — unlike everything gated below.
        entities.append(ControllerLastUpdateSensor(coordinator, controller))
        # The controller's own cap on a shower. Read from `hub-configuration`, so created
        # for every controller.
        entities.append(ControllerMaxShowerDurationSensor(coordinator, controller))

        if controller_water:
            entities += [
                ControllerZoneTemperatureSensor(coordinator, controller, zone)
                for zone in controller.model.zones
            ]
            # Same gate as the outlet sensors, and for the same reason: this reports water
            # state from the controller, which contradicts the valve during a valve-driven
            # session (`status: OFF` with an all-zero outlet array while water runs). On a
            # controller-only account it is the authoritative answer; alongside a valve it
            # is a comparison tool.
            entities.append(ControllerStatusSensor(coordinator, controller))

    async_add_entities(entities)


class ValveStatusSensor(KohlerValveEntity, SensorEntity):
    """What the shower is doing right now, as one value.

    The headline entity, and **the one place the two devices are deliberately merged.**
    Everything else water-related on a both-devices account reads the valve alone, because
    the controller cannot see a valve-driven session and would contradict it. Warm-up is the
    exception: the valve and the controller each have their *own* warm-up function, and
    either one running means water is about to move. Reading only the valve would miss a
    controller-initiated warm-up entirely.

    That asymmetry is intentional and runs one way only. `ControllerStatusSensor` stays
    purely HUB-derived — it exists to show what the controller believes, and folding valve
    state into it would destroy the comparison it is there to provide.

    **Named `System Status` since 0.19.0**, because system-level is exactly what it is:
    warm-up and pause are properties of the whole valve on this hardware, not of a zone, so
    there is no per-zone `Status` this could ever be one of. On a multi-zone valve
    `Shower Active 1` / `Shower Active 2` answer the per-zone question beside it.

    ⚠️ **The unique id stays `_status`.** A rename that moved the id would orphan every
    automation and all recorded history; the entity id follows the device and name only if
    the owner has never customised it, which Home Assistant handles on its own.
    """

    _attr_name = "System Status"
    _attr_icon = "mdi:shower"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = VALVE_STATES

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_status"

    @property
    def _hub_warmup(self) -> bool:
        """Whether the controller reports a warm-up of its own.

        From `data.showerwarmup` on the controller's `SHOWER_VALVE_STS`. False on a
        valve-only account, where there is no controller to ask — and False on an account
        with **several** controllers or several valves, where there is no way to tell
        which controller fronts this valve: `hub-configuration` names no valve, so merging
        a controller's warm-up would report the guest bathroom's as this shower's. There
        the valve is read alone, and `controller_warmup` below says so with a None.
        """
        if not self._paired:
            return False
        return bool(self.coordinator.controllers[0].state.shower_warmup)

    @property
    def _paired(self) -> bool:
        """Whether the account has exactly one valve and one controller — the only case in
        which the two can be assumed to be the same shower."""
        return (
            len(self.coordinator.controllers) == 1 and len(self.coordinator.valves) == 1
        )

    @property
    def native_value(self) -> str | None:
        state = self._state
        if state is None or state.valve1 is None:
            return None
        # Order matters: a paused session still has a temperature and outlets configured,
        # and warmup runs water without anyone having started a shower.
        if state.is_paused:
            return STATE_PAUSED
        if state.warmup_in_progress or self._hub_warmup:
            return STATE_WARMING
        if state.is_running:
            return STATE_RUNNING
        return STATE_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Which device claimed the warm-up, so a merged value stays explainable.

        Without this, "Warming Up" on a both-devices account gives no clue which warm-up is
        running — and the two are independent, so the answer is genuinely useful when the
        wall panel and Home Assistant appear to disagree.
        """
        state = self._state
        attributes: dict[str, object] = {
            "valve_warmup": bool(state and state.warmup_in_progress),
            # None — "not asked", not "no" — when the pairing is ambiguous; see
            # `_hub_warmup`. False on a valve-only account, as it always was.
            "controller_warmup": (
                self._hub_warmup
                if self._paired or not self.coordinator.controllers
                else None
            ),
        }
        attributes.update(self._time_left())
        return attributes

    def _time_left(self) -> dict[str, object]:
        """How long water has been running, and how long before the valve cuts it off.

        **Moved here from `Shower Active` in 0.19.0**, which a single-zone valve no longer
        has. `seconds_remaining` is the number that matters mid-shower and nothing else
        publishes it, so it had to outlive that entity rather than go with it.

        System-level, by taking the **soonest** cutoff across zones: with two zones running
        the first one to stop is the one worth warning about, and a maximum would promise
        time that one of the showers is not going to get. Multi-zone valves keep the
        per-zone figures on `Shower Active`, which is where "which zone" gets answered.

        None — never 0 — when the limit is unknown, when nothing is flowing, or after a
        reconnect: the zone clocks are dropped across a gap rather than reporting a duration
        they cannot stand behind, and a zero would read as "cutoff imminent".
        """
        flowing: list[float] = []
        remaining: list[float] = []
        for zone in self._valve.model.zones:
            elapsed = self._valve.zone_flowing_for(zone)
            if elapsed is None:
                continue
            flowing.append(elapsed)
            limits = self._valve.run_time_limits_for_zone(zone)
            if limits:
                remaining.append(min(limits) - elapsed)
        return {
            "flowing_for_seconds": round(max(flowing), 1) if flowing else None,
            "seconds_remaining": round(min(remaining), 1) if remaining else None,
        }


class ValveSystemStateSensor(KohlerValveEntity, SensorEntity):
    """The valve's own `currentSystemState` — `normalOperation` or `showerInProgress`.

    Two more are declared since 2026-10-07 because Konnect 3.0.6 acts on them, though no
    capture has carried either: ``error`` (a valve fault — the Problem sensor turns on too)
    and ``FirmwareUpdate`` (an install in progress). Before, both would have read unknown.

    **A second opinion, not a restatement of `Status`.** `Status` is decoded from the
    command word — outlet mask, pause flag, warm-up — whereas this is a flag the valve
    sets for itself. They usually agree, and when they do not, that disagreement is the
    useful signal: it is the valve saying a session is open while the word says no outlet
    is flowing, or the reverse.

    Reported as the device's own strings rather than remapped onto `VALVE_STATES`. Folding
    them into the same four words would make the two sensors look interchangeable, which
    is exactly the confusion this one exists to expose. Automations that only want "is a
    shower on" should read `Status`.
    """

    _attr_name = "System State"
    _attr_icon = "mdi:state-machine"
    _attr_device_class = SensorDeviceClass.ENUM
    # The two values observed across the whole capture corpus, plus the two Konnect 3.0.6
    # handles. An unrecognised value is published as-is by returning None below rather
    # than being forced into this list, since an ENUM sensor reporting an option it never
    # declared is logged as an error by Home Assistant on every single update.
    _attr_options: ClassVar[list[str]] = [
        "normalOperation",
        "showerInProgress",
        "error",
        "FirmwareUpdate",
    ]

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_system_state"

    @property
    def native_value(self) -> str | None:
        state = self._state
        if state is None or state.system_state is None:
            return None
        value = state.system_state
        # The app compares `error` case-insensitively; so does this.
        if value.strip().lower() == "error":
            return "error"
        # Never hand HA an option outside `_attr_options` — see the class docstring. A
        # firmware that adds another state shows as `unknown` here and in the attribute
        # below as its real string, rather than spamming the log.
        return value if value in self._attr_options else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        reported = None if state is None else state.system_state
        return {
            # What the valve actually said, including a value this integration does not
            # yet know about.
            "reported": reported,
            "recognised": reported in self._attr_options if reported else None,
        }


class ValveMonthlyWaterSensor(KohlerValveEntity, SensorEntity):
    """Water used in the current calendar month, from Kohler's own usage history.

    **This is the number the Konnect app charts**, not a figure derived from the lifetime
    counter. It comes from `gcs-usage`, whose per-month series the app reads and which no
    other integration calls — its query parameters are PascalCase where the rest of the API
    is camelCase, so it answers a generic 400 to anything else and had gone unsolved.

    `volume` arrives in **litres** whatever the account's unit setting; the app converts with
    0.264172 when `waterUnits` is `Standard`, and this follows that exactly so the value
    matches the app rather than merely being close.

    **Read once at setup, not polled.** A monthly total moves slowly and Kohler's own chart
    is not live either, so this refreshes when Home Assistant restarts or the entry reloads.
    `TOTAL` rather than `TOTAL_INCREASING`: the figure resets each month by design, and
    telling Home Assistant otherwise would make every month boundary look like a meter swap.
    """

    _attr_name = "Water Used This Month"
    _attr_icon = "mdi:calendar-month"
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_water_this_month"
        # **Rendered once, not per message.** Every MQTT message re-renders every entity, and
        # this one's attributes are a 13-month dict built from data read once at setup and
        # never again. Recomputing it per message cost two linear scans, two `strftime` calls
        # and thirteen conversions — and, because attributes are serialised into the state
        # machine and the recorder, wrote that churn to the database on every message.
        #
        # Keyed on the identity of the usage payload, so a re-seed that replaces it
        # invalidates the cache without needing an explicit hook.
        self._cache_key: int | None = None
        self._cached: tuple[dict[str, Any] | None, dict[str, Any]] = (None, {})

    @property
    def _metric(self) -> bool:
        return self.coordinator.water_units == "Liters"

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfVolume.LITERS if self._metric else UnitOfVolume.GALLONS

    def _rendered(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """This month's entry and the attribute dict, built once per usage payload.

        The month is matched on `intervalKey` rather than taken as the last entry: the series
        can end on a month the valve reported nothing for, and trusting position would then
        publish a stale month's total as the current one.
        """
        usage = self._valve.usage
        key = id(usage)
        if key == self._cache_key:
            return self._cached

        entries = usage_series(usage)
        month = datetime.now(UTC).strftime("%Y-%m")
        current = next(
            (entry for entry in entries if entry.get("intervalKey") == month), None
        )

        attributes: dict[str, Any] = {}
        if entries:
            attributes["history"] = {
                str(entry.get("intervalKey")): self._volume(entry)
                for entry in entries
                if entry.get("intervalKey")
            }
        if current is not None:
            attributes["month"] = current.get("intervalKey")
            duration = current.get("onDuration")
            if isinstance(duration, (int, float)):
                # Seconds on the wire; minutes is what a shower is measured in.
                attributes["running_minutes"] = round(float(duration) / 60, 1)

        self._cache_key = key
        self._cached = (current, attributes)
        return self._cached

    def _volume(self, entry: dict[str, Any]) -> float | None:
        """One entry's volume in the account's unit, or None when it is not a number."""
        litres = entry.get("volume")
        if not isinstance(litres, (int, float)):
            return None
        value = float(litres) if self._metric else usage_volume_gallons(float(litres))
        return round(value, 1)

    @property
    def _current(self) -> dict[str, Any] | None:
        return self._rendered()[0]

    @property
    def native_value(self) -> float | None:
        entry = self._current
        return None if entry is None else self._volume(entry)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The month this covers, how long the valve ran, and the series behind it.

        `history` is every month Kohler returned, in the account's unit — enough to answer
        "how does this month compare" without a second call, and the reason the whole series
        is fetched rather than only the current month. Built once per payload; see
        `_rendered`.
        """
        return self._rendered()[1]


class ValveDiagnosticSensor(KohlerValveEntity, SensorEntity):
    """Base for the diagnostics: hidden unless deliberately enabled."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False


class ValveYearlyWaterSensor(KohlerValveEntity, SensorEntity):
    """Water used over the last twelve complete months, from Kohler's own usage history.

    **The way out of the lifetime-counter problem.** The valve's `totalVolume` is a real
    meter — an isolated 2.4 gallon session moved it by 19 counts, so it counts eighths of a
    US gallon — but its absolute value is around 537 million, which at that unit would be 67
    million gallons and cannot be what it claims. Something else is packed into the field and
    nobody has established what, so a lifetime total derived from it would be a guess.

    This needs none of that. `gcs-usage` returns a per-month series in **litres**, which is
    the same data the Konnect app charts, and summing twelve of its entries is arithmetic on
    values whose unit is already settled. A year is not a lifetime, but it is a real number
    that matches the app — which a wrong lifetime figure would not be.

    **Complete months only.** The current partial month is excluded, so the value does not
    creep upward through the month and then drop when the window rolls: it changes once, at a
    month boundary, which is what `TOTAL` means. `Water Used This Month` covers the partial
    month beside it.

    Read once at setup with the rest of the usage series — no extra API call, and no polling.
    """

    _attr_name = "Water Used This Year"
    _attr_icon = "mdi:calendar-range"
    _attr_device_class = SensorDeviceClass.WATER
    # `TOTAL`, not `TOTAL_INCREASING`: a rolling window falls whenever the month dropping off
    # the back was wetter than the one joining, and calling that a meter reset would inject a
    # phantom year of water into long-term statistics.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_water_this_year"
        # Cached on the payload's identity, exactly as the monthly sensor is: every MQTT
        # message re-renders every entity, and this sums a 13-entry series that changes only
        # when the entry reloads.
        self._cache_key: int | None = None
        self._cached: tuple[float | None, dict[str, Any]] = (None, {})

    @property
    def _metric(self) -> bool:
        return self.coordinator.water_units == "Liters"

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfVolume.LITERS if self._metric else UnitOfVolume.GALLONS

    def _rendered(self) -> tuple[float | None, dict[str, Any]]:
        usage = self._valve.usage
        key = id(usage)
        if key == self._cache_key:
            return self._cached

        this_month = datetime.now(UTC).strftime("%Y-%m")
        months: dict[str, float] = {}
        for entry in usage_series(usage):
            interval = entry.get("intervalKey")
            # The current month is deliberately excluded — see the class docstring.
            if not interval or str(interval) >= this_month:
                continue
            litres = entry.get("volume")
            if isinstance(litres, (int, float)):
                months[str(interval)] = float(litres)

        total: float | None = None
        attributes: dict[str, Any] = {}
        if months:
            # The twelve most recent complete months. Fewer where the account is younger, and
            # `months_counted` says so rather than letting a short series read as a low year.
            recent = sorted(months)[-12:]
            litres = sum(months[key_] for key_ in recent)
            value = litres if self._metric else usage_volume_gallons(litres)
            total = round(value, 1)
            attributes = {
                "months_counted": len(recent),
                "first_month": recent[0],
                "last_month": recent[-1],
                "excludes_current_month": this_month,
            }

        self._cache_key = key
        self._cached = (total, attributes)
        return self._cached

    @property
    def native_value(self) -> float | None:
        return self._rendered()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._rendered()[1]


class _DailyWaterSensor(KohlerValveEntity, SensorEntity):
    """Shared base for the day- and week-scoped water totals.

    Both read the same `Interval=DAY` series and differ only in how many trailing days they
    sum, so the parsing, the unit handling and the cache live here once.

    **`DAY` is the only sub-monthly interval this endpoint serves.** `WEEK` was refused at
    400, 90 and 28 days on 2026-09-11 — the last being four buckets, against the fifteen
    `DAY` returned happily in the same run — so a week is seven daily buckets rather than a
    `WEEK` call. `docs/gcs/api.md` records the whole probe.

    `volume` is litres on the wire whatever the account's unit, exactly as in the monthly
    series; the account's `waterUnits` decides only the display. Verified the day this was
    written: the daily entries for the current month summed to the same 465 L the `MONTH`
    series reported for it.
    """

    _attr_device_class = SensorDeviceClass.WATER
    # `TOTAL`, not `TOTAL_INCREASING`: both of these reset — one at midnight, one as the
    # window rolls — and calling that a meter reset would inject phantom water into
    # long-term statistics.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_registry_enabled_default = True

    #: Trailing days to sum, counting today.
    _days: int = 1

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._cache_key: tuple[int, date, date] | None = None
        self._cached: tuple[float | None, dict[str, Any]] = (None, {})

    @property
    def _metric(self) -> bool:
        return self.coordinator.water_units == "Liters"

    @property
    def native_unit_of_measurement(self) -> str:
        return UnitOfVolume.LITERS if self._metric else UnitOfVolume.GALLONS

    def _rendered(self) -> tuple[float | None, dict[str, Any]]:
        """Sum the trailing `_days` buckets, cached on the payload's identity.

        Cached the same way the monthly sensors are: every MQTT message re-renders every
        entity, and this series changes only when a shower ends.
        """
        usage = self._valve.usage_daily
        local_today = dt_util.now().date()
        utc_today = datetime.now(UTC).date()
        key = (id(usage), local_today, utc_today)
        if key == self._cache_key:
            return self._cached

        # **Local dates, not UTC.** "Today" is the owner's today; the series keys its
        # buckets by calendar date, and comparing them against a UTC date would roll the
        # day over at the wrong hour for most of the world.
        wanted = [local_today - timedelta(days=offset) for offset in range(self._days)]

        litres = 0.0
        days: dict[str, float] = {}
        volumes: dict[date, float] = {}
        # `numberOfTimesValveSwitchedOn` rides in every bucket beside `volume` — how many
        # times the shower was turned on that day. A plain count, so published as is.
        # (`averageBlendTemperature` rides there too, but nothing — the app included —
        # states its unit, so it is left out rather than shown in a guessed one.)
        starts: dict[date, int] = {}
        for entry in usage_series(usage):
            interval = entry.get("intervalKey")
            bucket = _usage_bucket_date(interval)
            if bucket is None:
                continue
            volume = entry.get("volume")
            if not isinstance(volume, (int, float)):
                continue
            volumes[bucket] = volumes.get(bucket, 0.0) + float(volume)
            count = entry.get("numberOfTimesValveSwitchedOn")
            if isinstance(count, (int, float)) or (
                isinstance(count, str) and count.isdigit()
            ):
                starts[bucket] = starts.get(bucket, 0) + int(count)

        turned_on: int | None = None
        for day in wanted:
            bucket = day
            # Some accounts appear to expose the daily chart on UTC bucket labels. In US
            # evenings that can put local "today" under tomorrow's ISO date, which used to
            # render as zero/unknown even after water was used.
            if (
                day == local_today
                and utc_today > local_today
                and volumes.get(day, 0.0) == 0
                and volumes.get(utc_today, 0.0) > 0
            ):
                bucket = utc_today
            volume = volumes.get(bucket)
            if volume is None:
                continue
            if bucket in starts:
                turned_on = (turned_on or 0) + starts[bucket]
            litres += volume
            days[bucket.isoformat()] = round(
                volume if self._metric else usage_volume_gallons(volume),
                1,
            )

        total: float | None = None
        attributes: dict[str, Any] = {}
        if days:
            value = litres if self._metric else usage_volume_gallons(litres)
            total = round(value, 1)
            attributes = {"days_counted": len(days)}
            if turned_on is not None:
                attributes["times_turned_on"] = turned_on
            if self._days == 1 and days:
                bucket_date = next(iter(days))
                if bucket_date != local_today.isoformat():
                    attributes["bucket_date"] = bucket_date
            if self._days > 1:
                # The per-day breakdown is the point of a rolling window: it says which day
                # the water went, which a single figure cannot.
                attributes["per_day"] = dict(sorted(days.items()))
                attributes["window_days"] = self._days

        self._cache_key = key
        self._cached = (total, attributes)
        return self._cached

    @property
    def native_value(self) -> float | None:
        return self._rendered()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._rendered()[1]


class ValveDailyWaterSensor(_DailyWaterSensor):
    """Water used today, from Kohler's own per-day usage series.

    **Refreshed when a shower ends, not on a clock.** Usage moves only while water runs, so
    the read is tied to the running -> stopped edge; a day with no shower costs no calls.
    There is a short delay first, because Kohler aggregates the session after the valve
    reports it closed — see `USAGE_REFRESH_DELAY_SECONDS`.
    """

    _attr_name = "Water Used Today"
    _attr_icon = "mdi:water-check"
    _days = 1

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_water_today"


class ValveWeeklyWaterSensor(_DailyWaterSensor):
    """Water used over the last seven days, today included.

    A rolling seven-day window rather than a calendar week: `Interval=WEEK` is refused by
    this endpoint (see `_DailyWaterSensor`), and a rolling week answers "how much have we
    used lately" without depending on which day Kohler would have called the start.
    """

    _attr_name = "Water Used This Week"
    _attr_icon = "mdi:calendar-week"
    _days = 7

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_water_this_week"


class ValveLastUpdateSensor(ValveDiagnosticSensor):
    """When the valve last reported.

    Carries the event's own timestamp rather than relying on ``last_changed``, which Home
    Assistant stamps when it writes the state — so a restart would otherwise reset it to
    the restart time.
    """

    _attr_name = "Last Update"
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_last_update"

    @property
    def native_value(self) -> datetime | None:
        state = self._state
        if state is None or state.last_update is None:
            return None
        return datetime.fromtimestamp(state.last_update, tz=UTC)


class ValveFirmwareSensor(ValveDiagnosticSensor):
    """The **interface** firmware — the touchscreen's own version.

    One of three, and the reason there are now three: the Konnect app shows an interface
    version, a valve version and a gateway version for a single shower, and they are
    genuinely different numbers (2.2, 10 and 00.74 on the reference hardware). Until 0.11.0
    this was a lone `Firmware` entity reporting whichever version it found first, which on
    one valve was the **artwork bundle** version — `2.00` where the app showed 2.2.

    Keeps the `_firmware` unique id it has always had, so the entity, its history and any
    automation referring to it survive the split. Its *name* changes from `Firmware` to
    `Interface Firmware`, which is what it always meant.

    Reads `unknown` where the record carries no interface version, which is honest: a blank
    is better than a confidently wrong version in a bug report, and a report from such an
    install carries `version_fields`, which is what a fix needs.
    """

    _attr_name = "Interface Firmware"
    _attr_icon = "mdi:monitor"

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_firmware"

    @property
    def native_value(self) -> str | None:
        return self._valve.firmware


class ValveComponentFirmwareSensor(ValveDiagnosticSensor):
    """The firmware of one named part of the system — the valve or the gateway.

    ⚠️ **Two valves on one account can be on different firmware, and the app hides it.** The
    reference system reads `10` on one valve and `11` on the other while Konnect shows 10 for
    both. Two showers that should be identical are not, and nothing else surfaces that.

    The gateway version is per-account rather than per-valve — both valves report the same
    `00.74` — but it is published on each valve's device anyway, because that is where a
    reader looking at one shower will look for it, and a single shared entity would have no
    obvious device to live on.
    """

    _attr_icon = "mdi:chip"

    def __init__(
        self,
        coordinator: KohlerAnthemCoordinator,
        valve: Valve,
        component: str,
        name: str,
        *,
        slug: str,
    ) -> None:
        super().__init__(coordinator, valve)
        self._component = component
        self._attr_name = name
        self._attr_unique_id = f"{self._device_id}_firmware_{slug}"

    @property
    def native_value(self) -> str | None:
        return self._valve.component_firmware(self._component)


class ValveRegisteredSensor(ValveDiagnosticSensor):
    """When Kohler's cloud first created this valve's record — ``createdTime``.

    **This is a registration date, not an installation date**, and the distinction is not
    pedantic: it is when the device row appeared in Kohler's cloud, so a valve replaced
    under warranty or re-registered after a service call reads as newer than the plumbing.
    For most systems the two are within a day of each other, which is what makes it useful;
    the name says which one it actually is.

    The only date the API carries. Nothing else — the device list, the state reads, the
    preset records, the MQTT stream — reports one at all.

    Accepts the two shapes a JSON timestamp arrives in, an ISO-8601 string or epoch
    milliseconds, because only one install's payload has ever been seen and a sensor that
    breaks on the other would be a poor trade for a few lines.
    """

    _attr_name = "Registered"
    _attr_icon = "mdi:calendar-clock"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: KohlerAnthemCoordinator, valve: Valve) -> None:
        super().__init__(coordinator, valve)
        self._attr_unique_id = f"{self._device_id}_registered"

    @property
    def native_value(self) -> datetime | None:
        raw = self._valve.created_time
        if raw is None:
            return None
        text = str(raw).strip()

        # Epoch, seconds or milliseconds.
        if text.isdigit():
            epoch = int(text)
            if epoch > 10**12:
                epoch //= 1000
            try:
                return datetime.fromtimestamp(epoch, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return None

        # ISO-8601. `fromisoformat` handles `Z` only from Python 3.11, and Home Assistant
        # supports older runtimes, so the suffix is normalised first.
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        # A timestamp device class requires an aware datetime; a naive one from the cloud
        # is UTC, which is what every other date this API returns has been.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """The raw string, so an unparsed format is diagnosable rather than just blank."""
        return {"reported": self._valve.created_time}


class ValveHexSensor(ZoneWordEntity, ValveDiagnosticSensor):
    """The valve command word for one zone.

    The single most useful thing to look at when behaviour is surprising: it shows exactly
    what the valve believes, before any decoding. It is also the **intended way to build a
    `kohler_anthem.send_valve_hex` call** — set the shower up with the ordinary outlet
    switches and temperature controls, read the word off here, and paste it into the service.

    **Reports the 8-character command half**, uppercased. Two deliberate normalisations:

    * *Truncated* — the device sends 16 characters, whose second half is live sensor
      feedback. On this hardware measured temperature and flow read zero in every message
      ever captured, so the extra half is 8 zeroes that only make the value harder to copy.
      Nothing is lost: `measured_temperature_celsius`, `measured_flow_percent`, `error_code`
      and `error_flag` are all still published as attributes.
    * *Uppercased* — the device sends lowercase, `encode_word` emits uppercase. Before this,
      the sensor flipped case depending on whether the value arrived over MQTT or came from
      the REST seed, which is a poor thing to ask anyone to copy from.

    It must never rebuild the string from the decoded fields. The version that did was
    written against the superseded ``25.6 + byte1/10`` temperature reading and kept it after
    the codec moved to the 10-bit encoding. The two agree between 25.6 °C and 51.1 °C, so it
    looked right in every ordinary shower and produced nonsense at the edges — a 0 °C
    "full cold" setpoint, which the hardware genuinely accepts, rendered its temperature
    byte as ``-100``. So the truncation above is a slice of `raw`, never a re-encode.
    """

    _attr_icon = "mdi:hexadecimal"

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, valve: Valve, zone: int
    ) -> None:
        # `_zone` and `_word` come from `ZoneWordEntity`.
        super().__init__(coordinator, valve, zone)
        # Same rule as the temperature and flow numbers: no prefix where there is only one
        # zone to name. See `entity.zone_label`.
        self._attr_name = zone_label(valve, zone, "Hex")
        self._attr_unique_id = f"{self._device_id}_zone_{zone}_hex"

    @property
    def native_value(self) -> str | None:
        word = self._word
        if word is None:
            return None
        # A REST-seeded word has no wire string. Encode the command half with the current
        # codec rather than showing nothing, so the sensor is useful before the first MQTT
        # message arrives after a restart. `encode_word` already returns 8 uppercase chars.
        if not word.raw:
            return encode_word(
                word.prefix,
                word.temperature_celsius,
                word.flow_percent,
                word.outlet_mask,
                paused=word.paused,
            )
        # Slice, never re-encode — see the class docstring.
        return word.raw[:8].upper()

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        word = self._word
        if word is None:
            return {}
        return {
            "temperature_celsius": word.temperature_celsius,
            "flow_percent": word.flow_percent,
            # `flow_percent` is reported exactly as the word carries it — this sensor's job
            # is to be faithful to the raw word — but on an **idle** valve that number is not
            # the commanded flow. The corpus has 296 such words, in recurring pairs like
            # 34.5%/82.5%, with `totalFlow` collapsing to 2 in the same message and
            # everything back to normal seconds later. So the value is flagged rather than
            # hidden: read `flow_percent` only when this is True.
            "flow_is_live": bool(word.outlet_mask) and not word.paused,
            "flow_setpoint": word.flow_setpoint,
            "outlet_mask": f"0x{word.outlet_mask:02X}",
            "paused": word.paused,
            "prefix": f"0x{word.prefix:02X}",
            "at_temperature": word.at_temperature,
            "at_flow": word.at_flow,
            "error_flag": word.error_flag,
            "error_code": word.error_code,
            "measured_temperature_celsius": word.measured_temperature_celsius,
            "measured_flow_percent": word.measured_flow_percent,
            # Absent on a REST-seeded word, present on anything from MQTT.
            "from_device": bool(word.raw),
        }


class ControllerDiagnosticSensor(KohlerControllerEntity, SensorEntity):
    """Base for controller diagnostics: hidden unless deliberately enabled."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False


class ControllerLastUpdateSensor(ControllerDiagnosticSensor):
    """When the controller last reported.

    The counterpart to ``sensor.anthem_valve_last_update``, and **not redundant with it** —
    the two devices report on entirely separate schedules. The controller can go a whole
    valve-driven session without saying anything (32 of 95 measured episodes), so a
    controller timestamp that lags the valve's by an hour is normal here rather than a
    fault.

    Carries the event's own timestamp rather than relying on ``last_changed``, which Home
    Assistant stamps when it writes the state — so a restart would otherwise reset it to the
    restart time.
    """

    _attr_name = "Last Update"
    _attr_icon = "mdi:clock-check-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_last_update"

    @property
    def native_value(self) -> datetime | None:
        state = self._state
        if state is None or state.last_update is None:
            return None
        return datetime.fromtimestamp(state.last_update, tz=UTC)


class ControllerMaxShowerDurationSensor(ControllerDiagnosticSensor):
    """The controller's Max Shower Duration, in minutes.

    From ``hub-configuration`` ``systemSettings.maxShowerDuration`` — readable from the
    cloud, which this integration did not know until Konnect 3.0.6 showed the app reading it
    (2026-10-07). The controller times a shower independently of the valve's own Max
    Shower Duration, and whichever is shorter ends it, so this explains a shower that stops
    earlier than the valve's setting says. Re-read on each reconnect, not live: an edit on
    the controller shows here after the next one.

    Enabled by default, unlike the other controller diagnostics, because it is a setting
    someone may need to act on rather than a protocol curiosity.
    """

    _attr_name = "Max Shower Duration"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_registry_enabled_default = True

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_max_shower_duration"

    @property
    def native_value(self) -> int | None:
        return self._controller.settings.max_shower_duration_minutes


class ControllerStatusSensor(KohlerControllerEntity, SensorEntity):
    """What the controller believes the shower is doing, from the controller's own data.

    Deliberately **three states, not four: the controller has no concept of "Paused".** That
    is not an omission here, it is absent from the protocol. Across 466 HUB messages in 32
    capture sessions, no attribute key resembling pause, hold, or suspend appears anywhere,
    and per-zone ``status`` takes exactly two values, ``ON`` and ``OFF`` — 264 and 256
    observations. Pause is a GCS concept: bit ``0x40`` of the valve command word. A paused
    session therefore surfaces here as ``Idle``, and the only way to distinguish it is
    ``sensor.anthem_valve_system_status``, which is on the other device by design.

    Sources, all HUB-native — nothing here reads the valve:

    * **Warming Up** — ``data.showerwarmup`` on ``SHOWER_VALVE_STS``
    * **Water Running** — any zone's ``status`` is ``ON``
    * **Idle** — everything else

    Warm-up outranks running because warm-up *is* running water: all 9 observed warm-up
    messages also had both zones ON, so testing "running" first would mask every one of them.

    **Named `System Status` since 0.19.0**, matching the valve's own. It sits on a
    different device, so the two do not collide; the unique id is unchanged, for the reason
    given on :class:`ValveStatusSensor`.

    Seeded from the ``hub-state`` REST read at setup and on each reconnect, then driven by
    MQTT — the same path as every other controller entity. Without that seed it would read
    ``unknown`` from every restart until the controller next said something, which has been
    as long as 11.9 hours.
    """

    _attr_name = "System Status"
    _attr_icon = "mdi:shower"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONTROLLER_STATES

    def __init__(
        self, coordinator: KohlerAnthemCoordinator, controller: Controller
    ) -> None:
        super().__init__(coordinator, controller)
        self._attr_unique_id = f"{self._device_id}_status"

    @property
    def native_value(self) -> str | None:
        state = self._state
        if state is None:
            return None
        if state.shower_warmup:
            return STATE_WARMING
        if state.is_running:
            return STATE_RUNNING
        return STATE_IDLE

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        state = self._state
        if state is None:
            return {}
        return {
            # Exposed so a stale or never-populated warm-up reads as data rather than as a
            # confident False: None means no message has ever carried the field.
            "shower_warmup": state.shower_warmup,
            "zone_status": {
                number: zone.status for number, zone in sorted(state.zones.items())
            },
            # A standing reminder on the entity itself that this cannot say "Paused".
            "supports_paused": False,
        }


class ControllerZoneTemperatureSensor(KohlerControllerEntity, SensorEntity):
    """Temperature the controller reports for one zone.

    From ``SHOWER_VALVE_STS``, and **only created on a controller-only account** — where a
    valve exists, its own per-zone setpoint is authoritative and this would go stale the
    moment the valve is driven directly.

    Reported in the **account's** unit rather than Celsius: unlike the GCS valve word, which
    is always tenths of a degree Celsius, the controller sends whatever unit the account is
    configured for. Captured values of ``102`` alongside a 38.8 °C valve setpoint confirm it
    is following the Fahrenheit preference.

    ``null`` while the zone is off, which is why this has no ``state_class`` — it is a live
    reading, not a statistic, and gaps are normal rather than missing data.
    """

    _attr_device_class = SensorDeviceClass.TEMPERATURE

    def __init__(
        self,
        coordinator: KohlerAnthemCoordinator,
        controller: Controller,
        zone: int,
    ) -> None:
        super().__init__(coordinator, controller)
        self._zone = zone
        self._attr_name = zone_label(controller, zone, "Temperature")
        self._attr_unique_id = f"{self._device_id}_zone_{zone}_temperature"
        fahrenheit = coordinator.temperature_unit.lower().startswith("f")
        self._attr_native_unit_of_measurement = (
            UnitOfTemperature.FAHRENHEIT if fahrenheit else UnitOfTemperature.CELSIUS
        )

    @property
    def native_value(self) -> float | None:
        state = self._state
        zone = None if state is None else state.zones.get(self._zone)
        if zone is None or zone.temperature is None:
            return None
        try:
            return float(zone.temperature)
        except (TypeError, ValueError):
            return None
