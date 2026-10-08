"""Authenticated REST client for the Kohler Konnect cloud API.

Wraps :class:`~.auth.KohlerAuth` with the headers every call needs, transparent token
refresh, and translation of Kohler's two error channels into exceptions.

Kohler reports failure in two places and you have to check both:

* the HTTP status, and
* a ``statusCode`` field **inside** a 200/400 body — ``900`` means the device is offline,
  ``901``/``902`` that it is running and refuses the edit; the rest of the table is in
  ``const.STATUS_MESSAGES``.

A request can therefore "succeed" with HTTP 200 and still have done nothing.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import aiohttp

from .auth import AuthError, KohlerAuth
from .const import (
    API_BASE,
    APIM_SUBSCRIPTION_KEY,
    CUSTOMER_DEVICE,
    GCS_ABOUT,
    GCS_ADVANCE_STATE,
    GCS_CONFIGURATION,
    GCS_DIAGNOSTICS,
    GCS_FIRMWARE,
    GCS_GATEWAY_FIRMWARE,
    GCS_PRESETS,
    GCS_STATE,
    GCS_USAGE,
    HUB_CONFIGURATION,
    HUB_DIAGNOSTICS_ACTIVE,
    HUB_EXPERIENCES,
    HUB_FAVORITES,
    HUB_FIRMWARE,
    HUB_STATE,
    MOBILE_SETTINGS,
    SKU_GCS,
    SKU_HUB,
    STATUS_DEVICE_OFFLINE,
    STATUS_DEVICE_RUNNING,
    STATUS_DEVICE_RUNNING_ALT,
    STATUS_MESSAGES,
)
from .models import OutletStateSource, resolve_outlet_source

_LOGGER = logging.getLogger(__name__)

#: Matches the id segment of a device-management path: everything after the endpoint name up
#: to the next `/` or `?`. Kohler ids are `gcs-...`, `hub-...` and bare tenant GUIDs, so this
#: keys off the endpoint prefix rather than trying to recognise an id by shape.
_ID_IN_PATH = re.compile(
    r"/((?:gcs|hub|customer)-[a-z-]*/(?:[a-z]+/)?)[^/?]+",
    re.IGNORECASE,
)
#: The firmware family puts the id after a bare product name instead —
#: `/platform/api/v1/firmware/gcs/gateway/<id>?releasetarget=Public`.
_ID_IN_FIRMWARE_PATH = re.compile(
    r"(/firmware/(?:gcs|hub)/(?:gateway/)?)[^/?]+", re.IGNORECASE
)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class KohlerError(Exception):
    """A Kohler API call failed."""

    def __init__(
        self, message: str, payload: Any = None, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.payload = payload
        #: HTTP status when this came from a response, else None. Lets a caller tell apart
        #: failures that mean different things — a 404 on a collection endpoint is "empty",
        #: not "broken" — without parsing the message string.
        self.status = status


class DeviceOffline(KohlerError):
    """The device is powered off or has lost its cloud link (statusCode 900).

    Expected and transient — surface it gently rather than as a failure.
    """


class DeviceRunning(KohlerError):
    """The device is running and refuses the change (statusCode 902, or 901).

    Editing a HUB favorite requires the system to be stopped first. Activating one is
    allowed at any time, which is why the practical pattern is to pre-create a favorite
    per state and switch by activation rather than editing at runtime.
    """


class Device:
    """One device on the account."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.device_id: str = raw.get("deviceId") or raw.get("deviceid") or ""
        self.sku: str = raw.get("sku") or ""
        self.name: str = raw.get("logicalName") or raw.get("name") or self.device_id
        self.serial_number: str | None = raw.get("serialNumber")

    def __repr__(self) -> str:
        return f"<Device {self.sku} {self.device_id} {self.name!r}>"


