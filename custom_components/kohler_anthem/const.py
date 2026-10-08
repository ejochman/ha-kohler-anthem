"""Home Assistant constants for the Kohler Anthem integration.

Protocol constants live in ``anthem/const.py``. This module holds only what Home
Assistant itself needs: the domain, config-entry keys, and tuning.
"""

from __future__ import annotations

DOMAIN = "kohler_anthem"

# Device display names. These also decide entity_id prefixes, because Home Assistant builds
# entity ids from the device name plus the entity name — so "Anthem Valve" + "Outlet 1"
# yields `binary_sensor.anthem_valve_outlet_1`.
#
# A second Anthem Plus controller on the account cannot also be called "Anthem Plus" — two
# devices with one name would hand the second one `_2` entity ids — so with several, each is
# suffixed with its Konnect name. See `coordinator.controller_names`.
DEVICE_NAME_VALVE = "Anthem Valve"
DEVICE_NAME_CONTROLLER = "Anthem Plus"

# ---------------------------------------------------------------------------
# Config-entry keys
# ---------------------------------------------------------------------------
# username comes from homeassistant.const.CONF_USERNAME.
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TENANT_ID = "tenant_id"
CONF_VALVE_MODEL = "valve_model"
# The detected outlet split, e.g. [3, 3]. Stored alongside the SKU so an install that does
# not match a catalogue model still works, and so a later SKU rename cannot change topology.
CONF_ZONE_OUTLETS = "zone_outlets"
CONF_TEMPERATURE_UNIT = "temperature_unit"
CONF_WATER_UNITS = "water_units"
# The Azure IoT Hub identity this install registers as, generated once and then reused.
#
# Without this, every connect registered a fresh `uuid4()` identity — so each restart and
# each reconnect left another dead "phone" on the Kohler account. Reusing one identity also
# means any first-registration delay is paid once, ever, rather than on every connect.
#
# Stored per config entry, never global: two Home Assistant instances on one account must
# not share an identity or they would fight over the same MQTT client id.
CONF_MOBILE_DEVICE_ID = "mobile_device_id"
# Per-valve settings, keyed by the valve's device id, in both `entry.data` and
# `entry.options` — an account can carry several valves since 2026-09-08, and each has
# its own Warmup Auto-Restore and remembered warm-up mode (options). The flat keys those
# used to be are migrated under here once, at setup, by
# `KohlerAnthemCoordinator._migrate_valve_settings`.
# Reload-ignored in both lists for the same reason every one of those keys was.
CONF_VALVES = "valves"

# How multi-zone valve outlets, controls, and sensors are grouped and named. Stored in
# `entry.options[CONF_ZONE_GROUPING]` via the Configure dialog and deliberately NOT in
# `RELOAD_IGNORED_OPTION_KEYS` — changing it reloads the entry so entities and zone
# sub-devices re-register with the chosen layout.
CONF_ZONE_GROUPING = "zone_grouping"
ZONE_GROUPING_NUMBERED = "numbered"
ZONE_GROUPING_SUBDEVICES = "subdevices"
ZONE_GROUPING_OUTLET_LABELS = "outlet_labels"
ZONE_GROUPING_MODES: tuple[str, ...] = (
    ZONE_GROUPING_NUMBERED,
    ZONE_GROUPING_SUBDEVICES,
    ZONE_GROUPING_OUTLET_LABELS,
)
DEFAULT_ZONE_GROUPING = ZONE_GROUPING_NUMBERED

# ---------------------------------------------------------------------------
# Polling — deliberately none
# ---------------------------------------------------------------------------
# `None` disables the coordinator's interval entirely. This integration is push-driven:
# every state change arrives over MQTT, and REST is read on two **events** — setup, and each
# MQTT (re)connect — never on a clock.
#
# The reads cannot be dropped altogether, because **the broker replays nothing on connect**.
# Measured across 27 capture sessions: the first message after connecting is always a change
# event, never a state dump. Six sessions received nothing at all, and the longest silence
# was 11.9 hours. Without a read at connect, every restart would leave entities `unknown`
# until somebody next used the shower.
#
# `homeassistant.update_entity` still forces a refresh on demand — that is the manual path,
# and the only one.
SCAN_INTERVAL = None

# How long to wait after a shower stops before re-reading the daily usage series.
#
# Kohler aggregates a session server-side *after* the valve reports it closed, so reading
# the moment the water stops returns the day's total without the shower that just finished —
# which is precisely the reading someone would go and check. Ninety seconds is comfortably
# past that and still fast enough to be there when they look.
#
# This is the only delayed read in the integration and it is not polling: it fires on the
# running -> stopped edge, so a day with no shower costs no calls at all.
USAGE_REFRESH_DELAY_SECONDS = 90

