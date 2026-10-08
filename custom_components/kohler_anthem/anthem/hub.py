"""Commands for the Anthem Plus system controller (SKU ``HUB``).

The HUB is favorite-centric. There is **no direct "set outlet/temperature/flow now"**
command: to run a specific configuration you create or edit a *favorite* and activate it.
The only direct commands are bare on/off for the controller's own stored default
(``valvecontrol`` / ``steamcontrol``) and ``stopall``. Konnect 3.0.6 confirms it: nine HUB
command paths plus ``hub/factoryreset``, and no light, music, volume or temperature command
anywhere in the app.

Two constraints shape every caller:

* **Editing a favorite is rejected while the system runs** (``statusCode 902``), surfaced
  as :class:`~.client.DeviceRunning`. Activating one is allowed at any time — so the
  practical pattern is to pre-create a favorite per state you want and switch between
  them by activation, never editing at runtime.
* **An all-off favorite is not the same as ``stopall``.** Activating an empty favorite
  stops the outputs but leaves the system reporting that favorite as running; only
  ``stopall`` fully idles it.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from .client import KohlerClient
from .const import (
    EXPERIENCE_ENDPOINTS,
    HUB_FAVORITE,
    HUB_FAVORITE_CONTROL,
    HUB_STEAM_CONTROL,
    HUB_STOP_ALL,
    HUB_VALVE_CONTROL,
    SKU_HUB,
)
from .models import ValveModel

ON = "ON"
OFF = "OFF"

# Read and write disagree on shape, which is a standing source of bugs:
#   READ  water.zoneN.outlets     = a COUNT, and outletState = a 6-slot array
#   WRITE water.zoneN.outlets     = a list of 0-BASED POSITIONS to open
#
# The MQTT SHOWER_VALVE_STS message reports the same 6-slot array per zone, e.g.
# [1,0,0,0,0,0] meaning that zone's outlet 1 is on. Every array is padded to six slots
# regardless of hardware; only the leading slots for THAT zone's valve carry meaning —
# three on a K-28210/K-28212, two on a K-28209 or either half of a K-28211. The trailing
# slots are always zero and must be ignored, not read as extra outlets.
#
# A zone maps to a valve: zone1 is valve1, zone2 is valve2.
OUTLETS_PER_ZONE = 3
ZONE_ARRAY_SLOTS = 6


def outlet_positions(outlets: list[bool]) -> list[int]:
    """Convert one zone's outlet flags into the 0-based position list a write expects."""
    return [index for index, is_on in enumerate(outlets) if is_on]


def outlet_flags(
    outlet_state: list[int] | None, outlet_count: int = OUTLETS_PER_ZONE
) -> list[bool]:
    """Convert one zone's padded outlet array into that zone's real outlet flags.

    ``outlet_count`` is how many outlets that zone's valve actually has; the remaining
    slots are padding.
    """
    state = outlet_state or []
    return [bool(state[i]) if i < len(state) else False for i in range(outlet_count)]


def zone_outlet_flags(
    model: ValveModel,
    zone1_outlets: list[int] | None,
    zone2_outlets: list[int] | None = None,
) -> list[bool]:
    """Combine both zones' padded arrays into global Home Assistant outlet flags.

    Returns one flag per physical outlet, numbered the way every surface in this
    integration numbers them: zone1's outlets first, then zone2's. On a 4-outlet K-28211
    that makes zone2's first outlet "Outlet 3".
    """
    flags = outlet_flags(zone1_outlets, model.outlets_valve1)
    if model.uses_valve2:
        flags += outlet_flags(zone2_outlets, model.outlets_valve2)
    return flags


# The HUB uses "zone" and "valve" interchangeably, and not consistently within one payload:
# hub-state's shower entries carry `zone: "1"`, favorites nest under `water.zone1`,
# hub-configuration's parts are `valve1`/`valve2`, and MQTT SHOWER_VALVE_STS attributes may
# carry either a `zone` number or a `component` of "valve1"/"valve2". They all mean the same
# thing. Anything reading a HUB payload should go through zone_number() rather than picking
# one field and hoping.
_ZONE_FIELDS = ("zone", "component", "valve", "valveIndex", "zoneIndex")


