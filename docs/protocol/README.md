# Kohler Konnect protocol reference

A developer reference to the cloud protocol behind the Kohler Konnect app. It covers sign-in, every REST endpoint and MQTT message the app uses for the Anthem valve, the Anthem Plus controller and the Sensate faucet, the status codes, and the firmware flow. It is written to be **reused by any Kohler Konnect integration**, not just this one; [`ha-kohler-sensate`](https://github.com/kedube/ha-kohler-sensate) is the second consumer.

| Chapter | Covers |
|---|---|
| [platform.md](platform.md) | What every product shares: Azure AD B2C sign-in, REST conventions and the `statusCode` table, the account read, MQTT over Azure IoT Hub, connection state, water usage, firmware, diagnostics, notifications |
| [gcs_valve.md](gcs_valve.md) | Anthem digital valve (`GCS`): reads, commands, outlet types and records, presets and experiences, settings ranges, warmup, UI config, MQTT codes, bath fill |
| [hub_controller.md](hub_controller.md) | Anthem Plus controller (`HUB`): configuration and settings, fitted-but-disconnected rules, commands, the favorite schema, MQTT codes, the local LAN API |
| [sensate_faucet.md](sensate_faucet.md) | Sensate / Setra faucets (`SEN`/`SET`): endpoints, dispense and presets, MQTT, leaks, freeze mitigation, firmware — plus a gap list against `ha-kohler-sensate` |
| [../gcs/valve_hex.md](../gcs/valve_hex.md) | The valve's command word, byte by byte |

## Where it comes from

- **Live captures** from one Anthem system (K-28212 + Anthem Plus on firmware 2.88, plus the owner's two K-28210 valves). This covers several thousand MQTT messages and REST reads, Aug–Oct 2026.
- **Two decompiles of the Konnect Android app:** 3.0.1 (2026-08-20) and **3.0.6, version code 260 (2026-10-07)**. The 3.0.6 pass resolved most open questions and is what the "app" marker below refers to.
- The Sensate chapter also draws on `ha-kohler-sensate`'s own `PROTOCOL.md` and code.

No decompiled source is kept in either repository. Everything here is restated as facts — names, fields, values, behaviour — with the decompile location given so it can be re-checked.

## Confidence

Every fact carries one of these:

| Marker | Meaning |
|---|---|
| **live** | Observed against real hardware or a real account response |
| **app** | Read directly in the Konnect 3.0.6 decompile. It shows what **the app sends and expects**, not what the device does with it |
| **inferred** | Reasoned from the above. Treat it as a hypothesis |

Prefer **live** wherever the two disagree. Several "app" findings contradicted earlier assumptions that had never been tested: the stop word, the 12-key outlet record, favorite temperatures in °F. Several older beliefs were overturned by live tests: the usage interval, the preset start body. Before shipping a write path that is marked **app** only, capture it once against hardware.

## Regenerating the decompile

You need this when a new app version ships, or to check something here.

1. Get the APK. An `.xapk` from an APK mirror is a zip; the app itself is `com.kohler.hermoth.apk` inside it.
2. Install jadx (`brew install jadx`) and run:

   ```sh
   jadx -d konnect-src -j 8 --show-bad-code com.kohler.hermoth.apk
   ```

   This took about 15 minutes for 3.0.6 (48k classes). Around 390 classes fail to decompile; none are in the product packages.
3. Dump the dex strings for fast searching:

   ```sh
   unzip -o com.kohler.hermoth.apk 'classes*.dex' -d dex
   for f in dex/classes*.dex; do strings -n 6 "$f"; done > allstrings.txt
   ```

### Finding things in a new build

Product code lives under `com/kohler/hermoth/products/<product>/`: `anthem`, `anthemhub`, `faucet`, `dtv`, `evocycle`, `moxie`, `icebathcontrol`, `sfc`, `verdera`. Most logic, however, is in **obfuscated short-named packages whose names change every build** (`db0/`, `nc0/`, `rg0/`…), so search by content:

| To find | Search for |
|---|---|
| Every endpoint path | `com/utils/network/retrofit/proxy/ApiConstant.java`; Retrofit interfaces `com/kohler/hermoth/data/network/{DeviceApiCall,PlatformApiCall}.java` |
| Request and response fields | `@SerializedName` in `…/products/<product>/data/model/`, `…/compose/models/` and `com/utils/network/retrofit/proxy/device/model/` |
| The valve word encoder / decoder | `"primaryValve1"`, or the °F switch over 59–122 (`db0/c.java` `f1()`/`C()`/`A()` in 3.0.6) |
| Outlet type table | `"Rainhead"` with `"Silk"` / `"Real Rain"` (`db0/c.java` `X()`) |
| MQTT dispatch | `"GCS_SOLO_STS"`, `"SHOWER_VALVE_STS"`, `"FAVORITES_SNAPSHOT"` (the `*Activity.java` files under each product) |
| MQTT client / heartbeat | `$iothub/methods`, `ExecuteControlCommand`, `MOBILECONNECT` |
| Water usage requests | `"MM-dd-yyyy"` beside `"Month"` / `"Day"` (`rg0/t0.java` `b0()`) |
| Status code table | `DEVICE_OFFLINE_900` (enum in `sy0/a.java`; messages in `oy0/b.java`) |
| User-facing strings | `resources/res/values/strings.xml` — e.g. `txt_freeze_*`, `*_disconnect` |
| App defaults | `assets/deviceconfig.properties` (mostly placeholders; see the product chapters) |

**MSAL constants hide values.** The compiler reused identical strings from the auth library, so a few outlet codes appear as `PublicApiId.PCA_ACQUIRE_TOKEN_SILENT_WITH_PARAMETERS` (= `"21"`) and similar. Resolve them in `PublicApiId.java`.

## Keeping this current

- When a capture confirms an **app** fact, change its marker to **live** and say where it was confirmed.
- When the app and a capture disagree, record both. The disagreement is the useful part.
- Product integrations should link here rather than copy. If a fact is wrong in one place, it is wrong for every consumer.