# How long to wait after writing an outlet configuration before reading it back.
#
# ⚠️ **An immediate read-back lies.** `gcsadvancestate` is a cloud document that only updates
# once the device reports, so a read about a second after a 201 still shows the OLD value —
# measured in the live write sweep of 2026-08-21, where the change appeared within 25 s.
# Verifying too early is precisely how a working write looks like a device-side limit, which
# is a mistake this project has already made about this API more than once.
#
# Thirty seconds: past the observed 25, and still inside what someone will wait for a
# confirmation after changing a setting.
OUTLET_WRITE_VERIFY_DELAY_SECONDS = 30

# ---------------------------------------------------------------------------
# Shower switch
# ---------------------------------------------------------------------------
# Turning `switch.anthem_valve_shower` on activates this preset. The valve has no "run my
# default" command, so a whole-shower start has to name a stored preset — see ShowerSwitch.
#
# Preset ids are positional and the app hides preset 1 from its own list, so the id shown in
# the app is not this id. Adding a preset appends (a new one became id 3, leaving 1 and 2
# alone), but a deletion is expected to renumber, exactly as HUB favorites do. If a start
# ever runs the wrong scene, re-read the preset list before assuming the valve misbehaved.
SHOWER_ON_PRESET_ID = 1

# Presets never offered to the user as a choosable scene.
#
# Preset 1 is the valve's mandatory default-shower configuration and the Konnect app hides it
# from its own list, so surfacing it would show something the app does not. It stays
# reachable — it is exactly what `SHOWER_ON_PRESET_ID` above activates — but it is the
# shower switch's business, not an entry in a preset picker.
PRESET_HIDDEN_IDS: frozenset[int] = frozenset({1})

# ---------------------------------------------------------------------------
# The controller's own water state, published alongside the valve's
# ---------------------------------------------------------------------------
# **No longer temporary — do NOT set this back to False.** It went in as a debugging aid
# and became load-bearing on 2026-08-18.
#
# Normally nothing derived from `SHOWER_VALVE_STS` is published on an account that also has
# a valve: the controller does not observe a valve-driven session and reports `status: OFF`
# with an all-zero outlet array while water is running, so it would contradict the valve's
# own entities on the same dashboard.
#
# That contradiction turned out to be the information, not the noise. The two sources
# answer different questions — the valve "is water running", the controller "does this
# controller know about it" — and the second decides whether the controller's `stopall`,
# `valvecontrol OFF`, and 60-minute session ceiling apply at all. So the `ControllerOutlet`
# sensors are now the Anthem Plus device's reference for water, and
# `Controller.water_is_running` — which backs both controller switches — is defined to
# agree with them exactly.
#
# Turning this off would delete the rows the owner reads the controller's view from. The
# switches keep working (they read the controller's state, not the entities), but the
# evidence behind them becomes invisible.
EXPOSE_CONTROLLER_WATER_STATE = True

# ---------------------------------------------------------------------------
# RAW MQTT LOG — diagnostic capture of every payload, before decoding
# ---------------------------------------------------------------------------
# Full explanation, file format, and the runtime switch: `anthem/raw_log.py`.
# Find every piece of this feature with:
#
#     grep -rn "RAW MQTT LOG" custom_components/kohler_anthem/
#
# Prefer the runtime switch over this constant — it needs no restart and no file edit.
# Developer Tools -> Actions -> `logger.set_level`:
#
#     custom_components.kohler_anthem.anthem.raw_log: debug
#
# This constant pins capture on across restarts instead.
#
# **Off, and it must ship off.** It was switched on 2026-08-13 for a stretch of work
# involving frequent restarts, where a UI toggle that resets on every restart was useless —
# and then shipped that way through 0.6.7, which was a mistake: this constant *overrides*
# the `logger.set_level` switch (see `raw_log.py`), so a released build with it True
# captures every MQTT payload with no supported way for the owner to stop it short of
# editing the integration's source.
#
# An earlier version of this comment claimed the capture was "bounded (8 MB x 6 files)".
# It is not — `RAW_MQTT_LOG_KEEP_FILES` below is None, so nothing is ever pruned and growth
# is unbounded in file count. On a Raspberry Pi's SD card that is a real cost.
#
# Turn it on for a debugging session by setting this True locally; do not commit it.
ENABLE_RAW_MQTT_LOG = False

