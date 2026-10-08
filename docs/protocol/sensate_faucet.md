# Kohler Sensate faucet: cloud protocol (app-side)

**Source:** Kohler Konnect for Android **3.0.6** (version code 260), decompiled
with jadx 1.5.6 on 2026-10-07. Covers SKU `SEN` (Sensate) and its sibling `SET`.

**Caveat.** Everything here comes from the app's side: what the 3.0.6 app sends,
what it expects back, and how it reacts. It does not describe what the faucet
or the Kohler cloud actually does. Field names and request shapes are exact,
because they come from Gson `@SerializedName` models and Retrofit annotations.
Value ranges and meanings come from app logic, and the server may accept more
or less than the app sends. Items marked **live check** still need to be
confirmed against a real faucet. "Not found in the decompile" means just that;
the behaviour may still exist server-side.

Shared mechanics (auth, API base and APIM key, `customer-device`, mobile
registration, the IoT Hub MQTT transport, the response-body `statusCode` table,
the general firmware and notification patterns, water-usage request format, and
the freeze-mitigation config endpoint) are in [platform.md](platform.md). This
file covers only what is faucet-specific.

**Finding things in the decompile.** Paths are relative to jadx's `sources/`
root. The short package names (`ah0/`, `bh0/`, `la0/` …) are obfuscated and
change between builds, so every citation also gives a string, class, field or
method name you can grep for. `la0/b.java` is the repository wrapper over
`DeviceApiCall` and `PlatformApiCall`. Endpoint constants live in
`com/utils/network/retrofit/proxy/ApiConstant.java`.

---

## 1. Device types (SKUs) and how the app branches

| SKU | What it is | Evidence |
|---|---|---|
| `SEN` | Sensate kitchen faucet ("Kitchen Faucet") | product tile `rt0/m.java:30` (`productModel.x("SEN")`, `text_device_kitchen_faucet`) |
| `SET` | A second Konnect kitchen faucet that the app handles exactly like `SEN`. Probably the **Setra**: the reserved-name list `devices_name` in `res/values/arrays.xml:197` contains both "Sensate" and "Setra". | product tile `rt0/n.java:30` (`productModel.x("SET")`); Wi-Fi step text `text_provision_step_two_faucet` (strings.xml:2572): "network name starting with 'SEN…' or 'SET…'" |

No other faucet SKU appears in the decompile. In particular, `FAUCET` is not a
SKU anywhere in the app.

Where the app branches on `SEN`/`SET` (it treats them the same everywhere):

- **Dashboard → control screen:** `fr0/f.java:1049-1053` (`"SET"` / `"SEN"`)
  then `fr0/f.java:1886` opens `FaucetControlActivity`.
- **Provisioning:** `WifiProvisionActivity.java:783-784` (`case "SEN": case "SET":`).
  `ht0/q.java:113-133` maps the soft-AP SSID prefix `SEN-` → `SEN` and `SET-` → `SET`,
  and accepts a `SET-` SSID when the user picked `SEN`.
- **Firmware type:** `ui/ota/f.java:135,203,262`. Firmware type is
  `sensate` for check and install, and config type is `faucet` for the result read
  (§2.6).
- **Water usage:** `rg0/t0.java:307-326`. Both SKUs use the faucet usage call;
  the mapper is `rg0/t0.java:1550` (`g0(...)`, `getSenSateUsageDataDetailsList`).
- **Settings "Identify":** `ep0/b0.java:715-731` (`V3()`, `text_faucet_dispense_note`).
  For `SEN`/`SET`, Identify dispenses one cup (§4).

Every faucet request builder takes `sku` from the device record
(`devicesModel.getSku()`), never from a constant. Examples:
`ah0/r.java:369` (`G()`, dispense), `bh0/n.java:298` (`H()`, onoff),
`hh0/n.java:567` (`M()`, experience), `ep0/i0.java:1565` (`V()`, identify).

---

## 2. REST endpoints

The base URL, headers and auth are as described in platform.md. The app always
passes `"v1"` for `{version}`; see the call sites in `la0/b.java` listed below.

### 2.1 Summary

