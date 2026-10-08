# Anthem Plus system controller (SKU `HUB`)

The controller is a Linux box in front of one or two valves. It adds music (with the K-30319 amplifier), Zigbee lighting, steam and favorites. **It is favorite-centric.** There is no "set outlets and temperature now" command; you activate a stored favorite. Shared mechanics are in [platform.md](platform.md), and confidence markers are explained in the [README](README.md#confidence).

---

## 1. Zones, valves and parts

- **The words disagree across payloads, and all mean the same thing:** hub-state `zone: "1"`, favorites `water.zone1`, configuration `zoneone`/`zonetwo`, MQTT `zone` or `component: "valve1"`. Resolve all of them through one helper (`zone_number` in `anthem/hub.py`).
- **`parts.valve1`/`valve2` count physical valve bodies**, while GCS `valve1`/`valve2` count zones within one body. A 6-outlet K-28212 correctly reports `valve1: Connected, valve2: NotConnected` (live). **Never gate zone-2 entities on `parts.valve2`.** Use `zoneone`/`zonetwo` `configuredoutlets`.
- **Outlet arrays are padded to 6 slots per zone** whatever the hardware. Only the leading slots that belong to that zone's valve mean anything (live).
- **Read and write shapes differ** (live):
  - read: `water.zoneN.outlets` is a **count** and `outletState` a 6-slot array;
  - write: `water.zoneN.outlets` is a list of **0-based positions** to open.
- **Message arrival is not evidence of hardware.** The controller emits `STEAM_STS` and `LIGHT_STS` even on a system where `parts` reports neither (live).

## 2. Reads

All are under `/devices/api/v1/device-management/`.

| Route | Returns | Confidence |
|---|---|---|
| `hub-state/{id}` | Top level: `connectionState`, `lastConnected`, `errorState` (bool), `errorComponent {amplifier, hub, light, steam, valve1, valve2}`, `error {errorCode, title, details, isActive, timeStamp}`, `showerWarmUp`. `state.shower[] {zone, status, outlets, temperature, flowRate, startTime, totalTime}`, `state.hubSteamState {status, temperature, startTime, totalTime}`, `state.musicStateModel {status}`, `state.light[] {name, status, state {brightness, colorInfo, colorTemperature}}`, `state.valveState {hot/cold inlet temperature, outletTemperature, setpoints, flowRate, timeSinceTurnedOn, valveStateFlags}` | live for zones/status; app for the rest |
| `hub-configuration/{id}` | `configuration.{parts, zoneone, zonetwo, systemSettings, steamSettings, amplifierSettings, lightSettings[], about, valve1Settings, valve2Settings, systemConfiguration, otaInProgress, …}` — §2.1 | live (parts, zones); app (settings) |
| `hub-experience/{id}/favorites` | `favorites[] {id, title, isExperience, water, steam, music, light, …}`. **404 when no favorites are saved**, not an empty list | live |
| `hub-experience/{id}/experiences` | `experiences {showerExperiences[], steamExperiences[], iceShowerExperiences[]}`, each item `{id, title, state, isActive, duration, experienceDurationMinutes/Seconds, description, isExperienceInError}` | app |
| `hub-diagnostics/{id}` (GET/DELETE), `hub-diagnostics/{id}/active` | Fault log / active faults, see [platform §8](platform.md#8-diagnostics-fault-logs) | app |
| `hub-usage/{id}` | See [platform §6](platform.md#6-water-usage); also adds max/min hot inlet temperature | app |

Favorite names arrive under **two different keys**: REST `title`, MQTT `FAVORITES_SNAPSHOT` `name`. Read both. Favorite ids are **reassigned when a favorite is deleted**, so always resolve by name (live).

### 2.1 `hub-configuration` settings (app, `com/utils/.../configuration/*`)

| Block | Fields |
|---|---|
| `systemSettings` | **`maxShowerDuration` (minutes)**, `showerMaxTemperature`, `temperatureUnit`, `flowRateEnable` (`"1"` = flow editable), `flowRate`, `dateFormat`, `dst`, `language`, `timeFormat`, `timeZone`, `units`, `uiSettingsLock`, `usersLock`, `webSettingsLock` |
| `steamSettings` | `defaultTemperature`, `defaultTime`, `maxTemperature`, `steamStatus`, `temperature`, `time`, `tempratureunit` (sic), `isSteamError` |
| `amplifierSettings` | `monoVolume`, `stereoVolume`, `sdCard` (`notpresent` = no card), `music` (`notpresent`/`unknown` = no songs), `isAmplifierError` |
| `lightSettings[]` | `name` (`groupA`/`groupB`/`groupC`), `icon`, `color`, `hue`, `brightness`, `saturation`, `multiColor` (false = white only), `connectivity` (`"No"` = unreachable) |
| `about` | `hub {wlan {ip, …}, eth, mac, ssid, signalstrength, …}`, `valve1`/`valve2 {serialNumber, firmware, …}` and others. A valve that isn't fitted has serial `0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0:0` |

**Max Shower Duration is readable here.** The Anthem integration long believed it was local-API-only. It matters because Endless Shower needs the controller's and the valve's durations to match (see `anthem/runtime_cutoff.py`). Whether the cloud copy follows an edit made on the controller promptly is unverified: REST has been seen to lag on `monoVolume`.

### 2.2 "Fitted but disconnected" (the app's rules, `nc0/z.java` `k()`)

| Accessory | Counts as fitted when | Reported disconnected when |
|---|---|---|
| Valve N | `about.valveN.serialNumber` is not the all-zero string | `parts.valveN` is not `Connected` (case-insensitive) |
| Steam | `steamSettings.defaultTime` is non-null and non-zero | `parts.steam` is not `Connected` |
| Amplifier | `amplifierSettings.stereoVolume` or `monoVolume` is present | `parts.amplifier` is not `Connected` |
| Light | `lightSettings` is non-empty | `parts.light` is not `Connected` **or** any group has `connectivity == "No"` |
| SD card | the amplifier is fitted | `sdCard == notpresent` → "SD card not inserted"; otherwise `music` is `notpresent`/`unknown` → "no songs" |

Body text for all of these: "Your device is no longer communicating. Please power cycle your device."

Product states: `OFFLINE` (`connectionState` is not `Connected`), `POWER_CLEAN_RUNNING`, `ON_BOARDING`, `ON_ERROR`, `NORMAL`.

## 3. Commands

All are under `/platform/api/v1/commands/hub/`. Every body starts `{deviceId, sku: "HUB", tenantId, …}`. Konnect 3.0.6 has exactly the ones below; there is **no light, music, volume or temperature command anywhere** (app).

| Verb | Body | Effect | Confidence |
|---|---|---|---|
| `valvecontrol` | `valveOnOff: "ON"\|"OFF"` | Runs or stops the controller's **own default** shower. No outlet or temperature fields. OFF stops water only | live |
| `steamcontrol` | `steamOnOff: "ON"\|"OFF"` | Runs or stops steam at `steamSettings` defaults. The app offers it when steam is Connected and `defaultTime` ≠ 0 | app |
| `favorite/control` | `id: "<string>", name, state: "ON"\|"OFF"` | Activates or stops a favorite. **Allowed while running** | live |
| `favorite` POST | §4 body with `"id": 0` | Creates a favorite | live (shape); app (`id: 0`) |
| `favorite` PATCH | §4 body with `id: <int>` | Edits. **Rejected while the system runs** (`statusCode 902`) | live |
| `favorite` DELETE (with body) | `name, id: <int>` | Deletes | live |
| `stopall` | — | Idles everything; the only true "off". An all-off favorite still reports as running | live |
| `shower/experience/control`, `steam/experience/control`, `iceshower/experience/control` | `name: <title>, status: "ON"\|"OFF"` | Starts or stops an experience. **The path must match the category the experience was listed under** | live (shower) |
| `factoryreset` | — | Factory reset | app |

App rules:
- A favorite cannot contain both shower and steam.
- Shower and steam cannot run at the same time.
- Favorites cannot be edited or deleted while running.

Experiences run from zone 1's first outlet ("Experiences will run from the fitting connected to the first port of zone 1").

## 4. Favorites

The write schema (app, `…/favorite/updatefavorite/*`):

```json
{
  "deviceId": "…", "sku": "HUB", "tenantId": "…",
  "id": 0,
  "name": "Rinse",
  "water": {
    "duration": "15",
    "zone1": {"temperature": 102, "flowrate": 100, "outlets": [0, 2]},
    "zone2": {"temperature": 102, "flowrate": 100, "outlets": [1]}
  },
  "steam": {"temperature": 110, "time": 15},
  "music": {"source": "Aux", "songID": "", "musicRepeat": "", "volume": 40},
  "light": [{"name": "groupA", "color": "Blue", "hue": "175", "brightness": 60}]
}
```

- **Temperatures are whole °F on the wire, whatever the account's unit** (app). A Celsius account's entry is converted with `round(c × 1.8 + 32)` before sending (`nc0/z.java` `G0()` → `F()`, at every zone and steam setter). It is converted back only for display.
- **Unused components are omitted, not nulled.** The app serialises with Gson defaults: `water` only when a zone has outlets, `zone2` only when used, `steam` only when steam is ON, `music` only with an amplifier source, `light` only for active groups. An all-null `music` object fails with HTTP 400 (live).
- **`water.duration`** is minutes as a string: `"0.5"` and `"0.75"` mean 30 and 45 s, then 1-10, 15, 20, 25, 30, 45, 60, and `"180"` means off (no limit). It defaults to `systemSettings.maxShowerDuration` (180 if missing), and a value above that is refused ("…adjust the Max Shower Duration from the Embedded Server Page"). **Shown only on controller firmware above 2.88.**
- **Music:** `source` is `Aux` or `SdCard` (no Bluetooth); `songID` and `musicRepeat` are `""`; volume runs 0-100 in steps of 5 and defaults to stereo, then mono, volume.
- **Light:**
  - up to 3 groups;
  - `color` is a **display name** from an 11-colour palette: Warm White, Neutral White, Cool White, Red, Orange, Yellow, Green, Light Blue, Blue, Purple, Pink;
  - `hue` is a string step, 5 per colour — Red 0/4/8/12/16, Orange 18-34, Yellow 36-52, Green 84/90/95/100/105, Light Blue 138-158, Blue 169-187, Purple 195-215, Pink 218-236, Whites 1-5;
  - brightness runs 10-100 in steps of 10;
  - a group with `multiColor=false` is white only.
- **Bounds:**
  - zone temperature 59 °F to `showerMaxTemperature`;
  - flow 10-100, editable only when `flowRateEnable == "1"`;
  - steam temperature 90 °F to `steamSettings.maxTemperature`, timer 1-20.
- **Limits:** at most **9 favorites**. Names must be non-empty, contain no special characters, and be at most two words (they are used for voice commands).
- **Lumiwave** `{animation, brightness, color, spray}` appears in `FAVORITES_SNAPSHOT` items, but nothing writes it.

## 5. MQTT messages

Dispatch on `data.code`. Accessory messages also carry `favoriteid` and `experienceid` at the `data` level. These attribute the component to whatever started it; they are **not** the favorite's run state.

| Code | Attributes | Notes |
|---|---|---|
| `SHOWER_VALVE_STS` | Per zone `{zone\|component, status, outlets[6], temperature, flowrate, errorcode, errorstate}`; `data.showerwarmup` | live. Reports valve-driven sessions only patchily (51 of 95 immediately, 12 late, 32 never; preset-driven sessions never) |
| `STEAM_STS` | `{status: ON\|OFF\|POWERCLEAN, temperature, starttime, totaltime, errorcode, errorstate, …}`; `data.steamwarmup` | `POWERCLEAN` = "Power clean is in progress. Please stay out of your shower." The app treats a message as default steam only when `favoriteid` and `experienceid` are both `"0"` |
| `MUSIC_STS` | `{component: amplifier, status, errorcode, errorstate}` | On/off only (live) |
| `LIGHT_STS` | `{component: lightgroupA\|B\|C, name, status, state {brightness, colorinfo {color, hue, saturation}, colortemperature}, errorstate, errorcode}` | **One group per message**; the app reads only the first attribute. On the reference system it arrives with an empty array (no lights) |
| `FAVORITE_STS` | `{id, name, status}` | The favorite's own run state. Start and stop carry the same id and differ only in `status` (live) |
| `FAVORITES_SNAPSHOT` | The whole favorites list (`name`, not `title`) | Pushed after every create, edit and delete, and on reboot (live) |
| `CREATE_FAVORITE_STS`, `UPDATE_FAVORITE_STS`, `DELETE_FAVORITE_STS` | ack | The app refetches only on create; the snapshot follows 1-3 s later (live) |
| `SHOWER_EXP_STS`, `STEAM_EXP_STS`, `ICE_SHOWER_EXP_STS` | `{code, name (title), ready, status}` | Experience run state (app) |
| `SYSTEM_STS`, `STATUS_SNAPSHOT`, `LUMIWAVE_STS`, `*_EXP_SNAPSHOT` | — | The app ignores them; they are still proof of life |

## 6. Local LAN API

The controller serves a web UI and an API at `http://{host}/web/api/v1/device` (live, firmware 2.88):
- It handles setup, configuration and diagnostics only, and **cannot actuate anything**. `water_test_start` runs a fixed ~5 s self-test on zone 1, outlet 1.
- Login uses a short-lived JWT obtained from a PIN. The PIN is encrypted as `base64(RSA_PKCS1v15(sha256(pin).hexdigest_ascii))` with a public key baked into the hub's Angular bundle (see `anthem/const.py`).
- Some endpoints answer with no token at all, including two that change state: `set_hub_datetime` and the date config.
- **Signed-in use of the web UI writes the valve's warmup mode to disabled** (live).

Konnect 3.0.6 never calls this API. It opens the controller's page (the "Embedded Server Page") in a WebView at `hub-configuration` `about.hub.wlan.ip`, which is also how a client can find the host. BLE is used only for Wi-Fi provisioning.

## 7. Firmware

`/platform/api/v1/firmware/hub/{id}?releasetarget=Public`. See [platform §7](platform.md#7-firmware).

## 8. What the Anthem integration implements

| Capability | Status |
|---|---|
| Shower switch (`valvecontrol`), System switch (`stopall`), favorite select | ✅ |
| Steam switch (`steamcontrol`), with the shower-and-steam guard | ✅ app-confirmed only |
| Experience select (catalogue + `*_EXP_STS`) | ✅ app-confirmed only |
| Max Shower Duration sensor + duration-mismatch repair issue | ✅ |
| Problem sensor: error flags, active errors, fitted-but-disconnected accessories | ✅ |
| Per-group light state; steam detail and `POWERCLEAN` | ✅ |
| Firmware update entity (read-only) | ✅ |
| Favorite create/edit/delete (library methods exist, aligned with 3.0.6; no UI) | ⚠️ library only |
| hub-usage, LAN IP discovery, light colour/brightness control (no command exists) | ❌ documented only |