# Written under the Home Assistant config directory, so it is reachable from the File editor
# and Samba add-ons rather than buried in the container.
RAW_MQTT_LOG_DIR = "kohler_anthem_raw"
RAW_MQTT_LOG_MAX_BYTES = 8 * 1024 * 1024
# None = no limit on the number of files; every capture is kept forever. Set at the user's
# request on 2026-08-14 — this directory is meant to be a permanent record, not a rotating
# buffer, and deleting old captures automatically risks losing the ones a future session
# needs. Each file is still capped at RAW_MQTT_LOG_MAX_BYTES, so growth is in file count, not
# a single unbounded file.
RAW_MQTT_LOG_KEEP_FILES = None

# ---------------------------------------------------------------------------
# REPORT LOG — the consumer-side capture, keyed to the "Report Log" switch
# ---------------------------------------------------------------------------
# A second raw MQTT capture, deliberately separate from the one above: that one is the
# development evidence machine (pinned on here, per-run files, `/config/kohler_anthem_raw/`),
# this one is a user's bug-report tool — a switch on both device pages, one file per
# switch-on, and a Home Assistant restart appends to the SAME file rather than starting a
# new one. See `anthem/report_log.py` for the full semantics.
#
# The options key stores the active episode's name — its presence IS the switch state, so
# an episode survives restarts. It is in `RELOAD_IGNORED_OPTION_KEYS` for the same reason
# every switch-written key is: toggling a capture must not reload the entry and drop the
# very MQTT stream being captured.
CONF_REPORT_LOG_FILE = "report_log_file"

# Inside the integration folder, at the owner's decision (2026-08-22): reports sit with the
# integration they describe, reachable like any custom_components path. The accepted costs,
# documented in the README this capture writes beside its files: a HACS update or reinstall
# replaces the integration folder and deletes any reports still inside, and on the
# development install the directory is gitignored.
REPORT_LOG_DIR_NAME = "reports"
REPORT_LOG_MAX_BYTES = 8 * 1024 * 1024

# ---------------------------------------------------------------------------
# Preset 1's hidden timer — normalised once at setup
# ---------------------------------------------------------------------------
# A GCS preset carries its own `time`, a second run-time limit independent of the outlets'
# `maximumRunTime`. Whichever is lower stops the shower, and nothing ever re-syncs the preset
# to the hardware value: `time` is only ever what the last writer sent. Full protocol detail
# in `docs/gcs/api.md`, "two independent timers".
#
# That is a problem for **preset 1 specifically, and only preset 1**, because it is hidden
# from the owner in both the first-generation touchscreen and the Konnect app. Its timer is
# whatever the setup wizard happened to store when the preset was created — on this install,
# 1800 s, frozen at a factory reset on 2026-08-14 and then stranded when `maximumRunTime`
# went to 3600 s. The owner has no interface anywhere that can correct it.
#
# So the integration sets it once, to `DEFAULT_PRESET_TIMER_SECONDS`, and then leaves it
# alone. The intent is not to manage the timer but to take it *out* of the way, so the
# hardware gate is the thing that limits a shower — one limit, in one place, that the owner
# can actually see and change.
#
# **Every other preset is deliberately untouched.** Presets 2-10 are visible and editable in
# the Konnect app, their timers are the owner's choice, and on the first-generation
# touchscreen that timer is also the countdown shown during a run. Normalising those would
# overwrite a deliberate setting and change what the panel displays. Preset 1 is exempt from
# that reasoning precisely because it is the one the owner cannot see.
#
# Why a constant rather than following `maximumRunTime`: the hardware limit cannot be read on
# demand *over MQTT*, which is where this runs: `READ_GCS_OUTLET_CONFIG_CFG` arrives unprompted,
# one outlet at a time, so at setup the value is frequently not known yet. (It **is** readable
# over REST from `gcsadvancestate` — corrected 2026-08-17 — but this sync deliberately does not
# depend on a second network read succeeding.) A fixed target that is at or above every observed hardware value
# leaves the gate to the hardware in every case.
SYNC_DEFAULT_PRESET_TIMER = True
# Preset 1 is "Default shower" on every install seen: created by the setup wizard, and the
# slot the app hides.
DEFAULT_PRESET_ID = 1
# 3600 s is the highest `maximumRunTime` observed on this hardware (900/1800/3600). Setting
# the preset at the ceiling means the outlet limit is always the binding constraint.
DEFAULT_PRESET_TIMER_SECONDS = 3600