class Customer:
    """The account record, which is also where the device list lives.

    Devices are nested under ``customerHome[].devices[]`` — note the singular key, which is
    easy to guess wrong. The account also decides the units the API reports in.
    """

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        # "Fahrenheit" or "Celsius". Kohler's REST API and the GCS valve byte both report
        # Celsius regardless; this is the account's *display* preference, which the mobile
        # app converts to locally. The HUB's favorite temperatures are the opposite case —
        # whole °F on the wire whatever this says (Konnect 3.0.6) — so this decides only
        # how a value typed in the account's unit is converted, never the wire unit.
        self.temperature_unit: str = raw.get("temperatureUnit") or "Fahrenheit"
        self.water_units: str = raw.get("waterUnits") or "Standard"
        self.devices: list[Device] = [
            Device(device)
            for home in _as_list(raw.get("customerHome") or raw.get("homes"))
            if isinstance(home, dict)
            for device in _as_list(home.get("devices"))
            if isinstance(device, dict)
        ]

    def device(self, sku: str) -> Device | None:
        """The first device with the given SKU, or None."""
        return next((d for d in self.devices if d.sku == sku), None)

    def devices_of(self, sku: str) -> list[Device]:
        """Every device with the given SKU."""
        return [d for d in self.devices if d.sku == sku]

    @property
    def gcs_devices(self) -> list[Device]:
        """Anthem digital valves on the account."""
        return self.devices_of(SKU_GCS)

    @property
    def hub_devices(self) -> list[Device]:
        """Anthem Plus system controllers on the account."""
        return self.devices_of(SKU_HUB)

    @property
    def has_gcs(self) -> bool:
        """Whether the account has at least one Anthem digital valve."""
        return bool(self.gcs_devices)

    @property
    def has_hub(self) -> bool:
        """Whether the account has at least one Anthem Plus controller."""
        return bool(self.hub_devices)

    @property
    def other_devices(self) -> list[Device]:
        """Devices this integration does not support.

        The Konnect app covers many Kohler product lines (DTV, Numi, Blade, faucets, and
        more). They appear on the same account and must be ignored rather than treated as
        a malformed Anthem device.
        """
        return [d for d in self.devices if d.sku not in (SKU_GCS, SKU_HUB)]

    @property
    def supported_devices(self) -> list[Device]:
        """Anthem devices this integration can drive."""
        return self.gcs_devices + self.hub_devices

    @property
    def outlet_source(self) -> OutletStateSource | None:
        """Which channel should feed outlet, temperature, and flow entities.

        GCS wins whenever it is present — see
        :func:`~.models.resolve_outlet_source` for why HUB-sourced outlets would go stale
        rather than merely lag on a both-devices account.
        """
        return resolve_outlet_source(self.has_gcs, self.has_hub)

    def describe(self) -> str:
        """A short human summary of what was found, for logs and the config flow.

        Note a GCS and a HUB on one account are often the SAME physical shower, reached
        through two different touchscreen interfaces — see :mod:`.models` for why. They are
        still presented as two separate devices: they behave differently, and the HUB's
        state consistently trails the valve's.
        """
        parts = []
        if self.has_gcs:
            parts.append(f"{len(self.gcs_devices)} Anthem valve(s)")
        if self.has_hub:
            parts.append(f"{len(self.hub_devices)} Anthem Plus controller(s)")
        if not parts:
            return "no Anthem devices found on this account"
        summary = " and ".join(parts)
        if self.other_devices:
            skus = ", ".join(sorted({d.sku for d in self.other_devices}))
            summary += f" (ignoring other Kohler devices: {skus})"
        return summary


