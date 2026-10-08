# Anthem digital valve (SKU `GCS`)

The valve has built-in Wi-Fi and is addressed directly. It has **no "run my default" command**: every start specifies the whole valve state as a command word, or names a stored preset. Shared mechanics (sign-in, REST conventions, MQTT, firmware, usage) are in [platform.md](platform.md). Confidence markers are explained in the [README](README.md#confidence).

The command word is documented byte by byte in [../gcs/valve_hex.md](../gcs/valve_hex.md). This chapter covers everything around it.

---

## 1. Hardware and topology

| Model | Outlets | Split (valve 1 + valve 2) |
|---|---|---|
| K-28209 | 2 | 2 + 0 |
| K-28210 | 3 | 3 + 0 |
| K-28211 | 4 | 2 + 2 |
| K-28212 | 6 | 3 + 3 |

- **A "zone" is a valve body's half.** Zone 1 is `primaryValve1` and zone 2 is `secondaryValve1`. Each carries its own temperature, flow and three outlet bits.
- **How a valve gets into the app.** Only the first-generation touchscreen `K-28214`, plugged into the valve, adds a GCS valve to the Konnect app. The Anthem Plus screen `K-28214-ASC` plugs into the controller and adds only the HUB. Both can be on one valve at once, which is how an account ends up with a GCS and a HUB for one shower (live).
- **Topology source.** Read `gcs-state/gcsadvancestate` → `setting.valveSettings[].outletConfigurations` (works without a controller, live). `gcs-configuration`'s structural fields are null on a controller-attached valve.

### 1.1 `outLetId` and outlet bits

- **Ids.** Each valve body reserves three `outLetId` slots: 0–2 for valve 1 and 3–5 for valve 2. The app numbers new outlets position−1, **except on a 4-port valve, whose zone 2 keeps ids 3 and 4**. So a K-28211 reports `0, 1, 3, 4` (app, `ra0/n0.java`; live on a K-28211 via PR #2).
- **Bits.** The app sorts each valve's `outletConfigurations` by `outLetId` and maps **list index *i* to bit *i*** (`0x01`, `0x02`, `0x04`). A 2-outlet valve or half therefore uses `0x01`/`0x02` (app, `db0/c.java`; not yet live-verified on a 2-outlet valve).
- ⚠️ The app drops outlets whose type is not in its table (§1.2) from that list, which would shift the index-to-bit mapping.

### 1.2 Outlet types (`outLetType`)

This is the app's whole outlet picker (`db0/c.java` `X()`, app). Codes 1, 11, 21, 31 and 52 are also live-confirmed.

| Code | Fixture | Variant |
|---|---|---|
| 0 | unassigned (older screens) | |
| 1 | Handshower | |
| 11 | Showerhead | single |
| 12 | Showerheads | multiple |
| 21 | Bathfiller (tub filler) | "Spout" on older screens |
| 30 | Not plumbed | |
| 31 | Rainhead | Katalyst |
| 32 | Rainhead | Cascade |
| 33 | Rainhead | Kinetic |
| 34 | Rainhead | Rain Curtain |
| 35 | Rainhead | Laminar |
| 36 | Rainhead | Massage (Wave) |
| 37 | Rainhead | Hydro Massage |
| 38 | Rainhead | Silk |
| 39 | Rainhead | Real Rain® |
| 51 | Body Spray | single |
| 52 | Body Spray | multiple |
| 53 | Body Spray | Massage (Wave) |
| 61 | Foot Spray | single |
| 62 | Foot Spray | multiple |

The type is a **label on the valve**. The valve derives no flow envelope from it (the controller does). Valve and controller may hold different codes for the same fixture (live). Names given to outlets in the app are **not transmitted** anywhere.

## 2. Reads

All are under `/devices/api/v1/device-management/`.

| Route | Returns | Notes |
|---|---|---|
| `gcs-state/{id}` | `state.valve1/valve2 {out1..out3, temperatureSetpoint (°C), flowSetpoint (0-50), pauseFlag}`, `warmUpState {warmUp, state}`, `currentSystemState`, `presetOrExperienceId`, `totalVolume`, `totalFlow` | Seeds state at setup (live). `totalFlow` is **not** a water meter: it cycles among values 4× apart (live). Konnect 3.0.6 no longer calls this route |
| `gcs-state/gcsadvancestate/{id}` | Top level: `connectionState`, `lastConnected`, `firmwareUpdate`, `firmwareVersionInfo`. `state.{rebootStatus, ready, ioTActive, bleConnected, blePairing, …}`. `setting.{valveSettings[], uiConfig[], eco{ecoMode, ecoTimeLimit, flowRate}, flowControl, gcsDateTime, interfaceFirmwareTypeVersion, …}` | **The** settings source: topology, outlet records and UI config (live) |
| `gcs-configuration/{id}` | `configuration.about {uI2, primaryValve, secondaryValve1, gateway}` each `{firmware, assetsFirmware}`; top-level `about.firmware {version, latestVersion}`; `otaReportedProperties`; `firmwareUpdate {progress, progressPercent, status, version, …}`; `createdTime` | Firmware per part (live). `about` nests under `configuration` on some accounts and sits at the top on others |
| `gcs-configuration/{id}/about` | `gatewayConfigInfo {firmware, installDate, model, serialNo}`, `valvesConfigInfo[] {firmware, model, name, serialNo, status}`, `interfacesConfigInfo[] {firmware, name, status}` | Per-part identity (app) |
| `gcs-preset/{id}` | `gcsPresetExperienceDetails[] {presetId, title\|logicalName, isExperience ("True"/"False"), time, volume, valveDetails[{valveIndex: "Valve1", hexString}]}` | Ten preset slots plus experiences (live) |
| `gcs-diagnostics/{id}` | Fault log, see [platform §8](platform.md#8-diagnostics-fault-logs). `DELETE` clears it | app |
| `gcs-experience` | `[{name, type, description, duration, url}]` — the catalogue of experiences that can be added | app |
| `gcs-usage/{id}` | See [platform §6](platform.md#6-water-usage) | live |

### 2.1 Outlet record: read and write spellings differ

| Field | REST (`gcsadvancestate`) | MQTT (`READ_GCS_OUTLET_CONFIG_CFG`) | Write (`writeoutletconfig`) |
|---|---|---|---|
| Run time (s) | `maximumRuntime` | `maximumRunTime` | `maximumRuntime` |
| Flow (min / default / max) | `minimumFlowrate` / `defaultFlowrate` / `maximumFlowrate`, **display 0-50** | `minimumFlowRate` / `defaultFlowRate` / `maximumFlowRate`, **bytes 16-200** | lowercase `r`, **bytes** |
| Temperature (min / default / max) | `…OutletTemperature`, **display °C** (`45`, `47.8`) | **tenths of °C** (`450`) | **tenths of °C** |
| `outLetId`, `outLetType`, `outLetFlags` | same on all three surfaces | | |
| `maxVolume`, `purge` | not seen | not seen | always sent by the app |

**The write is a whole-record replace** (live): twelve string keys, one call per outlet, each sent after the previous one succeeds.
- Omitting a key, or sending the read spelling, makes Gson drop it. The API still returns 201, and the field silently takes a server default.
- One of these fields is the **scald limit**.
- The app sends `maxVolume: "0"` and `purge: ""` (newer screens), and forces `minimumOutletTemperature = 150` and `minimumFlowrate = 16`. Flow is ×4 rounded; temperature is ×10 truncated, with 49.0 clamped to 488 (app).
- `outLetFlags` is echoed back and never interpreted (`1` everywhere); the app defaults it to `"1"` (app).

## 3. Commands

All are under `/platform/api/v1/commands/gcs/`. Every body starts `{deviceId, sku: "GCS", tenantId, …}`.

| Verb | Body (beyond the common three) | Effect | Confidence |
|---|---|---|---|
| `solowritesystem` | `gcsValveControlModel {primaryValve1, secondaryValve1, secondaryValve2..7}` | **The** direct write. The single-valve `secondaryValve1` is `"00000000"`; slots 2-7 are `""` (newer screens) or `"00000000"` | live |
| `controlpresetorexperience` | `preset: "<id>", action: "On"\|"Off"` | Starts or stops a preset **or an experience** by itself; no valve write follows. ⚠️ `presetOrExperienceId` in this body is accepted and then ignored | live (presets), app (experiences) |
| `startpreset` | `presetOrExperienceId` | The older screens' start | app |
| `createpreset` | Flat: `name, time, volume: "0", valve1..valve8` (3-byte preset words; `"000000"` for unused) | Fills the lowest free slot | app |
| `writepreset` | `gcsPresetControlModel {presetId, name, time, volume: "0", valve1..valve8}` | **Whole-record replace.** Delete = name `""`, time `"0"`, all words `"000000"`. Posting to `createpreset`, omitting the wrapper, or sending 4-byte words all silently do nothing | live |
| `writeoutletconfig` | `gcsOutletConfigControlModel {12 keys}` (§2.1) | One outlet's record | live (10 keys), app (12) |
| `warmup` | `warmUp: <mode>` | Mode toggle (§5). An omitted `warmUp` returns 200 and is ignored | live |
| `valvereset` | `reset: "productRestart"` | Reboots the valve; running water stops | app |
| `factoryreset` | — | The app's *Remove Product*, followed by `PATCH customer-device/{tenant}/{home}/device {operation: "Delete", keepDeviceData, logicalName, deviceId, sku}`; MQTT `GCS_DELETE_PROFILE_STS` follows | app |
| `writeuiconfig` | `gcsUIConfigModel {whole record}` (§6) | Touch-panel settings | app |
| `uiconfigsuccess` | — | Handshake at the end of first-time setup; not a setting | app |
| `bathfillervolume` | `index: "0"` | Asks the valve to report `READ_DISPENSED_WATER_VOLUME_STS` | app |
| `experience` | `operation: Insert\|Replace\|Delete, newExperienceTitle, replaceId` | Adds, replaces or removes a stored experience | app |
| `experiences` | `gcsExperiencesManageRequestModel: [{operation: "Insert", experienceTitle}]` | Batch insert | app |

### 3.1 Starting and stopping water

| Action | How | Confidence |
|---|---|---|
| Open outlets | `solowritesystem` with the full words: outlet bits, temperature and flow for both zones | live |
| Stop | **The app writes byte 3 = `0x40`**: the pause bit, no outlets, bit 7 clear. It sets the per-valve pause bit and zeroes the outlets (`db0/c.java`; the older screens do the same) | app |
| Stop, `0x00` | Mask `0x00` with a valid prefix also stops. The Anthem Plus controller stops this way, and so does the Anthem integration: a `0x00` stop can never be mistaken for the valve's own run-time cutoff, which pauses with `0x40` (§4.1) | live |
| Broken stop | An all-zero `primaryValve1` (prefix `0x00`) addresses no valve and is **ignored** — the "can turn on but never off" bug in other libraries | live |
| Full cold | Temperature `0` °C: the valve stops mixing in hot water. The app's slider has a `COLD` stop one below the minimum that sends this | app + live |

### 3.2 Word details the app adds to valve_hex.md

- **°F setpoints use a lookup table, not arithmetic.** The table covers 59–122 °F and is unchanged from 3.0.1 to 3.0.6 (`db0/c.java` `A()`). Celsius sends whole degrees ×10. Outside the table the app sends 0.
- **Byte 3 bit 7** (`skipWarmUp` on write) is set only by **bath fill** (§8). Its read meaning is `errorFlag`.
- **Decode** (`db0/c.java` `C()`):
  - temperature high bits `0x03`, atFlow `0x08`, atTemp `0x04` (byte 0);
  - flow = byte 2 / 4 (setpoint scale);
  - error `0x80` and pause `0x40` (byte 3);
  - measured temperature in bytes 4-5, measured flow in byte 6 (raw), error code in byte 7.

### 3.3 Preset words and temperatures

- **A preset word is 3 bytes:** `[byte0][temp low][flow]`.
- **Byte 0 carries the outlets at different bit positions from a command word**: `0x04` outlet 1, `0x08` outlet 2, `0x10` outlet 3, plus `0x01` for the temperature high bit (live).
- **Ids share one space.** Presets are 1-11 and experiences are 17 and up (`db0/c.java`). Slot 1 is the mandatory default shower.
- **A preset's °F temperature is arithmetic**, `(F − 32) × 0.5555` formatted to one decimal. So 102 °F → 389 (0x185), 100 °F → 378 and 120 °F → 489. That disagrees with the solo-write table at 13 of the 29 values from 92 to 120. A running preset's temperature then shows up in the valve word, so a 389 in a `GCS_SOLO_STS` can come from a favorite.
- **A preset's flow** is outlet 0's `defaultFlowrate` × 4, not a fixed 100% (app).
- **A preset carries its own `time`**, a second limit independent of `maximumRunTime`. The lower of the two stops the shower (live).

## 4. Settings ranges (what the app offers)

| Setting | Range | Notes |
|---|---|---|
| Max Temperature (scald limit) | 33-48 °C, shown as 92-118 °F (newer screens); 95-120 °F (older) | A **setting**, not a hardware limit; seen changing 450 → 477 tenths live |
| Default Temperature | 59 °F / 15 °C up to the current max | |
| Zone temperature slider | `COLD` (min − 1, sends 0), then the minimum (59 °F) up to the current `maximumOutletTemperature` | `qa0/p.java` |
| Flow | Max-flow setting 5-50 setpoint (20-200 bytes); default flow minimum 4 setpoint (16 bytes); shown as a % of max | `0x10`-`0xC8` on the wire |
| Max Shower Duration | 15, 20, 25, 30, 45, 60 min (newer); 15-30 (older); written as minutes × 60 | 3.0.1 misread anything above 30 min as 25; fixed in 3.0.5 |

`deviceconfig.properties` in the APK (`maxFahrenheitValve2 = 100`, and so on) holds placeholders that GCS code never reads. The app overwrites them with the valve's own `maximumOutletTemperature`.

### 4.1 Run-time limit

How the valve's Max Shower Duration (`maximumRunTime`) actually ends a shower. All **live**, from 156 zone sessions captured 2026-08-07 to 08-14 (spanning a change from 3600 s to 900 s) and five later case studies.

- **Set per outlet, timed per zone.** The clock starts when a zone goes from nothing flowing to something flowing. It **does not reset** when outlets change within the zone: opening a second head, closing the first or swapping between them leaves it running. Timing each outlet separately misses most cutoffs in sessions where someone moves between heads.
- **A zone is "flowing"** when its word has outlet bits set and the `0x40` pause bit clear.
- **At the limit the valve pauses that zone**: byte 3 becomes `0x40` and the outlet bits are cleared in the same message. Nothing else marks it as a timeout: `currentSystemState` stays `normalOperation`.
- **It fires slightly early**: −0.08 to −0.23 s against the limit. All 11 cutoffs in the corpus landed within 1.32 s of a limit. No other pause came within 334 s of one, and no `0x00` stop within 123 s.
- **A preset-driven session pauses every zone the preset owns** when one of them expires, not only that zone.
- **The outlets of one zone can disagree.** The app writes the duration one outlet at a time and stops at the first failure, so a lost write strands the old value on the rest (seen 2026-09-10: 1800 s on two outlets, 3600 s on the third).
- **Three limits can end a shower, and the shortest wins:**
  - the valve's `maximumRunTime`, which pauses (`0x40`);
  - the **Anthem Plus controller's** own `maxShowerDuration` ([hub_controller.md](hub_controller.md)), also timed per zone, which **stops** (`0x00`) and fires slightly late (+0.20 to +1.00 s);
  - a **preset's `time`** (§3.3), for a session that preset started.
- **Who stops how:** the valve's timer, the Konnect app and the first-generation touchscreen use `0x40` (the touchscreen on both zones). The controller and the Anthem integration use `0x00`. So a `0x40` pause near the limit cannot be told apart from an app stop at the same moment.

## 5. Warmup

| Mode | Written by the app? |
|---|---|
| `warmUpDisabled` | yes |
| `warmUpAllOutletsWithNoStartDelay` | yes |
| `warmUpSelectedOutletsWithNoStartDelay` | yes |
| `warmUpAllOutlets`, `warmUpSelectedOutlets` | **read only**, treated as "enabled". No delay is defined anywhere; `delayStart` is echoed, never interpreted |

- `warmUpState.state` reports whether warmup is running now: `warmUpInProgress` / `warmUpNotInProgress`. ⚠️ "NotInProgress" ends with "InProgress", so compare the whole value.
- The app picks **selected outlets** whenever its selected-outlets option is on **or** `uiConfig.waterSavingMode == "Enabled"` (app, `jc0/o.java`). Water-saving mode therefore forces selected-outlet warmup.
- The app writes `warmUpDisabled` before it customises outlets.
- An Anthem Plus controller's web UI writes `warmUpDisabled` on every signed-in use (live). See `warmup_manager.py`.

## 6. Touch-panel UI config

Each touch interface has its own record, keyed by `UI`. Records arrive in `gcsadvancestate` `setting.uiConfig[]` and are pushed as `READ_GCS_UI_CFG`.

- **Settings fields:** `temperatureUnits`, `flowUnits`, `standbyLighting`, `timeFormat`, `demoMode`, `bathFillPresetId`, `delayStart`, `toggleOutlets`, `waterSavingMode`, `accessibilityMode`, `backLight`, `hapticFeedback`, `sounderVolume`, `defaultHomeScreen`, `proximitySensorMode`, `proximitySensorDistance`, `proximitySensorTime`, `notification`, `defaultMemory`, `language`, `languageRegion`.
- **Identity and status fields** (REST only): `status`, `firmware`, `bootloder` (sic), `installedDate`, `model`, `serialNo`.

The app writes the **whole record** through `writeuiconfig`. Values it writes:

| Field | Values |
|---|---|
| `temperatureUnits` | `C`/`F` |
| `flowUnits` | `Liters`/`Gallons` |
| `accessibilityMode`, `hapticFeedback` | `1`/`0` |
| `waterSavingMode` | `1`/`0` |
| `defaultHomeScreen` | `0`-`2`, +192 when valve 2 has outlets |
| `demoMode` | `Disabled` |

⚠️ Reads appear to use `Enabled` where writes use `1`/`0`. This is unverified live, and neither integration writes UI config yet.

## 7. MQTT messages

Dispatch on `data.code` (see [platform §4.3](platform.md#43-message-envelope)).

| Code | Attributes | Notes |
|---|---|---|
| `GCS_SOLO_STS` | `primaryValve1`, `secondaryValve1` (16-hex words); `currentSystemState`; `presetOrExperienceId`; `warmUpStatus`; `totalFlow`, `totalVolume`; `firmwareUpdate`, `BLEPairing`, `BLEConnected`, `IoTActive`, `IoTProvision`; `configChangeIndent` | The live valve word (live). `presetOrExperienceId` latches for the session, is cleared by pause **and** stop, and is never set during warmup |
| `GCS_PRESET_STS` | `presetId`, `name` | Pushed on every create, edit, rename and delete, and for all slots after a reboot. A delete arrives as an empty name (live) |
| `GCS_WARM_STS` | `warmup` (all lowercase, unlike REST's `warmUp`) | live |
| `READ_GCS_OUTLET_CONFIG_CFG` | One outlet record, MQTT spelling (§2.1) | Unprompted, about twice a session, one outlet per message (live) |
| `READ_GCS_UI_CFG` | One UI record (§6) | live (shape: app) |
| `READ_GCS_EXPERIENCE_STS` | — | The app matches it and does nothing |
| `DEVICE_REBOOT_STS` | `code` | The app only reacts during first-time setup ("re-enter your shower customization") |
| `GCS_DELETE_PROFILE_STS` | `status` (`"true"`) | After Remove Product (app) |
| `READ_DISPENSED_WATER_VOLUME_STS` | `volume` (string) | A running counter, requested by `bathfillervolume` (app) |
| `READ_ALL_INTERFACES_FIRMWARE_VERSION_STATUS_INFO`, `GCS_RECIEVED_STS` | — | Seen live; **not in the app at all** |

`currentSystemState` values:
- **Captured:** `normalOperation`, `showerInProgress`.
- **Acted on by the app, never captured:** `error` (case-insensitive) — a fault; `FirmwareUpdate` — an install is running.

The app's fault rule is `currentSystemState == error`, **or** `errorFlag` set on either valve word (app).

## 8. Bath fill (older app screens only)

1. `POST bathfillervolume {index: "0"}`. The valve answers with `READ_DISPENSED_WATER_VOLUME_STS {volume}`.
2. To start, `solowritesystem` with **bit 7 set** plus the tub filler's (type 21) outlet bit added to what is open, at the current temperature and flow.
3. To stop, byte 3 = `0xC0`.
4. Setup samples the volume before and after a fill and stores the difference. Under 1.0, it falls back to `bathfillDefaultFillAmount` (4 L) from `deviceconfig.properties`.

The units are unstated, and none of this is live-verified.

## 9. Firmware

There are two parts:
- the valve: `firmware/gcs/{id}?releasetarget=Public`;
- its gateway: `firmware/gcs/gateway/{id}?releasetarget=Public`.

The shapes and install flow are in [platform §7](platform.md#7-firmware). The configuration's `about` lists three versions: interface (`uI2`), valve (`primaryValve`, plus `secondaryValve1`) and gateway. They are genuinely different numbers (2.2, 10 and 00.74 on one system, live), and two valves on one account can differ while the app shows one (live).

## 10. What the Anthem integration implements

| Capability | Status |
|---|---|
| Outlet switches, zone temperature (`COLD` to max) and flow, `custom_shower`, `send_valve_hex` | ✅ |
| Presets (start), warmup, outlet settings (12-key write) | ✅ |
| Time-left attributes (zone clock against `maximumRunTime`, §4.1) | ✅ |
| Endless Shower (restart a shower the valve's timer ended) | removed 2026-10-08 |
| Experiences (start/stop via `controlpresetorexperience`) | ✅ app-confirmed only |
| Restart (`valvereset`) | ✅ app-confirmed only, disabled by default |
| Firmware update entities (read-only) | ✅ |
| Fault: error flag plus `currentSystemState == error`; fault log in diagnostics | ✅ |
| Connection state (`Disconnected`) | ✅ |
| Bath fill, UI config writes, eco/water-saving mode, experience management, factory reset | ❌ documented only |
