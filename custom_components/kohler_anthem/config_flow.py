"""Config flow for Kohler Anthem.

Two steps:

1. ``user``  — email and password. Sign-in runs entirely server-side, so there is no
   browser round trip and nothing to paste back.
2. ``valve`` — the valve model, which decides how many outlets exist and which valve each
   one sits on. The split is detected from the API where possible (the valve's
   ``gcsadvancestate``, else the controller's configuration) and the question is skipped;
   the dropdown only appears when detection fails. A model is required even on a HUB-only
   account because the HUB's per-zone outlet arrays need the same split.

Only the rotating refresh token is stored, never the password. When the token finally
expires (B2C allows up to ~90 days) Home Assistant raises a reauth prompt that asks for the
password again.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .anthem import (
    AuthError,
    InvalidCredentials,
    KohlerAuth,
    KohlerClient,
    KohlerError,
    SignInBlocked,
    describe_topology,
    model_for_topology,
    topology_from_hub_configuration,
    topology_from_valve_settings,
)
from .anthem.models import (
    DEFAULT_VALVE_MODEL,
    VALVE_MODELS,
    get_valve_model,
)
from .const import (
    CONF_REFRESH_TOKEN,
    CONF_TEMPERATURE_UNIT,
    CONF_TENANT_ID,
    CONF_VALVE_MODEL,
    CONF_WATER_UNITS,
    CONF_ZONE_GROUPING,
    CONF_ZONE_OUTLETS,
    DEFAULT_ZONE_GROUPING,
    DOMAIN,
    ZONE_GROUPING_NUMBERED,
    ZONE_GROUPING_OUTLET_LABELS,
    ZONE_GROUPING_SUBDEVICES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
)


def _valve_schema() -> vol.Schema:
    """Dropdown of valve models, labelled by outlet count."""
    options = [
        SelectOptionDict(
            value=model.sku,
            label=f"{model.sku} — {model.total_outlets} outlet"
            f"{'s' if model.total_outlets != 1 else ''}",
        )
        for model in sorted(VALVE_MODELS.values(), key=lambda m: m.total_outlets)
    ]
    return vol.Schema(
        {
            vol.Required(CONF_VALVE_MODEL, default=DEFAULT_VALVE_MODEL): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            )
        }
    )


def _options_schema(current_grouping: str) -> vol.Schema:
    """Selector for multi-zone outlet and control grouping."""
    options = [
        SelectOptionDict(
            value=ZONE_GROUPING_SUBDEVICES,
            label=(
                "Sub-device per zone — separate Zone 1 / Zone 2 devices, "
                "no zone numbers on outlets or controls"
            ),
        ),
        SelectOptionDict(
            value=ZONE_GROUPING_OUTLET_LABELS,
            label=(
                "Outlet names on controls — single device, e.g. "
                "Temperature (Showerhead, Body Sprays)"
            ),
        ),
        SelectOptionDict(
            value=ZONE_GROUPING_NUMBERED,
            label=(
                "Zone numbers (default) — single device, appends 1 / 2 "
                "to outlets and controls"
            ),
        ),
    ]
    return vol.Schema(
        {
            vol.Required(CONF_ZONE_GROUPING, default=current_grouping): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
            )
        }
    )


class KohlerAnthemConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Kohler Anthem config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> KohlerAnthemOptionsFlow:
        """Return the options flow for this handler."""
        return KohlerAnthemOptionsFlow(config_entry)

    # Attribute names are deliberately prefixed. Home Assistant's ConfigFlow base class
    # defines read-only properties such as `_reauth_entry_id`, and assigning to one raises
    # AttributeError when the flow is constructed — which surfaces only as a 500 from the
    # config-flow endpoint, with no hint that a name clashed.
    def __init__(self) -> None:
        self._kohler_data: dict[str, Any] = {}
        self._kohler_summary: str = ""
        self._kohler_reauth: bool = False
        self._kohler_topology: tuple[int, int] | None = None

    # ------------------------------------------------------------------ #
    # Step 1: credentials
    # ------------------------------------------------------------------ #
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await self._async_sign_in(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except InvalidCredentials:
                errors["base"] = "invalid_auth"
            except SignInBlocked as err:
                _LOGGER.error("Kohler sign-in blocked: %s", err)
                errors["base"] = "signin_blocked"
            except (AuthError, KohlerError) as err:
                _LOGGER.error("Kohler sign-in failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if not self._kohler_data.get("has_device"):
                    errors["base"] = "no_devices"
                else:
                    await self.async_set_unique_id(
                        user_input[CONF_USERNAME].strip().lower()
                    )
                    if not self._kohler_reauth:
                        self._abort_if_unique_id_configured()
                    return await self.async_step_valve()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def _async_sign_in(self, username: str, password: str) -> None:
        """Sign in, then read the account to see what hardware is on it."""
        session = async_get_clientsession(self.hass)
        auth = KohlerAuth(session)
        tokens = await auth.async_sign_in(username, password)
        client = KohlerClient(session, auth)
        customer = await client.async_get_customer()

        self._kohler_summary = customer.describe()
        _LOGGER.debug("Kohler account: %s", self._kohler_summary)
        self._kohler_topology = await self._async_detect_topology(client, customer)
        self._kohler_data = {
            CONF_USERNAME: username,
            CONF_REFRESH_TOKEN: tokens.refresh_token,
            CONF_TENANT_ID: tokens.tenant_id,
            CONF_TEMPERATURE_UNIT: customer.temperature_unit,
            CONF_WATER_UNITS: customer.water_units,
            "has_device": bool(customer.supported_devices),
        }

    async def _async_detect_topology(self, client, customer) -> tuple[int, int] | None:
        """Work out the outlet split without asking, if either device will tell us.

        The valve is preferred: ``gcsadvancestate`` is its own account of its hardware and
        needs no controller. A HUB-only account has no valve id to query, so it falls back
        to a controller's zone configuration — the first of them that answers.
        """
        # The first valve that answers. As with the controllers below, this only decides
        # the entry's model; at setup each valve reads its own layout from the same
        # endpoint, so a second valve of a different model is not held to this answer.
        for valve in customer.gcs_devices:
            try:
                setting = await client.async_get_gcs_settings(valve.device_id)
                detected = topology_from_valve_settings(setting)
                if detected:
                    _LOGGER.debug(
                        "Topology from valve %s: %s", valve.device_id, detected
                    )
                    return detected
            except (AuthError, KohlerError) as err:
                _LOGGER.debug(
                    "Could not read valve settings for %s: %s", valve.device_id, err
                )

        # The first controller that answers. This only decides the entry's model — the
        # valve's layout, and the fallback for a controller whose own read fails. At setup
        # the coordinator reads every controller's configuration for itself, so a second
        # bathroom with a different valve is not held to this answer.
        for controller in customer.hub_devices:
            try:
                config = await client.async_get_hub_configuration(controller.device_id)
                detected = topology_from_hub_configuration(
                    config.get("configuration") or {}
                )
                if detected:
                    _LOGGER.debug(
                        "Topology from controller %s: %s",
                        controller.device_id,
                        detected,
                    )
                    return detected
            except (AuthError, KohlerError) as err:
                _LOGGER.debug(
                    "Could not read controller configuration for %s: %s",
                    controller.device_id,
                    err,
                )

        _LOGGER.debug("Outlet topology could not be detected; asking the user")
        return None

    # ------------------------------------------------------------------ #
    # Step 2: valve model
    # ------------------------------------------------------------------ #
    async def async_step_valve(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            data = {k: v for k, v in self._kohler_data.items() if k != "has_device"}
            data[CONF_VALVE_MODEL] = user_input[CONF_VALVE_MODEL]
            chosen = get_valve_model(user_input[CONF_VALVE_MODEL])
            data[CONF_ZONE_OUTLETS] = [
                chosen.outlets_valve1,
                chosen.outlets_valve2,
            ]

            if self._kohler_reauth:
                entry_id = self.context.get("entry_id")
                entry = (
                    self.hass.config_entries.async_get_entry(entry_id)
                    if entry_id
                    else None
                )
                if entry is not None:
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, **data}
                    )
                    # The explicit reload is required, not belt-and-braces. Reauth usually
                    # changes nothing but the refresh token, and `_async_update_listener`
                    # deliberately ignores that key — so leaving the reload to the listener
                    # would leave `KohlerAuth` holding the dead token that caused the reauth
                    # in the first place. Where other fields changed too, the listener may
                    # also fire and reload a second time; harmless, and reauth is rare.
                    await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

            return self.async_create_entry(
                title=f"Kohler Anthem ({data[CONF_USERNAME]})", data=data
            )

        # Detection succeeded and matches a catalogue model: skip the question entirely.
        detected = self._kohler_topology
        if detected is not None:
            model = model_for_topology(*detected)
            if model.sku in VALVE_MODELS:
                _LOGGER.info(
                    "Detected %s — %s; no valve model needed",
                    model.sku,
                    describe_topology(detected),
                )
                return await self.async_step_valve({CONF_VALVE_MODEL: model.sku})

        return self.async_show_form(
            step_id="valve",
            data_schema=_valve_schema(),
            description_placeholders={"summary": self._kohler_summary},
        )

    # ------------------------------------------------------------------ #
    # Reauth
    # ------------------------------------------------------------------ #
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The stored refresh token expired or was revoked; ask for the password again."""
        self._kohler_reauth = True
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry_id = self.context.get("entry_id")
        entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        username = (entry.data.get(CONF_USERNAME) if entry else "") or ""
        if user_input is not None:
            merged = {CONF_USERNAME: username, **user_input}
            return await self.async_step_user(merged)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": username},
        )


class KohlerAnthemOptionsFlow(OptionsFlow):
    """Configure display options for Kohler Anthem."""

    def __init__(self, config_entry: ConfigEntry | None = None) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage how multi-zone outlets, controls, and sensors are grouped."""
        entry = self._entry
        if entry is None:
            try:
                entry = self.config_entry
            except (AttributeError, ValueError):
                entry = None
        existing_options = dict(entry.options) if entry is not None else {}

        if user_input is not None:
            # Merge with `existing_options` rather than replacing wholesale:
            # `entry.options` also holds per-valve Warmup Auto-Restore and
            # report-log keys.
            return self.async_create_entry(
                title="",
                data={**existing_options, **user_input},
            )

        current_grouping = existing_options.get(
            CONF_ZONE_GROUPING, DEFAULT_ZONE_GROUPING
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current_grouping),
        )