| # | Method | Path | Retrofit method (`la0/b.java` call) | Body → Response model |
|---|---|---|---|---|
| R1 | GET | `/devices/api/v1/device-management/faucet-state/{deviceid}` | `getSenSateState` (`PlatformApiCall.java:296`, `la0/b.java:9557`), const `SENSET_STATE_API` (`ApiConstant.java:260`) | – → `FaucetStateResponse` |
| R2 | GET | `devices/api/v1/device-management/faucet-configuration/{deviceid}` | `getFaucetDeviceConfiguration` (`DeviceApiCall.java:187`, `la0/b.java:9950`), const `GET_DEVICE_CONFIGURATION` (`:143`); also `getDevicesConfiguration` with `{deviceType}`=`faucet` (`DeviceApiCall.java:181`, const `GET_DEVICE_CONFIGURATIONS_V` `:145`) | – → `DeviceConfigurationResponse` |
| R3 | GET | `/devices/api/v1/device-management/faucet-experience?DeviceIds={deviceId}` | `getPresetListData` (`PlatformApiCall.java:293`, `la0/b.java:6790`) | – → `PresetListResponse` |
| R4 | GET | `/devices/api/v1/device-management/faucet-experience/{deviceid}/experience/{experienceid}` | `getFaucetExperiences` (`DeviceApiCall.java:190`, `la0/b.java:10419`), const `FAUCET_EXPERIENCES` (`:109`) | – → `ExperienceListModel` |
| R5 | GET | `/devices/api/v1/device-management/customer-experience/{tenantid}` | `getAllPresetApi` (`DeviceApiCall.java:121`), const `GET_ALL_PRESET_API` (`:125`) | – → `AllPresetResponse` (faucet part: `faucetPresetsExperiences`, `recentlyUsedPresetsExperiences`) |
| R6 | GET | `/devices/api/v1/device-management/faucet-usage/{deviceId}?FromDate&ToDate&Interval` | `getSenSetWaterUsageData` (`DeviceApiCall.java:220`, `la0/b.java:10011`), const `SENSET_WATER_USAGE_API` (`:262`) | – → `WaterUsageModel` |
| C1 | POST | `/platform/api/v1/commands/faucet/onoff` | `senSateOnOffAPI` (`PlatformApiCall.java:320`, `la0/b.java:1139`), const `SENSET_ON_OFF_API` (`:258`) | `FaucetOnOffRequest` → `FaucetResponse` |
| C2 | POST | `/platform/api/v1/commands/faucet/dispense` | `sensateDispenseAPI` (`PlatformApiCall.java:332`, `la0/b.java:4531`), const `SENSET_DISPENSE_APIS` (`:256`) | `FaucetDispenseRequest` → `FaucetResponse` |
| C3 | POST | `/platform/api/v1/commands/faucet/experience` | `senSateStartExperienceAPI` (`PlatformApiCall.java:323`, `la0/b.java:1629`), const `SENSET_START_EXPERIENCE_API` (`:259`) | `FaucetExperienceRequest` → `FaucetResponse` |
| C4 | POST | `/platform/api/v1/commands/faucet/presetexperience` | `callFaucetPresetExperience` (`PlatformApiCall.java:161`, `la0/b.java:10862`, wrapper `H1`), const `FAUCET_PRESET_EXPERIENCE` (`:111`) | `PresetExperienceRequestModel` → `FaucetResponse` |
| C5 | POST | `/platform/api/v1/commands/faucet/factoryreset` | `callFactoryResetApis("v1","faucet","factoryreset",…)` (`PlatformApiCall.java:158`; caller `hp0/k.java:150`) | `FaucetFactoryResetRequest` → `FaucetFactoryResetResponse` |
| P1 | POST | `/devices/api/v1/device-management/faucet-experience` | `senSateCreateExperienceAPI` (`PlatformApiCall.java:317`, `la0/b.java:648`) | `FaucetCreateExperienceRequest` → `FaucetCreateExperienceResponse` |
| P2 | PATCH | `/devices/api/v1/device-management/faucet-experience/{deviceid}` | `senSateUpdateExperienceAPI` (`PlatformApiCall.java:326`, `la0/b.java:2129`), const `SENSET_UPDATE_EXPERIENCE_API` (`:261`) | `FaucetCreateExperienceRequest` → `FaucetCreateExperienceResponse` |
| F1 | GET | `/platform/api/v1/firmware/sensate/{deviceid}` (**no** `releasetarget` query) | `getFirmwareState` (`PlatformApiCall.java:284`, const `FIRMWARE_UPDATES_V` `:114`), via `la0/b.java:12592` (`I1`) | – → `FaucetFirmwareUpdateResponseModel` |
| F2 | POST | `/platform/api/v1/firmware/sensate/{deviceid}` | `postFirmwareUpdate` (`PlatformApiCall.java:308`), via `la0/b.java:12856` (`V2`); same URL as `SENSATE_FIRMWARE_UPDATE(S)` (`:252-253`, used only in log labels) | `FaucetFirmwareUpdateRequestModel` → `FaucetResponse` |
| N1 | POST | `/platform/api/v1/notifications` | `callPushNotificationAPI` (`PlatformApiCall.java:173`, `la0/b.java:4669`), const `PUSH_NOTIFICATION_API` (`:237`) | `PushNotificationRequest` → `CommonResponse` (§6; probably never sent) |

`SENSET_EXPERIENCE_LIST_API` and `SENSET_CREATE_EXPERIENCE_API` (`:255,257`) have
the same value as the literal paths of R3 and P1. Every model below is under
`com/utils/network/retrofit/proxy/`.

### 2.2 Reads

**R1 `faucet-state`.** Model: `platform/model/faucet/FaucetStateResponse.java`
+ `FaucetStateModel.java`.

| Field | Type | App usage |
|---|---|---|
| `connectionState` | String | Compared case-insensitively with `"Connected"`. Anything else, **including a missing value**, disables controls and shows offline (`bh0/n.java:360` `U()`; `hh0/n.java:736` `l0()` and `bh0/j.java:378-381` default it to `"Disconnected"`). The OTA pre-check tests for `"Disconnected"` (`ui/ota/f.java:540`). |
| `state.status` | String | `"On"` / `"Off"` (compared case-insensitively). A null status is treated as Off (`bh0/n.java:360-380`). |
| `state.handleState` | String | `"OPEN"` / `"CLOSED"`. **`CLOSED` disables remote control** (§5.2). |
| `state.progress` | String | **Firmware download state, not dispense progress.** `"Downloading"` while `Connected` makes the app show "Firmware Upgrade is in progress" and disable controls (`bh0/j.java:367-400` `U2()`; `hh0/n.java:736-752` `l0()`, where a null value defaults to `"Completed"`). |
| `state.quantity` | Double | Never read by the app. |
| `lastConnected`, `updatedTimestamp`, `createdTimestamp` | Long | Not used by faucet code. |
| `id`, `deviceId`, `ioTHub`, `lastUpdatedSource`, `deviceConnectionEventSequenceNumber` | String | Declared but not used by faucet code. Values of `lastUpdatedSource` are not found in the decompile. |

After an onoff or dispense, if MQTT is **not** connected, the app re-reads R1
3 s later (`bh0/n.java` `e0()` uses `sleepTime3sec`; `ah0/r.java:505-519` `c0()` uses `3000L`).

**R2 `faucet-configuration`.** Model:
`device/model/configuration/DeviceConfigurationResponse.java`.

| Field | Notes |
|---|---|
| `id`, `deviceId`, `sku`, `tenantId`, `version`, `applicationSource`, `installedDate`, `createdTime`, `updatedTimestamp`, `iot` | Top-level metadata. |
| `configuration.about` (`AboutResponse`) | Faucet-relevant: `name`, `model`, `modelNumber`, `serialNumber`, `hardware`, `installedDate`, `firmware` (`FirmwareModel`: `version`, `latestVersion`, `progress`, `releasedOn`, plus `application`/`bootloader`/`os`/… `VersionDetails`). |
| `configuration.otaInProgress` | Boolean (`ConfigurationModel.java:63`). **Not read by faucet code.** |
| `firmwareUpdate` (top-level, `firmwareupdate/FirmwareUpdate.java`) | `_interface`, `firmwareType`, `isAutoUpdate`, `lastUpdated`, `progress`, `progressPercent`, `retryAttempts`, `startTimestamp`, `status`, `version`. **Not read by faucet code.** |
| `leakDetectionHistory` | `List<LeakDetectionHistoryModel>`. **Each entry is `{ "leakDetectionTime": <Long epoch seconds> }`** (`LeakDetectionHistoryModel.java:13-14`). The About screen sorts entries newest first and renders each as a date and time row (`ep0/b0.java:1553` `C3()`, which multiplies by 1000). |

During a firmware install, the app polls R2 until `configuration.about.firmware.version`
equals the target version (§2.6).