class KohlerClient:
    """Authenticated access to the Kohler Konnect cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: KohlerAuth,
        tenant_id: str | None = None,
    ) -> None:
        self._session = session
        self._auth = auth
        self._tenant_id = tenant_id

    @property
    def auth(self) -> KohlerAuth:
        """The underlying auth handler, for persisting a rotated refresh token."""
        return self._auth

    @property
    def tenant_id(self) -> str | None:
        """The account id every device call is keyed on."""
        return self._tenant_id or self._auth.tenant_id

    async def async_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        allow_retry: bool = True,
    ) -> Any:
        """Make an authenticated request, refreshing the token once on a 401."""
        token = await self._auth.async_get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Ocp-Apim-Subscription-Key": APIM_SUBSCRIPTION_KEY,
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                json=json_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                text = await resp.text()
                status = resp.status
        except aiohttp.ClientError as err:
            raise KohlerError(f"Network error calling {path}: {err}") from err

        # A 401 usually means the access token aged out mid-flight; one retry with a
        # freshly minted token is enough. Retrying more would mask a real auth failure.
        if status == 401 and allow_retry:
            # INFO, not DEBUG. This is rare, it costs a token refresh plus a second round
            # trip — seconds, not milliseconds — and it is the only thing in this client that
            # can make a single call take that long. A 5.05 s valve restore on 2026-08-15
            # could not be explained afterwards precisely because this line was invisible
            # under default logging. See the session 8 handoff.
            _LOGGER.info(
                "401 from %s — the access token aged out mid-request; refreshing it and "
                "retrying once. Expect this call to take a few seconds longer than usual",
                self.safe_path(path),
            )
            # Through `async_get_access_token`, not `async_refresh`, so this goes via the
            # refresh lock. B2C rotates the refresh token on every use, and two concurrent
            # 401s calling `async_refresh` directly would redeem the same token twice —
            # exactly the double-redemption the lock exists to prevent. Invalidating first
            # makes the locked path treat the token as expired and mint a new one.
            self._auth.invalidate_access_token()
            await self._auth.async_get_access_token()
            return await self.async_request(
                method, path, json_body=json_body, allow_retry=False
            )

        payload: Any = None
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = text

        # ------------------------------------------------------------------ #
        # API PROBE LOG — diagnostic, OFF BY DEFAULT
        # ------------------------------------------------------------------ #
        # Every REST call in this integration funnels through this method, so one line
        # here captures the whole API surface: which endpoints are reached, what they
        # return, and — the part no other diagnostic can answer — **fields Kohler sends
        # that this integration does not read**. `docs/gcs/api.md` was written from
        # exactly this kind of observation, and the questions it still leaves open (is
        # there an install date? does a GCS-only valve populate `gcs-configuration`?) are
        # answerable only by looking at a real response.
        #
        # Turn it on without a restart, from Developer Tools → Actions:
        #
        #     action: logger.set_level
        #     data:
        #       custom_components.kohler_anthem.anthem.client: debug
        #
        # Set it back to `info` to stop. The MQTT half of the same picture is
        # `raw_log.py`; between them every byte the integration receives is capturable.
        #
        # **Credentials are redacted, not logged.** `mobile/settings` returns the IoT Hub
        # SAS password, which is exactly the sort of thing a user pastes into an issue
        # without looking. `_redact_payload` drops it before this line sees it.
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "API %s %s -> %s %s", method, path, status, _redact_payload(payload)
            )

        self._raise_for_payload(status, path, payload)
        return payload

    @staticmethod
    def safe_path(path: str) -> str:
        """An endpoint path with its device or tenant id replaced by `<id>`.

        **Every read endpoint carries an id in its path**, so an error message built from one
        carries a cloud address — and those messages reach WARNING and ERROR logs and
        service responses. `diagnostics.py` goes to length to
        redact exactly this; an exception string handed it back.

        The shape is what makes an error useful (`gcs-usage/<id> failed with HTTP 400` says
        which endpoint and why), and the shape is all this keeps. Write endpoints have no id
        in the path — the id travels in the body — so they are unaffected either way.
        """
        return _ID_IN_FIRMWARE_PATH.sub(r"\1<id>", _ID_IN_PATH.sub(r"/\1<id>", path))

    @staticmethod
    def _raise_for_payload(status: int, path: str, payload: Any) -> None:
        """Translate Kohler's HTTP status and in-body statusCode into exceptions.

        ``statusCode`` is compared **as a string**. Konnect models it as one, and an int
        comparison — what this did until 2026-10-07 — silently misses a ``"900"``.
        """
        raw = payload.get("statusCode") if isinstance(payload, dict) else None
        inner = None if raw is None else str(raw).strip()
        if inner == STATUS_DEVICE_OFFLINE:
            raise DeviceOffline(
                "The Kohler device is offline. Check that it is powered on and "
                "connected to Wi-Fi.",
                payload,
            )
        if inner in (STATUS_DEVICE_RUNNING, STATUS_DEVICE_RUNNING_ALT):
            raise DeviceRunning(
                "The system is running, so this change was rejected. Stop it first "
                "(stopall), then retry.",
                payload,
            )
        if status >= 400:
            # A code from the app's own table says *why* in words; Kohler's body `message`
            # is usually just "Something went wrong".
            meaning = STATUS_MESSAGES.get(inner or "")
            if meaning is not None:
                raise KohlerError(
                    f"{KohlerClient.safe_path(path)} was refused: {meaning} "
                    f"(statusCode {inner})",
                    payload,
                    status,
                )
            detail = payload if isinstance(payload, str) else repr(payload)
            raise KohlerError(
                f"{KohlerClient.safe_path(path)} failed with HTTP {status}: {detail}",
                payload,
                status,
            )

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    async def async_get_customer(self) -> Customer:
        """Read the account: its devices plus the units the API reports in."""
        tenant_id = self.tenant_id
        if not tenant_id:
            raise AuthError("No tenant id available; sign in first.")
        payload = await self.async_request(
            "GET", CUSTOMER_DEVICE.format(tenant_id=tenant_id)
        )
        if not isinstance(payload, dict):
            raise KohlerError("Unexpected customer-device response", payload)
        return Customer(payload)

    async def async_register_mobile_device(
        self, mobile_device_id: str | None = None
    ) -> dict[str, Any]:
        """Register a mobile client and return Azure IoT Hub credentials.

        This is how the real-time MQTT status stream is reached. The returned settings
        carry ``ioTHub`` (hostname), ``deviceId`` (the MQTT client id to use), ``username``
        and ``password`` (a short-lived SAS token).

        The credentials are per-session and must NEVER be persisted or logged — obtain
        them fresh on each connect.

        ``mobile_device_id`` **should** be persisted and reused, though: it is the identity,
        not a credential. Omitting it generates a throwaway one, which registers what Kohler
        sees as another phone on every single connect.
        """
        tenant_id = self.tenant_id
        if not tenant_id:
            raise AuthError("No tenant id available; sign in first.")
        device_id = mobile_device_id or uuid.uuid4().hex[:16]
        payload = {
            "tenantId": tenant_id,
            "mobileDeviceId": device_id,
            "username": "HomeAssistant",
            "os": "Android",
            "devicePlatform": "FirebaseCloudMessagingV1",
            "deviceHandle": f"ha_{device_id}",
            "tags": ["FirmwareUpdate"],
        }
        data = await self.async_request("POST", MOBILE_SETTINGS, json_body=payload)
        settings = (data or {}).get("ioTHubSettings") or {}
        if not settings.get("ioTHub"):
            raise KohlerError("Kohler returned no IoT Hub settings", data)
        return settings

    async def async_get_gcs_state(self, device_id: str) -> Any:
        """Live valve state: both zone words' fields, warmup, and the active preset.

        The read that seeds every valve entity at setup and on each MQTT reconnect, and
        the same field the Konnect app reads for warmup (``warmUpState.warmUp``). Partly
        cached on Kohler's side — the device's own MQTT push is the final word.
        """
        return await self.async_request("GET", GCS_STATE.format(device_id=device_id))

    async def async_get_gcs_settings(self, device_id: str) -> dict[str, Any]:
        """Read the valve's own settings block, including its outlet topology.

        This is the authoritative source for how many outlets sit on each valve — it comes
        from the valve itself, so it works without an Anthem Plus controller. Note the path
        is ``gcs-state/gcsadvancestate/…``, NOT the plain ``gcs-configuration/…``, which
        returns null for every structural field on a controller-attached valve.
        """
        payload = await self.async_request(
            "GET", GCS_ADVANCE_STATE.format(device_id=device_id)
        )
        if not isinstance(payload, dict):
            return {}
        return payload.get("setting") or {}

    async def async_get_usage(
        self, device_id: str, *, from_date: str, to_date: str, interval: str = "MONTH"
    ) -> dict[str, Any]:
        """Per-period water usage — the series behind the Konnect app's chart.

        **`volume` comes back in litres**, whatever the account's unit setting. Verified from
        the Konnect app, which multiplies it by 0.264172 (litres to US gallons) when the
        customer's `waterUnits` is `Standard` and shows it raw otherwise. `LITRES_PER_...`
        callers should use `usage_volume_gallons` rather than repeat the constant.

        `interval` is `DAY` or `MONTH`. The query parameters are PascalCase — `FromDate`,
        `ToDate`, `Interval` — which is the whole reason this endpoint went unsolved: every
        other endpoint in this API is camelCase, and a wrong case is rejected with the same
        generic 400 as a bare call. Konnect 3.0.6 sends exactly two intervals, `Day` and
        `Month`, with `MM-dd-yyyy` dates; `WEEK` and `YEAR` are not intervals the server
        knows. See `GCS_USAGE` in `const.py`.

        Returns `{}` rather than raising when the read fails: this feeds a diagnostic sensor,
        and a setup that already works must not start failing over it.
        """
        path = (
            f"{GCS_USAGE.format(device_id=device_id)}"
            f"?FromDate={from_date}&ToDate={to_date}&Interval={interval}"
        )
        try:
            payload = await self.async_request("GET", path)
        except KohlerError as err:
            _LOGGER.debug("Could not read gcs-usage: %s", err)
            return {}
        return payload if isinstance(payload, dict) else {}

    async def async_get_gcs_configuration(self, device_id: str) -> dict[str, Any]:
        """Read the valve's configuration record — firmware, and possibly nothing else.

        **Not the outlet topology source.** ``async_get_gcs_settings`` above is, and the
        two are easy to confuse: on the reference install — a valve wired to an Anthem Plus
        controller — every structural field here (``zoneone``, ``zonetwo``, ``parts``,
        ``valve1Settings``, ``valve2Settings``, ``systemConfiguration``, ``systemSettings``)
        comes back ``null``, because such a valve reports its configuration through the
        controller. Only ``about.firmware`` and ``firmwareOTADetails`` carry data there.

        Whether a **GCS-only** install populates the rest has never been captured — see
        ``docs/gcs/api.md``. This method exists so a report from such an account can answer
        that, and so the firmware version is readable at all; nothing in the integration
        depends on the structural fields being present.

        Returns ``{}`` rather than raising when the read fails: this is diagnostic, and a
        setup that already works must not start failing over it.
        """
        payload = await self.async_request(
            "GET", GCS_CONFIGURATION.format(device_id=device_id)
        )
        return payload if isinstance(payload, dict) else {}

    async def async_get_hub_state(self, device_id: str) -> dict[str, Any]:
        """Live HUB status: per-zone shower, steam, music, light."""
        return await self.async_request("GET", HUB_STATE.format(device_id=device_id))

    async def async_get_gcs_presets(self, device_id: str) -> dict[str, Any]:
        """The valve's stored presets, as ``gcsPresetExperienceDetails[]``.

        Only needed to seed and as a backstop: the device pushes ``GCS_PRESET_STS`` on every
        create, edit, rename, and delete, so preset changes do not need polling.
        """
        return await self.async_request("GET", GCS_PRESETS.format(device_id=device_id))

    async def async_get_hub_favorites(self, device_id: str) -> dict[str, Any]:
        """The HUB's saved favorites — the unit of control for this device."""
        return await self.async_request(
            "GET", HUB_FAVORITES.format(device_id=device_id)
        )

    async def async_get_hub_experiences(self, device_id: str) -> dict[str, Any]:
        """The HUB's firmware experience programs, grouped by category."""
        return await self.async_request(
            "GET", HUB_EXPERIENCES.format(device_id=device_id)
        )

    async def async_get_hub_configuration(self, device_id: str) -> dict[str, Any]:
        """Zones, outlets, installed parts, capability flags — and the controller's settings.

        ``configuration.systemSettings`` carries ``maxShowerDuration`` (minutes),
        ``showerMaxTemperature``, ``temperatureUnit`` and ``flowRateEnable``;
        ``steamSettings`` the steam defaults; ``lightSettings[]`` the light groups; and
        ``about.hub.wlan.ip`` the controller's LAN address. See ``HubSettings``.
        """
        return await self.async_request(
            "GET", HUB_CONFIGURATION.format(device_id=device_id)
        )

    async def async_get_hub_active_errors(self, device_id: str) -> dict[str, Any]:
        """The controller's currently active faults, as ``errorDetails[]``.

        What the app shows as "`<title>` error `<errorCode>` detected". Returns ``{}``
        rather than raising: this feeds a problem sensor's attributes and must not fail a
        setup that otherwise works.
        """
        try:
            payload = await self.async_request(
                "GET", HUB_DIAGNOSTICS_ACTIVE.format(device_id=device_id)
            )
        except KohlerError as err:
            _LOGGER.debug("Could not read hub-diagnostics/active: %s", err)
            return {}
        return payload if isinstance(payload, dict) else {}

    async def async_get_gcs_about(self, device_id: str) -> dict[str, Any]:
        """Per-part identity — gateway, valves and interfaces, with serials and models.

        Returns ``{}`` rather than raising: device-registry detail, never worth failing
        setup over.
        """
        try:
            payload = await self.async_request(
                "GET", GCS_ABOUT.format(device_id=device_id)
            )
        except KohlerError as err:
            _LOGGER.debug("Could not read gcs-configuration/about: %s", err)
            return {}
        return payload if isinstance(payload, dict) else {}

    async def async_get_gcs_diagnostics(self, device_id: str) -> dict[str, Any]:
        """The valve's fault log. ``{}`` on failure; read for diagnostics only."""
        try:
            payload = await self.async_request(
                "GET", GCS_DIAGNOSTICS.format(device_id=device_id)
            )
        except KohlerError as err:
            _LOGGER.debug("Could not read gcs-diagnostics: %s", err)
            return {}
        return payload if isinstance(payload, dict) else {}

    async def async_get_firmware(self, device_id: str, part: str) -> dict[str, Any]:
        """Installed vs latest firmware for one part: ``gcs``, ``gateway`` or ``hub``.

        Read-only. ``firmwareUpdateAvailable`` is the app's whole test for "update
        available". Returns ``{}`` rather than raising: an update entity going unknown is
        the right failure, not a broken setup.
        """
        template = {
            "gcs": GCS_FIRMWARE,
            "gateway": GCS_GATEWAY_FIRMWARE,
            "hub": HUB_FIRMWARE,
        }[part]
        try:
            payload = await self.async_request(
                "GET", template.format(device_id=device_id)
            )
        except KohlerError as err:
            _LOGGER.debug("Could not read %s firmware: %s", part, err)
            return {}
        return payload if isinstance(payload, dict) else {}


