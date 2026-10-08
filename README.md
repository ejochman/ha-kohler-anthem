<p align="center">
<h1 align="center">(Unofficial) Home Assistant Integration for Kohler Anthem and Athem+ Shower Systems</h1>
</p>

<p align="center">
  A custom Home Assistant integration for <b>Kohler Digital Anthem</b> and <b>Anthem+</b> shower systems.
</p>

<p align="center">
  <a href="https://github.com/kedube/ha-kohler-anthem/actions/workflows/test.yml"><img src="https://github.com/kedube/ha-kohler-anthem/actions/workflows/test.yml/badge.svg" alt="Test"></a>
  <a href="https://github.com/kedube/ha-kohler-anthem/actions/workflows/validate.yml"><img src="https://github.com/kedube/ha-kohler-anthem/actions/workflows/validate.yml/badge.svg" alt="Validate"></a>
  <a href="#install"><img src="https://img.shields.io/badge/HACS-custom-41BDF5" alt="HACS: custom repository"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.3%2B-41BDF5" alt="Home Assistant 2026.3 or later">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT licence">
</p>

> ⚠️ This is an **unofficial** integration. It is not affiliated with or endorsed by Kohler, and the underlying API may change at any time.

<p align="center">
  <img src="docs/images/screenshot_1.png" alt="Kohler Anthem integration in Home Assistant">
</p>

## Two products, one integration

Kohler sells two different things under the Anthem name, and an account can have either or
both.

**Anthem** — the digital valve itself, Wi-Fi built in. Controlled with a raw command word:
any outlet, any temperature, any time.

