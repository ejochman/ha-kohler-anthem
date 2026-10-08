# Changelog

Notable changes for each tagged release. Versions correspond to git tags and to the
`version` field in `custom_components/kohler_anthem/manifest.json`. Add entries under
**Unreleased** as part of each change; the release workflow rotates that section into a
version heading and publishes it as the release's Highlights.


## Unreleased

**New**

- Multi-zone Anthem valves previously always appended the zone number to every outlet switch, temperature/flow slider, active binary sensor, and zone hex sensor (e.g. 'Showerhead 1', 'Temperature 1').
- Add a 'Zone & outlet grouping' option under Settings -> Devices & services -> Kohler Anthem -> Configure (CONF_ZONE_GROUPING) with three modes:

    'subdevices': Splits each zone on a multi-zone valve into its own child device ('Anthem Valve Zone 1', 'Anthem Valve Zone 2') linked via via_device to the parent valve, and drops the trailing zone numbers from outlet switches ('Showerhead', 'Body Sprays'), controls ('Temperature', 'Flow'), and sensors ('Zone Active', 'Hex'). Whole-valve entities remain on the parent valve device.

    'outlet_labels': Keeps a single device per valve, drops redundant zone numbers from outlet switches (unless the same fixture type appears in multiple zones), and labels per-zone controls and sensors with their zone's fixtures (e.g. 'Temperature (Showerhead, Body Sprays)', 'Zone Active (Showerhead, Body Sprays)').

    'numbered' (default): Preserves the existing single-device numbered naming ('Showerhead 1', 'Temperature 1', 'Shower Active 1').

- Entity unique_ids remain identical across all three modes so switching modes updates existing entities in place without orphaning them, and stale zone child devices are automatically cleaned up when switching away from 'subdevices'.

## 0.22 — 2026-10-08

**New**

- **Anthem Plus device page links to the controller's web settings page**, using the
  network address Kohler's cloud reports for it (Wi-Fi, or wired when there's no Wi-Fi
  address). Found the same way the Konnect app finds it, but not yet tried on a real
  controller.

**Removed**

- **Endless Shower.** The switch that turned the water back on when the valve reached its
  Max Shower Duration is gone. Running water without end isn't something the Konnect app
  offers, and this integration no longer does either. For a longer shower, raise the valve's
  `Max Shower Duration` (up to 60 minutes) and, with an Anthem Plus, the controller's own.
  Upgrading removes the switch, its stored settings and its repair notices automatically.
- **The duration-mismatch repair notice** added in 0.20. It existed only for Endless Shower.
  The controller's `Max Shower Duration` sensor stays: the shorter of the two limits ends a
  shower.
- **The `cutoff_*.jsonl` debug journal**, which recorded Endless Shower's decisions. The
  `Start new MQTT capture` button now starts a new raw capture file only.

**Changed**

