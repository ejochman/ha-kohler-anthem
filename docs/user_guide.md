<h1 align="center">Kohler Anthem</h1>

<p align="center">
  Home Assistant integration for <b>Kohler Digital Anthem</b> and <b>Anthem+</b> shower systems.
</p>

<p align="center">
  <a href="#install"><img src="https://img.shields.io/badge/HACS-custom-41BDF5" alt="HACS: custom repository"></a>
  <img src="https://img.shields.io/badge/Home%20Assistant-2026.3%2B-41BDF5" alt="Home Assistant 2026.3 or later">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT licence">
</p>

<p align="center">
  <sub>Unofficial. Not affiliated with or endorsed by Kohler.</sub>
</p>

<p align="center">
  <sub>The full guide. For the short overview, see the <a href="../README.md">README</a>.</sub>
</p>

---

## Contents

- [What it does](#what-it-does)
- [At a glance](#at-a-glance)
- [Two products, one integration](#two-products-one-integration)
- [How it works](#how-it-works)
- [In Home Assistant](#in-home-assistant)
- [Entities](#entities)
- [Using both together](#using-both-together)
- [Services](#services)
- [Features in detail](#features-in-detail)
- [Requirements](#requirements)
- [Install](#install)
- [Setup](#setup)
- [Automation examples](#automation-examples)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Tested against](#tested-against)
- [Documentation](#documentation)
- [Licence and trademarks](#licence-and-trademarks)

---

## What it does

Puts your shower in Home Assistant — outlets, temperature, presets, and the Anthem Plus
controller's music, lighting and steam.

No official Kohler integration exists for this product. The community integrations that do
exist support only the older, simpler Anthem valve. This one was built by reverse-engineering
Kohler's Konnect cloud protocol from captured traffic on real hardware.

State arrives by **push, not polling.** The integration subscribes to Kohler's Azure IoT Hub
MQTT stream and updates the moment the hardware reports a change (`iot_class: cloud_push`,
`SCAN_INTERVAL = None`). REST is read once at setup, and again on each reconnect to reseed —
the broker replays nothing when you connect, so the state has to come from somewhere.

## At a glance

* **Both products, one integration.** The Anthem valve and the Anthem+ controller appear as
  two devices, and you can have either or both.
* **Push-based.** No polling loop. Changes made at the touchscreen, in the Konnect app, or by
  the hardware itself show up in Home Assistant within seconds.
* **Per-outlet control.** Every outlet is a switch; every zone has a temperature number.
* **Presets and favorites.** The valve's stored presets and the controller's named
  favorites are both exposed as dropdowns.
* **Warmup.** Kohler's pre-heat feature as a three-option dropdown, with an optional watchdog
  that puts it back when something silently turns it off.
* **One-command shower.** A `custom_shower` action: choose outlets and temperatures, and it
  goes to the valve as a single command — the form to use from an automation, because the valve
  cannot take two commands back to back. Optionally keeps the shower on past the valve's
  warm-up pause (beta).
* **Raw escape hatch.** A `send_valve_hex` service for anything the normal controls cannot do.
* **Diagnostics built in.** A Report Log switch and two debug loggers for capturing evidence —
  see [Diagnostics](#diagnostics).

## Two products, one integration

Kohler sells two different things under the Anthem name, and an account can have either or
both. They speak different protocols, and most confusion about this system comes from
assuming there is one device when there are two.

| | **Anthem** (the valve) | **Anthem+** (the controller) |
|---|---|---|
| What it is | The digital valve itself, Wi-Fi built in | A Linux controller sitting in front of the valve |
| Adds | — | Music (needs a K-30319 amplifier), lighting, steam, a second valve body |
| Controlled by | A raw hex command word — temperature, flow, outlet mask | Activating named favorites |
| Granularity | Any outlet, any temperature, any time | Whole scenes only |
| In the code and docs | `GCS` | `HUB` |

A physical Anthem valve is **one unit containing two zones**, each with up to three outlets.

Four valve models exist, and the integration knows the outlet split for each:

| Model | Outlets | Zone 1 | Zone 2 |
|---|---|---|---|
| K-28209 | 2 | 2 | — |
| K-28210 | 3 | 3 | — |
| K-28211 | 4 | 2 | 2 |
| K-28212 | 6 | 3 | 3 |

An installation that doesn't match one of these four still works — an unrecognised outlet
split produces a usable model rather than an error.

> ⚠️ **Digital Anthem, not the mechanical Anthem.** Kohler sells both under that name. Only
> the digital, network-connected one has an API to talk to.

## How it works

**Two APIs, in two directions.** Commands go out over Kohler's Konnect REST API. State comes
back over Azure IoT Hub MQTT, which Kohler's cloud pushes to. There is no polling loop at all.

**REST is read twice, not repeatedly.** Once at setup, and again on every MQTT reconnect. The
broker replays no history on connect, so without that reseed the integration would sit blind
until the hardware happened to say something.

**The valve is commanded with a hex word.** Temperature, flow and an outlet bitmask packed
into four bytes. Temperature is a 10-bit value spanning two bytes — `°C = ((byte0 & 0x03) << 8
| byte1) / 10` — so `0x184` (388) is 38.8 °C, or 101.8 °F. Flow is a single byte from `0x10`
(16) to `0xC8` (200), where `0xC8` (200) is 100 %. The full breakdown, including the outlet
mask and the pause bit, is in [`gcs/valve_hex.md`](gcs/valve_hex.md).

**The controller is not commanded that way.** It only activates named favorites — whole
scenes configured in the Konnect app, combining outlets, temperature, lighting, music and
steam. You cannot ask it for "outlet 2 at 39 °C"; that goes to the valve.

**Both are cloud-only.** The controller exposes a local LAN API, but it can read configuration
and cannot actuate anything.

## In Home Assistant

One device per product, each with its own entities: an **Anthem Valve** for each valve on
the account, and an **Anthem Plus** for each controller.

**Several of either.** An account with more than one valve or controller — one per
bathroom, say — gets one device per unit, each with the full set of entities, its own
favorites and settings (Warmup Auto-Restore is per valve), and each decoding outlets with the layout its own hardware reports,
so a 6-outlet valve and a 3-outlet valve on one account each get the right rows. To keep
their entity IDs apart, the devices are named after the unit's name in the Konnect app:
**Anthem Valve Master Bath**, **Anthem Plus Guest Bath**, and so on. With a single valve
or controller the device is plainly **Anthem Valve** or **Anthem Plus**, as it always was.
The `custom_shower` and `send_valve_hex` actions show a **Valve** field to say which valve
they are for. It appears only when there is more than one — with a single valve there is
nothing to choose, so the field is not shown, just as Zone 2 is not shown on a single-zone
valve.

## Entities

Entity IDs below assume the default device names **Anthem Valve** and **Anthem Plus**. If you
rename a device, its entity IDs change with it — and on an account with several valves or
controllers the default name already carries the Konnect name, so `switch.anthem_plus_shower`
becomes `switch.anthem_plus_master_bath_shower` and `switch.anthem_valve_shower` becomes
`switch.anthem_valve_master_bath_shower`.

### How entities are named

**On a single-zone valve — K-28209 and K-28210 — nothing carries a zone number.**
`Temperature` and `Flow` say everything a number would, and there's no `Shower Active`:
`System Status` already reports whether water is running, warming up, or paused.

**A multi-zone valve numbers each zone as a suffix:** `Temperature 1` and `Temperature 2`,
`Rainhead 1` and `Rainhead 2`. The number trails rather than leading (`Zone 2 Temperature`)
so related entities sort together in every Home Assistant list. Where one zone has two
outlets of the same fixture, the fixture takes its own number too — `Showerhead 1`,
`Showerhead 2` — and on a multi-zone valve the two combine as `Showerhead 1.2` (the second
showerhead in zone 1). An outlet whose type the valve hasn't reported falls back to its
position: `Outlet 1`, or `Outlet 2.1` on a multi-zone valve.

### Anthem valve

| Entity | Type | What it does |
|---|---|---|
| `Water Used This Month` | sensor | This calendar month's usage, from **Kohler's own history** — the same figure the Konnect app charts. Carries every month it returned as a `history` attribute |
| `Water Used This Year` | sensor | The last **twelve complete months** from Kohler's own usage history, summed. The current partial month is excluded so the value changes once a month rather than creeping daily — `Water Used This Month` covers that |
| `Shower on` | switch | Turns the shower on or off. From cold it opens **the valve's own default outlets**; if outlets are already open it preserves them |
| `Rainhead`, `Showerhead`, `Handshower`, `Tub Filler`, `Body Sprays`, `Foot Sprays`… | switch | One per outlet, named after the fixture the valve reports. See **Outlet names** below |
| `Temperature` | number | Setpoint for that zone, in your account's unit — the Konnect app's slider: **Cold**, then 59 °F up to the valve's current `Max Temperature`. The bottom step (58 °F / 14 °C) is **Cold**: the valve stops mixing in hot water, as the app's `COLD` stop does, and the `cold` attribute reads true. `Temperature 1` / `Temperature 2` on a two-zone valve |
| `Flow` | number | Flow as a percentage, bounded by the limits the valve itself reports. `Flow 1` / `Flow 2` on a two-zone valve. See **Flow** below |
| `Favorite` | select | Presets **stored on the valve**, added in the Konnect app or at the first-generation touchscreen |
| `Experience` | select | The valve's stored **experiences** (Wake Up, Cool Down…), added in the Konnect app. Started with the same command the app uses — new in this release and not yet tried on hardware; see [Favorites and presets](#favorites-and-presets) |
| `Warmup` | select | Off / All outlets / Selected outlets |
| `System Status` | sensor | `Water Running`, `Paused`, `Warming Up`, `Idle`. Whole-valve: warm-up and pause are system-level, not per-zone. Carries `seconds_remaining` — how long before the valve's run-time limit closes the water |
| `System State` | sensor | The valve's own `normalOperation` / `showerInProgress` flag — a second opinion to `System Status`, decoded differently. Also `error` and `FirmwareUpdate`, which the Konnect app acts on but no install has yet been seen to send |
| `At Temperature` | binary sensor | Whether the water has reached its setpoint |
| `Problem` | binary sensor | Whether the valve reports a fault — the error flag in its status word, or a `System State` of `error` |
| `Firmware`, `Gateway Firmware` | update | Installed and latest firmware for the valve and its Wi-Fi gateway, checked twice a day. **Read-only** — install updates in the Konnect app, which guards against installing mid-shower |
| `Restart` | button | Reboots the valve, as the app's *Restart Product* does. **Disabled by default** — a button can't ask "are you sure?", so enable it on purpose. Stops any running water; can't revive a valve that has dropped off the cloud |

#### The three settings the Konnect app also has

Configuration entities — they report what the valve holds **and change it**.

| Entity | Kind | Range |
|---|---|---|
| `Max Temperature` | number | 🚨 The **scald limit**, 92-118 °F — the app's own range |
| `Default Temperature` | number | Where a shower starts, 59-118 °F. Setting it **above the valve's current `Max Temperature` fails** with a message naming both numbers — a shower cannot start hotter than the scald limit. The limit in force is published as `scald_limit` |
| `Max Shower Duration` | select | 15 / 20 / 25 / 30 / 45 / 60 minutes — the app's six options, not a free range |

**Every change is verified.** The valve's endpoint replaces one outlet's whole record with no
list form, so a change is one call per outlet, chained — a `201` from that endpoint means
*accepted for delivery*, never *applied*. The entity responds at once, but the integration
re-reads the valve about 30 s later to confirm what actually landed; if some outlets took the
new value and others didn't, a **repair notice** appears under Settings naming the setting and
the outlets still holding the old value. `Max Shower Duration` carries `outlets_agree` —
`false` means a write like that was lost part-way. Selecting a duration rewrites all outlets.

⚠️ **Konnect 3.0.1 misreads any duration above 30 minutes**, showing it as 25 and writing 1500 s
if you tap Save. Fixed in 3.0.5. Setting 45 or 60 here is safe for the valve and safe in a
current app; only an out-of-date one would quietly undo it.

#### Outlet names

Outlet switches are named after the fixture the valve reports for that outlet, not its
position. The names come from the Konnect app's own outlet list — every type a valve can be
set up with:

| Code | Name | Variant (`outlet_variant` attribute) |
|---|---|---|
| 1 | Handshower | |
| 11 / 12 | Showerhead / Showerheads | |
| 21 | Tub Filler | |
| 30 | Not Plumbed | |
| 31–39 | Rainhead | Katalyst, Cascade, Kinetic, Rain Curtain, Laminar, Massage (Wave), Hydro Massage, Silk, Real Rain |
| 51 / 52 / 53 | Body Spray / Body Sprays / Body Spray | Single, Multiple, Massage (Wave) |
| 61 / 62 | Foot Spray / Foot Sprays | Single, Multiple |

An outlet whose type the valve hasn't reported yet keeps the positional form, `Outlet 1` or
`Outlet 2.1`. When a name later arrives — on a restart after the valve reports it, or because
this release learned a code that earlier ones didn't (38, 39 and 62 were unknown before) — the
switch is **renamed in place**: its entity id, history and automations stay as they were.

On a **single-zone valve the `Zone N ` prefix is dropped** throughout — there is nothing to
disambiguate. A two-zone valve keeps it. Two outlets of the same fixture in one zone are
numbered: `Showerhead 1`, `Showerhead 2`.

#### Flow

Each zone has a Flow number, bounded by the minimum and maximum the valve reports for that
zone rather than a fixed range — a valve with flow control disabled at the fixture offers
only the value it will honour, and says so in `flow_control_available`.

While the shower is off it shows the last flow Home Assistant wrote, defaulting to 100 %,
**not** the valve's idle flow byte — that byte can hold a calibrated ceiling rather than a
chosen setting while idle. While water is running the valve's own byte is authoritative,
including a change made at the panel mid-shower. `flow_is_live` says which of the two you are
reading.

> ⚠️ **A first-generation Anthem touchscreen may overwrite this.** Opening that panel's flow
> control has been captured rewriting *both* zones before any adjustment was made. If yours
> behaves that way, disable the entity — the protocol layer is unaffected either way.

### Anthem+ controller

| Entity | Type | What it does |
|---|---|---|
| `Shower` | switch | Starts or stops the shower via the controller, opening **the controller's own default outlets** — a separate setting from the valve's |
| `System` | switch | The controller's overall system state |
| `Favorite` | select | Favorites **stored on the controller**, added in the Konnect app or at the Anthem+ touchscreen. A different list from the valve's |
| `System Status` | sensor | `Water Running`, `Warming Up`, `Idle` |
| `Zone N Temperature` | sensor | Read-only; the controller offers no live temperature control |
| `Zone N Outlet M` | binary sensor | Read-only outlet state as the controller sees it |
| `Steam` | switch | Starts or stops the steam generator at **the controller's own default temperature and time**, as the app's *Steam start* card does. Refused while the controller is running the shower — the app won't run both at once. Only on controllers with steam attached. New in this release and not yet tried on hardware |
| `Experience` | select | The controller's experiences — shower, steam and ice-shower programs such as Breathe, Focus or Detox — started and shown running. They run from zone 1's first outlet, as the app notes |
| `Music` / `Light` / `Steam` | binary sensor | Accessory state. `Light` carries each light group's own state; `Steam` carries temperature, timers and `power_clean` (the generator's self-clean) |
| `Problem` | binary sensor | A fault the controller reports, an active error (its title and code are attributes), or an accessory that is set up but has stopped responding — "Steam is disconnected", an SD card missing — the cases the app shows as error cards |
| `Max Shower Duration` | sensor | The controller's own limit on a shower, in minutes. The shorter of this and the valve's `Max Shower Duration` ends a shower — see [Shower time limits](#shower-time-limits) |
| `Firmware` | update | Installed and latest controller firmware, checked twice a day. Read-only |

The controller's device page also has a **Visit** link to its own web settings page — where its
Max Shower Duration and other settings are changed — using the network address Kohler's
cloud reports for it. New in this release and not yet tried on a real controller: if the
address isn't reported, there's simply no link. Signing in to that page turns the valve's
warmup off; `Warmup Auto-Restore`, if it's on, puts it back.

<details>
<summary><b>Diagnostic entities</b> — mostly disabled by default, for protocol work rather than daily use</summary>

<br>

| Entity | Device | What it does |
|---|---|---|
| `MQTT Connection` | both | Whether the push stream is connected — **our** link to Kohler |
| `Cloud Connection` | valve | Whether **Kohler's cloud** can reach the valve. A different question, and the one behind "the app says my valve is offline" — see [When the valve drops off the cloud](#when-the-valve-drops-off-the-cloud) |
| `Last Update` | both | Timestamp of the most recent message |
| `Start new MQTT capture` | both | Button; rolls the raw capture over to a fresh file |
| `Report Log` | both | Switch; one-file bug-report capture of the raw MQTT stream — see [Diagnostics](#diagnostics) |
| `Hex` | valve | The current command word for that zone — copy it into `send_valve_hex`. `Zone N Hex` on a two-zone valve |
| `Water Used Today` | valve | Water used since local midnight, from Kohler's own per-day usage series — the same data behind the app's chart. Refreshed about 90 seconds after a shower ends, not on a clock |
| `Water Used This Week` | valve | The last seven days including today, as a rolling window — **not a calendar week**. Summed from seven daily buckets; `per_day` carries the breakdown |
| `Shower Active N` | valve | Whether that zone is currently running water. **Multi-zone valves only** — with one zone `System Status` answers the same question and more, so no such entity is created |
| `Preset Active` | valve | Whether a stored preset is driving the valve |
| `Interface Firmware` | valve | The touchscreen's own version — what the Konnect app calls the interface firmware |
| `Valve Firmware` | valve | The valve's own version |
| `Second Valve Firmware` | valve | The second valve's version, on two-valve systems only |
| `Gateway Firmware` | valve | The gateway's version. Per-account rather than per-valve, but published on each valve's device |
| `Registered` | valve | When Kohler's cloud created this device's record — a registration date, not an installation date |
| `Warmup Auto-Restore` | valve | Switch; puts warmup back when something silently disables it. **Only on accounts that also have an Anthem Plus controller.** Off by default. See [Warmup auto-restore](#warmup-auto-restore) |

</details>

### Each device has its own defaults and its own favorites

On a system with both products this catches people out: the valve and the controller are not
two views of one set of settings. Each stores its own, and they can differ.

**Default outlets.** Press the dial on the first-generation touchscreen and the **valve's**
default outlets open. Press the dial on the Anthem+ screen and the **controller's** defaults
open. These are separate settings, so the same gesture on two screens in the same room can
start two different showers. The `Shower on` switch on the valve, and `Shower` on
the controller, each do exactly what that device's dial does.

**Favorites.** Both lists can be built either in the Konnect app or at the matching
touchscreen, but they are stored in different places — valve favorites live on the valve,
controller favorites live on the controller. So the two screens show **different lists**,
and so do the two `Favorite` dropdowns in Home Assistant. Only the controller's can carry
lighting, music or steam, because only the controller knows those exist.

## Using both together

### What is the combined system?

**First generation — Kohler Digital Anthem.** A digital valve plus a touchscreen interface.
The valve is the whole product: it has the Wi-Fi, it talks to Kohler's cloud, and the Konnect
app drives it from there.

> ⚠️ **The first-generation touchscreen is not a smart device.** It is an HID with a screen —
> **no Wi-Fi, no Bluetooth, no radio of any kind.** This is the single most common
> misconception about the system. The part with Wi-Fi is the valve.
>
> The useful consequence: **once set up, the touchscreen is optional.** With the valve on
> Wi-Fi and showing in Konnect, you can run a shower entirely from your phone. It is *not*
> optional for getting there — see [Common questions](#common-questions).

**Second generation — Anthem+.** Kohler added things the first-generation touchscreen had no
room for:

- **Coordinating two valve bodies** as one system — a product capability; *this integration
  is built for a single valve*, see [Known limitations](#known-limitations)
- **Zigbee hub for lighting** — the radio is there and Kohler lists Sengled Element bulbs as
  supported, but no bulb has been paired successfully with this integration; see
  [Known limitations](#known-limitations)
- **Music**, which needs a separate **Kohler amplifier, K-30319** — it is not built into the
  controller
- **Steam**, via Kohler's steam generator

That is more than a touchscreen can host, so Anthem+ is really **a second-generation
touchscreen attached to a Linux system controller** — a separate box that every accessory
plugs into. The controller then plugs into *the same first-generation digital valve*, and
gets its own page in the Konnect app.

**The combined system.** Because the valve is unchanged between generations and the Anthem+
controller is essentially a smarter interface for it, both can be attached at once — the
valve has two ports:

```
Kohler Konnect cloud
│
├─ Digital valve ...................... Wi-Fi   ← the only part with a radio
│  ├─ port 1 → 1st-gen touchscreen ............. HID only: no Wi-Fi, no Bluetooth
│  └─ port 2 → Anthem+ system controller ──┐
│                                          │  same box, two roles
└─ Anthem+ system controller .......... Wi-Fi ──┘  (its own Konnect page)
   ├─ Anthem+ touchscreen
   ├─ amplifier (K-30319) ............. music, sold separately
   ├─ Zigbee radio .................... lighting
   └─ steam generator
```

The system controller appears twice on purpose: it is a **client of the valve** on port 2,
and a **cloud device in its own right** over Wi-Fi — the whole explanation for why two things
can drive one shower, and why this integration presents two Home Assistant devices for what
is almost always **one physical shower reached through two touchscreens.** They stay separate
devices anyway: they behave differently and their state arrives on different schedules, so
merging them would imply a consistency that does not exist.

### ⚠️ Five entity names exist on both devices

The most common mistake in a combined setup is grabbing the wrong one. These names appear
twice, once per device:

| Name | On the valve | On the controller |
|---|---|---|
| `Shower on` / `Shower` | switch — opens **the valve's own default outlets** | switch — opens **the controller's own default outlets**, a separate setting |
| `Favorite` | select — presets **stored on the valve** | select — favorites **stored on the controller**; a different list |
| `System Status` | sensor — `Water Running` / `Paused` / `Warming Up` / `Idle` | sensor — `Water Running` / `Warming Up` / `Idle` |
| `Zone N Temperature` | **number** — the setpoint, writable | **sensor** — read-only |
| `Zone N Outlet M` | **switch** — writable | **binary sensor** — read-only |

The device prefix is what separates them — `switch.anthem_valve_shower` against
`switch.anthem_plus_shower`. Note the last two rows differ in *type*, not just in device: if
an entity you expected to set turns out to be read-only, you have the controller's copy.

> The valve's `Favorite` and the controller's `Favorite` are **different lists**. Valve
> presets are stored on the valve; controller favorites are scenes configured in the Konnect
> app, and only the latter can carry lighting, music or steam.

### Which device to reach for

| You want | Use | Why |
|---|---|---|
| A specific outlet, temperature or flow | **valve** | It takes a raw command word — any outlet, any temperature, any time |
| A whole scene, with lights/music/steam | **controller** | It activates named favorites only; whole scenes or nothing |
| To know whether water is actually running | **valve** | `System Status`, `Shower Active` and `At Temperature` are true whenever water is running, regardless of how the shower was started |
| To act only on a controller-driven shower, or on `Music` / `Light` / `Steam` | **controller** | No valve equivalent exists for these |

A useful rule for automations: if it would still make sense with the controller unplugged, it
belongs on the valve. You cannot ask the controller for "outlet 2 at 39 °C", and the valve
knows nothing about music or lighting — that split is the whole reason both devices exist.

### Common questions

**Will running both break my shower?** No. The valve treats each port as a source of
commands; nothing about having two attached puts it in a state it does not already handle.
Kohler support will tell you not to combine them — the reasons appear to be practical
(support burden) rather than technical, but treat the combination as unsupported: it works,
and it is what this integration was built against, but firmware updates carry no guarantee.

**Can I use both touchscreens at the same time?** Yes. Each screen talks to whatever it is
plugged into and is drawn by that same thing (1st-gen screen ↔ valve; Anthem+ screen ↔
system controller ↔ valve). A touch becomes a command; the state that comes back tells the
screen what to draw, effectively instantly. The controller's screen holds a picture the valve
cannot — lighting, music, steam, a second valve body — but for water the two agree, because
the controller's own view of the valve comes from the valve. What the controller can't do is
see round the valve: if the valve is driven from the first-generation screen, the controller
finds out the same way this integration does, so it can legitimately show `Idle` while the
valve runs water, and both are telling the truth about their own question.

**Do I need a first-generation touchscreen too?** Only to get the valve onto Wi-Fi in the
first place. **The valve only raises its Wi-Fi access point — the one you join to hand it
your network and register it in Konnect — when told to from the first-generation
touchscreen.** There is no app-side, button, or web route to it, and plugging an Anthem+
touchscreen straight into the valve does nothing (it is not a first-generation screen and the
valve does not answer it). So a first-generation screen is required *at least once*; after
that it can be unplugged and the valve stays connected. If you're upgrading from first
generation, keep one of your existing screens rather than discarding it — that's what turns
the install into the combined system.

## Services

### `kohler_anthem.custom_shower`

Starts the shower with the outlets and temperature you choose, sent to the valve as **one
command**. This is the form to use whenever an automation opens an outlet *and* sets a
temperature: the valve cannot take two commands back to back (see
[Known limitations](#known-limitations)), and this sends one.

In the automation editor, add an action, search for **Custom shower**, turn on the outlets,
set the temperature, and optionally turn on **No pausing warm-up (beta)**. The same
thing in YAML:

```yaml
action: kohler_anthem.custom_shower
data:
  device_id: 1a2b3c…          # only with more than one valve: the valve device's id
  zone1_temperature: 108      # in your account's unit; 59–118 °F
  zone1_outlet_1: true        # outlets you leave out are closed
  keep_on_after_warmup: true  # optional and beta, see below
```

Every field the action takes, with what each one does when you leave it out:

```yaml
action: kohler_anthem.custom_shower
data:
  # Zone 1 — the only required field is the temperature
  zone1_temperature: 108      # required; your account's unit, 59–118 °F or 15–48 °C
  zone1_outlet_1: true        # default false — an outlet you leave out is closed
  zone1_outlet_2: false
  zone1_outlet_3: false

  # Zone 2 — omit the whole zone on a single-zone valve
  zone2_temperature: 104      # default: follows zone1_temperature
  zone2_outlet_1: false
  zone2_outlet_2: false
  zone2_outlet_3: false

  keep_on_after_warmup: true  # default false; beta, see below
  flow: 100                   # default 100; 8–100 % of full flow
```

Every key is flat — the **Zone 1**, **Zone 2** and **Advanced** headings you see in the editor
are display only and never appear in the YAML. Outlets your valve does not have are hidden from
the form, and turning one on in hand-written YAML is an error rather than a silent no-op.

What it does:

* **It states the whole shower.** Every outlet you leave off is closed, on both zones.
  Leaving every outlet off stops the shower. Nothing is taken from what the valve last
  reported, which is exactly what makes it safe to fire from a button.
* **Temperature is per zone**, like the valve's own setpoints and the temperature entities.
  Zone 2's is optional: left unset, it follows zone 1's.
* **It fires once.** A second command during the valve's warm-up hijacks the warm-up onto the
  outlets you wrote, so this action never sends a second command on its own.
* **Flow** is under *Advanced* and defaults to full flow, like every other command from Home
  Assistant. The flow you set holds only until someone presses the flow button on the
  touchscreen, which takes over from then on.
* **No pausing warm-up (beta).** Keeps the shower on after the valve's warm-up is finished.
  Only matters when the valve's warm-up is enabled. On such a valve every outlet command first
  runs the warm-up, and when the water is warm the valve **pauses** for two minutes, just as it
  always does — left alone, that pause ends the session. With this on, the integration watches
  the valve's own reports and, when that pause arrives, sends your outlets and temperature
  again, once. It does nothing if no warm-up follows (warm-up disabled, or the water was
  already warm), if the warm-up ends in a stop rather than a pause, or if someone takes over at
  the wall. **Beta:** tested on one valve so far — please report how it behaves on yours.

The response carries the two command words it sent, so the action doubles as a way to learn
the word for `send_valve_hex` below.

On a single-zone valve the Zone 2 outlets are hidden from the form automatically, as are
outlets a zone does not have.

> ⚠️ **This can start water.** It writes directly to the valve.

### `kohler_anthem.send_valve_hex`

Sends a command word straight to the valve, for anything the normal controls cannot do. If
what you want is outlets plus a temperature, use `custom_shower` above; this one is for the
rest.

The workflow is copy-and-paste rather than hand-assembly. Set the shower up the way you want
it using the outlet switches and temperature controls, read the resulting code off the
`Hex` diagnostic sensor (`Zone N Hex` on a two-zone valve), and store that string. Sending it later reproduces that exact
state.

```yaml
action: kohler_anthem.send_valve_hex
data:
  device_id: 1a2b3c…         # only with more than one valve: the valve device's id
  zone1_hex: "0184C801"      # zone 1, 38.8 °C, flow 0xC8 (200) = 100 %, outlet 1
  zone2_hex: "1184C801"      # optional; omitted entirely on a single-zone valve
```

Both fields accept 8 or 16 hex characters. On a single-zone valve the Zone 2 field is hidden
from the UI form automatically. See [`gcs/valve_hex.md`](gcs/valve_hex.md) for the full byte
layout.

> ⚠️ **This can start water.** It writes directly to the valve with none of the guards the
> switches apply.

The temperature range is the zone slider's without its **Cold** step: typing `0` is refused
rather than giving you a cold shower. Use the slider's bottom step, or `send_valve_hex`, for
full cold.

> **`probe_usage` has been removed.** It existed to discover how Kohler's usage-history
> endpoint wanted to be called; that is now known, and the usage sensors already use it.

## Features in detail

### Favorites and presets

The valve stores presets; the controller stores named favorites. Both are exposed as
`select` entities, and both are configured in the Konnect app rather than here — this
integration activates them, it doesn't create them.

**Experiences** are Kohler's built-in programs — on the valve, ones you've added in the app
(Wake Up, Cool Down, the ice-shower routines); on the controller, its fixed catalogue of
shower, steam and ice-shower programs. Each device has its own `Experience` dropdown. The
valve's starts with the same command the Konnect app sends, but earlier versions of this
integration believed the valve ignored it, and nobody has yet confirmed it on hardware — if
choosing one does nothing, the dropdown falls back to `Off` after a few seconds. Please open an
issue either way.

### Warmup

Kohler's pre-heat feature: run water until it reaches temperature, so the shower is ready
when you step in. The dropdown offers the three modes the app can actually write —
**Off**, **All outlets**, **Selected outlets**.

> **"All outlets" skips a tub filler** (measured): a warm-up that ran the tub filler would
> fill the tub, so the valve leaves that outlet closed and warms through the others.

The protocol defines two additional delayed-start modes. Those remain decodable because a
valve can be holding one, but nothing defines their delay and they are refused as write
targets. If your valve is holding one, it appears in the dropdown while it is in force and
disappears once you change it.

> Which outlets "Selected outlets" refers to is **not readable from the cloud API.** That
> lives in per-zone configuration on the controller's local API.

### Warmup auto-restore

Off by default. Turn it on if your warmup mode keeps turning itself off.

⚠️ **This is a fix for a hub problem.** If your account has no Anthem Plus controller — valves
only — the known cause below cannot occur, and this switch defends against nothing. Leave it
off unless you actually observe the mode reverting.

On a hub-attached installation, warmup can revert to **Off** with no visible cause at the
valve. The identified cause: ordinary signed-in use of the Anthem Plus controller's local web
UI silently writes the valve's warmup mode to disabled — a PIN sign-in alone is enough — as a
fixed part of its login routine, from the *hub's* firmware. It cannot be prevented from
outside that firmware, so this switch is the mitigation: it sets the mode back sixty seconds
after a disable this integration did not cause, and writes a journal entry with a window of
MQTT traffic either side.

It is deliberately cautious: it will not undo a disable this integration performed itself, it
will not fire on a restatement after a reboot, it stops after five restores that fail to
stick, it never writes while water is running, and it does nothing unless it has previously
seen the mode enabled.

### Shower time limits

The valve turns the water off once a zone has run for its **Max Shower Duration** — 15, 20,
25, 30, 45 or 60 minutes, set with the valve's `Max Shower Duration` dropdown or in the
Konnect app. The timer runs per zone and keeps counting when you switch between outlets in
that zone.

Two other limits can end a shower sooner:

* **An Anthem Plus controller** has its own Max Shower Duration, set on its web settings page
  and shown by the controller's `Max Shower Duration` sensor. The shorter of the two wins.
* **A favorite** stored on the valve carries its own run time. When it is shorter, it ends a
  shower started from that favorite.

While water runs, `System Status` (and `Shower Active` on a two-zone valve) shows
`flowing_for_seconds` and `seconds_remaining` until the valve's limit.

### Captures and journals

Separately from the Report Log (see [Diagnostics](#diagnostics)), the integration writes its
own development-style evidence to `/config/kohler_anthem_raw/`:

| File | What it holds |
|---|---|
| `raw_mqtt_*.jsonl` | Every MQTT message, as received |
| `warmup_*.jsonl` | Warmup mode changes, with traffic windows either side |

Each is capped at 8 MB per file and rolls over rather than pruning. The `Start new MQTT
capture` button opens a fresh file, which is useful before a deliberate experiment. If you
report a problem, these are the files that make it diagnosable.

## Requirements

* Home Assistant **2026.3** or later
* A Kohler Konnect account, with the shower already set up in the Konnect app
* **Internet access.** Control is cloud-only for both products. If Kohler's cloud is
  unreachable, nothing here can turn the shower on or off.

The only Python dependency is `paho-mqtt`, installed automatically.

## Install

This integration is **not in HACS's default store.** Add it as a custom repository.

### Via HACS

1. In HACS, open the ⋮ menu and choose **Custom repositories**
2. Add `https://github.com/kedube/ha-kohler-anthem`, category **Integration**
3. Find **Kohler Anthem** in HACS and install it
4. Restart Home Assistant

> HACS installs from **releases**, not from the latest commit.

### Manually

Copy the `custom_components/kohler_anthem/` folder from this repository into the
`custom_components/` folder of your Home Assistant configuration directory, so that
`config/custom_components/kohler_anthem/manifest.json` exists, and restart Home
Assistant.

## Setup

**Settings → Devices & Services → Add Integration → Kohler Anthem**

1. **Sign in** with your Konnect username and password. Credentials are exchanged for a token;
   the password is not stored.
2. **Confirm your valve model.** The integration detects the outlet split from your account
   and pre-selects the matching model, so this is usually one click.

Temperature and water units are read from your Konnect account, not chosen here — set them in
the Konnect app and they follow.

There is no Configure dialog — every setting that can change after setup is an entity on the
device page (`Warmup`, `Max Shower Duration` and — where an Anthem Plus controller is
present — `Warmup Auto-Restore`), where automations and dashboards can reach it too.

### Diagnostics

Every device page and the integration card have a **Download diagnostics** button, producing
one redacted JSON report of the whole installation. For raw MQTT/REST traffic — the **Report
Log** switch and the two debug loggers (`anthem.client`, `anthem.raw_log`) — see
[`mqtt/capture_runbook.md`](mqtt/capture_runbook.md) for the full walkthrough.

In short: turn on the Report Log switch on a device page to capture a bug-report-sized file
under `custom_components/kohler_anthem/reports/`, or use `logger.set_level` in Developer Tools
to turn on one of the two debug loggers for a live, verbose view in Home Assistant's own log.
Both redact credentials but not device ids — skim a file before attaching it to an issue.

## Automation examples

**Notify when the shower is up to temperature**

```yaml
automation:
  - alias: Shower ready
    triggers:
      - trigger: state
        entity_id: binary_sensor.anthem_valve_at_temperature
        to: "on"
    actions:
      - action: notify.mobile_app
        data:
          message: Shower is at temperature.
```

**Start a favorite from anywhere**

```yaml
automation:
  - alias: Morning shower
    triggers:
      - trigger: time
        at: "06:30:00"
    actions:
      - action: select.select_option
        target:
          entity_id: select.anthem_plus_favorite
        data:
          option: Morning
```

**Open an outlet and set its temperature, from one button**

```yaml
automation:
  - alias: Rain head at 108
    triggers:
      - trigger: state
        entity_id: input_button.rain_head
    actions:
      - action: kohler_anthem.custom_shower
        data:
          zone1_temperature: 108
          zone1_outlet_1: true
          keep_on_after_warmup: true   # beta; only matters with the valve's warm-up on
```

One action, one command to the valve. In the UI editor this is the **Custom shower** action
with zone 1 outlet 1 on and the zone 1 temperature set; no YAML needed. Use `custom_shower`
rather than an outlet switch followed by a temperature change — see
[Known limitations](#known-limitations) for why two valve commands back to back can undo each
other ([issue #1](https://github.com/kedube/ha-kohler-anthem/issues/1)).

The raw equivalent is `send_valve_hex` with a hand-built word:

```yaml
      - action: kohler_anthem.send_valve_hex
        data:
          zone1_hex: "01A6C801"   # 0x1A6 (422) = 42.2 °C = 108 °F, flow 100%, outlet 1
```

Zone 2 is re-sent as it stands when `zone2_hex` is omitted. Build the word from the *Hex*
diagnostic sensor: change the last byte for outlets (`01`, `02`, `04`, and sums), and the
temperature per the [hex reference](gcs/valve_hex.md) — or read it off `custom_shower`'s
response.

**Pre-heat when you leave work**

```yaml
automation:
  - alias: Warm the shower on the way home
    triggers:
      - trigger: zone
        entity_id: person.me
        zone: zone.work
        event: leave
    actions:
      - action: select.select_option
        target:
          entity_id: select.anthem_valve_warmup
        data:
          option: All outlets
```

**Alert if the push stream drops** — this is about *your* connection to Kohler.

```yaml
automation:
  - alias: Anthem push stream lost
    triggers:
      - trigger: state
        entity_id: binary_sensor.anthem_valve_mqtt_connection
        to: "off"
        for: "00:05:00"
    actions:
      - action: persistent_notification.create
        data:
          message: Lost the push connection to the Anthem valve.
```

**Alert if the valve drops off Kohler's cloud** — a different fault, and the one that needs
you to go and unplug something.

```yaml
automation:
  - alias: Anthem valve offline
    triggers:
      - trigger: state
        entity_id: binary_sensor.anthem_valve_cloud_connection
        to: "off"
    actions:
      - action: persistent_notification.create
        data:
          message: >-
            Kohler cannot reach the Anthem valve. The shower still works at the wall.
            Power-cycle the valve to bring it back.
```

⚠️ **Do not trigger on `unknown`.** That state means the check has not run yet — usually just
after a restart — not that anything is wrong.

## Troubleshooting

**Entities are unavailable, or state is stale.** Check the `MQTT Connection` diagnostic binary
sensor on either device. Control is cloud-only, so an internet outage or a Kohler-side problem
takes everything with it. The integration reconnects and reseeds from REST on its own.

### When the valve drops off the cloud

**The Konnect app says the valve is offline, but Home Assistant looks fine.** These are
different faults and it is worth knowing which you have. `MQTT Connection` reports *our* link
to Kohler; the valve can vanish from Kohler's side while that stays green, and then every valve
entity simply freezes at its last value with nothing to say so.

`Cloud Connection` on the Anthem Valve device is the answer to that. It reports what Kohler's
cloud believes about the valve: **on**, **off**, or **unknown** (not yet checked — a failed
check leaves the previous answer alone rather than inventing an outage; look at the
`last_error` attribute). It is enabled and visible by default — an installation from before
0.11.2 may still have it hidden, so show hidden entities on the Anthem Valve device if you
don't see it.

**It is not polled.** The integration asks Kohler only when there is a reason to:

* the controller reports a zone running while the valve says nothing for a minute — the
  signature of a valve that is working at the wall but gone from the cloud; or
* the valve has said nothing at all for three hours.

At most one check every thirty minutes either way. The `checked_because` and `last_checked`
attributes say which reason fired and when.

**If it says off:** the valve is still working from its own touchscreen — this is a cloud
problem, not a plumbing one. Nothing in Home Assistant can bring it back; the valve needs to be
power-cycled at the outlet. It returns on its own once that happens, and the integration picks
it up from the valve's `DEVICE_REBOOT_STS` announcement without any further action.

**Home Assistant keeps asking me to sign in again.** Kohler's identity provider rotates the
refresh token on every use. If another tool is refreshing the same grant, it invalidates the
copy Home Assistant holds. Give this integration its own sign-in.

**The Warmup dropdown has an option I didn't expect.** Your valve is holding one of the two
legacy delayed-start modes. It's shown so Home Assistant can display the true state, and it
disappears once you select something else. You can't select it.

**The shower stops after about fifteen minutes.** That's the configured maximum run time, and
it's working as designed. Raise the valve's `Max Shower Duration` (up to 60 minutes). With an
Anthem Plus controller, raise the controller's own Max Shower Duration too — the shorter one
wins. See [Shower time limits](#shower-time-limits).

**Zone 2 entities are missing.** Expected on a single-zone valve — K-28209 and K-28210 have
one zone.

**Temperature is in the wrong unit.** It follows your Konnect account. Change it in the
Konnect app.

## Known limitations

* **Cloud-only.** No local control path exists for either product. The controller's local API
  can read configuration but cannot actuate anything.
* **Flow can be overwritten on some hardware.** Each zone has a Flow number. But a
  first-generation Anthem touchscreen has been captured rewriting *both* zones' flow the
  moment its flow panel is opened — before any adjustment is made — applying its own linked
  scaling and a calibration-derived ceiling. On such an install a setpoint written from Home
  Assistant can change on its own; disable the entity if yours behaves that way. The protocol
  layer is unaffected either way.
* **Music and lighting are read-only; steam runs only its defaults.** The controller exposes
  music and lighting as state, and steam's only direct command runs the controller's default
  program; anything else means activating a favorite that includes it. The Konnect app has
  no other command for any of them — this is the limit of what **Konnect** exposes, not what
  the hardware can do.
* **New controls are app-confirmed, not hardware-confirmed.** The `Steam` switch, both
  `Experience` dropdowns and the valve's `Restart` button send exactly what the Konnect app
  sends, but none has been run against hardware by this integration yet.
* **Which controller fronts which valve is not knowable.** Nothing on the cloud side says
  it, so on an account with more than one valve or more than one controller the valve's
  `Status` no longer folds in a controller-initiated warm-up — it reads the valve alone and
  reports `controller_warmup: null`. With exactly one of each they are assumed to be the
  same shower, as before.
* **One valve body.** Anthem+ can coordinate two; this integration was written against a
  single-valve system and does not handle a second. Open an issue if you have a two-valve
  system.
* **Lighting is unproven.** Anthem+ has a Zigbee radio and Kohler lists Sengled Element bulbs
  as supported, but no Sengled bulb has been tried against this integration; the one attempt
  used a Philips Hue bulb and it did not pair.
* **Warmup's selected outlets can't be read** from the cloud API.
* **One valve command at a time.** The valve reports a command back about a second after
  accepting it, and the next command is built from that report. Two valve commands inside that
  second undo each other — the second one closes what the first opened. With the valve's warm-up
  off, a `delay:` of about 3 s between valve actions (outlet switches, temperature, the Shower
  switch, favorites) is enough. With warm-up on, no delay makes a two-step automation work — a
  command during the warm-up hijacks it onto the outlets you wrote, and one after the warm-up's
  pause ends the session — so send everything in one command: the `custom_shower` action, or a
  single `send_valve_hex` word. The failure mode is water off, never water on.
* **One installation tested.** See below.
* **The API is undocumented** and Kohler can change it without notice.

## Tested against

**One installation.** A single K-28212 — 6 outlets, 3 + 3 across two zones — plus an Anthem
Plus controller on firmware 2.88. Every finding in this guide is derived from and verified
against that one system.

Other models and configurations are supported on the basis of what the protocol says, not on
the basis of anyone having run them. If you have different hardware, reports are genuinely
useful — particularly from a single-zone valve, or from a valve without a controller in front
of it.

## Documentation

| Document | What's in it |
|---|---|
| [`gcs/valve_hex.md`](gcs/valve_hex.md) | The valve command word, byte by byte — for `send_valve_hex` |
| [`mqtt/capture_runbook.md`](mqtt/capture_runbook.md) | Capturing and reading diagnostics |
| [`protocol/`](protocol/README.md) | **For developers.** Kohler Konnect's cloud protocol — sign-in, every endpoint and message this integration uses, status codes, firmware, and what the Konnect app does that this integration doesn't. Written to be reused by other Kohler integrations |

Issue reports from **different hardware** are the most useful thing anyone can contribute — a
single-zone valve, a four-outlet valve, or a valve with no controller in front of it would
each test paths that have never run outside their own source code. If you're reporting a
problem, [Download diagnostics](#diagnostics) plus the Report Log are what make it
diagnosable — check them for anything you'd rather not share before attaching them.

## Licence and trademarks

MIT — see [LICENSE](../LICENSE).

Kohler, Anthem, Anthem+ and Konnect are trademarks of Kohler Co. This project is not
affiliated with, authorised by, or endorsed by Kohler Co., and is not a supported product.