# Keys whose VALUES are credentials or identity, redacted before anything is logged. The
# match is on the lowercased key containing one of these, so `sasToken`, `SharedAccessKey`
# and `refresh_token` are all caught without listing every spelling Kohler uses.
_SECRET_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "sas",
    "key",
    "authorization",
    "credential",
)


# Credential-bearing VALUES whose key name gives nothing away. `_SECRET_KEY_PARTS` catches
# the field names Kohler is known to use, but this runs on whatever the cloud returns,
# including endpoints whose full shape no capture has covered. An Azure
# connection string is the realistic case: the whole secret sits inside one string under a
# neutral key like `connectionString`, so a key-only check copies it out in full.
_SECRET_IN_VALUE = re.compile(
    r"(?:SharedAccessKey|AccountKey|SharedAccessSignature|sig)=", re.IGNORECASE
)


def _redact_payload(value: Any, _depth: int = 0) -> Any:
    """Copy a payload with credential values replaced, for the API probe log.

    Structure is preserved exactly — every key stays, only secret *values* are swapped —
    because the whole point of the log is seeing which fields exist. A redacted field still
    tells you it was there.

    Depth-limited rather than trusting the payload to be shallow: this runs on whatever
    Kohler returns, and a cycle or a pathological nesting must not take the event loop down
    with it.
    """
    if _depth > 12:
        return "<too deep>"
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if any(part in str(key).lower() for part in _SECRET_KEY_PARTS):
                redacted[key] = "**REDACTED**"
            else:
                redacted[key] = _redact_payload(item, _depth + 1)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item, _depth + 1) for item in value]
    # A secret can hide in the value under an innocuous key — see `_SECRET_IN_VALUE`.
    if isinstance(value, str) and _SECRET_IN_VALUE.search(value):
        return "**REDACTED**"
    return value


def _as_list(value: Any) -> list[Any]:
    """Coerce a possibly-missing API list field into a list."""
    return value if isinstance(value, list) else []