def zone_number(attribute: dict[str, Any]) -> int | None:
    """Identify which zone/valve a HUB payload entry refers to, or None.

    Accepts every spelling seen in the wild: ``zone: "1"``, ``zone: 1``,
    ``component: "valve1"``, ``valveIndex: "Valve2"``, and so on.
    """
    for field in _ZONE_FIELDS:
        raw = attribute.get(field)
        if raw is None:
            continue
        text = str(raw).strip().lower()
        # Bare number: "1" / "2".
        if text in {"1", "2"}:
            return int(text)
        # Prefixed forms: "valve1", "zone2", "Valve1".
        for prefix in ("valve", "zone"):
            if text.startswith(prefix):
                suffix = text[len(prefix) :].strip()
                if suffix in {"1", "2"}:
                    return int(suffix)
    return None


CONNECTED = "Connected"

#: What `about.valveN.serialNumber` reads for a valve body that is not fitted.
_NO_SERIAL = "0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0"


def _connected(value: Any) -> bool:
    """`parts.X == "Connected"`, case-insensitively — the app's own comparison."""
    return str(value or "").strip().lower() == CONNECTED.lower()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _ip_or_none(value: Any) -> str | None:
    """A usable IP address, or None for anything else — blank, "null", ``0.0.0.0``."""
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    return None if address.is_unspecified else str(address)


@dataclass(frozen=True)
class HubSettings:
    """The controller's own settings, as ``hub-configuration`` publishes them.

    **Max Shower Duration is here.** This integration long held that the controller's
    duration was readable only through the local API. Konnect 3.0.6 reads it from
    ``configuration.systemSettings.maxShowerDuration`` — minutes — as the default and cap
    for a favorite's duration. Whether the cloud copy follows an edit made on the
    controller's web page promptly is unverified: REST has been seen to lag on
    ``amplifierSettings.monoVolume``.

    ``disconnected`` applies the app's own rules (``nc0/z.java`` ``k()``): an accessory
    counts as fitted from configuration evidence — a valve body whose ``about`` serial is
    not all zeros, a steam generator with a non-zero ``defaultTime``, an amplifier with a
    volume, any configured light group — and is reported when fitted but its ``parts``
    entry is not ``Connected``. That is the case ``HubCapabilities`` cannot see: it gates
    entities on ``parts`` alone, so a fitted accessory that drops off simply vanishes.

    ``lan_ip`` is the controller's address on the home network: ``about.hub.wlan.ip``, the
    address Konnect 3.0.6 opens the controller's web settings page (the "Embedded Server
    Page") at, or ``about.hub.eth.ip`` for a wired controller with no Wi-Fi address.
    """

    max_shower_duration_minutes: int | None = None
    shower_max_temperature: int | None = None
    temperature_unit: str | None = None
    flow_rate_enabled: bool | None = None
    steam_default_temperature: int | None = None
    steam_default_time: int | None = None
    steam_max_temperature: int | None = None
    light_groups: tuple[str, ...] = ()
    lan_ip: str | None = None
    disconnected: tuple[str, ...] = ()

    @classmethod
    def from_configuration(cls, configuration: dict[str, Any]) -> HubSettings:
        """Read the settings from a ``hub-configuration`` ``configuration`` block."""
        configuration = configuration if isinstance(configuration, dict) else {}

        def block(name: str) -> dict[str, Any]:
            value = configuration.get(name)
            return value if isinstance(value, dict) else {}

        system = block("systemSettings")
        steam = block("steamSettings")
        amplifier = block("amplifierSettings")
        parts = block("parts")
        about = block("about")
        lights = configuration.get("lightSettings")
        lights = (
            [x for x in lights if isinstance(x, dict)]
            if isinstance(lights, list)
            else []
        )

        def serial(name: str) -> str | None:
            entry = about.get(name)
            if not isinstance(entry, dict):
                return None
            value = entry.get("serialNumber")
            return None if value in (None, "") else str(value)

        disconnected: list[str] = []
        for name in ("valve1", "valve2"):
            number = serial(name)
            if (
                number is not None
                and number != _NO_SERIAL
                and not _connected(parts.get(name))
            ):
                disconnected.append(name)
        steam_time = _int_or_none(steam.get("defaultTime"))
        if steam_time and not _connected(parts.get("steam")):
            disconnected.append("steam")
        amplifier_fitted = (
            amplifier.get("stereoVolume") is not None
            or amplifier.get("monoVolume") is not None
        )
        if amplifier_fitted and not _connected(parts.get("amplifier")):
            disconnected.append("amplifier")
        if lights and (
            not _connected(parts.get("light"))
            or any(str(x.get("connectivity")) == "No" for x in lights)
        ):
            disconnected.append("light")
        if amplifier_fitted:
            sd_card = str(amplifier.get("sdCard") or "").strip().lower()
            music = str(amplifier.get("music") or "").strip().lower()
            if sd_card == "notpresent":
                disconnected.append("sd_card")
            elif music in {"notpresent", "unknown"}:
                disconnected.append("sd_card_empty")

        flow = system.get("flowRateEnable")
        hub = about.get("hub")
        lan_ip = None
        if isinstance(hub, dict):
            for link in ("wlan", "eth"):
                details = hub.get(link)
                lan_ip = _ip_or_none(
                    details.get("ip") if isinstance(details, dict) else None
                )
                if lan_ip:
                    break
        return cls(
            max_shower_duration_minutes=_int_or_none(system.get("maxShowerDuration")),
            shower_max_temperature=_int_or_none(system.get("showerMaxTemperature")),
            temperature_unit=(
                str(system["temperatureUnit"])
                if system.get("temperatureUnit")
                else None
            ),
            flow_rate_enabled=None if flow is None else str(flow).strip() == "1",
            steam_default_temperature=_int_or_none(steam.get("defaultTemperature")),
            steam_default_time=steam_time,
            steam_max_temperature=_int_or_none(steam.get("maxTemperature")),
            light_groups=tuple(str(x.get("name")) for x in lights if x.get("name")),
            lan_ip=lan_ip,
            disconnected=tuple(disconnected),
        )

    @property
    def web_url(self) -> str | None:
        """The controller's web settings page, or None when its address is unknown."""
        if not self.lan_ip:
            return None
        host = f"[{self.lan_ip}]" if ":" in self.lan_ip else self.lan_ip
        return f"http://{host}/"

    @property
    def steam_ready(self) -> bool:
        """Whether the app would offer its "Steam start" card: a non-zero default time."""
        return bool(self.steam_default_time)