**R3 faucet preset list** (used by the faucet's own screen). Query parameter
name is PascalCase `DeviceIds`. Response `PresetListResponse`:
`{ faucetExperienceList: [ FaucetExperienceListModel ] }`, where
`FaucetExperienceListModel` = `{ id, customerId, deviceId, sku, updatedTimestamp, experience: [ExperienceListModel] }`.
The app only reads `faucetExperienceList[0].experience` (`zg0/v.java:234` `R()`).

`ExperienceListModel` (`platform/model/preset/ExperienceListModel.java`):

| Field | Type | Meaning |
|---|---|---|
| `experienceId` | String | Preset id. The server assigns it; the app omits it on Insert. |
| `title` | String | Preset name (rules in §4.4). |
| `dispenseAmount` | Double | Liters. |
| `displayQuantity` | String | `"<amount> <Unit>"`, where Unit ∈ `Milliliters` `Liters` `Cups` `Quarts` `Gallons` (`preset/create/b.java:671` `g0()`). The amount may contain `¼ ½ ¾` glyphs, e.g. `"1¾ Quarts"` (`b.java:379` `a0()`). |
| `unit` | String | `"Standard"` or `"Metric"`, copied from the account's `waterUnits` (`b.java:376`). |
| `state` | String | `"Off"` on Insert. `"ON"`/`"OFF"` after MQTT. |
| `isStarted` | Boolean | Default `false`, so Gson always serializes it. |
| `progress` | String | Values not found in the decompile. |
| `operation` | String | Request-only: `"Insert"`, `"Update"` or `"Delete"`. |
| `createdTime`, `lastUsedTime` | Integer | Epoch seconds. |

**R4 single preset.** Returns one `ExperienceListModel`. Used by the
all-presets screen (`ui/home/preset/allpreset/b.java:1421` `q0()`, log label
`getFaucetPresetExperience`).

**R5 `customer-experience`.** `AllPresetResponse` (`platform/model/preset/AllPresetResponse.java`)
declares `faucetPresetsExperiences`, `recentlyUsedPresetsExperiences`, `dtv…`,
`gcs…`, `hub…`, `pfc…`, `sfc…` and `evo…`. Faucet entries are `PresetModel`
(`id`, `title`, `deviceId`, `logicalName`, `deviceLogicalName`, `sku`, `state`,
`lastUsedTime`, `createdTime`, `isExperience`, …).
**`sensateExperiences` is not read by 3.0.6.** It is declared only in
`device/anthem/model/AnthemExperienceResponseModel.java:45`, a model that no
Retrofit method returns.

**R6 `faucet-usage`.** The request format is shared (platform.md); the 3.0.6 app
sends `MM-dd-yyyy` with `Day`/`Month`. Response `device/model/WaterUsageModel.java`.
The faucet bucket list is `faucetUsageDataDetailsList` (`:168`, inner class
`SenSateUsageDataDetailsList` `:336`):
`{ intervalKey, quantity, waterUsage, hotWaterUsage, coldWaterUsage, usageDuration, temperature, volume }` (all Double except `intervalKey`).
Summary: `deviceId`, `interval`, `avg/min/max` × `WaterUsage`, `Quantity`,
`HotWaterUsage`, `ColdWaterUsage`, `UsageDuration`, `Temperature` (the model also
carries other products' counters).

The app uses only `intervalKey`, `waterUsage`, `usageDuration`, `maxWaterUsage`,
`avgWaterUsage`, `avgUsageDuration`, `maxUsageDuration` and `interval`
(`rg0/t0.java:1550-1575` `g0()`). `waterUsage` is liters; imperial users see it
×0.264172 as gallons (`qf0/b.java:182` `K()`), to 2 decimals for SEN/SET
(`rg0/e0.java:729`). `usageDuration` is **seconds**: the chart divides it by 60
(`uu0/l.java:316`).

### 2.3 Commands

The command response is `FaucetResponse { correlationId, timestamp }`.

**C1 onoff.** `FaucetOnOffRequest { action, deviceId, sku, tenantId }`.

| Caller | `action` sent |
|---|---|
| Faucet start/stop button (`bh0/j.java:1113,1120` `t3()`) | `"On"` / `"Off"` |
| Stop a running dispense (`ah0/r.java:378-380` `H()`) | `"Off"` |
| Stop a running preset (`hh0/n.java:461` `N()`, `fh0/v.java:366` `I()`) | `"OFF"` |

So the server accepts mixed case. The app **stops dispenses and presets with
onoff Off**; it never stops one through the experience endpoint.

**C2 dispense.** `FaucetDispenseRequest { deviceId, quantity (Double, liters), sku, tenantId }`.
There is no temperature, flow or unit field: the faucet uses whatever
temperature the handle is set to. The unit conversion before sending
(`ah0/r.java:408` `L()`) is:

| Unit | Factor to liters |
|---|---|
| Milliliters | × 0.001 |
| Liters | × 1 |
| Cups | × 0.2365880012512207 (float of 0.236588) |
| Quarts | × 0.946353 |
| Gallons | × 3.785411784 |

The result is computed in **single precision** and then widened to Double.
Fraction glyphs are parsed by `yg0/a.java` `a()` (`"¾"` → 0.75, `"1¼"` → 1.25).
The dispense button toggles: when water is running it sends onoff `"Off"`,
otherwise it sends dispense (`ah0/l.java:117-148`, inner class `a.C0012a`).

**C3 experience** (start a preset from the faucet's own screens).
`FaucetExperienceRequest` (`platform/model/faucet/FaucetExperienceRequest.java`;
**all fields are String**):

| Field | Value sent |
|---|---|
| `status` | Always `"ON"` (`hh0/n.java:570`; `fh0/v.java:430` `H()`) |
| `experienceId` | Preset id |
| `experienceTitle` | Preset title |
| `experienceQuantity` | Liters **as a string**, re-derived from `displayQuantity`, not from `dispenseAmount`. For example `"1 Cups"` → `"0.236588"` (`hh0/n.java:567-590` `M()`) |
| `deviceId`, `tenantId`, `sku` | from the device |

**C4 presetexperience** (start or stop a preset from the account-wide
All Presets / Recent screen). `PresetExperienceRequestModel { deviceId, experienceId, sku, status, tenantId }`
(`ui/home/preset/allpreset/b.java:1062` `u0()`). `experienceId` is `PresetModel.id`.
`status` toggles: it is `"OFF"` if the preset's current `state` is ON, else `"ON"`.
This is the only place the app stops a faucet preset through a preset endpoint.

**C5 factoryreset.** `FaucetFactoryResetRequest { deviceId, sku, tenantId }`. It is
sent from the remove-product flow before removing the device from the home
(`hp0/k.java:150`, body built in `I()`). It is destructive; do not use it.

### 2.4 Preset create / edit / delete (`faucet-experience`)

Builders are in `products/faucet/presentation/preset/create/b.java`.

| Action | Call | Body |
|---|---|---|
| Create, when the device has **no presets yet** (`is_list_empty` = true, `fh0/r.java:441-445`) | **P1 POST** `faucet-experience` | `{ customerId: tenantId, deviceId, id: deviceId, sku, experience: [ { operation: "Insert", state: "Off", isStarted: false, title, unit, dispenseAmount, displayQuantity } ] }` (`b.java:433` `F()`) |
| Create, when presets already exist | **P2 PATCH** `faucet-experience/{deviceId}` | same body as above (`b.java:498` `L()`, branch `"CreatePreset" && isListEmpty`) |
| Edit | P2 PATCH | same envelope; the existing `ExperienceListModel` with `operation: "Update"`, `state: null` (omitted), `isStarted: false`, plus new `title`, `unit`, `dispenseAmount`, `displayQuantity` |
| Delete | P2 PATCH | `{ experience: [ { experienceId, operation: "Delete", isStarted: false } ] }`, with **no** envelope fields (`b.java:470` `G()`; also `fh0/v.java:447` `J()`) |

The response is `FaucetCreateExperienceResponse { id, customerId, deviceId, sku, updatedTimestamp, experience[] }`.
**HTTP 400** on create or edit is shown as "This name is already in use. Try
entering another name." (`text_product_name_duplicate_error`, `b.java:235`, inner
class `d.c`). The app does not refuse to create or edit while
the water is running (no such check found for faucets).

### 2.5 Faucet settings the app exposes

Only these were found:

- **Identify & Rename.** "Identify" dispenses one cup:
  C2 with `quantity` 0.2365880012512207 (`ep0/b0.java:205` → `ep0/i0.java:1705` `e0()`).
- **Rename.** Shared `customer-device` PATCH; see platform.md.
- **Firmware** (§2.6), **About** (R2, including the leak list), **Remove product**
  (C5), **voice commands** (a link to `SENSATE_VOICE_COMMANDS_URL`, `ApiConstant.java:254`),
  and the account-level **Freeze Mitigation** page (§6).

There is **no** faucet setting for temperature, maximum run time, flow, sensor
range, or auto-off. No faucet "settings" or "diagnostics" endpoint exists in
3.0.6 (the `*-setting` and `*-diagnostics` endpoints are GCS/HUB only).

### 2.6 Firmware (`sensate` type)

Mapping is in `ui/ota/f.java`. Check: `b()` at `:141` → `I1(id,"sensate")`.
Result read: `c()` at `:209` → `z1(id,"faucet")`. Install: `d()` at `:268` →
`V2(req,id,"sensate")`.

1. **Check, F1.** `GET /platform/api/v1/firmware/sensate/{id}`. It has **no**
   `?releasetarget=Public`, unlike GCS/HUB, which go through
   `getAnthemFirmwareState` and `ANTHEM_FIRMWARE_GET_VERSION_API_V`.
   Response `FaucetFirmwareUpdateResponseModel`:
   `{ firmwareUpdateAvailable (bool), firmware (target version), currentFirmware, mandatoryUpdate, otaStatus, skip, estimatedTimeForOTA, fileSizeInMb (float), url, configuration }`.
   On a "not found" error the app retries every 2 s for up to 90 s (`jt0/l.java:330-346`).
2. **Pre-checks before install.** The app refuses if `faucet-state.state.status == "On"`
   (`ui/ota/f.java:731` `q()`, dispatched at `:650`) or if
   `connectionState == "Disconnected"` (`:540`). The estimated duration shown is
   "5 mins" (`C()` at `:102`).
3. **Install, F2.** `POST /platform/api/v1/firmware/sensate/{id}` with
   `{ tenantId, firmwareNumber: <F1.firmware>, releaseTarget: "Public" }`
   (`provision/faucet/wifiprovision/firmware/f.java:431-436`; target set from
   `getFirmware()` in `firmware/d.java:434-449`).
4. **Completion.** The app polls R2 every 10 s for up to 3 min until
   `configuration.about.firmware.version == firmwareNumber`
   (`firmware/f.java:200-245`; `CountDownTimer(180000, 10000)` at `:299`). Meanwhile:
   - `faucet-state.progress == "Downloading"` locks the control screen (§2.2).
   - MQTT `INSTALL_FIRMWARE_STS` reports `Installed` or `Aborted` (§3).
5. **Skip** uses the shared `PATCH /platform/api/v1/firmware/skip/{id}`
   (platform.md).

---

## 3. MQTT / direct-method messages for faucets

The transport, ack and envelope are in platform.md. The envelope
(`com/utils/network/mqtt/model/MqttCommonMessageDataModel.java`) has these fields:
`correlationid, deviceid, durable, internalid, messageid, protocol, simulated, sku, sysid, tenantid, timestamp, ttl, type, ver`.
The payload is under `data` = `{ type, code, attributes: [ … ] }`.

| `data.code` / `attributes[0].code` | Attributes (model) | Meaning, app reaction | Handlers |
|---|---|---|---|
| `SENSATE_STS` | `code`, `status` (`"On"`/`"Off"`), `handle` (`"OPEN"`/`"CLOSED"`) (`mqtt/model/faucet/SensateMqttAttributes.java`) | Water on/off and handle position. Fields are copied into the cached `faucet-state`. Then: `CLOSED` → controls disabled with "Open the handle to remote dispense water."; `OPEN`+`On` → running; `OPEN`+`Off` → idle. | `bh0/j.java:423` `W2()` (start/stop tab), `ah0/l.java:500` `V2()` + `:729` `B3()` (dispense tab), `PresetViewActivity.java:461`, `AllPresetHomeActivity.java:918` (refresh list), `gr0/j.java:983` (dashboard) |
| `SENSATE_EXP_STS` | `code`, `experienceid`, `name`, `status` (`"ON"`/`"OFF"`) (`mqtt/model/faucet/preset/PresetMqttAttributes.java`) | A preset started or ended. The app sets the matching preset's `state` and `isStarted`, treating anything other than `OFF` as ON. | `FaucetControlActivity.java:620` `d4()` (called from `j1()` at `:1062`), `gr0/j.java:983` (matches `experienceid`, `status == "ON"`), `AllPresetHomeActivity.java:918` |
| `SENSATE_LEAK_DETECTED_ALT` | read as `SensateMqttAttributes`; only `code` is checked | **Leak alert.** The app shows "There's been a leak detected" (`text_error_leak_detected`) and disables controls. | `ah0/l.java:506`, `bh0/j.java:440` |
| `INSTALL_FIRMWARE_STS` | `code`, `status`, `version` (`mqtt/model/firmwareupdate/FirmwareUpdateMqttAttributes.java`) | OTA result. `status` `"Installed"` → done; `"Aborted"` → failed (case-insensitive). Other values are not checked. | `SettingActivity.java:614` → `ep0/b0.java:1673` `R0()`; `WifiProvisionActivity.java:1949` |

Notes:
- `FaucetControlActivity.j1()` (`:1062`) drops messages whose `deviceid` does
  not equal the open device (case-sensitive compare). It forwards
  `SENSATE_EXP_STS` to `d4()` and forwards everything to both tab view models.
  Those view models treat any non-leak message as a `SENSATE_STS`-shaped status.
- **Dispense progress:** no message carries a dispensed amount or progress, and
  no dispense-specific code exists. A dispense shows up only as `SENSATE_STS`
  On, then Off.
- After sending a command, the screens start an MQTT "response" timer:
  `yw0/c.java:384-388`, 5 s ticks (`DEFAULT_TIMEOUT`), up to 2 retries. If no
  message arrives in time, an MQTT error banner is shown. `j1()` cancels the
  timer (`cVar.q()`).
- The app does not send faucet-specific MQTT messages. The `MOBILECONNECT`
  event is shared; see platform.md.

---

## 4. Settings, units, limits and name rules

### 4.1 Units
- The account `waterUnits` is `"Standard"` or `"Metric"`. It decides the preset
  `unit` field and the usage display (L vs gal).
- The dispense and preset pickers offer Milliliters, Liters, Cups, Quarts and
  Gallons. The default is Milliliters (`ah0/l.java:412`; list in `ah0/r.java` `O()`).
  There is **no fl oz** unit.
- Faucet picker defaults (`assets/deviceconfig.properties:57-70`, "# Faucet"):
  `amount`/`selectedItemMl` = 750, `selectedItemLtr` = ¾, `selectedItemCups` = 3,
  `selectedItemQt` = ¾, `selectedItemGl` = ¾ (0xBE in Latin-1 = "¾"), all `position*` = 2.
  The new-preset default is `"750 Milliliters"` (`CreatePresetActivity.java:110`).

### 4.2 Dispense and preset amount ranges (UI wheels)

The dispense wheels (`ah0/l.java`) and the preset wheels (`preset/create/b.java`)
are identical:

| Unit | Values | Source | Liters range |
|---|---|---|---|
| Milliliters | 250 … 11 000, step 250 | `ah0/l.java:1219` `z3()`; `b.java:631` `Z()` | 0.25 – 11.0 |
| Liters | ¼ … 11, step ¼ | `ah0/l.java:1189` `y3()`; `b.java:614` `W()` | 0.25 – 11.0 |
| Cups | 1 … 48, step 1 | `ah0/l.java:1121` `v3()`; `b.java:565` `Q()` | 0.237 – 11.36 |
| Quarts | ¼ … 12, step ¼ | `ah0/l.java:699` `A3()`; `b.java:658` `e0()` | 0.237 – 11.36 |
| Gallons | ¼ … 3, step ¼ | `ah0/l.java:1159` `x3()`; `b.java:601` `U()` | 0.946 – 11.36 |

So the app never sends less than **≈0.2366 L** (1 cup or ¼ qt) or more than
**≈11.36 L** (3 gal, 12 qt or 48 cups). No other client-side clamp was found.
Server-side limits are not found in the decompile (**live check**).

### 4.3 Preset limits
- **At most 10 presets per faucet** (client-side). When creating the 11th, the app
  shows "Your preset have reached the maximum limit." (`error_msg_915_preset`,
  `fh0/r.java:435`). The server code 915 maps to the same text (platform.md).

### 4.4 Preset name rules (client-side)
- Trimmed and not empty: "Preset name should not be empty." (`b.java:322-327`).
- **At most two words**: two or more spaces → "Sorry! You can not use more than two words as your preset name." (`text_preset_name_word_error`, `b.java:329`).
  The note explains why: names are used for voice commands (`text_preset_name_note`).
- **Max 18 characters**, and no emoji or other-symbol characters (`CreatePresetActivity.java:1011`;
  filter `ey0/b.java` drops `Character` types 19 SURROGATE and 28 OTHER_SYMBOL).
- Must not equal (ignoring case) any entry of `devices_name` (`arrays.xml:197`:
  "Sensate", "Setra", "DTV+", "Touchless", "H2Wise", "Name", …). Error:
  `error_msg_916` "This name already exists…" (`b.java:408` `c0()`).
- Must not equal another device's `logicalName`:
  `text_product_name_duplicate_error` (`b.java:420-426`).
- Repeated whitespace is collapsed before sending (`r11/a.java:16` `b()`).
- Duplicate preset names are rejected server-side with HTTP 400 (§2.4). The
  "no special characters or numbers" string `text_invalid_preset_name` belongs to
  another product and is not applied to faucets.

---

## 5. Errors, leaks and alerts

### 5.1 Status codes relevant to faucets

The general table is in platform.md. Faucet-specific findings:

| Code | Text (strings.xml) | Faucet use |
|---|---|---|
| **906** | `error_msg_906` (`:369`) "Water could not be dispensed. Please turn on your faucet manually." | **Faucet-specific.** All faucet view models handle HTTP **400** this way: if the mapped message equals the 906 text, show it; otherwise show the 900 "Product is offline." text (`bh0/n.java:392` `V()`, `hh0/n.java:522` `i0()`, `fh0/v.java:408` `b0()`; also `ep0/i0.java:646`, `ui/home/preset/allpreset/b.java:336`). The trigger is not found in the decompile; a closed handle is a plausible cause (**live check**). |
| 913 / 914 | "Please be seated…" / "…no one is seated…" | Seat-sensing toilet wording. No code references outside the table in `oy0/b.java`. **Not faucet.** |
| 919 | "Ozone generation error has occurred." | EvoCycle (`uf0/c1.java:3288`). **Not faucet.** |
| 915 | `error_msg_915_preset` | Preset limit (§4.3). |
| 916 | `error_msg_916` | Name clash (§4.4). |

### 5.2 Handle closed
`handleState == "CLOSED"`, from either `faucet-state` or `SENSATE_STS`, puts every
faucet screen in a "can't control" state with the text **"Open the handle to remote
dispense water."** (`text_error_sensate_closed`, strings.xml:1857). This is also
the dispense tab's default error text (`ah0/l.java:418-419`, `:555-556`; `bh0/n.java:360-366`;
`PresetViewActivity.java:478`). The freeze flow has a matching push type,
`freeze_emergency_handle_off_alert` (§6). In the app's model, remote dispense and
preset commands need the handle **OPEN**.

### 5.3 Leaks
- **Real-time:** MQTT `SENSATE_LEAK_DETECTED_ALT` (§3). Text: "There's been a leak detected".
- **History:** `faucet-configuration.leakDetectionHistory[]` = `{ leakDetectionTime: epoch seconds }`
  (§2.2). The app has no "clear" or acknowledge call; the list is display-only.
- No leak-specific push `type` for faucets was found. The FCM handler is generic
  (§6). Strings `text_error_water_leak` ("Water Leak!") and
  `text_leak_detection_alarm` (Aquifer) are not used by faucet code.

### 5.4 Other faucet strings
- Offline: `text_faucet_offline_error` (strings.xml:1911), used in the firmware flow.
- OTA busy: `text_firmware_update_in_progress` "Firmware Upgrade is in progress" (:1965).
- `text_error_sensate_already_on` (:1856) and `text_dispense_on` "0.5 ltr Dispense started"
  (:1781) exist but are not referenced.

---

## 6. Freeze mitigation, end to end

This is an account-level H2Wise (Phyn) feature. Kohler's cloud decides when to
open the faucet; the app only toggles the feature and handles notifications.

1. **Toggle.** My Account → "Freeze Mitigation" (`oi0/g.java` `d3()`, tab
   `tab_freeze_mitigation`) opens `FreezeMitigationToolActivity`. Its view model
   `ih0/f.java`:
   - `GET customer-device/{tenantId}/freezeMitigation` on open (`Q()` `:425`).
     The switch reflects `freezeMitigationEnabled`.
   - On a change, `PATCH` sends `FreezeMitigationConfigModel` with **only**
     `freezeMitigationEnabled` and `appVersion: "3.0.6"` (`H()` `:360-368`, `K()` `:392`).
     The other fields (`deviceIds`, `dontShowAgain`, `remindMeLater`,
     `freezeMitigationReminderCounter`, `timeStamp`) are never set in 3.0.6, and
     `showFreezeMitigationTogglePopup` is never read.
   - Explanatory text: `txt_freeze_mitigation_detect_info` (:3178), `txt_freeze_description` (:3175),
     `txt_freeze_mitigation_user_info` (:3179). When H2Wise detects freezing, Kohler
     fixtures "open briefly to relieve pressure".
2. **Push notifications.** These come through FCM (`common/fcm/KKFirebaseMessagingService.java:72` `u()`).
   Data keys: `deviceid`, `sku`, `type`, `logicalname`, `message`, `alertid`, `title`,
   `recordid`, `completiontime`. A notification is shown only when `sku`,
   `logicalname` and `message` are present. A tap opens `SplashActivity`, which
   routes by `type` (`SplashActivity.java:391` `I3()`):

   | `type` | Route |
   |---|---|
   | `freeze_warning_alert` | Dashboard with `phyn_notification_type` and `alertid` |
   | `freeze_emergency_alert` | Dashboard |
   | `freeze_emergency_disconnected_alert` | Dashboard |
   | `freeze_emergency_handle_off_alert` | Dashboard |
   | `freeze_pipe_resolved_alert` | Dashboard |
   | `freeze_emergency_tool_disabled_alert` | **`FreezeMitigationToolActivity`** with `alertid`, `sku`, `device_info` (`N3()` `:529`) |
   | `update` | `NotificationFirmwareUpdateActivity` (faucet firmware flow, §2.6) |
   | `OTASuccessful` | Dashboard |

   Notification detail screens group the emergency types under "Freeze Emergency
   Details" and the warning under "Freeze Warning Details"
   (`products/feature/notificationdetail/c.java:184-196`).
3. **The "Internal" push (N1).** When the page was opened from the
   `freeze_emergency_tool_disabled_alert` notification and the user turns the
   feature on, the app follows the PATCH with
   `POST /platform/api/v1/notifications` and this `PushNotificationRequest`:
   `{ tenantId, deviceId, sku, logicalName, notificationType: "Internal", message: "", alertId }`
   (`FreezeMitigationToolActivity.java:234` `A3()`; `ih0/f.java:371-400` `I()`/`L()`).
   This presumably tells the cloud to act on the pending emergency now.
   **However**, the Retrofit method declares `@Path("version")` while the URL
   `/platform/api/v1/notifications` has no `{version}`. Retrofit's
   `validatePathName` (`retrofit2/RequestFactory.java:557-563`) throws
   `IllegalArgumentException` for that, so this POST most likely **never leaves
   the phone** in 3.0.6. The endpoint itself may still exist (**live check**).
4. **What the faucet does.** Not found in the decompile. The app never sends a
   freeze-specific faucet command; the cloud presumably drives the faucet the
   same way as onoff. Expect `SENSATE_STS` On/Off messages during an event
   (**live check**). The existence of `freeze_emergency_handle_off_alert` implies
   the faucet cannot relieve pressure while its handle is closed.

---

## 7. Wi-Fi provisioning and local API (brief)

There is no BLE transport for faucets. The faucet code only borrows the
`BLEProfile.PERIOD_TIME` constant (10 s).

- **Soft-AP** SSIDs are `SEN-…` / `SET-…` (`ht0/q.java:115-117`, `:151`). The device's
  local HTTP API (`ApiConstant.java`):

  | Endpoint | Constant |
  |---|---|
  | `http://10.123.45.1/api/v1/device/ping` | `SECURE_PING_REQUEST_API` `:249` |
  | `http://10.123.45.1/api/v1/device/key` | `:248` |
  | `http://10.123.45.1/api/v1/device/profile` (Wi-Fi credentials) | `:250` |
  | `http://10.123.45.1/api/v1/device/version` | `FIRMWARE_VERSION_API` `:115` |
  | `http://10.123.45.1/api/v1/device/deviceid` | `:77` |
  | `http://10.123.45.1/api/v1/device/dpsdetails` | `:79` |
  | `https://{deviceName}/api/v1/device/ping` and `/profile` (TLS variants) | `:208`, `:292` |

  `rr0/p.java:278` chooses between the secure and plain ping.
- **Authorization:** the ping answers `WaitingForAuthentication` until the user
  opens and closes the handle once (`text_authorize_faucet_note`). Other answers
  are `Failure` or success (`gt0/m.java:549-557`).
- **Firmware gate:** if the local version is `< 10.17` or exactly `11.0`, or the
  TLS ping fails (which the app treats as version "0.0"), the app takes a
  certificate-expired path (`gt0/v.java:189,279`; `gt0/m.java:540-546,613-627`).
  That path uses `VALIDATE_CERT` / `INVALID_CERT` (`ApiConstant.java:284,160`).
- **Cloud claim** uses the shared `device-provisioning/{deviceId}/{tenantId}`.
  After that, the app runs the firmware step (§2.6) and forwards
  `INSTALL_FIRMWARE_STS` (`WifiProvisionActivity.java:1949`).
- None of this is a usable runtime local-control API. Once provisioned, the
  device talks only to the cloud. Nothing in the decompile reaches a provisioned
  faucet on the LAN.

---

## 8. Cross-check against `ha-kohler-sensate/PROTOCOL.md`

| PROTOCOL.md says | Decompile (3.0.6) says | Verdict |
|---|---|---|
| `faucet-experience` is an unknown route (`PROTOCOL.md:35`) | The app calls **GET `faucet-experience?DeviceIds={id}`** (no path segment), **GET `faucet-experience/{id}/experience/{expId}`**, **POST `faucet-experience`** and **PATCH `faucet-experience/{id}`** (§2.1 R3, R4, P1, P2). A GET to `faucet-experience/{id}` is not something the app does. | CONTRADICTS (likely a probing artefact; **live check**) |
| `progress` always `NotStarted` (`:41`); the integration treats `…InProgress` as dispensing | `progress` is OTA download state; the app reacts to `"Downloading"` | CONTRADICTS (meaning) |
| `handleState` = manual-handle position (`:42`) | It is also the gate for remote control: `CLOSED` → "Open the handle to remote dispense water." | EXTENDS |
| Shape of `leakDetectionHistory` entries unknown (`:181-183`) | `{ leakDetectionTime: Long epoch seconds }` | RESOLVES |
| `presetexperience` / `experience` both take `FaucetExperienceRequest` (`:69-71`) | Two different bodies: `experience` takes `FaucetExperienceRequest` (status always ON, `experienceQuantity` string liters, `experienceTitle`); `presetexperience` takes `PresetExperienceRequestModel` (`deviceId, experienceId, sku, status ON/OFF, tenantId`) | CONTRADICTS (doc detail) |
| Presets from `customer-experience` → `sensateExperiences` (`:84-99`) | 3.0.6 does not parse `sensateExperiences`. The faucet screen reads R3; the account screen reads `faucetPresetsExperiences` | EXTENDS (the field the integration uses is unused by the app, so it could disappear without breaking the app) |
| Dispense body and conversion factors (`:55-61`) | Identical: `FaucetDispenseRequest`, factors as in §2.3. The app's cup factor is the float 0.2365880012512207 | CONFIRMS |
| `onoff` action `ON`/`OFF` (`:63-67`) | The app sends `On`/`Off` and `OFF`, so the server accepts any case | CONFIRMS |
| MQTT `SENSATE_STS` / `SENSATE_EXP_STS` payloads (`:140-157`) | Same fields. The app also handles `SENSATE_LEAK_DETECTED_ALT` and `INSTALL_FIRMWARE_STS` | CONFIRMS + EXTENDS |
| `faucet-usage` `Interval` `DAY`/`MONTH`, ISO dates (`:101-119`) | The app sends `Day`/`Month` with `MM-dd-yyyy`. It uses `waterUsage` (liters) and `usageDuration` (seconds) | CONFIRMS (both formats evidently work) |
| A missing `connectionState` is treated as online (`:80-82`) | The app treats missing as `"Disconnected"` | CONTRADICTS (minor) |
| `factoryreset` exists, not used (`:73`) | Body `{deviceId, sku, tenantId}`, used before removing a product | CONFIRMS |

---

## Gaps vs ha-kohler-sensate

Integration paths are relative to `ha-kohler-sensate/custom_components/kohler_sensate/`
unless they start with `PROTOCOL.md`. The list is ranked by value to the integration.

1. **`faucet-state.progress` is firmware-download state, not dispense progress.**
   - *App:* `"Downloading"` (with `Connected`) means an OTA is in progress, and the
     control screen locks. A null value defaults to `"Completed"`
     (`bh0/j.java:367-400`, `hh0/n.java:736-752`).
   - *Integration:* treats `progress` ending in `inprogress` as a running dispense
     (`coordinator.py:88-93` `_dispensing`) and lists only `NotStarted` as an off
     state (`coordinator.py:71`). The update entity's `in_progress` reads
     `configuration.otaInProgress` and `about.firmware.progress`
     (`update.py:69-74`), and faucet code in the app reads neither.
   - *Suggested change:* stop treating `progress` as a dispense signal. Drive
     `UpdateEntity.in_progress` from `faucet-state.progress == "Downloading"`
     and treat any non-null `progress` value as data, not as the water status.
   - **Status:** CONTRADICTS. **Confidence:** medium-high (explicit app logic; the
     server-side value set is not enumerated). **Live check:** watch `progress`
     during an OTA.

2. **Firmware: real availability check and install command.**
   - *App:* `GET /platform/api/v1/firmware/sensate/{id}` (no `releasetarget`) →
     `firmwareUpdateAvailable`, `firmware` (target), `currentFirmware`,
     `mandatoryUpdate`, `otaStatus`, `estimatedTimeForOTA`.
     Install: `POST` the same path with `{tenantId, firmwareNumber: firmware, releaseTarget: "Public"}`.
     Pre-checks: water not `On`, not `Disconnected`. Completion:
     `about.firmware.version == firmwareNumber` (polled every 10 s, up to 3 min)
     and/or MQTT `INSTALL_FIRMWARE_STS` `Installed`/`Aborted` (§2.6, §3).
   - *Integration:* "Kohler's install command isn't known" (`update.py:33-37`).
     `latest_version` comes from `about.firmware.latestVersion`
     (`update.py:63-66`), a field 3.0.6 never reads for faucets.
   - **Status:** NEW. **Confidence:** high for request shapes. **Live check:** F1
     is a safe read; F2 starts a real OTA, so try it only when an update is
     actually offered.

3. **A closed handle blocks remote water.**
   - *App:* `handleState == "CLOSED"` disables dispense, start and presets with
     "Open the handle to remote dispense water." (§5.2). There is also a push
     type, `freeze_emergency_handle_off_alert`.
   - *Integration:* exposes `handleState` only as a sensor (`sensor.py:51-54`).
     It still sends `dispense`/`onoff` (`coordinator.py:481-504`), and its only
     pre-check is the online test (`coordinator.py:506-512`).
   - *Suggested change:* refuse (or make unavailable) the water switch and the
     dispense buttons and actions while the handle is `CLOSED`, with the app's
     wording.
   - **Status:** NEW. **Confidence:** high (app-side). **Live check:** what the
     cloud returns when commanded with the handle closed (HTTP 400 + 906? or 200
     with nothing happening).

4. **Second faucet SKU `SET`, and per-device `sku` in commands.**
   - *App:* handles `SET` identically to `SEN` everywhere (§1) and always sends
     the device's own `sku`.
   - *Integration:* `FAUCET_SKUS = {"SEN","FAUCET"}` (`const.py:85`; `FAUCET` is
     not a Kohler SKU in the app). Commands hard-code `"sku": SKU` = `"SEN"`
     (`api.py:529`, `api.py:543`; `const.py:84`).
   - *Suggested change:* discover `SET`, store each device's SKU, and send it.
   - **Status:** NEW / CONTRADICTS. **Confidence:** high (app); "SET = Setra" is
     an inference from `devices_name`. **Live check:** needs a SET owner.

5. **Dispense and preset amount limits.**
   - *App:* never sends below ≈0.2366 L (1 cup / ¼ qt), allows up to ≈11.36 L
     (3 gal), and saves presets anywhere in that range (§4.2).
   - *Integration:* hard limit 10–4000 mL (`const.py:110-111`). It refuses to pour
     app presets above 4 L (`button.py:114`, `services.py:107`) and lets users ask
     for as little as 10 mL (`services.py:115`, `number.py:74`).
   - *Suggested change:* raise the maximum to the app's 11.36 L (or at least
     allow presets the app created), and decide on a minimum after a live test.
   - **Status:** CONTRADICTS (narrower maximum, lower minimum). **Confidence:**
     high for the app's UI range; server limits unknown. **Live check:** a 5 L
     dispense; a dispense below 236 mL (e.g. 50 mL) and how accurately it stops.