- **The firmware update entities are renamed `Firmware Status` and `Gateway Firmware
  Status`** (the controller's is `Firmware Status` too), so they're no longer confused with
  the diagnostic sensors that show the version numbers. Two entities on the valve were both
  called `Gateway Firmware`. Existing entity IDs don't change.
- **They show icons instead of the Kohler logo**: a package icon that changes when an update
  is available.
- Diagnostics: the `endless_shower` block is now `run_time`, holding the valve's run-time
  limits and how long each zone has been running. The `flowing_for_seconds` and
  `seconds_remaining` attributes are unchanged.

## 0.20 — 2026-10-08

Built from a decompile of the Kohler Konnect Android app, version 3.0.6. It settled most of
the protocol questions this integration had left open, and showed where the integration
differed from the app. Controls marked *new* send exactly what the app sends but have not
been run against hardware yet. Please report how they behave.

**New**

- **Firmware update entities** for the valve, its gateway and the Anthem Plus controller:
  installed vs latest version, checked twice a day. Read-only — install in the Konnect app.
- **Anthem Plus `Steam` switch** (*new*): runs the controller's default steam program, as
  the app's *Steam start* card does. Refused while the controller is running the shower.
- **`Experience` dropdowns** on the valve and the controller (*new*): start and stop
  Kohler's built-in programs. The valve's uses the same command as favorites, which earlier
  versions of this integration believed the valve ignored.
- **Valve `Restart` button** (*new*, disabled by default): the app's *Restart Product*.
- **Anthem Plus `Problem` sensor**: faults, active errors, and accessories that are set up
  but have stopped responding ("Steam is disconnected", a missing SD card), which used to
  just disappear.
- **Anthem Plus `Max Shower Duration` sensor**, read from Kohler's cloud. When Endless
  Shower is on and it differs from the valve's limit, a **repair notice** says so. That is
  the configuration that lets the controller end a shower Endless Shower is keeping alive.
- `Water Used Today` / `This Week` gain `times_turned_on`. `Light` gains per-group state.
  `Steam` gains temperature, timers and `power_clean`. Outlet switches gain `outlet_variant`
  (Silk, Real Rain, Katalyst…).

**Changed**

- **Zone temperature now matches the app's slider**: **Cold**, then 59 °F up to the
  valve's current Max Temperature, which was a fixed 92–118 °F before. The bottom step sends
  full cold. `custom_shower` accepts 59–118 °F.
- **Every outlet type now has a name**, from the app's own list. 38 (Silk rainhead), 39
  (Real Rain rainhead), 62 (foot sprays) and others that showed as `Outlet 1.2` are now
  named. **Renamed switches keep their entity ids**, so automations are unaffected.
- `System State` knows `error` and `FirmwareUpdate`; `error` also turns `Problem` on.
- **Cloud Connection reads `Disconnected` directly**, and an unfamiliar value no longer
  counts as an outage. This is the app's rule.
- **Outlet settings are written with all twelve fields** the app sends, adding `maxVolume`
  and `purge`, rather than ten.
- Presets written by Home Assistant use the app's `"000000"` for unused valves. A favorite
  saved at 120 °F in the app no longer has a valve dropped from it when the default preset's
  timer is synced.
- Kohler's in-body status codes are compared as strings and explained in errors: firmware
  update in progress, favorites full, name taken.

**Removed**

- **`kohler_anthem.probe_usage`.** It existed to work out how Kohler's usage-history
  endpoint wanted to be called; the app answered that.

**Documentation**

- **[`docs/protocol/`](docs/protocol/README.md)**: a developer reference to the Kohler
  Konnect cloud protocol — sign-in, every endpoint and message, status codes, firmware —
  for the Anthem valve, the Anthem Plus controller and the Sensate faucet, written to be
  reused by other Kohler integrations.

## 0.17 — 2026-10-06

- **K-28211 (4-outlet) valves: zone 2 now reads its own outlets.** The valve numbers its
  outlets 0, 1, 3, 4 — each valve body reserves three slots — where the integration
  assumed 0-3. Zone 2's fixture names, flow range and run-time limits were read from the
  wrong outlet, and Max Shower Duration's attributes failed with `ValueError: K-28211 has
  outlets 1-4; got 5`. Thanks to @ejochman (#1).
- **⚠️ K-28211 owners: check automations that use zone 2 outlet switches.** An outlet
  switch's entity id follows its fixture name, and zone 2's names were taken from the wrong
  outlet. After updating, each zone 2 switch is named for the fixture it actually controls,
  so an existing entity id can now point at a different physical outlet — for example,
  `switch.anthem_valve_rainhead_2` used to control zone 2's *second* outlet and now
  controls the first, which is the actual rainhead. The old `Outlet 2.1` switch is left
  unavailable and can be removed.

## 0.02 — 2026-09-12

- **The integration is renamed: Kohler Anthem Plus is now Kohler Anthem.** "Plus" named
  the second-generation hub hardware (Anthem Plus, the HUB system controller), not the
  integration itself — the integration has always covered the base Anthem valve too.
  Nothing about the physical **Anthem Plus** hardware is renamed; it is still called that
  throughout the documentation and the entity layer, because that is Kohler's own product
  name for it.
- **⚠️ Breaking: the domain changed**, `kohler_anthem_plus` → `kohler_anthem`. Home
  Assistant treats a domain change as a different integration, so this is not an in-place
  upgrade — remove the old integration and add **Kohler Anthem** fresh from Settings →
  Devices & Services. Entity ids created fresh will read `..._anthem_..._` rather than
  `..._anthem_plus_...`; automations and dashboards referencing the old ids will need
  updating.
- **Documentation trimmed to what's needed to use the integration.** `docs/` previously
  carried the protocol reverse-engineering research this integration was built from —
  capture analysis, decompile notes, case studies working through individual showers
  message by message. What remains is scoped to installing and using the integration: the
  entity reference and service docs (`docs/user_guide.md`), the valve command word
  reference for `send_valve_hex` (`docs/gcs/valve_hex.md`), and how to capture diagnostics
  for a bug report (`docs/mqtt/capture_runbook.md`).
- **Minimum Home Assistant version raised to 2026.3** — the version that added the brands
  proxy this integration's icon relies on.
- **Versioning switched to `x.y`** — no third component, rolling from `.99` to the next
  major (`0.99` → `1.00`).