@dataclass(frozen=True)
class HubCapabilities:
    """Which accessories are attached, and therefore which favorite fields exist.

    A favorite bundles ``water``, ``steam``, ``music``, and ``light`` components, but only
    those whose hardware is present are meaningful. An account with no amplifier has no
    music field to set.

    None of this affects **activating** a favorite — that is always just an id and a
    name, whatever the favorite contains.
    """

    water: bool = False
    music: bool = False
    light: bool = False
    steam: bool = False
    # Whether this was ever populated from a real read. Cannot be inferred from the flags:
    # a controller with no accessories is legitimately all-False, so "empty" and "unread"
    # look identical without this.
    known: bool = False

    @classmethod
    def from_configuration(cls, configuration: dict[str, Any]) -> HubCapabilities:
        """Read capabilities from a ``hub-configuration`` response.

        ``parts`` reports ``Connected`` / ``NotConnected`` / ``null`` per component.

        Note ``parts.valve1`` / ``valve2`` count **physical valve units**, whereas the GCS
        API's ``valve1``/``valve2`` count **zones** within one unit —
        ``HUB valve1 == GCS valve1 + valve2``. So a 6-outlet K-28212 correctly reports
        ``valve1: Connected`` and ``valve2: NotConnected``: there is no second valve body.

        Never gate zone-2 entities on ``parts.valve2``; it would hide half the outlets on a
        normal install. Use ``zoneone``/``zonetwo`` ``configuredoutlets`` for topology.
        """
        parts = (configuration or {}).get("parts") or {}

        def connected(*names: str) -> bool:
            return any(parts.get(name) == CONNECTED for name in names)

        return cls(
            water=connected("valve1", "valve2", "valveOne", "valveTwo"),
            music=connected("amplifier", "music"),
            light=connected("light", "lightBridge"),
            steam=connected("steam"),
            known=True,
        )

    def describe(self) -> str:
        """A short human summary of the attached accessories."""
        present = [n for n in ("water", "music", "light", "steam") if getattr(self, n)]
        return ", ".join(present) if present else "no accessories detected"