# ---------------------------------------------------------------------------
# What a config-entry change has to be before it is worth a reload
# ---------------------------------------------------------------------------
# Read by `_async_update_listener` in `__init__.py`; the mechanism is in
# `anthem/entry_reload.py`, which also explains why the comparison needs a snapshot.
#
# Keys the running integration writes to its OWN entry. A change to one of them is
# bookkeeping, not configuration, and must never cause a reload:
#
# * `CONF_REFRESH_TOKEN` — B2C rotates it on every refresh and invalidates the previous one,
#   so the newest has to be persisted immediately or a restart comes up unauthenticated.
#   That makes it the most frequently written key here, and reloading on it would flap every
#   entity and drop MQTT for nothing.
# * `CONF_MOBILE_DEVICE_ID` — generated once on first connect, then reused forever.
# * `CONF_VALVES` — per-valve bookkeeping, such as the remembered warm-up mode.
RELOAD_IGNORED_DATA_KEYS = frozenset(
    {CONF_REFRESH_TOKEN, CONF_MOBILE_DEVICE_ID, CONF_VALVES}
)

# `RELOAD_IGNORED_OPTION_KEYS` is defined further down, after the warmup constants it
# names — see the Warmup auto-restore section.

# ---------------------------------------------------------------------------
# REMOVED 2026-08-15 — valve reboot counter, controller ping, outage counter
# ---------------------------------------------------------------------------
# `CONF_GCS_REBOOT_COUNT` / `CONF_GCS_REBOOT_LAST` / `CONF_HUB_LOCAL_HOST` /
# `CONF_HUB_OUTAGE_COUNT` / `CONF_HUB_OUTAGE_LAST` / `CONF_HUB_OUTAGE_LAST_SECONDS` /
# `HUB_LOCAL_POLL_SECONDS` all lived here, alongside `anthem/hub_local.py`.
#
# They existed to diagnose the valve reboot fault, and that investigation is closed: the
# cause was a failing Moes smart outlet, not the Kohler hardware
# (`docs/gcs/valve_reboot_fault.md`). With both devices moved off it, the counters had no
# remaining question to answer — and the probe was the integration's **only** polling loop
# in an otherwise push-only design, at 1 Hz against the controller.
#
# Stale keys may remain in the config entry on installations that ran the old code; they are
# ignored. `_async_purge_removed_diagnostics` in `__init__.py` strips them on load.
# ---------------------------------------------------------------------------
# Temperature slider bounds (Home Assistant side only)
# ---------------------------------------------------------------------------
# What the temperature sliders offer. **These are a UI gate, not a device limit.** The valve
# accepts far more — 0 °C is a real setting meaning "full cold", and the app's own ceiling is
# 48.8 °C — and `valve_hex.py` still encodes the whole range, so a preset, the touchscreen,
# or `send_valve_hex` can still put the valve outside these bounds.
#
# Narrowed to the range people actually shower in, because a slider spanning 32-119 °F makes
# every useful degree a pixel wide.
#
# ⚠️ **The ceiling was 113 °F until 0.12.0, on a premise that turned out to be false.** The
# note here read "113 °F is also exactly the `maximumOutletTemperature` the valve reports for
# every outlet (450 tenths °C)" — true of the reference valve, and generalised from it. It is
# not universal: the owner's two valves report **450 tenths (113 °F) and 477 tenths
# (117.9 °F)**, confirmed 2026-09-10. So the old ceiling silently withheld five degrees the
# hardware would have accepted, on any valve set higher than the one this was written from.
#
# **92-118 °F is exactly what the Konnect app offers for the Max Temperature setting**
# (owner-confirmed 2026-09-10; Konnect 3.0.6 `gc0/i.java` bounds it 33-48 °C, shown as
# 92-118 °F). That is what `Max Temperature` and `Default Temperature` use these for.
#
# ⚠️ **It is not the zone temperature slider's range, which this used to claim.** Konnect
# 3.0.6 bounds that slider from a `COLD` stop one degree below the outlet's minimum (sent
# as 0 °C, full cold) up through `minimumOutletTemperature` (59 °F) to the valve's *current*
# `maximumOutletTemperature` (`qa0/p.java`, `db0/c.java` `n0()`). The zone control follows
# that since 2026-10-07 — owner's decision — using `ZONE_TEMPERATURE_MIN_F` below and the
# valve's live maximum; see `number.ZoneTemperatureNumber`.
#
# The bounds were 80-113 before 0.12.0 — both ends invented here rather than taken from the
# app. The old ceiling was justified as matching `maximumOutletTemperature`, which was a
# double mistake: that value was read from one valve and generalised, and it is a **setting**
# rather than a hardware limit. The owner changed one valve from 113 °F to 118 °F in the app
# on 2026-09-10 and the valve took it. So there is no fixed device ceiling for this slider to
# match — only the app's range, which is what it matches now. `maximumOutletTemperature` is
# still the ceiling in force at any moment, and the `Max Temperature` sensor reports it.
#
# Stated in Fahrenheit and converted for a Celsius account — the reverse would make these
# unrecognisable to anyone checking them against the shower.
#
# Consequence to keep in mind: if the wall panel sets a temperature below the minimum, the
# entity still *reports* it, but the slider cannot represent it accurately.
# ---------------------------------------------------------------------------
# Outlet type codes
# ---------------------------------------------------------------------------
# The valve reports a type code per outlet in `outLetType`. **This is now the full table** —
# Konnect 3.0.6's own outlet picker (`db0/c.java` `X()`, duplicated in `mc0/n.java`), the
# list a user chooses from when setting a valve up, so every code a valve can hold is one of
# these. Recovered 2026-10-07.
#
# It agrees with every code confirmed on hardware before then — 1, 11 and 21 from Kohler's
# own documentation, 31 on a K-28210 (2026-09-10) and 52 on a K-28211 (2026-10-05) — and it
# names the three seen in captures but never matched to a fixture: **38 is a Silk rainhead,
# 39 a Real Rain rainhead, 62 a pair of foot sprays.** (The 0.5.1 mistake of naming 52 and
# 62 by lining one install's outlet order up against another's is still the thing never to
# do; these come from the app's table, not from inference.)
#
# Names are the fixture as the app titles it. Where the picker distinguishes one fixture from
# several (`Showerhead` / `Showerheads`, `Single` / `Multiple` body or foot sprays), the
# plural is kept, because "Body Sprays" switches more than one head. Rainhead and body-spray
# *variants* (Katalyst, Silk, Massage…) are the same fixture to switch, so they share a name
# and the variant is published separately — see `OUTLET_TYPE_VARIANTS`.
#
# **This is a label, not behaviour.** The valve derives no flow envelope from the type; the
# controller does. Nothing in this integration reads these names to decide anything.
#
# The Konnect app's own outlet names are *not* here because they are not transmitted: no
# name string appears anywhere in the captured API surface, so a rename in the app cannot
# be read back. These are fixture types, which is the closest the hardware gets.
#
# ⚠️ Naming a code renames its switch, and the switch's unique id follows its name. An
# existing position-named switch (`Outlet 1.3`) is migrated to its fixture id in place, so
# its entity id and automations survive — see `switch._async_migrate_outlet_unique_ids`.
OUTLET_TYPE_NAMES: dict[int, str] = {
    1: "Handshower",
    11: "Showerhead",
    12: "Showerheads",
    21: "Tub Filler",
    30: "Not Plumbed",
    31: "Rainhead",
    32: "Rainhead",
    33: "Rainhead",
    34: "Rainhead",
    35: "Rainhead",
    36: "Rainhead",
    37: "Rainhead",
    38: "Rainhead",
    39: "Rainhead",
    51: "Body Spray",
    52: "Body Sprays",
    53: "Body Spray",
    61: "Foot Spray",
    62: "Foot Sprays",
}

