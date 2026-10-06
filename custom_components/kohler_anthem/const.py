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
# its own learned run times (data) and its own Endless Shower, Warmup Auto-Restore and
# remembered warm-up mode (options). The flat keys those used to be are migrated under
# here once, at setup, by `KohlerAnthemCoordinator._migrate_valve_settings`.
# Reload-ignored in both lists for the same reason every one of those keys was.
CONF_VALVES = "valves"

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
# CUTOFF DEBUG LOG — why the run-time cutoff fired, or didn't
# ---------------------------------------------------------------------------
# Full explanation and how to read it against the raw capture: `anthem/cutoff_log.py`.
# Find every piece of this feature with:
#
#     grep -rn "CUTOFF DEBUG LOG" custom_components/kohler_anthem/
#
# Runtime switch, no restart needed — Developer Tools -> Actions -> `logger.set_level`:
#
#     custom_components.kohler_anthem.anthem.cutoff_log: debug
#
# **Off by default, and it must ship off.** Switched on 2026-08-14 after the detector was
# found to be timing the wrong thing, and shipped that way through 0.6.7 by oversight. A
# cutoff that fails to fire writes nothing to `home-assistant.log`, so this journal is the
# only way to tell "no cutoff happened" from "a cutoff was missed" — genuinely valuable
# while investigating, and not something to leave running on every install.
#
# Written into the same directory as the raw capture and stamped from the same clock, so the
# two interleave by sorting on `ts`. Volume is a handful of lines per shower, but unlike the
# raw capture `CutoffDebugLog` has **no size cap at all** and `..._KEEP_FILES` below is None,
# so nothing bounds a single file's growth.
ENABLE_CUTOFF_DEBUG_LOG = False

# None = no limit on the number of files; every log is kept forever, matching
# RAW_MQTT_LOG_KEEP_FILES. Deliberately the same directory as the raw capture: these two logs
# are read together, and splitting them across directories only makes the join harder.
CUTOFF_DEBUG_LOG_KEEP_FILES = None

# ---------------------------------------------------------------------------
# Run-time cutoff restart (option, default off)
# ---------------------------------------------------------------------------
# The valve shuts a **zone** off once it has been running for `maximumRunTime` — 900 s on the
# reference install now, 3600 s before it was reconfigured, reported per outlet in
# `READ_GCS_OUTLET_CONFIG_CFG` but timed per zone. With this option on, the integration
# re-opens the outlets that were running and the shower carries on.
#
# The per-zone part is not a detail: the timer starts when a zone begins flowing and does not
# reset when outlets change within it. Timing each outlet instead — which shipped until
# 2026-08-14 — misses every cutoff where somebody moved between shower heads. See
# `anthem/runtime_cutoff.py`.
#
# **This defeats a manufacturer cutoff, and there is no resume limit.** Water will keep
# coming back for as long as somebody leaves it running, with no software or hardware stop
# behind it — the hardware stop is the thing being overridden. That is the owner's stated
# choice, made after the trade-off was put to them explicitly; it is not an oversight to
# "fix". Leave it default-off, keep every restart logged at WARNING, and do not extend it to
# fire on anything other than a positive run-time match.
CONF_RESTART_ON_RUNTIME_CUTOFF = "restart_on_runtime_cutoff"


# Learned per-outlet `maximumRunTime`, persisted so the cutoff feature is armed from the first
# second after a restart rather than waiting on an unprompted announcement.
#
# ⚠️ **Corrected 2026-08-17.** This comment used to say the value was "otherwise unobtainable on
# demand" because every `gcs-outlet-config`-style REST path 404s. Those paths really do 404, but
# the data was reachable all along: **`gcsadvancestate` carries
# `setting.valveSettings[].outletConfigurations[]`**, and this integration already calls that
# endpoint — `topology.py` reads `noOfOutlets` from the very same response. Verified live.
#
# Persisting is still worth it (one fewer REST round trip on the hot path), but the "blind
# window" that justified it is not the constraint it was believed to be. Reading it at setup
# was done on 2026-08-17 and runs on every reseed since — `_async_seed_state` reads
# `gcsadvancestate` and feeds `_learn_run_times`, so the window is closed and this
# persistence is now the belt to that suspender (it still arms the feature during the
# seconds before the first seed completes, and across a seed that fails).
#
# Without persistence the cutoff feature is inert after every restart until the valve happens
# to announce again, which can be a long wait and gives no sign of why nothing is happening.
# The value is installation configuration and does not drift, so remembering it is safe; a
# fresh announcement always overwrites what is stored.
CONF_OUTLET_RUN_TIMES = "outlet_run_times"