class HubDevice:
    """Command surface for one Anthem Plus system controller."""

    def __init__(
        self,
        client: KohlerClient,
        device_id: str,
        temperature_unit: str = "Fahrenheit",
    ) -> None:
        self._client = client
        self.device_id = device_id
        # Favorite temperatures are **whole °F on the wire, whatever the account's unit.**
        # Konnect 3.0.6 converts a Celsius account's entry to °F before it goes into the
        # request (`nc0/z.java` `G0()` -> `F()`, `round(c * 1.8 + 32)`, at every zone and
        # steam setter) and back to °C only for display. This said the opposite until
        # 2026-10-07 — harmless only because nothing called the favorite writers yet.
        self.temperature_unit = temperature_unit

    def to_wire_temperature(self, temperature: float) -> int:
        """A temperature in the account's unit -> the whole °F a favorite carries."""
        if str(self.temperature_unit).strip().lower().startswith("c"):
            return 0 if int(temperature) == 0 else round(temperature * 1.8 + 32)
        return round(temperature)

    def _base(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "sku": SKU_HUB,
            "tenantId": self._client.tenant_id,
        }

    # ------------------------------------------------------------------ #
    # Direct control (the controller's own stored default)
    # ------------------------------------------------------------------ #
    async def async_set_shower(self, on: bool) -> Any:
        """Run or stop the controller's default shower configuration."""
        return await self._client.async_request(
            "POST",
            HUB_VALVE_CONTROL,
            json_body={**self._base(), "valveOnOff": ON if on else OFF},
        )

    async def async_set_steam(self, on: bool) -> Any:
        """Run or stop the controller's default steam configuration.

        Runs at ``steamSettings.defaultTemperature`` for ``defaultTime`` — the body carries
        nothing else. The app refuses to start steam while the shower runs, and the
        controller refuses a favorite holding both; ``KohlerAnthemCoordinator`` mirrors the
        first guard.
        """
        return await self._client.async_request(
            "POST",
            HUB_STEAM_CONTROL,
            json_body={**self._base(), "steamOnOff": ON if on else OFF},
        )

    async def async_stop_all(self) -> Any:
        """Fully idle the system — the only true "off"."""
        return await self._client.async_request(
            "POST", HUB_STOP_ALL, json_body=self._base()
        )

    # ------------------------------------------------------------------ #
    # Favorites
    # ------------------------------------------------------------------ #
    async def async_activate_favorite(
        self, favorite_id: Any, name: str, on: bool = True
    ) -> Any:
        """Start or stop a favorite. Allowed even while something else runs."""
        return await self._client.async_request(
            "POST",
            HUB_FAVORITE_CONTROL,
            json_body={
                **self._base(),
                # Control takes the id as a STRING; create/delete take it as an integer.
                "id": str(favorite_id),
                "name": name,
                "state": ON if on else OFF,
            },
        )

    async def async_create_favorite(
        self,
        name: str,
        *,
        zone1: dict[str, Any] | None = None,
        zone2: dict[str, Any] | None = None,
        steam: dict[str, Any] | None = None,
        music: dict[str, Any] | None = None,
        light: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Create a favorite. ``id: 0`` is what makes it a create, as the app sends it."""
        body = self._favorite_body(
            name, zone1=zone1, zone2=zone2, steam=steam, music=music, light=light
        )
        body["id"] = 0
        return await self._client.async_request("POST", HUB_FAVORITE, json_body=body)

    async def async_edit_favorite(
        self,
        favorite_id: int,
        name: str,
        *,
        zone1: dict[str, Any] | None = None,
        zone2: dict[str, Any] | None = None,
        steam: dict[str, Any] | None = None,
        music: dict[str, Any] | None = None,
        light: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Edit a favorite.

        Raises :class:`~.client.DeviceRunning` if the system is active — stop it first.
        """
        body = self._favorite_body(
            name, zone1=zone1, zone2=zone2, steam=steam, music=music, light=light
        )
        body["id"] = int(favorite_id)
        return await self._client.async_request("PATCH", HUB_FAVORITE, json_body=body)

    async def async_delete_favorite(self, favorite_id: int, name: str) -> Any:
        """Delete a favorite."""
        return await self._client.async_request(
            "DELETE",
            HUB_FAVORITE,
            json_body={**self._base(), "name": name, "id": int(favorite_id)},
        )

    def _favorite_body(
        self,
        name: str,
        *,
        zone1: dict[str, Any] | None,
        zone2: dict[str, Any] | None,
        steam: dict[str, Any] | None,
        music: dict[str, Any] | None,
        light: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Assemble a favorite body the way Konnect 3.0.6 does.

        **Components are omitted, not nulled.** The app serialises with Gson defaults, so
        an unset component never reaches the wire: ``water`` only when a zone has outlets,
        ``steam`` only when steam is on, ``music`` only with an amplifier source, ``light``
        only for active groups, and ``zone2`` only when used. (``music`` was already known
        to fail with HTTP 400 when sent all-null.) Until 2026-10-07 this sent ``zone2: null``,
        ``steam: {0, 0}`` and ``light: []``; app-side only, untested live either way.

        🚨 **A favorite may not hold both water and steam** — the app refuses to build one
        ("A favorite cannot contain both Shower and Steam"), so this refuses too rather
        than find out what the controller does with it.
        """
        water = {
            key: value
            for key, value in (("zone1", zone1), ("zone2", zone2))
            if value is not None
        }
        if water and steam:
            raise ValueError(
                "A favorite cannot contain both shower and steam — the Konnect app forbids "
                "it, and what the controller would do with one is untested."
            )
        body: dict[str, Any] = {**self._base(), "name": name}
        if water:
            body["water"] = water
        if steam:
            body["steam"] = steam
        if music is not None:
            body["music"] = music
        if light:
            body["light"] = light
        return body

    @staticmethod
    def zone(
        temperature: int, outlets: list[bool], flowrate: int = 100
    ) -> dict[str, Any]:
        """Build a water zone for a favorite body.

        ``temperature`` is **whole °F** — convert an account-unit value with
        :meth:`to_wire_temperature` first. The app bounds it 59 °F to the controller's
        ``showerMaxTemperature``, and ``flowrate`` 10-100 (only editable when
        ``flowRateEnable`` is ``"1"``). ``outlets`` are per-outlet flags, converted here to
        the 0-based position list the API expects.
        """
        return {
            "temperature": int(temperature),
            "flowrate": int(flowrate),
            "outlets": outlet_positions(outlets),
        }

    @staticmethod
    def zones_for(
        model: ValveModel,
        outlets: list[bool],
        temperature: int,
        flowrate: int = 100,
    ) -> dict[str, dict[str, Any] | None]:
        """Build both water zones from GLOBAL outlet flags for this valve model.

        Pass one flag per physical outlet (four for a K-28211, six for a K-28212); this
        splits them across zone1/zone2 the way the model dictates and converts each zone's
        flags to the 0-based position list a write expects.

        A zone the model does not have comes back as ``None``, which
        :meth:`_favorite_body` leaves out of the body, as the app does.
        """
        zone1_flags, zone2_flags = model.split_outlets(outlets)
        zones: dict[str, dict[str, Any] | None] = {
            "zone1": HubDevice.zone(temperature, zone1_flags, flowrate)
        }
        zones["zone2"] = (
            HubDevice.zone(temperature, zone2_flags, flowrate)
            if model.uses_valve2
            else None
        )
        return zones

    @staticmethod
    def music(source: str, volume: int = 70) -> dict[str, Any]:
        """Build a music component. ``source`` is ``"Aux"`` or ``"SdCard"``.

        There is no Bluetooth source. ``songID``/``musicRepeat`` are only meaningful for
        Kohler Playlist streaming and are sent empty otherwise.
        """
        return {
            "source": source,
            "songID": "",
            "musicRepeat": "",
            "volume": int(volume),
        }

    # ------------------------------------------------------------------ #
    # Experiences
    # ------------------------------------------------------------------ #
    async def async_control_experience(
        self, title: str, category: str, on: bool = True
    ) -> Any:
        """Start or stop a firmware experience.

        All three experience endpoints share one body; the path is chosen by the category
        the experience appeared under in the experiences read. Sending a shower experience
        to the steam path does not work.

        ``title`` is the experience's TITLE string, not its numeric id.

        Experiences carry no outlet or curve data in the API — the program is internal to
        the firmware and always runs on the default zone1/outlet1 (the app says so: "will
        run from the fitting connected to the first port of zone 1"). Use a favorite when
        you need a specific outlet. Run state comes back as ``SHOWER_EXP_STS`` /
        ``STEAM_EXP_STS`` / ``ICE_SHOWER_EXP_STS``.
        """
        endpoint = EXPERIENCE_ENDPOINTS.get(category)
        if endpoint is None:
            raise ValueError(
                f"Unknown experience category {category!r}; expected one of "
                f"{sorted(EXPERIENCE_ENDPOINTS)}"
            )
        return await self._client.async_request(
            "POST",
            endpoint,
            json_body={**self._base(), "name": title, "status": ON if on else OFF},
        )