#: The variant the app shows beneath the fixture name, for codes that have one. Published as
#: the outlet switch's `outlet_variant` attribute; never part of a name or an id.
OUTLET_TYPE_VARIANTS: dict[int, str] = {
    31: "Katalyst",
    32: "Cascade",
    33: "Kinetic",
    34: "Rain Curtain",
    35: "Laminar",
    36: "Massage (Wave)",
    37: "Hydro Massage",
    38: "Silk",
    39: "Real Rain",
    51: "Single",
    52: "Multiple",
    53: "Massage (Wave)",
    61: "Single",
    62: "Multiple",
}

UI_TEMPERATURE_MIN_F = 92
UI_TEMPERATURE_MAX_F = 118

#: The zone temperature control's floor when the valve has not reported its own minimum —
#: the `minimumOutletTemperature` of 15.0 °C every captured outlet carries, and the value the
#: app forces on every outlet-config write. The control's lowest step is one below this, and
#: means full cold, as the app's `COLD` stop does.
ZONE_TEMPERATURE_MIN_F = 59

# ---------------------------------------------------------------------------
# Writable outlet configuration — the Konnect app's own three settings
# ---------------------------------------------------------------------------
# **Max Shower Duration** is a curated list, not a range. Konnect 3.0.5 offers exactly these
# six; 35/40/50/55 minutes are skipped even though they are legal multiples of 300 s, and
# whether the valve would take one is untested (`docs/gcs/api.md` — "still open"). A select of
# what the app offers is honest about that; a slider would imply the gaps are reachable.
#
# 3600 s is live-verified writable (sweep, 2026-08-21), which is what makes 45 and 60 real
# rather than theoretical.
OUTLET_RUN_TIME_CHOICES_SECONDS = (900, 1200, 1500, 1800, 2700, 3600)