6. **Leak details: entry shape and the real-time leak event.**
   - *App:* `leakDetectionHistory[] = {leakDetectionTime: epoch s}`. MQTT
     `SENSATE_LEAK_DETECTED_ALT` arrives immediately (§3, §5.3).
   - *Integration:* the entry shape is "unknown" (`PROTOCOL.md:181-183`). It
     fingerprints entries by `id`/`eventId`/`leakId` or a hash
     (`coordinator.py:65-85`). Push parsing ignores the leak code
     (`push.py:57-82`), although any faucet message does trigger a configuration
     re-read (`coordinator.py:600-603`).
   - *Suggested change:* key cleared leaks on `leakDetectionTime`, expose the
     latest leak time, and turn the leak sensor on directly from
     `SENSATE_LEAK_DETECTED_ALT`.
   - **Status:** NEW (resolves an open question). **Confidence:** high for the
     shape; medium for the MQTT envelope (only `attributes[0].code` is checked).
     **Live check:** the full leak message payload.

7. **Preset source: `faucet-experience?DeviceIds=` instead of `sensateExperiences`.**
   - *App:* the faucet screen lists presets with
     `GET /devices/api/v1/device-management/faucet-experience?DeviceIds={id}` →
     `faucetExperienceList[0].experience[]` (§2.2 R3). The account screen uses
     `customer-experience.faucetPresetsExperiences`. `sensateExperiences` is unused
     by 3.0.6.
   - *Integration:* reads `customer-experience.sensateExperiences` (`api.py:217-245`)
     and records `faucet-experience` as an unknown route (`PROTOCOL.md:35`).
   - *Suggested change:* switch to R3, or fall back to it.
   - **Status:** CONTRADICTS. **Confidence:** medium-high. **Live check:** GET R3
     with the `DeviceIds` query.

