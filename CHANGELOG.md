# Changelog

Notable changes for each tagged release. Versions correspond to git tags and to the
`version` field in `custom_components/kohler_anthem/manifest.json`. Add entries under
**Unreleased** as part of each change; the release workflow rotates that section into a
version heading and publishes it as the release's Highlights.

## Unreleased

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