# ⚠️ **Konnect 3.0.1 misreads any duration above 1800 s.** Its picker snaps the device value
# into 15-30 before choosing a wheel index, so a valve set to 45 or 60 minutes displays as 25
# and one tap of Save silently writes 1500. A vendor defect in that build, fixed by 3.0.5 —
# but worth a warning wherever this integration lets someone choose the higher values.
OUTLET_RUN_TIME_APP_SAFE_MAX_SECONDS = 1800

# **Default Temperature** — what a shower starts at when nothing specifies otherwise. The app
# bounds it below by a fixed floor and above by whatever the scald limit currently is, which
# is why the maximum here is read from the valve rather than being a constant.
UI_DEFAULT_TEMPERATURE_MIN_F = 59

# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------
# What flow Home Assistant writes when a command does not name one — which is every command
# it can currently issue, while the flow entities were absent, between 2026-08-13 and 0.6.0 (see `docs/gcs/api.md`).
#
# **This must not be "whatever the valve currently holds".** It used to be, and that quietly
# handed control of every HA write to the touchscreen: opening an outlet from Home Assistant
# re-sent the last flow the wall panel wrote. Measured over 1,346 captured valve words, 419 —
# **31%** — were below 100%, the lowest at **8%**. So roughly a third of the time, turning on
# a shower from Home Assistant would have produced a trickle, with nothing in the UI to
# explain why or to fix it.
#
# 100% is the only defensible default: it is the one value a user who has no flow control
# cannot be surprised by. (This used to add "it is what the Konnect app pins favorites to".
# Konnect 3.0.6 does not: a favorite takes the outlet's own `defaultFlowrate`, `db0/c.java`.
# The reasoning above stands without it.)
DEFAULT_FLOW_PERCENT = 100.0

# ---------------------------------------------------------------------------
# Warmup dropdown labels
# ---------------------------------------------------------------------------
# The device's mode strings are camel-case protocol values and make a poor dropdown, so each
# gets a display label here.
#
# ⚠️ **These are Home Assistant's labels, not the Konnect app's.** They tracked the app's
# wording until 2026-08-21, when the owner renamed them: `All Outlets` took a capital O, and
# `Selected outlets` became **`Started Outlets`**, which describes what the mode does here
# rather than echoing the phone. So the dropdown and the app now read differently for that
# mode — deliberately. Only the labels moved; the protocol values, the write path and which
# modes are offered are all unchanged.
#
# The two legacy delayed-start modes get labels too, but they are never *offered* — they are
# only added to the dropdown when the valve is already holding one, so the entity can report
# the truth instead of blanking. Their labels follow the same wording, so the dropdown stays
# internally consistent if one ever appears. See `select.py`.
WARMUP_LABELS = {
    "warmUpDisabled": "Off",
    "warmUpAllOutletsWithNoStartDelay": "All Outlets",
    "warmUpSelectedOutletsWithNoStartDelay": "Started Outlets",
    "warmUpAllOutlets": "All Outlets (delayed start)",
    "warmUpSelectedOutlets": "Started Outlets (delayed start)",
}

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
# **One service, and this is the whole list.** Everything else this integration does is an
# entity — a switch, a select, a number — because an entity shows state as well as accepting
# a command, and a service only accepts one.
#
# Twelve more names lived here until 2026-08-20: `set_outlets`, `start_preset`,
# `activate_favorite`, `stop_all`, `set_warmup` and their `ATTR_*` fields. **None was ever
# registered** — they were scaffolding for a service-shaped design that entities replaced,
# and the two warmup ones additionally described an older Konnect build. A constant nothing
# reads is a claim that something exists; these claimed five services that did not.
# `tests/test_warmup_select.py` fails if the warmup pair reappears.
SERVICE_SEND_VALVE_HEX = "send_valve_hex"
# The form-driven sibling: outlets, temperature and an optional flow as typed fields, sent as
# ONE complete write. Added 2026-09-06 after GitHub issue #1 showed that every UI-built
# automation ends up as two valve commands back to back, which the valve cannot take.
SERVICE_CUSTOM_SHOWER = "custom_shower"

# ---------------------------------------------------------------------------
# Warmup auto-restore
# ---------------------------------------------------------------------------
# The Anthem Plus hub sets the valve's warmup mode back to `warmUpDisabled` on every
# signed-in use of its web UI — a constant in the hub's login/UI routine, solved 2026-08-21
# after reproducing it live six times in a day (`docs/gcs/api.md` §3h). It cannot be
# prevented from outside the hub's firmware, so this feature is the standing mitigation:
# it puts the mode back, once, a minute later.
CONF_WARMUP_AUTO_RESTORE = "warmup_auto_restore"