**Anthem Plus** — a Linux system controller that sits in front of the valve, adding music
(with Kohler's K-30319 amplifier), lighting, steam, and support for a second valve body.
Controlled by activating named favorites — whole scenes, not individual outlets.

Each appears as its own device in Home Assistant, with its own entities, and an account with
both gets both. See [the full guide](docs/user_guide.md#using-both-together) for how they
interact when combined.

<p align="center">
  <img src="docs/images/screenshot_2.png" alt="Kohler Anthem Device in Home Assistant">
</p>

### Highlights

* **Per-outlet control** — every outlet is its own switch, in both zones.
* **Live outlet and temperature** — move a setpoint or flip an outlet and the water follows
  immediately. No scene to apply, no confirm step.
* **Every valve on the account** — one device per Anthem valve and per controller, each with
  its own entities and settings.
* **One-command shower** — a `custom_shower` action sends outlets and temperature to the
  valve as a single command, the reliable way to drive it from an automation.
* **Raw escape hatch** — a `send_valve_hex` service for anything the normal controls can't do.

### MQTT real-time state

Kohler's cloud tells Home Assistant the moment anything changes, and this integration simply
listens — nothing here polls the shower's state on an interval.

* **It is live.** Open an outlet at the touchscreen, nudge the temperature in the Konnect app,
  or let the shower stop itself — Home Assistant knows as it happens.
* **Nothing slips past.** A pause, a shut-off, the restart right behind it — each arrives
  instantly, with nothing lost between polling intervals (because there are none).
* **Automations fire on the moment**, not a cycle later.
* **Easy on your network — and on Kohler's.** The connection stays open and waits, rather than
  signing in and asking over and over.

## Requirements

* Home Assistant **2026.3** or later
* A Kohler Konnect account, with the shower already set up in the Konnect app
* **Internet access.** Control is cloud-only for both products. If Kohler's cloud is
  unreachable, nothing here can turn the shower on or off.

The only Python dependency is `paho-mqtt`, installed automatically.

## Install

This integration is **not in HACS's default store.** Add it as a custom repository.

**Via HACS**

[![Open this repository in HACS on your Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=kedube&repository=ha-kohler-anthem&category=integration)

That button opens this repository in HACS on your own instance, adds it as a custom repository
and offers the download. It needs [My Home Assistant](https://my.home-assistant.io/) set up in
your browser. Otherwise, add it by hand:

1. In HACS, open the ⋮ menu and choose **Custom repositories**
2. Add `kedube/ha-kohler-anthem` (or the full GitHub URL), category **Integration**
3. Find **Kohler Anthem** in HACS and install it
4. Restart Home Assistant

HACS installs from **releases**, not from the latest commit.

**Manually**

Copy the `custom_components/kohler_anthem/` folder from this repository into the
`custom_components/` folder of your Home Assistant configuration directory, so that
`config/custom_components/kohler_anthem/manifest.json` exists, and restart.

## Setup

**Settings → Devices & Services → Add Integration → Kohler Anthem**

Sign in with your Konnect account and you are done. The integration reads the account, works
out which hardware you have — valve model, how the outlets split across zones, whether a
controller is in front of it — and builds the matching devices itself. Your password is
exchanged for a token and never stored, and temperature units follow whatever your Konnect
account already uses.

There is no Configure dialog. Every setting that can change after setup is an entity on the
device page, where automations and dashboards can reach it too.

## What works, and what does not

**Supported**

* **Kohler Anthem Digital Valve** — `K-28209`, `K-28210`, `K-28211`, `K-28212`. One unit
  containing up to two zones, each with up to three outlets. An installation that doesn't
  match one of the four still works — an unrecognised outlet split produces a usable model
  rather than an error.
* **Anthem Interface** (`K-28214`) and **Anthem+ Interface** (`K-28214-ASC`), with the
  **Anthem+ System Controller** (`K-27756`).

⚠️ **Not supported**

* **The older DTV systems.** A previous generation of Kohler digital showering, on a
  different protocol entirely. Nothing here applies to them.
* **Kohler Duo Control.** No Wi-Fi and no Konnect connection, so there is nothing for an
  integration to talk to.
* **The mechanical Anthem.** Kohler sells both under that name. Only the digital,
  network-connected one has an API.

## Contributing

Different hardware is the most useful thing anyone can contribute. Two things make a report
diagnosable:

* **Download diagnostics** — on the integration card and both device pages. One JSON report
  of the whole installation, with credentials, account identity and serial numbers redacted.
  On anything other than a K-28212, this is the single most useful file you can send.
* **Report Log** — a switch on both device pages that captures every raw MQTT message, one
  file per switch-on, continuing across a Home Assistant restart so "it breaks when I
  restart" stays one piece of evidence.

Check both before sharing — they carry device identifiers and show when the shower was used.
The reports folder lives inside the integration, so updating or reinstalling deletes it.

## Known limitations

* **Cloud-only.** No local control path exists for either product.
* **Flow may be overwritten on some hardware.** Each zone has a Flow number. A
  first-generation Anthem touchscreen has been captured rewriting both zones the moment its
  flow panel is opened, so on such an install a setpoint may not hold — disable the entity if
  yours behaves that way.
* **The API is undocumented** and Kohler can change it without notice.
* **One installation tested.** A single K-28212 — six outlets, three and three — with a
  controller on firmware 2.88. Other models are supported on what the protocol says, not on
  anyone having run them.

This is an unofficial, community-built integration, reverse-engineered from Kohler's cloud
protocol. It is not a supported product, and it comes with no warranty of any kind. Anything
that can run water deserves that caution.

## Documentation

**[The full guide](docs/user_guide.md)** covers every entity, the `custom_shower` and
`send_valve_hex` actions, each feature in detail, automation examples and troubleshooting.

**[docs/](docs/)** also has the valve command word reference
([`gcs/valve_hex.md`](docs/gcs/valve_hex.md)), how to capture diagnostics
([`mqtt/capture_runbook.md`](docs/mqtt/capture_runbook.md)), and — for developers — a
reference to Kohler Konnect's cloud protocol ([`protocol/`](docs/protocol/README.md)),
written so other Kohler integrations can reuse it.

## Prior art

This project builds on two earlier ones: [frozenmartini/kohler-anthem-plus](https://github.com/frozenmartini/kohler-anthem-plus)
and [kenyonj/kohler-konnect-ha](https://github.com/kenyonj/kohler-konnect-ha). Credit to both
for the original work.

## Licence and trademarks

MIT — see [LICENSE](LICENSE).

Kohler, Anthem, Anthem+ and Konnect are trademarks of Kohler Co. This project is not
affiliated with, authorised by, or endorsed by Kohler Co., and is not a supported product.