# ---------------------------------------------------------------------------
# Endless Shower — the messages the owner actually reads
# ---------------------------------------------------------------------------
# Written for someone standing in a bathroom, not for whoever wrote the integration. The
# feature is called **Endless Shower** everywhere the owner can see it; `maximumRunTime`,
# `restart_on_runtime_cutoff` and the zone/outlet split are internal and stay out of these.
#
# The one setting a user can act on is **Max Shower Duration** in the Kohler Konnect app, so
# every "it is not working" message points at exactly that and nothing else.
#
# Shared between the startup log in `coordinator.py` and the switch-on log in `switch.py`, so
# the two cannot drift into saying different things about the same state.

# Nothing to work with: no outlet has reported a duration, or only some have. Also used when
# a cutoff fires but no outlet snapshot exists to restore.
# ⚠️ **Reworded 2026-08-17 and it must stay this way.** This used to read "please reconfigure
# 'Max Shower Duration' in the Kohler Konnect app" — advice that existed only because the limit
# arrived over MQTT unprompted, so changing the app setting was the one way to provoke an
# announcement. The integration now reads it over REST at setup (`gcsadvancestate`), so that
# instruction is obsolete: this state is transient and self-healing, not something the owner
# should be sent to the app to fix.
ENDLESS_SHOWER_NOT_SET_UP = (
    "Endless Shower is ON but the shower time limit has not been read from the valve yet, so "
    "nothing will be restarted. It arms itself automatically as soon as the valve reports it."
)

# Armed. %s is the duration in whole minutes, from `describe_duration`.
ENDLESS_SHOWER_ON = (
    "Endless Shower is ON. Your shower will restart automatically every %s minutes, when "
    "'Max Shower Duration' is reached."
)

# `ENDLESS_SHOWER_MATCH_DURATIONS` stood here until 2026-08-22 — an unconditional "set the
# controller's Max Shower Duration to the SAME value" warning, printed at every start and
# every toggle of any dual-product install. Removed on the owner's decision: this integration
# cannot know whether the durations actually differ (the hub's number is local-API-only, and
# storing the hub PIN was ruled out), so the nag fired regardless. The advice itself still
# holds — the valve fires marginally early, the controller marginally late, so equal
# durations mean the valve's restorable `0x40` always wins — and the mismatch warning that
# remains is evidence-based: `runtime_cutoff.py` warns when a minute-boundary stop shows the
# controller preempting the valve (the direction with a valve-side fix), and only journals a
# sweep past the valve's limit (no HA-side action exists).

# A cutoff was caught and the shower put back. %s is the local time it was cut off.
ENDLESS_SHOWER_RESTARTED = "Max Shower Duration reached at %s. Restarted the shower."

# Defensive only. A cutoff cannot normally fire without a mask to restore: the detector sets
# its start time and its last-running mask on the same update, and `forget()` clears both
# together, so "timed a zone" and "knows what was in it" cannot come apart. It has never
# fired — all seven restores in the capture corpus had a mask.
#
# ⚠️ **This is NOT the "Home Assistant restarted mid-shower" case.** That one produces no log
# at all, and cannot: the clock restarts with the process, so at the valve's real cut-off the
# measured duration falls short of the limit, nothing matches, and no cutoff is detected. The
# shower simply ends. Deliberate — see `ZoneCutoffDetector`, which would rather miss a cutoff
# than reopen a valve on a duration it did not actually measure.
#
# Says nothing about zones: the owner has no use for the zone number, and the cutoff debug
# log carries it for anyone investigating. Logged once per affected zone, so two lines mean
# two zones.
# Repairs card shown while Endless Shower is on but cannot act. A log line states this once,
# at startup, and then scrolls away — it can never answer "is it still broken?", which is the
# only question the owner actually has. A repair is the opposite: it appears when the
# condition becomes true, persists while it stays true, and removes itself when the valve
# finally reports a duration. Nobody has to dismiss it.
#
# Doubles as the `translation_key`, so the text lives in `strings.json` under `issues`.
ISSUE_NOT_SET_UP = "endless_shower_not_set_up"