# The last *enabled* mode seen on the valve, persisted so a restore reinstates what was
# actually in force rather than a default. Without it there is nothing to restore to, and
# guessing "all outlets" would silently change a fixture set to "selected outlets".
CONF_LAST_WARMUP_MODE = "last_warmup_mode"


# Options the coordinator reads live from `entry.options` on every access instead of caching,
# so they already take effect the moment they are saved and a reload would be pure cost.
#
# Anything NOT listed here reloads. An option added later therefore works by default, and
# only an option proven to be read live gets added to this set — a decision someone has to
# write down rather than inherit by accident.
#
# ⚠️ **That default is safe but not free, and the warmup pair paid for it.** Both were read
# live from the entry options from the first line they existed, so a toggle takes effect
# immediately — but neither was ever added here. So until 2026-08-21:
#
# * **Toggling the auto-restore switch reloaded the whole entry.** It writes
#   `CONF_WARMUP_AUTO_RESTORE`, which fell through to a reload.
# * **Choosing a warmup mode did too**, one beat later: a confirmed write calls
#   `_remember_warmup_mode`, which persists `CONF_LAST_WARMUP_MODE`.
#
# Either way every entity flapped to `unavailable` for several seconds and the MQTT stream
# was dropped and re-warmed — the exact cost `_async_update_listener` exists to avoid, paid
# on two of the integration's own controls. Reported by the owner, who saw the integration go
# unavailable every time they touched either one.
#
# `CONF_LAST_WARMUP_MODE` is written by the integration itself rather than by a user, and
# `_async_seed_state` now writes it during setup, so leaving it out also meant a reload
# chasing its own tail on the first start after the mode changed while Home Assistant was
# down.
RELOAD_IGNORED_OPTION_KEYS = frozenset(
    {
        CONF_WARMUP_AUTO_RESTORE,
        CONF_LAST_WARMUP_MODE,
        CONF_REPORT_LOG_FILE,
        CONF_VALVES,
    }
)

# One minute, as asked for. Long enough that a re-sync burst has finished writing before we
# write back — restoring into the middle of one would just be overwritten again.
WARMUP_AUTO_RESTORE_DELAY_SECONDS = 60.0

# A disable *we* caused, from the dropdown, must never be undone by this — otherwise choosing
# `Off` becomes impossible. Our own writes are recorded and any matching disable inside this
# window is ignored. Generous because the device echo itself takes ~3.4 s.
WARMUP_SELF_WRITE_GRACE_SECONDS = 30.0

# If the mode is disabled again immediately after each restore, something is actively fighting
# us and a restore loop would hammer Kohler's API forever. Stop after this many consecutive
# restores that failed to stick, and say so.
WARMUP_AUTO_RESTORE_MAX_CONSECUTIVE = 5

# A restore that stayed put for this long counts as successful, and resets the counter above.
WARMUP_AUTO_RESTORE_SETTLED_SECONDS = 900.0

WARMUP_AUTO_RESTORE_ON = (
    "Warmup Auto-Restore is ON. If something sets the Anthem valve's warmup mode to Off, "
    "Home Assistant will set it back to %s after %.0f seconds. Turning warmup off from the "
    "Warmup dropdown is not affected — only changes this integration did not make."
)

WARMUP_AUTO_RESTORE_NO_TARGET = (
    "Warmup Auto-Restore is ON but no enabled warmup mode has been seen yet, so there is "
    "nothing to restore to. Pick a mode on the Warmup dropdown and it will be remembered."
)

WARMUP_AUTO_RESTORE_GIVING_UP = (
    "Warmup Auto-Restore has put the mode back %d times and it keeps being disabled again. "
    "Something on the system is actively rewriting it, and retrying is not fixing that — "
    "stopping until the mode stays enabled or Home Assistant restarts. See "
    "docs/gcs/api.md section 3e."
)

# ---------------------------------------------------------------------------
# Warmup diagnostic journal
# ---------------------------------------------------------------------------
# **Off by default.** It was forced on during an investigation and shipped that way through
# 0.6.7. Built to catch what kept
# disabling warmup; that question is solved (`docs/gcs/api.md` §3h — the hub's web UI).
#
# Worth turning on locally if warm-up is being rewritten by something on your system: it
# verifies every auto-restore end to end and would be the first thing to notice a different
# writer. Volume is a handful of records a day; `DebugJournal` rolls each file at 8 MB.
ENABLE_WARMUP_DEBUG_LOG = False

