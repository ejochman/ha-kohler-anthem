"""Protocol constants for the Kohler Anthem cloud and local APIs.

Values here are app-global — baked into the Konnect Android app and identical across
accounts. Nothing in this module is a per-user secret.

Originally recovered from Konnect Android 3.0.1 and re-checked against **3.0.6** (version
code 260) on 2026-10-07. ``docs/protocol/`` is the developer reference for everything here —
what each endpoint takes and returns, which facts are app-confirmed and which live-verified.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Cloud API
# ---------------------------------------------------------------------------
API_BASE = "https://api-kohler-us.kohler.io"

# The APIM subscription key identifies the app to Kohler's API gateway. It is app-global and
# stable (verified identical across sessions and accounts), not a per-user credential.
# api-kohler-us.kohler.io does NOT require mTLS — the client certificate in the APK is only
# for the alternate *.kohlerkonnect-apim.azure-api.net gateway, which this client never uses.
APIM_SUBSCRIPTION_KEY = "429ecb1d0b5e4258aa0a2bfadd82a493"

# ---------------------------------------------------------------------------
# Azure AD B2C auth
# ---------------------------------------------------------------------------
# Writes to /commands/* are only accepted for tokens issued by the B2C_1A_signin policy.
# ROPC-policy tokens get HTTP 403 on writes; reads accept either. That is why sign-in is
# interactive rather than username/password.
CLIENT_ID = "8caf9530-1d13-48e6-867c-0f082878debc"
API_RESOURCE = "f5d87f3d-bdeb-4933-ab70-ef56cc343744"
B2C_TENANT = "konnectkohler.onmicrosoft.com"
B2C_SIGNIN_POLICY = "B2C_1A_signin"
B2C_AUTHORITY = (
    f"https://konnectkohler.b2clogin.com/tfp/{B2C_TENANT}/{B2C_SIGNIN_POLICY}"
)
B2C_AUTHORIZE_URL = f"{B2C_AUTHORITY}/oauth2/v2.0/authorize"
B2C_TOKEN_URL = f"{B2C_AUTHORITY}/oauth2/v2.0/token"
B2C_SCOPE = f"openid offline_access https://{B2C_TENANT}/{API_RESOURCE}/apiaccess"

# The registered redirect URI. Probed against B2C 2026-08-11: this exact value is accepted
# and near misses are not (msauth.com.example.fake://auth, msauth.com.kohler.hermoth://other
# and .../auth/extra all return AADB2C90006), so validation is strict and this is a genuine
# registration rather than loose scheme matching.
#
# The older APK-derived URI "msauth://com.kohler.hermoth/2DuDM2vGmcL4bKPn2xKzKpsy68k%3D" is
# NO LONGER REGISTERED — B2C rejects it outright. Any flow still using it is broken before
# the browser is even involved.
#
# Nothing ever navigates here. The sign-in runs server-side and reads the code out of the
# 302 Location header, so the unresolvable custom scheme never reaches a browser.
B2C_REDIRECT_URI = "msauth.com.kohler.hermoth://auth"

# Server-side sign-in endpoints (B2C custom-policy "SelfAsserted" flow).
B2C_POLICY_BASE = f"https://konnectkohler.b2clogin.com/{B2C_TENANT}/{B2C_SIGNIN_POLICY}"
B2C_SELF_ASSERTED_URL = f"{B2C_POLICY_BASE}/SelfAsserted"
B2C_CONFIRMED_URL = f"{B2C_POLICY_BASE}/api/CombinedSigninAndSignup/confirmed"

# B2C error codes worth distinguishing for the UI.
ERROR_BAD_CREDENTIALS = "AADB2C90053"
ERROR_REDIRECT_NOT_REGISTERED = "AADB2C90006"

# B2C rotates the refresh token on every silent refresh; dropping the new one strands the
# account until the next interactive sign-in. Refresh tokens last up to ~90 days.
TOKEN_EXPIRY_MARGIN_SECONDS = 300

# ---------------------------------------------------------------------------
# SKUs
# ---------------------------------------------------------------------------
# GCS is the Anthem digital valve body (built-in Wi-Fi, addressed directly).
# HUB is the Anthem Plus Linux system controller (drives valves, music, light, steam).
# Device IDs are NOT a reliable discriminator — an Anthem Plus controller's id can begin
# with "gcs". Always branch on the sku field.
SKU_GCS = "GCS"
SKU_HUB = "HUB"

# ---------------------------------------------------------------------------
# Reads — /devices/api/v1/device-management/
# ---------------------------------------------------------------------------
DEVICE_API = "/devices/api/v1/device-management"
FIRMWARE_API = "/platform/api/v1/firmware"
CUSTOMER_DEVICE = f"{DEVICE_API}/customer-device/{{tenant_id}}"
HUB_STATE = f"{DEVICE_API}/hub-state/{{device_id}}"
HUB_FAVORITES = f"{DEVICE_API}/hub-experience/{{device_id}}/favorites"
HUB_EXPERIENCES = f"{DEVICE_API}/hub-experience/{{device_id}}/experiences"
HUB_CONFIGURATION = f"{DEVICE_API}/hub-configuration/{{device_id}}"
HUB_DIAGNOSTICS = f"{DEVICE_API}/hub-diagnostics/{{device_id}}"
HUB_DIAGNOSTICS_ACTIVE = f"{DEVICE_API}/hub-diagnostics/{{device_id}}/active"
# Same contract as `GCS_USAGE` below; the response adds hot inlet temperatures.
HUB_USAGE = f"{DEVICE_API}/hub-usage/{{device_id}}"
GCS_PRESETS = f"{DEVICE_API}/gcs-preset/{{device_id}}"
GCS_STATE = f"{DEVICE_API}/gcs-state/{{device_id}}"
# The valve's settings block — the only source of outlet topology that does not need a
# controller. Distinct from gcs-configuration, which is null on a controller-attached valve.
GCS_ADVANCE_STATE = f"{DEVICE_API}/gcs-state/gcsadvancestate/{{device_id}}"
# The valve's own configuration record. **Not** the source of outlet topology — on a
# controller-attached valve every structural field comes back null and `GCS_ADVANCE_STATE`
# above is what to read instead. This is here for `about` (firmware) and to settle whether
# a GCS-only install populates the rest, which no capture has ever covered.
GCS_CONFIGURATION = f"{DEVICE_API}/gcs-configuration/{{device_id}}"
# Gateway, valve and touch-interface identity — `{gatewayConfigInfo: {firmware, installDate,
# model, serialNo}, valvesConfigInfo: [{firmware, model, name, serialNo, status}],
# interfacesConfigInfo: [{firmware, name, status}]}`. A separate route from the plain
# configuration read (Konnect 3.0.6 `GET_ANTHEM_ABOUT_API`), and the one place serial
# numbers and model names for each part are published.
GCS_ABOUT = f"{DEVICE_API}/gcs-configuration/{{device_id}}/about"
# The valve's fault log — `{deviceId, errorDetails: [{errorCode, title, description, details,
# errorState, isActive, timestamp, valveId, component, area, ...}]}`. The app hides entries
# whose `errorCode` is `"0"` or empty and shows the server's own text; there is no local
# code table. Read on demand for diagnostics, never polled.
GCS_DIAGNOSTICS = f"{DEVICE_API}/gcs-diagnostics/{{device_id}}"
# The valve's own experience programs — the catalogue the app offers to add, not the ones
# stored on this valve (those come back in `gcs-preset` with `isExperience: "True"`).
GCS_EXPERIENCE_CATALOGUE = f"{DEVICE_API}/gcs-experience"
# **Water usage history — the endpoint behind the Konnect app's charts.**
#
# `?FromDate=…&ToDate=…&Interval=…` — PascalCase query parameters, unlike every other
# endpoint. What Konnect 3.0.6 sends (`rg0/t0.java` `b0()`, the view model every device type
# shares): dates as `MM-dd-yyyy`, and `Interval` as `Day` for its Week and Month tabs and
# `Month` for its Year tab. **It never sends `WEEK` or `YEAR`**, which is why `WEEK` is refused
# with a 400 at every range: it is not an interval the server knows. The app builds its week
# view from seven daily buckets, exactly as `ValveWeeklyWaterSensor` does.
#
# This integration sends ISO dates and uppercase `DAY` / `MONTH`, both verified live on the
# owner's account (2026-09-10/11) — the server accepts either form, so the working request
# was left alone rather than rewritten to match the app byte for byte.
#
# The response also carries per-bucket `averageBlendTemperature` and
# `numberOfTimesValveSwitchedOn`; see `docs/protocol/platform.md` §Water usage.
GCS_USAGE = f"{DEVICE_API}/gcs-usage/{{device_id}}"

# ---------------------------------------------------------------------------
# Firmware — /platform/api/v1/firmware/
# ---------------------------------------------------------------------------
# `GET …?releasetarget=Public` answers `{currentFirmware, firmware (the latest),
# firmwareUpdateAvailable, mandatoryUpdate, otaStatus, skip, estimatedTimeForOTA,
# fileSizeInMb, url, configuration}`. The app decides "update available" from
# `firmwareUpdateAvailable` alone. A `POST` to the same path without the query starts an
# install (`{tenantId, firmwareNumber, releaseTarget: "Public"}`); this integration only
# reads — installing is left to the app, which refuses while water runs or the device is
# `Disconnected`.
GCS_FIRMWARE = f"{FIRMWARE_API}/gcs/{{device_id}}?releasetarget=Public"
GCS_GATEWAY_FIRMWARE = f"{FIRMWARE_API}/gcs/gateway/{{device_id}}?releasetarget=Public"
HUB_FIRMWARE = f"{FIRMWARE_API}/hub/{{device_id}}?releasetarget=Public"

# ---------------------------------------------------------------------------
# Commands — /platform/api/v1/commands/
# ---------------------------------------------------------------------------
COMMANDS = "/platform/api/v1/commands"

# GCS: no bare on/off exists. Every start specifies the full valve state.
GCS_SOLOWRITESYSTEM = f"{COMMANDS}/gcs/solowritesystem"
GCS_CONTROL_PRESET = f"{COMMANDS}/gcs/controlpresetorexperience"
GCS_START_PRESET = f"{COMMANDS}/gcs/startpreset"
GCS_WRITE_PRESET = f"{COMMANDS}/gcs/writepreset"
# Whole-record replace of one outlet's configuration — eleven string keys, one call per
# outlet, chained on success. See `docs/gcs/api.md` §1c, and `GcsDevice.async_write_outlet_config`
# for the guards this endpoint needs.
GCS_WRITE_OUTLET_CONFIG = f"{COMMANDS}/gcs/writeoutletconfig"
GCS_CREATE_PRESET = f"{COMMANDS}/gcs/createpreset"
GCS_WARMUP = f"{COMMANDS}/gcs/warmup"
# Restart the valve: `{deviceId, sku, tenantId, reset: "productRestart"}` — the app's
# Settings → Restart Product, behind "Are you sure you want to restart this product?". The
# valve reboots, so any running water stops. It cannot revive a valve that has dropped off
# the cloud: that valve never receives the command.
GCS_VALVE_RESET = f"{COMMANDS}/gcs/valvereset"
GCS_RESET_RESTART = "productRestart"

# Preset and experience ids share `presetOrExperienceId`. Konnect 3.0.6 treats 1-11 as
# presets and 17 and up as experiences (`db0/c.java`), and starts **both** with the same
# `controlpresetorexperience {preset, action}` body.
GCS_EXPERIENCE_MIN_ID = 17

# HUB: favorite-centric. There is no direct "set outlet/temp/flow now" command —
# `valvecontrol` and `steamcontrol` take only an on/off toggle and run the controller's own
# defaults. Konnect 3.0.6 has exactly the nine HUB command paths below plus
# `hub/factoryreset`; there is no light, music, volume or temperature command anywhere.
HUB_VALVE_CONTROL = f"{COMMANDS}/hub/valvecontrol"
HUB_STEAM_CONTROL = f"{COMMANDS}/hub/steamcontrol"
HUB_FAVORITE_CONTROL = f"{COMMANDS}/hub/favorite/control"
HUB_FAVORITE = f"{COMMANDS}/hub/favorite"
HUB_STOP_ALL = f"{COMMANDS}/hub/stopall"
HUB_SHOWER_EXPERIENCE = f"{COMMANDS}/hub/shower/experience/control"
HUB_STEAM_EXPERIENCE = f"{COMMANDS}/hub/steam/experience/control"
HUB_ICESHOWER_EXPERIENCE = f"{COMMANDS}/hub/iceshower/experience/control"

# All three experience endpoints share one body; the path is chosen by the category the
# experience came from in the experiences read. Sending a shower experience to the steam
# path does not work.
EXPERIENCE_ENDPOINTS = {
    "showerExperiences": HUB_SHOWER_EXPERIENCE,
    "steamExperiences": HUB_STEAM_EXPERIENCE,
    "iceShowerExperiences": HUB_ICESHOWER_EXPERIENCE,
}

# ---------------------------------------------------------------------------
# Response status codes
# ---------------------------------------------------------------------------
# Kohler returns these inside the response body, not only as HTTP status. Konnect models
# `statusCode` as a **string** and translates it only when the HTTP status is 400
# (`oy0/b.java`), so compare as strings — an int comparison misses `"900"`.
STATUS_DEVICE_OFFLINE = "900"
# Editing a favorite while the system is running is rejected. Activating one is not. The
# app shows the same text for 901.
STATUS_DEVICE_RUNNING = "902"
STATUS_DEVICE_RUNNING_ALT = "901"
# The rest of the app's table, for messages. 906/913/914/919 belong to other products.
STATUS_MESSAGES = {
    "903": "a firmware update is in progress on the device",
    "904": "the device reported a product error",
    "905": "the device is preparing to retry a firmware update",
    "908": "the firmware is already up to date",
    "909": "the device reported a product error",
    "911": "the device reported a product error",
    "915": "the maximum number of favorites is already stored",
    "916": "a favorite with that name already exists",
    "917": "Kohler's cloud reported an unspecified error",
    "918": "the device reported a product error",
}

# ---------------------------------------------------------------------------
# GCS warmup modes
# ---------------------------------------------------------------------------
# Warmup is a mode toggle, not a run-now command: the command IS the enable/disable. Once
# enabled it runs automatically per the chosen mode. The library this replaces sent no
# warmUp field at all, so the device accepted the request (200) and ignored it.
WARMUP_DISABLED = "warmUpDisabled"
WARMUP_ALL_OUTLETS_NOW = "warmUpAllOutletsWithNoStartDelay"
WARMUP_ALL_OUTLETS = "warmUpAllOutlets"
WARMUP_SELECTED_OUTLETS_NOW = "warmUpSelectedOutletsWithNoStartDelay"
WARMUP_SELECTED_OUTLETS = "warmUpSelectedOutlets"

#: The three the **current** Konnect app offers, and the only ones anything should write.
#: Owner-established 2026-08-20 against the app in their hands. A 2026-08-20 decompile of
#: Konnect Android 3.0.1 had reported only two — disabled and all-outlets — and called
#: selected-outlets unverified; this install's own captures settle it, because the valve
#: held `warmUpSelectedOutletsWithNoStartDelay` three separate times on 2026-08-13 with no
#: other client in play. **The app moved on; the decompile was of an older build.**
WARMUP_MODES_CURRENT = (
    WARMUP_DISABLED,
    WARMUP_ALL_OUTLETS_NOW,
    WARMUP_SELECTED_OUTLETS_NOW,
)

#: The two delayed-start variants, kept because the firmware still parses them and a valve
#: could be holding one. **Decodable, not writable**: nothing defines what their delay is —
#: the app has no control that sets one, and "delay" appears nowhere in its string
#: resources. See `docs/gcs/api.md` §3e.
WARMUP_MODES_LEGACY = (WARMUP_ALL_OUTLETS, WARMUP_SELECTED_OUTLETS)

#: Every value the firmware recognises.
WARMUP_MODES = WARMUP_MODES_CURRENT + WARMUP_MODES_LEGACY

# Konnect 3.0.6 confirms all of the above (`jc0/o.java`): it writes only the three current
# modes, reads the delayed pair as plain "enabled", and echoes `delayStart` from the panel's
# UI config without ever interpreting it. One coupling worth knowing: the app picks the
# **selected-outlets** mode whenever its selected-outlets option is on **or** the panel's
# `waterSavingMode` is `Enabled` — so a valve in water-saving mode warms only selected
# outlets whatever else was chosen.

# warmUpState carries two independent axes: `warmUp` is the mode above, `state` is whether
# it is running right now.
WARMUP_IN_PROGRESS = "warmUpInProgress"
WARMUP_NOT_IN_PROGRESS = "warmUpNotInProgress"

# Registering a "mobile device" is how a client obtains Azure IoT Hub credentials for the
# real-time status stream. The returned SAS password is short-lived and per-session: obtain
# it per run and never persist it.
MOBILE_SETTINGS = "/platform/api/v1/mobile/settings"

# ---------------------------------------------------------------------------
# MQTT — Azure IoT Hub
# ---------------------------------------------------------------------------
# Status arrives as direct-method messages. The confirmed write path is HTTPS /commands/*:
# neither the app nor this client publishes control over MQTT.
#
# ⚠️ **The app is not entirely silent, though.** Konnect 3.0.6 (`qy0/a.java`) publishes one
# device-to-cloud telemetry event every time it connects, on `devices/{id}/messages/events`:
# `{type: "MOBILECONNECT", sku: "MOBILE", deviceid, tenantid, timestamp, ver: "1.0",
# protocol: "MQTT", ttl: "5000", durable: "true", simulated: "false"}`. This client does not
# send it. Whether its absence is what makes a fresh registration silent for the first
# minute (`MQTT_WARMUP_SECONDS`) is an untested hypothesis — see `docs/protocol/platform.md`.
MQTT_PORT = 8883
# Direct methods must be answered here or the service treats them as unhandled.
MQTT_RESPONSE_TOPIC = "$iothub/methods/res/200/?$rid={rid}"

# ONE topic carries everything. Across 856 messages in 20 capture logs, 100% arrived on
# `$iothub/methods/POST/ExecuteControlCommand/?$rid=N` and none on any other topic. The
# device-scoped topics some clients also subscribe to (devices/<id>/messages/events/# and
# .../devicebound/#) are acknowledged but have never delivered anything.
MQTT_SUBSCRIBE_TOPIC = "$iothub/methods/POST/#"

# A fresh registration receives NOTHING for roughly the first minute, despite a clean
# CONNECT and granted SUBACKs. Register once, hold the connection, and treat early silence
# as meaningless. Reconnecting per command guarantees receiving nothing at all.
MQTT_WARMUP_SECONDS = 60

# The direct-method subscription is account-level, so a session opened for one device
# receives messages for both. Filter on payload deviceid and sku.
MSG_GCS_SOLO_STATUS = "GCS_SOLO_STS"
MSG_GCS_PRESET_STATUS = "GCS_PRESET_STS"
MSG_GCS_WARMUP_STATUS = "GCS_WARM_STS"
MSG_GCS_EXPERIENCE_STATUS = "READ_GCS_EXPERIENCE_STS"
MSG_GCS_OUTLET_CONFIG = "READ_GCS_OUTLET_CONFIG_CFG"
# One record per touch interface: units, haptics, accessibility, `waterSavingMode`,
# language… The app writes it back whole via `/commands/gcs/writeuiconfig`; this integration
# only treats it as a sign of life. Fields are listed in `docs/protocol/gcs_valve.md`.
MSG_GCS_UI_CONFIG = "READ_GCS_UI_CFG"
MSG_GCS_REBOOT = "DEVICE_REBOOT_STS"
# Seen live but **not in Konnect 3.0.6 at all** — nothing in the app handles it, so its shape
# is known only from captures.
MSG_FIRMWARE_VERSIONS = "READ_ALL_INTERFACES_FIRMWARE_VERSION_STATUS_INFO"
# Two the app handles that this integration only records as proof of life:
# `GCS_DELETE_PROFILE_STS` (`attributes[0].status == "true"` after Remove Product) and
# `READ_DISPENSED_WATER_VOLUME_STS` (`attributes[0].volume`, a running counter the app's
# bath-fill setup samples before and after filling — requested via
# `/commands/gcs/bathfillervolume {index: "0"}`).
MSG_GCS_DELETE_PROFILE = "GCS_DELETE_PROFILE_STS"
MSG_GCS_DISPENSED_VOLUME = "READ_DISPENSED_WATER_VOLUME_STS"

#: `currentSystemState` values. The first two are the only ones ever captured; Konnect 3.0.6
#: also acts on the last two — `error` (case-insensitive) is a valve fault, and
#: `FirmwareUpdate` means an update is installing (the app sends the user away from controls).
SYSTEM_STATE_NORMAL = "normalOperation"
SYSTEM_STATE_SHOWER = "showerInProgress"
SYSTEM_STATE_ERROR = "error"
SYSTEM_STATE_FIRMWARE = "FirmwareUpdate"

MSG_HUB_SHOWER_VALVE = "SHOWER_VALVE_STS"
MSG_HUB_STEAM = "STEAM_STS"
MSG_HUB_MUSIC = "MUSIC_STS"
MSG_HUB_LIGHT = "LIGHT_STS"
MSG_HUB_FAVORITE = "FAVORITE_STS"
MSG_HUB_SYSTEM = "SYSTEM_STS"
# Carries the whole favorites list rather than a delta. Pushed after **every** create,
# edit, and delete (9 of 9 in the captures) as well as on reboot, so it is the favorite
# refresh mechanism and no polling is needed.
#
# The CREATE_FAVORITE_STS / UPDATE_FAVORITE_STS / DELETE_FAVORITE_STS acknowledgements are
# deliberately not modelled: each is followed 1-3 s later by this snapshot carrying the full
# list, so handling them would be work for a delta we are about to receive in full.
MSG_HUB_FAVORITES_SNAPSHOT = "FAVORITES_SNAPSHOT"
# Experience run state, one code per category — `{code, name, ready, status}` per attribute,
# where `name` is the experience title and `status` is `ON`/`OFF`.
MSG_HUB_SHOWER_EXPERIENCE = "SHOWER_EXP_STS"
MSG_HUB_STEAM_EXPERIENCE = "STEAM_EXP_STS"
MSG_HUB_ICE_EXPERIENCE = "ICE_SHOWER_EXP_STS"
MSG_HUB_EXPERIENCE_CODES = frozenset(
    {MSG_HUB_SHOWER_EXPERIENCE, MSG_HUB_STEAM_EXPERIENCE, MSG_HUB_ICE_EXPERIENCE}
)

#: `STEAM_STS` `status` while the steam generator runs its self-clean. The app shows "Power
#: clean is in progress. Please stay out of your shower." Neither ON nor OFF.
HUB_STEAM_POWERCLEAN = "POWERCLEAN"

#: Device `connectionState` values. Only `Connected` has ever been captured; Konnect 3.0.6
#: (`mc0/n.java`, `ui/ota/f.java`) uses `Disconnected` as the negative and treats a device as
#: online unless the value is exactly that.
CONNECTION_CONNECTED = "Connected"
CONNECTION_DISCONNECTED = "Disconnected"

# ---------------------------------------------------------------------------
# HUB local LAN API
# ---------------------------------------------------------------------------
# Setup, configuration, and diagnostics only — this surface cannot actuate anything on
# firmware 2.88. water_test_start runs a fixed zone1/outlet1 ~5s plumbing self-test and
# ignores any temperature/flow/outlet fields. Real control is cloud-side.
#
# Konnect 3.0.6 never calls this API itself: it opens the controller's own web page (the
# "Embedded Server Page") in a WebView, at the address `hub-configuration` publishes as
# `configuration.about.hub.wlan.ip`. That is also how a client can find the host.
LOCAL_API_BASE = "http://{host}/web/api/v1/device"
LOCAL_LOGIN = "request_user_login"
LOCAL_COMMAND = "req_update_command"

# The hub's JWT is short-lived (minutes) and obtained from a PIN. These endpoints are
# reachable with no token at all, and two of them mutate state.
LOCAL_PREAUTH_ENDPOINTS = frozenset(
    {
        "get_hub_running_state",
        "get_hub_version_info",
        "hub_date_config_state",
        "set_hub_datetime",
    }
)

# Baked into the hub's Angular bundle. Used to encrypt the PIN as
# base64(RSA_PKCS1v15(sha256(pin).hexdigest_ascii)).
LOCAL_RSA_PUBLIC_KEY_B64 = (
    "MIGJAoGBAOBnPtJlU6y62vyrcHgqZPAlr+FM10BpUxBvRx5u0fXNEjXcda4y3WSU"
    "2ECzf9HcmDU5r6fD2jiFPyTuXu7jY2qzAI7QME6eoaJd2q+QLKpcUVq5MTeFo9b6"
    "zpZlGHUiiy0NrFdKPjD+UdPXi/t1oEKaj/loWiZ7p0P02paUoI41AgMBAAE="
)