8. **Running presets the app's way.**
   - *App:* faucet screens send `POST /commands/faucet/experience` with
     `{status:"ON", experienceId, experienceTitle, experienceQuantity:"<liters as string>", deviceId, tenantId, sku}`.
     The All Presets screen sends `POST /commands/faucet/presetexperience` with
     `{deviceId, experienceId, sku, status:"ON"|"OFF", tenantId}`. Stopping a
     preset from the faucet screen is onoff `OFF` (§2.3).
   - *Integration:* pours presets with plain `dispense` (`PROTOCOL.md:96-99`), and
     `PROTOCOL.md:69-71` gives one body for both endpoints.
   - *Suggested change:* use `experience` (and `presetexperience` `OFF` to stop).
     Per the owner's live notes, only preset runs produce `SENSATE_EXP_STS` start
     and end events. They may also update `lastUsedTime` and Recent presets
     (unverified).
   - **Status:** CONTRADICTS (doc) / NEW (behaviour). **Confidence:** high for
     shapes. **Live check:** run each endpoint once and watch MQTT.

9. **Preset create, edit and delete.**
   - *App:* `POST faucet-experience` for the first preset, then
     `PATCH faucet-experience/{id}` with `operation` `Insert`/`Update`/`Delete`
     (§2.4). Limits: 10 presets, 18-character names, at most 2 words, no clash
     with `devices_name` or device names (§4.3-4.4).
   - *Integration:* read-only presets.
   - **Status:** NEW. **Confidence:** high for shapes, medium for the Insert-vs-POST
     rule. **Live check:** create and delete a test preset.