# Unlimited: this is evidence for an open question, and the whole point is comparing an
# event to ones weeks earlier.
WARMUP_DEBUG_LOG_KEEP_FILES = None

# How much wire traffic to carry in a disable record, either side of the event.
#
# 120 s back and 45 s forward, chosen from what the four known disables actually look like:
# the config re-sync burst around them (outlet configs, presets, experience snapshots) runs
# for roughly a minute beforehand, and `SYSTEM_STS: SYSTEM_READY` — the most distinctive
# marker — landed 7 to 9 s AFTER the disable in the two clearest cases. A window that only
# looked backwards would miss the strongest signal there is.
#
# ⚠️ **The forward window must stay shorter than WARMUP_AUTO_RESTORE_DELAY_SECONDS**, and
# The external harness asserted this; nothing in this repository's `tests/` does. Both were
# 60 s when first written, which put
# the close of the evidence window at the exact instant auto-restore writes to the valve —
# so whether our own traffic landed inside the evidence depended on which coroutine the loop
# happened to run first. 45 s ends the window a clear 15 s before any intervention, which
# costs nothing: the marker being hunted arrives within 10 s.
WARMUP_CONTEXT_BEFORE_SECONDS = 120.0
WARMUP_CONTEXT_AFTER_SECONDS = 45.0

# Cap on the rolling buffer of recent messages, so a chatty hour cannot grow it without bound.
WARMUP_CONTEXT_MAX_MESSAGES = 400

# ---------------------------------------------------------------------------
# Warmup write confirmation
# ---------------------------------------------------------------------------
# How long to wait, cumulatively, before treating a read-back that disagrees with the write
# as a real failure rather than lag.
#
# Measured live 2026-08-20 against this valve: a POST accepted at 08:01:34 still read back
# the OLD mode at t+0 and the new one by t+3, with the valve's own MQTT echo at +3.42 s. An
# immediate single read therefore reports a false mismatch every time. Three attempts spanning
# 6 s clears that with margin while keeping the service call short enough for a UI action.
WARMUP_READBACK_DELAYS = (0.0, 2.0, 4.0)

# ---------------------------------------------------------------------------
# CLOUD CONNECTION WATCH — is the valve still reachable by Kohler's cloud?
# ---------------------------------------------------------------------------
# Full explanation and the measurements behind every number here: `cloud_watch.py`.
#
#     grep -rn "CLOUD CONNECTION WATCH" custom_components/kohler_anthem/
#
# The problem this exists for: the GCS valve drops off Kohler's cloud on its own and only
# returns on a power cycle. MQTT cannot report it — everything on that stream is published
# *by* the valve, so a disconnect is silence, and silence is indistinguishable from idle.
# Measured over a 19-day corpus: the longest silence provably benign is **12 h 02 m**, and
# the one real outage was **12 h 22 m**. No silence threshold separates them, which is why
# neither of the triggers below alerts on silence — they only decide when to *ask*.

# How close a GCS message has to be to a HUB `SHOWER_VALVE_STS` for the pair to count as
# confirmed. Both directions, so a valve message just before or just after the controller's
# report pairs it.
#
# 60 s, not 5 s. Measured across the 19-day corpus: at ±5 s there are 23 unpaired controller
# reports (3.9 %), nearly all ordinary controller lag; at ±60 s there are **4**, and **3 of
# those are the 2026-08-26 outage**. The controller normally trails the valve by 0.3–2 s, but
# a documented restore once went **176.77 s** with no valve message at all — which is why the
# rule additionally requires a zone to be ON rather than trusting timing alone.
CLOUD_CHECK_PAIR_WINDOW_SECONDS = 60.0

# Minimum spacing between REST reads, whichever trigger asks for one.
#
# The point is that a shower produces a burst of `SHOWER_VALVE_STS` — 437 zone-ON reports in
# 13 days, clustered — and an unreachable valve would make every one of them fire. One read
# per half hour is enough to answer "is it gone", since the failure lasts hours and is only
# cleared by a human at the wall.
CLOUD_CHECK_COOLDOWN_SECONDS = 1800.0

# How long the valve may be silent before we ask the cloud about it directly.
#
# ⚠️ **This is not a silence alarm and must never become one.** 3 h of quiet is completely
# normal — the corpus holds 12 h idles that were provably fine. It is the interval after
# which the *question* is worth one HTTP GET, and `connectionState` answers it definitively,
# so a "false" trigger costs one request and reports Connected.
CLOUD_CHECK_QUIET_SECONDS = 3 * 60 * 60.0