ENDLESS_SHOWER_NOTHING_TO_RESTORE = (
    "Endless Shower could not restart the shower, because Home Assistant has no record of "
    "what was running."
)

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
# * `CONF_OUTLET_RUN_TIMES` — written whenever the valve announces a `maximumRunTime`, which
#   it does unprompted and **can do mid-shower**. A reload builds a new coordinator with a
#   fresh `ZoneCutoffDetector`, so every zone clock restarts at zero while the valve's own
#   timer keeps running: the exact mechanism by which a run-time cutoff gets missed. This
#   exclusion matters more than the comparison it is part of.
# * `CONF_MOBILE_DEVICE_ID` — generated once on first connect, then reused forever.
RELOAD_IGNORED_DATA_KEYS = frozenset(
    {CONF_REFRESH_TOKEN, CONF_OUTLET_RUN_TIMES, CONF_MOBILE_DEVICE_ID, CONF_VALVES}
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
# **92-118 °F is exactly what the Konnect app's own slider offers** (owner-confirmed
# 2026-09-10), and matching the app is the point: those are the numbers on the panel and in
# the app, and a Home Assistant control with a different range reads as broken rather than
# cautious.
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
# The valve reports a type code per outlet in `outLetType`. These are the codes whose
# meaning is **confirmed**, not the full set — an unrecognised code is published as a bare
# number and given no name, because a wrong fixture name is worse than an honest number.
#
# Provenance, because it decides how much these can be trusted:
#
# * `1`, `11`, `21` — documented in `docs/hub/cloud_api.md` §"Outlet position → physical
#   outlet", which also warns that other codes are install-specific.
# * `31` — owner-confirmed 2026-09-10 on a K-28210 reporting `31, 11, 1` for a rainhead,
#   showerhead and handshower. The other two codes on that valve are the documented ones,
#   which is what makes the first credible: two of three positions independently matched
#   Kohler's own table.
# * `52` — owner-confirmed 2026-10-05 on a K-28211 reporting `11, 52` (zone 1) and `31, 1`
#   (zone 2) for a showerhead, body sprays, rainhead and handshower.
# * `39`, `38`, `62` — seen in the capture corpus but **never confirmed against a
#   fixture**, so they are deliberately absent.
#
#   ⚠️ **`62` and `52` were briefly named here (0.5.1) and that was wrong.** They were
#   inferred by assuming a second install's outlets sat in the same id order as the corpus
#   reference machine's. The install that inference was built on turned out to report
#   `31, 11, 1`, so the corpus codes belong to different fixtures than assumed. Codes are
#   only added here on a direct owner report of *that* valve's own numbers — never by
#   lining two installs up against each other.
#
# **This is a label, not behaviour.** The valve derives no flow envelope from the type; the
# controller does. Nothing in this integration reads these names to decide anything.
#
# The Konnect app's own outlet names are *not* here because they are not transmitted: no
# name string appears anywhere in the captured API surface, so a rename in the app cannot
# be read back. These are fixture types, which is the closest the hardware gets.
OUTLET_TYPE_NAMES: dict[int, str] = {
    1: "Handshower",
    11: "Showerhead",
    21: "Tub Filler",
    31: "Rainhead",
    52: "Body Sprays",
}

UI_TEMPERATURE_MIN_F = 92
UI_TEMPERATURE_MAX_F = 118

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
# 100% is the only defensible default: it is what the Konnect app pins favorites to, and it
# is the one value a user who has no flow control cannot be surprised by.
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
#: Exploratory: calls the undocumented `gcs-usage` endpoint with candidate query strings and
#: writes what each returns to a file. See `anthem/const.py:GCS_USAGE`.
SERVICE_PROBE_USAGE = "probe_usage"

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
# live from the first line they existed — `warmup_auto_restore`'s own docstring says "read
# live from the entry options, *like `restart_on_runtime_cutoff`*, so the switch takes effect
# immediately" — but neither was ever added here. So until 2026-08-21:
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
        CONF_RESTART_ON_RUNTIME_CUTOFF,
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
# **Off by default**, like the cutoff journal, and off for the same reason: it was forced on
# during an investigation and shipped that way through 0.6.7. Built to catch what kept
# disabling warmup; that question is solved (`docs/gcs/api.md` §3h — the hub's web UI).
#
# Worth turning on locally if warm-up is being rewritten by something on your system: it
# verifies every auto-restore end to end and would be the first thing to notice a different
# writer. Volume is a handful of records a day, but it shares `CutoffDebugLog`'s missing
# size cap.
ENABLE_WARMUP_DEBUG_LOG = False

# Unlimited, matching the cutoff journal: this is evidence for an open question, and the
# whole point is comparing an event to ones weeks earlier.
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