10. **Error code 906 → actionable message.**
    - *App:* HTTP 400 whose body maps to 906 → "Water could not be dispensed.
      Please turn on your faucet manually." Any other 400 → "Product is offline." (§5.1).
    - *Integration:* surfaces `HTTP 400: <message>` generically (`api.py:267-272`,
      `api.py:402-409`).
    - *Suggested change:* parse the body `statusCode` (platform.md) and translate
      906 and 900.
    - **Status:** NEW. **Confidence:** medium (the trigger is unknown).
      **Live check:** reproduce 906, perhaps with the handle closed.

11. **Missing `connectionState` means offline in the app.**
    - *App:* defaults a missing value to `"Disconnected"` (`bh0/j.java:378-381`,
      `hh0/n.java:736-742`).
    - *Integration:* treats missing as online (`coordinator.py:650-654`).
    - **Status:** CONTRADICTS (minor). **Confidence:** high (app). **Live check:**
      not needed. Low impact, since the field has always been present.

12. **Freeze mitigation toggle.**
    - *App:* account-level `GET`/`PATCH customer-device/{tenantId}/freezeMitigation`,
      sending `{freezeMitigationEnabled, appVersion}` (§6).
    - *Integration:* none.
    - *Suggested change:* optionally add a config switch, useful only with an H2Wise.
    - **Status:** NEW. **Confidence:** high. **Live check:** GET is safe.

13. **Identify.**
    - *App:* Identify = dispense 1 cup (0.236588 L) (§2.5).
    - *Integration:* already offers quick-dispense buttons.
    - **Status:** NEW (trivial). **Confidence:** high. **Live check:** none.

14. **Confirmations (no action needed):**
    - The dispense body and liters conversion match (`api.py:520-532`, `units.py:22-29`).
    - The server accepts any `onoff` case (`api.py:534-546`).
    - `SENSATE_STS` / `SENSATE_EXP_STS` fields match (`push.py:57-82`).
    - Usage `waterUsage` is liters and `usageDuration` is seconds (`api.py:248-264`;
      the integration ignores duration).
    - No message carries a dispensed amount, as the integration already assumes.
