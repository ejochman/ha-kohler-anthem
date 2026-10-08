# Kohler Konnect cloud: the shared platform

Everything here is common to every Konnect product: Anthem valve (`GCS`), Anthem Plus controller (`HUB`), Sensate/Setra faucets (`SEN`/`SET`) and the rest. Product chapters cover what differs: [gcs_valve.md](gcs_valve.md), [hub_controller.md](hub_controller.md) and [sensate_faucet.md](sensate_faucet.md).

Confidence markers are explained in the [README](README.md#confidence): **live** (verified against hardware), **app** (read from Konnect 3.0.6) and **inferred**.

---

## 1. Sign-in: Azure AD B2C

| Item | Value | Confidence |
|---|---|---|
| Tenant | `konnectkohler.onmicrosoft.com` | live |
| Policy | `B2C_1A_signin` (custom policy) | live |
| Client id | `8caf9530-1d13-48e6-867c-0f082878debc` | live |
| API resource | `f5d87f3d-bdeb-4933-ab70-ef56cc343744` | live |
| Scope | `openid offline_access https://konnectkohler.onmicrosoft.com/f5d87f3d-bdeb-4933-ab70-ef56cc343744/apiaccess` | live |
| Redirect URI | `msauth.com.kohler.hermoth://auth` — strictly validated; the older `msauth://com.kohler.hermoth/<hash>` is **no longer registered** | live |
| Authority | `https://konnectkohler.b2clogin.com/tfp/konnectkohler.onmicrosoft.com/B2C_1A_signin` | live |

**Writes need a token from the `B2C_1A_signin` policy.** Tokens from the ROPC policy are accepted for reads but get HTTP 403 on `/commands/*` (live). The redirect URI is a custom scheme, so the usual browser round trip is impossible. Both integrations instead drive the policy server-side, the way the sign-in page's own JavaScript does:

1. `GET /authorize` with PKCE. Keep the cookies, and take `csrf` and `transId` from the page's `var SETTINGS = {...}` blob.
2. `POST {policy}/SelfAsserted` with the email and password. A small JSON status comes back; `AADB2C90053` means bad credentials.
3. `GET {policy}/api/CombinedSigninAndSignup/confirmed` **without following redirects**. The `302 Location:` header carries `msauth.com.kohler.hermoth://auth/?code=…`.
4. `POST /oauth2/v2.0/token` with that code and the PKCE verifier.

**Refresh tokens rotate on every refresh** (live). If you drop the new one, the account is stranded until the next interactive sign-in. They last up to about 90 days. A useful error code: `AADB2C90006` means the redirect URI is not registered.

The access token's claims carry the tenant id (`decode_tenant_id` in `anthem/auth.py`). Every request below needs it.

## 2. REST conventions

| Item | Value | Confidence |
|---|---|---|
| Base | `https://api-kohler-us.kohler.io` | live |
| Header | `Ocp-Apim-Subscription-Key: 429ecb1d0b5e4258aa0a2bfadd82a493` — app-global, identical across accounts | live |
| Auth header | `Authorization: Bearer <access token>` | live |
| mTLS | **Not** needed on `api-kohler-us.kohler.io`. The client certificate in the APK is for the alternate `*.kohlerkonnect-apim.azure-api.net` gateway only | live |

Path families:

| Family | Prefix | Used for |
|---|---|---|
| Device reads | `/devices/api/v1/device-management/` | State, configuration, presets, usage, diagnostics, notifications, account. The app also builds these as `/devices/api/{version}/…` with `version = "v1"` |
| Commands | `/platform/api/v1/commands/<product>/<verb>` | Every write that changes a device. The id travels in the body, not the path |
| Firmware | `/platform/api/v1/firmware/<type>/{deviceId}` | Version check (`GET`), install (`POST`), skip (`PATCH /firmware/skip/{id}`) |
| Mobile registration | `/platform/api/v1/mobile/settings` | MQTT credentials (§4) |
| Users | `/users/api/v1/…` | App-version checks; not needed by an integration |

Every command body starts `{deviceId, sku, tenantId, …}` (app). **Use the device's own `sku` from the account read.** The app never hard-codes it, and one product family can have several SKUs (faucets are `SEN` *and* `SET`).

### 2.1 Errors live in the body as well as the HTTP status

A request can return HTTP 200 and still have done nothing. Check both:

- the HTTP status, and
- `statusCode` inside the JSON body, alongside a `message`.

Konnect models `statusCode` as a **string** and translates it only when the HTTP status is 400 (`oy0/b.java`, app). Compare it as a string; an int-only comparison misses `"900"`.

| Code | Meaning (the app's text) | Product |
|---|---|---|
| 900 | Product is offline | all |
| 901, 902 | A favorite cannot be updated while running (same text for both). **Activating is still allowed** | all |
| 903 | Can't start: a firmware update is in progress | all |
| 904, 909, 911, 918 | Product error; resolve and retry (918 shows the server's message) | all |
| 905 | Preparing to retry the update | all |
| 906 | "Water could not be dispensed. Please turn on your faucet manually." | faucet |
| 908 | Firmware is up to date | all |
| 913, 914 | Seat-toilet wording; no code path references them | toilet |
| 915 | Presets (or favorites) are at their maximum | all |
| 916 | Name already exists | all |
| 917 | The server's message ("Something went wrong") | all |
| 919 | EvoCycle ozone | EvoCycle |

**201 means "accepted for delivery", never "applied".** Command endpoints return a correlation id and no echo. Confirm through MQTT or by reading back after a few seconds (live; an immediate read can still show the old value).

### 2.2 Ids in paths

Every read carries a device or tenant id in its path. If you log or raise errors that include paths, redact `…/gcs-xxx/<id>`, `…/customer-device/<id>` and `…/firmware/<type>/<id>`. See `KohlerClient.safe_path` in `anthem/client.py`. Kohler device ids double as cloud addresses.

## 3. The account

`GET /devices/api/v1/device-management/customer-device/{tenantId}` (live) returns:

- `temperatureUnit` (`Fahrenheit` / `Celsius`) — the account's **display** preference;
- `waterUnits` (`Standard` = US gallons, or `Liters`);
- `customerHome[].devices[]` — note the singular key. Each device has `deviceId`, `sku`, `logicalName` and `serialNumber`.

**Never use the shape of a device id to tell products apart.** An Anthem Plus controller's id can begin with `gcs`. Branch on `sku`.

Known SKUs (app):

| SKU | Product |
|---|---|
| `GCS` | Anthem digital valve |
| `HUB` | Anthem Plus controller |
| `SEN`, `SET` | Sensate and (probably) Setra faucets |
| others | DTV+, Numi, EvoCycle, Moxie, Blade, ice bath, SFC (fan) — not covered here |

Other account routes (app): `customer-device/{tenantId}/home` (address); `PATCH customer-device/{tenant}/{home}/device` (rename, or remove with `operation: "Delete"`); `customer-device/{tenantId}/freezeMitigation` (faucet, see the faucet chapter); `customer-experience/{tenantId}` (the cross-device presets list on the app's home screen).

## 4. Real-time state: Azure IoT Hub over MQTT

### 4.1 Getting credentials

`POST /platform/api/v1/mobile/settings` (live) registers a "mobile device" and returns `ioTHubSettings {ioTHub, deviceId, username, password, clientId, connectionString}`.

```json
{
  "tenantId": "<tenant>",
  "mobileDeviceId": "<stable id>",
  "username": "<display name>",
  "os": "Android",
  "devicePlatform": "FirebaseCloudMessagingV1",
  "deviceHandle": "<push token>",
  "tags": ["FirmwareUpdate"]
}
```

- **Persist `mobileDeviceId` and reuse it.** A fresh one per connect registers a new "phone" on the account every time. The app uses the phone's `ANDROID_ID`.
- **Never persist the password.** It is a short-lived SAS token, so fetch it on every connect.
- `DELETE /platform/api/v1/mobile/settings/{tenantId}/{mobileDeviceId}` unregisters. The app does this on sign-out.

### 4.2 Connecting

| Setting | Value | Confidence |
|---|---|---|
| Host | `ioTHub` | live |
| Port | 8883, TLS | live |
| Protocol | MQTT 3.1.1 | live |
| Client id | `deviceId` from the settings | live |
| Username / password | `username` / `password` from the settings | live |
| Keepalive | 60 s works | live |
| Subscribe | `$iothub/methods/POST/#` | live |

Status arrives as **direct-method calls** on `$iothub/methods/POST/ExecuteControlCommand/?$rid=N`. Across 856 captured messages, 100% arrived there and nothing on any device-scoped topic. **Answer every one** with `$iothub/methods/res/200/?$rid=N`, or the service treats it as unhandled. The app answers `ExecuteControlCommand` with 200 and anything else with 404 (app). It never uses the device twin and has no cloud-to-device handler.

**The subscription covers the whole account.** One session receives messages for every device, so filter on the payload's `deviceid` and `sku`.

**The app publishes one thing** (app, `qy0/a.java`). On every connect it sends a device-to-cloud telemetry event on `devices/{deviceId}/messages/events/`:

```json
{
  "type": "MOBILECONNECT",
  "sku": "MOBILE",
  "deviceid": "<IoT deviceId>",
  "tenantid": "<tenant>",
  "timestamp": "<epoch ms>",
  "ver": "1.0",
  "protocol": "MQTT",
  "ttl": "5000",
  "durable": "true",
  "simulated": "false"
}
```

The app also sends a fixed `messageId` and `sysid`. It never publishes control commands; all writes are HTTPS.

**A brand-new registration hears nothing for about the first minute**, despite a clean CONNECT and granted SUBACKs (live). Register once, hold the connection, and treat early silence as meaningless. Reconnecting per command guarantees you receive nothing. It is an **untested hypothesis** that the missing `MOBILECONNECT` event causes this delay; neither integration sends it yet.

**The broker replays nothing on connect** (live, 27 sessions). The first message is always a change event, never a state dump, and quiet periods of 12 h or more are normal. So read state over REST after every connect.

The app retries a dropped connection after 500 ms. After sending a command it waits for the MQTT reply in 5 s ticks, with up to 2 retries (app).

### 4.3 Message envelope

```json
{
  "deviceid": "gcs-…",
  "sku": "GCS",
  "tenantid": "…",
  "timestamp": "…",
  "data": {
    "code": "GCS_SOLO_STS",
    "attributes": [ { "code": "GCS_SOLO_STS", "…": "…" } ]
  }
}
```

Dispatch on `data.code`. Each product chapter lists its codes. Some products put extra keys beside `attributes` under `data`, for example the controller's `showerwarmup` and `favoriteid`. **Every message is proof of life**, including codes you don't decode.

## 5. Device connection state

State reads (`gcs-state/gcsadvancestate`, `hub-state`, the faucet state) carry:

- `connectionState`: `Connected` / `Disconnected`
- `lastConnected` (epoch)
- `deviceConnectionEventSequenceNumber`

Only `Connected` has been captured. `Disconnected` is the negative the app tests for, and **the app treats a device as online unless the value is exactly `Disconnected`** (app, `mc0/n.java`, `ui/ota/f.java`). Nothing pushes this field. Read it when you have a reason to doubt reachability; see `cloud_watch.py` for an event-driven way to decide when.

## 6. Water usage

`GET /devices/api/v1/device-management/<product>-usage/{deviceId}?FromDate=…&ToDate=…&Interval=…`, where `<product>` is `gcs`, `hub`, `faucet`, `blade`, `dtvplus` or `numi`.

- **The query parameters are PascalCase**, unlike every other endpoint. A wrong case gets the same generic 400 as a bare call. This is why the endpoint went unsolved for so long.
- **What the app sends** (app, `rg0/t0.java` `b0()` — one view model for every product): dates as `MM-dd-yyyy`, and `Interval` as `Day` or `Month` (mixed case).

  | App tab | Range | `Interval` |
  |---|---|---|
  | Week | Sunday to Saturday | `Day` |
  | Month | 1st to last day of the month | `Day` |
  | Year | Jan 1 to Dec 31 | `Month` |

  If today falls inside the range, the end date is clamped to today.
- **`WEEK` and `YEAR` are not intervals.** The server refuses `WEEK` with a 400 at every range (live), and the app never sends either. The uppercase constants that suggested otherwise belong to another product's screen. Build a week from seven `Day` buckets.
- **The server also accepts ISO dates (`yyyy-MM-dd`) and uppercase `DAY` / `MONTH`.** The Anthem integration uses those (live 2026-09-10/11); daily buckets summed exactly to the monthly figure.
- **Response** (`AnthemWaterUsageModel` for GCS; other products are analogous):
  - Summary fields: `avg/min/max` of `AverageBlendTemperature`, `NumberOfTimesValveSwitchedOn`, `OnDuration` and `Volume`, plus `interval` and `deviceId`.
  - Buckets: `gcsUsageDataDetailsList[] {intervalKey, volume, onDuration, numberOfTimesValveSwitchedOn, averageBlendTemperature, timestamp}`. `intervalKey` is `yyyy-MM-dd` or `yyyy-MM`.
  - The HUB adds hot inlet temperatures; the faucet list is `senSateUsageDataDetailsList`.
- **`volume` is litres** whatever the account unit. The app multiplies by `0.264172` for `Standard`.
- **No unit is known for `averageBlendTemperature`.** The app never displays it, so don't guess one.

## 7. Firmware

| Step | Request | Confidence |
|---|---|---|
| Check | `GET /platform/api/v1/firmware/<type>/{deviceId}` — GCS, HUB and EVO add `?releasetarget=Public`; sensate, blade, dtvplus, numi and sfc do not | app |
| Install | `POST` the same path without the query: `{tenantId, firmwareNumber: <latest>, releaseTarget: "Public"}` | app |
| Skip | `PATCH /platform/api/v1/firmware/skip/{deviceId}` with `{firmwareVersion, skip: true, sku}` | app |
| Auto-update time | `devices/api/v1/device-management/firmware/setautootatime` (constant only) | app |

`<type>` values: `gcs`, `gcs/gateway`, `hub`, `sensate`, `evo`, `numi`, `blade`, `dtvplus`, `aquifer`.

The check returns `{currentFirmware, firmware (the latest), firmwareUpdateAvailable, mandatoryUpdate, otaStatus, skip, estimatedTimeForOTA, fileSizeInMb, url, configuration}`. **The app decides "update available" from `firmwareUpdateAvailable` alone.**

Guards the app applies before installing:
- no water running;
- the device not `Disconnected`;
- on a faucet, the handle open.

While installing:
- The app polls every 10 s for up to 2 h, reading the product's configuration `firmwareUpdate` block `{progress, progressPercent, status, version, …}`.
- Faucets also send MQTT `INSTALL_FIRMWARE_STS {code, status: Installed|Aborted, version}`.

`otaStatus` values:
- **Progress:** `NotStarted`, `Started`, `Downloading`, `Installing`, `signatureverified`, `Completed`, `ConnectedDeviceOTAInProgress`
- **Failure:** `sasfailed`, `downloadingfailed`, `flashingfailed`, `signatureverifiedfailed`, `Failed`

## 8. Diagnostics (fault logs)

`GET` / `DELETE /devices/api/v1/device-management/<product>-diagnostics/{deviceId}` (`gcs`, `hub`, `evo`; plus `hub-diagnostics/{id}/active`) returns `{deviceId, id, sku, tenantId, errorDetails[]}` (app).

Each `ErrorDetail` entry has:
- `area`, `component`, `valveId`
- `title`, `description`, `detailTitle`, `details`
- `errorCode`, `errorDescription`, `errorState`, `isActive` (bool)
- `id`, `type`
- `timestamp` — GCS spells it this way; HUB uses `timeStamp`

The app drops entries whose `errorCode` is `"0"` or empty and shows the server's text. **There is no code-to-meaning table in the app.** strings.xml carries about 40 fault descriptions that no code references.

## 9. Customer notifications

`GET /devices/api/v1/device-management/customer-notification?CustomerIds=` returns entries with these fields (app):
- `alertId`, `id`, `recordId`
- `customerId`, `deviceId`, `deviceLogicalName`, `sku`
- `createdTime`, `completionTime`
- `isDeleted`, `isUnread`
- `notificationHeader`, `notificationMessage`, `notificationMessageType`, `notificationSource`, `notificationType`
- `title`

Related routes: `…/count?IsUnread=` returns the count; `PUT …/{customerId}/{id}` with `{isUnread}` marks one read; `DELETE …/{customerId}` with `{notificationIds}` deletes.

The only types the app acts on are `update`, `OTASuccessful`, and the faucet freeze types (see the faucet chapter). The `POST /platform/api/v1/notifications` the app declares has a `@Path("version")` with no `{version}` in its URL. Retrofit rejects that, so it almost certainly never fires in 3.0.6.

## 10. Provisioning (brief)

Wi-Fi setup is local and uses BLE or the device's soft-AP. Its endpoints are `device-provisioning/{deviceId}/{tenantId}` (v1 and v2), `validate-cert/{deviceId}` and `invalid-cert`. An integration that only controls already-provisioned devices needs none of this.
