"""Static configuration for the foil scanner.

Every threshold and band here comes from docs/SPEC.md. validate() must run
at startup; a bad config aborts before any network fetch (spec section 8.7).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Australia/Sydney")

FORECAST_DAYS = 7
MIN_MODELS_AGREE = 2
# Day offsets (0 = today) from which windows are flagged low confidence.
LOW_CONFIDENCE_FROM_DAY_OFFSET = 4

# Open-Meteo model ids -> display names (spec 3.1). BOM's ACCESS-G was the
# original fourth model but stopped returning data on Open-Meteo (verified
# dead 4 Jul 2026); UKMO global (10 km) covers Australia well and is live.
MODELS = {
    "gfs_seamless": "GFS",
    "ecmwf_ifs025": "ECMWF",
    "icon_seamless": "ICON",
    "ukmo_seamless": "UKMO",
}

# Grading relative to a trigger's target strength T (spec 6):
# yellow from 0.9*T, green from T, red above 1.25*T.
YELLOW_FACTOR = 0.9
RED_FACTOR = 1.25

# "Maybe" band below yellow (Rob, 10 Aug 2026). A blanket percentage of the
# same target every trigger already grades against, so one number widens the
# net everywhere instead of eight hand-tuned floors. Watch runs from 0.75*T up
# to the yellow floor at 0.9*T, and unlike a real window it only needs
# MIN_MODELS_WATCH model to hit - the point is to surface days worth a second
# look at the models, not to claim a run is on.
WATCH_FACTOR = 0.75
MIN_MODELS_WATCH = 1


def watch_floor_for(yellow_floor: float) -> float:
    """The maybe band measured down from a trigger's yellow floor, not from
    its target.

    Most triggers derive yellow as 0.9 * target, so this is just
    WATCH_FACTOR * target for them. The entrance reverse run does not - spec
    4.8 fires it at 20 kn against a 25 kn target - and taking 0.75 of the
    target there left it a 1.25 kn band where every other trigger gets 15%,
    which on 10 Aug missed a genuine single-model hit at 18.5 kn by a quarter
    of a knot. Anchoring to the yellow floor gives every trigger the same
    proportional band.
    """
    return yellow_floor * WATCH_FACTOR / YELLOW_FACTOR


@dataclass(frozen=True)
class Location:
    key: str
    name: str
    lat: float
    lon: float


LAKE = Location("lake", "Lake Illawarra (mid-lake)", -34.53, 150.84)
ENTRANCE = Location("entrance", "Lake entrance (Windang)", -34.535, 150.874)
OCEAN = Location("ocean", "Wollongong coast", -34.43, 150.92)
MARINE_POINT = Location("marine", "Offshore of entrance", -34.55, 150.90)

WIND_LOCATIONS = (LAKE, ENTRANCE, OCEAN)


@dataclass(frozen=True)
class Arc:
    """Direction band in degrees-from, inclusive, may wrap through 360."""

    lo: float
    hi: float

    def contains(self, deg: float) -> bool:
        d = deg % 360.0
        if self.lo <= self.hi:
            return self.lo <= d <= self.hi
        return d >= self.lo or d <= self.hi


# Lake runs (spec 4.1): trigger id -> (run name, arc, target kn, rare)
LAKE_RUNS = {
    "lake_oakflats_berkeley": ("Oak Flats to Berkeley", Arc(170, 215), 20.0, False),
    # One band, not two. Kanahooka and Berkeley are adjacent runs on the same
    # water (Rob, 11 Aug 2026), and the wind oscillates across the 260 deg
    # edge between them, so the distinction was never material on the day.
    # Splitting them named the wrong run 62% of the time even with the
    # direction corrected and both crossings hedged; one band gets it right
    # 81% of the time and needs no hedging machinery at all.
    "lake_west": ("Kanahooka / Berkeley", Arc(215, 285), 20.0, False),
    "lake_ne_rare": ("Sailing Club to Oak Flats", Arc(20, 70), 25.0, True),
}

# Lake Entrance (spec 4.2)
# The *preferred* window: high tide to +2 h, the run-out. Not a gate - see
# ENTRANCE_NO_GO_BEFORE_LOW_H for the only period that actually excludes a run.
ENTRANCE_TIDE_WINDOW_H = 2.0
# The tide gate downgrades an entrance window, it does not delete it (Rob,
# 10 Aug 2026: "it can still work on the odd tide, so any suitable should be
# flagged"). A wind-and-swell window that lands outside the gate keeps its
# event, drops this many colour steps and carries an "off-tide" tag; with
# WATCH in the ladder a yellow one lands on the calendar as a watch rather
# than vanishing. Set to 0 to stop treating the tide as a penalty at all.
ENTRANCE_OFF_TIDE_DOWNGRADE = 1
# Rob, 11 Aug 2026, replacing a distance-from-the-gate heuristic with the
# actual constraint: the entrance "can still work on any tide except the last
# 4 hours before dead low - water flow is too much". That is peak ebb, the
# lake draining through a narrow entrance, and it is a hard no rather than a
# downgrade. Everything outside it is workable; the high-tide run-out above
# is merely preferred.
ENTRANCE_NO_GO_BEFORE_LOW_H = 4.0
# Modelled sea level is hourly, so a tide peak lands anywhere in a +/- 30 min
# band around the sample - the 10 Aug entrance miss turned on exactly that
# (gate closed 07:00, window opened 07:00). high_tides()/low_tides() fit a
# parabola through the three samples around each extremum for a sub-hourly
# time; this offset is then added on top.
#
# CALIBRATED 11 Aug 2026 against the real Port Kembla gauge (IOC station
# "pkem", 1 km from the marine point), 14 days of 1-minute observations,
# 27 matched high tides and 27 lows - see scripts/calibrate_tide.py:
#
#   highs: observed - model = +28 min (sd 9)
#   lows:  observed - model = +32 min (sd 10)
#
# The model runs consistently early, so the real tide is later: positive.
# The spread is small enough that this is a systematic bias, not noise.
TIDE_TIME_OFFSET_MIN = 30.0
# Added to the modelled sea level to report tide-table-comparable heights.
#
# Renamed from PORT_KEMBLA_MSL_ABOVE_CD_M, which named a physical quantity
# this constant no longer holds. It was the NSW open-coast MSL-above-chart-
# datum figure (~0.95 m), used as a stand-in. Calibration showed Open-Meteo's
# "MSL" sea level carries its own offset here - it averages +0.148 m over a
# fortnight rather than 0 - so what the code actually needs is the combined
# model-to-gauge-datum shift, not a textbook datum separation.
#
# CALIBRATED 11 Aug 2026 against IOC gauge "pkem" (see the note on
# TIDE_TIME_OFFSET_MIN): observed minus model = +0.826 m over 54 matched
# tides, sd 0.03 m. The old 0.95 over-reported every tide height by ~12 cm.
TIDE_HEIGHT_OFFSET_M = 0.83
# Mode 1 is a SWELL run, and the entrance is open to the ocean - unlike the
# lake, which has nothing but wind. So wind grades this trigger, it does not
# gate it (Rob, 11 Aug 2026: "it's not a wind only place, so that shouldn't
# be the golden gate holding it all back").
#
#   favourable  -> full rating: offshore (ENTRANCE_M1_WIND_ARC), or light
#                  enough that direction stops mattering
#   unfavourable-> a watch. Swell and tide are still there; the wind is just
#                  wrong, and that is worth looking at rather than deleting
#   too strong  -> no go, but only when it is unfavourable as well. Strong
#                  offshore grooms a swell run; strong onshore ruins it
ENTRANCE_M1_WIND_MAX_KN = 10.0
ENTRANCE_M1_WIND_ARC = Arc(200, 340)
ENTRANCE_M1_CALM_KN = 5.0
# ESTIMATE, not measured - the one number here Rob did not give. "Way too
# strong" for onshore wind over a shallow entrance; dial it to taste.
ENTRANCE_WIND_NO_GO_KN = 25.0
ENTRANCE_M1_SWELL_ARC = Arc(35, 110)
ENTRANCE_M1_SWELL_TARGET_M = 0.8
ENTRANCE_M2_WIND_ARC = Arc(20, 80)
ENTRANCE_M2_TARGET_KN = 18.0

# South wind ocean runs (spec 4.3)
SOUTH_WIND_ARC = Arc(155, 210)
SOUTH_TARGET_KN = 20.0
SOUTH_SWELL_ARC = Arc(135, 205)
SOUTH_SWELL_SMALL_MAX_M = 1.0
SOUTH_SWELL_MEDIUM_MAX_M = 2.0
SOUTH_RUNS_SMALL = ("Bass Point", "Hill 60", "Boilers", "Bellambi")
SOUTH_RUNS_MEDIUM = ("Bellambi red buoy", "Hill 60")
SOUTH_RUNS_LARGE = ("Hill 60",)

# Hill 60 standalone swell run (spec 4.4)
HILL60_SWELL_TARGET_M = 2.0

# NE ocean runs (spec 4.5)
NE_WIND_ARC = Arc(20, 75)
NE_TRUE_ARC = Arc(34, 56)
NE_TARGET_KN = 15.0
NE_FLOOR_KN = 10.0
# Sustained-hours ladder at hourly resolution: the spec's 2.5 h middle rung
# rounds up to 3 whole hourly steps, so it collapses into the 3 h rule.
NE_LADDER = (
    (15.0, 2),  # last 2 hours all at 15 kn or more -> ready
    (10.0, 3),  # last 3 hours all at 10 kn or more -> ready
)

# Entrance reverse run near Boronia Ave (custom event, spec 4.8): opposite
# tide gate to the standard entrance runs (4.2) - works the incoming tide,
# not the run-out. W/NW wind; NW is prime, W is off-angle.
ENTRANCE_REVERSE_WIND_ARC = Arc(270, 315)
ENTRANCE_REVERSE_TRUE_ARC = Arc(295, 315)
ENTRANCE_REVERSE_TARGET_KN = 25.0
ENTRANCE_REVERSE_YELLOW_KN = 20.0
ENTRANCE_REVERSE_START_AFTER_LOW_H = 2.0
ENTRANCE_REVERSE_END_BEFORE_HIGH_H = 1.0

# Baysurf (custom event: east/NE swell, light wind, falling tide)
BAYSURF_SWELL_ARC = Arc(35, 90)
BAYSURF_SWELL_TARGET_M = 1.5
BAYSURF_SWELL_YELLOW_M = 1.5
BAYSURF_WIND_MAX_KN = 10.0
# Spec 4.7 says "light, up to 10 kn" with no floor; this lower bound isn't
# in the spec and predates this review (found undocumented, unnamed, as a
# bare literal in triggers.py). Preserved as-is since removing it would
# change which mornings qualify and that's Rob's call, not assumed here -
# just named and centralised like every other threshold in this file.
BAYSURF_WIND_MIN_KN = 4.0
BAYSURF_STRONG_WIND_ARC = Arc(225, 315)

# Swell compatibility for ocean downwinders (spec 4.6)
SWELL_IGNORE_BELOW_M = 0.5
SWELL_ALIGNED_MAX_DEG = 25.0
SWELL_ALIGNED_MAX_M = 1.5
SWELL_CROSS_KILL_M = 1.0

# Freshness caps (spec 3.5)
FORECAST_MIN_HORIZON_DAYS = 5
BOM_MAX_AGE_MIN = 45
HOLFUY_MAX_AGE_MIN = 30
HEARTBEAT_MAX_AGE_H = 8.0

# Holfuy station 366 reads roughly 10% high (spec 3.4)
HOLFUY_STATION = 366
HOLFUY_CORRECTION = 0.9

# https only: the plaintext first hop let a network MITM feed the pipeline
# forged observations that reach the public calendar and repo (5 Jul review).
BOM_JSON_URL = "https://www.bom.gov.au/fwo/IDN60801/IDN60801.94749.json"
BOM_JSON_URL_FALLBACK = "https://reg.bom.gov.au/fwo/IDN60801/IDN60801.94749.json"

# Live verification (spec 7)
LIVE_CONFIRM_FACTOR = 0.9
LIVE_MISS_FACTOR = 0.7
LIVE_REMINDER_MINUTES = 30

# live.yml's cron fires at :23 and :53 every hour, year-round. The :23 tick
# always runs (unchanged hourly cadence); the extra :53 tick only runs
# inside this local-clock window, so polling doubles to every 30 min while
# someone could plausibly be checking conditions and stays hourly overnight.
# Local hours, not UTC, so DST shifts the window for free through config.TZ
# - no seasonal cron edits needed (2026-08-04, missed 29 Jul lake event).
LIVE_FAST_POLL_START_HOUR = 5
LIVE_FAST_POLL_END_HOUR = 20
# Warn when the previous live poll was longer ago than this. The cron asks
# for 30 min during daylight; 75 min means at least two ticks were shed.
LIVE_POLL_GAP_WARN_MIN = 75.0

# Lake safety-net alert tiers (spec 7): live lake wind independent of the
# forecast. One source of truth for both the log text (live.py) and the
# actual calendar gate (gcal.py) - they used to be separate hardcoded copies
# that had drifted apart, so the 22 kn tier never actually reached the
# calendar (see 2026-08-04 review).
LAKE_ALERT_THRESHOLD_KN = 22.0
LAKE_ALERT_STRONG_KN = 25.0
# Was also 25.0, which made the loudest tier unreachable and left
# lake_recommendation's three tiers disagreeing with gcal._alert_tier's two
# (10 Aug 2026 review). Now a real third tier.
LAKE_ALERT_LOUD_KN = 30.0

# Live alerts beyond the lake (10 Aug 2026). The forecast missed a 32 kn NW
# blow entirely, so nothing was on the calendar, so nothing could be verified
# and nothing could fire: no forecast window meant no notification of any
# kind. These fire off live observations alone, whatever the forecast said.
# trigger_id -> (threshold kn, direction arc, run name, station preference).
# Thresholds are each trigger's own yellow floor so a live alert appears at
# the same bar a forecast window would have.
# trigger_id -> (threshold kn, arc, run name, station preference, group).
# `group` is the body of water. Alerts are keyed on it rather than on the
# trigger, because the lake bands abut exactly: a wind hunting either side of
# 260 deg matched Kanahooka at 258 and Berkeley at 262 and minted a separate
# event, with its own popup, for each - two phone pings for one blow.
LIVE_ALERT_TRIGGERS = {
    "lake_oakflats_berkeley": (18.0, Arc(170, 215), "Oak Flats to Berkeley", "lake", "lake"),
    "lake_west": (18.0, Arc(215, 285), "Kanahooka / Berkeley", "lake", "lake"),
    "lake_ne_rare": (22.5, Arc(20, 70), "Sailing Club to Oak Flats", "lake", "lake"),
    "entrance_ne": (16.2, Arc(20, 80), "Lake Entrance (NE wind)", "either", "entrance"),
    "entrance_reverse": (20.0, Arc(270, 315), "Entrance reverse run (Boronia Ave)", "either", "entrance"),
    "south_ocean": (18.0, Arc(155, 210), "South runs", "coast", "ocean"),
    "ne_ocean": (10.0, Arc(20, 75), "NE ocean runs", "coast", "ocean"),
}
# Live alert events are timed, not all-day: an all-day event's reminder is
# measured from local midnight, so it can never ping you at the moment the
# wind is actually blowing. Timed + a 0-minute popup does.
ALERT_DURATION_H = 2.0
# On a refresh the end tracks the latest observation plus this short tail,
# not another full ALERT_DURATION_H. Adding the full duration every poll grew
# a 5 h blow into a 7 h event, which misrepresents the record you look back
# at. ALERT_MAX_H is the hard ceiling however long the wind holds.
ALERT_TAIL_H = 1.0
ALERT_MAX_H = 8.0
# Start the event a minute ahead of now so the 0-minute popup is still in the
# future when Google receives it; a popup on a past start never fires.
ALERT_LEAD_S = 60
ALERT_REMINDER_MINUTES = 0

# Google Calendar colour ids (spec 6). Graphite for watch: visible, obviously
# not a call to go, and distinct from the three real grades.
COLOR_IDS = {"watch": "8", "yellow": "5", "green": "10", "red": "11"}
GRADE_ORDER = ("watch", "yellow", "green", "red")
# Existing downgrades (off-angle 4.5, cross swell 4.6) must never drop below
# yellow per spec 6. Watch sits below yellow, so downgrade() floors at yellow
# unless a caller explicitly opts into the watch tier.
DOWNGRADE_FLOOR = "yellow"

DATA_DIR = "data"
# 2: added the watch grade (windows[].grade gains "watch"), windows[].watch
# and windows[].tide_state, latest.json.expected_today and live.json.bias.
SCHEMA_VERSION = 2

# Forecast-vs-observed bias tracking (10 Aug 2026). All four models under-read
# a NW gale by ~13 kn at once and nothing recorded it, so the bust was
# invisible. The scan publishes what it expects hour by hour; the live job
# compares each observation against it and writes the gap to live.json.
BIAS_FLAG_KN = 8.0

# The Cloudflare Worker's GitHub PAT expires, and an expired one is the
# quietest failure in the system: dispatches simply stop, the schedule
# backstop keeps the scanner alive at a reduced rate, and nothing says why.
# GitHub returns the expiry on every response as the
# github-authentication-token-expiration header, so the Worker passes it
# through on each dispatch and the live job escalates as it approaches.
# Model wind correction, applied on ingest (fetch.fetch_wind) so triggers,
# expected_today and the snapshot all see the same numbers.
#
# CALIBRATED 11 Aug 2026 from 5 weeks against both stations - see
# scripts/calibrate_wind.py. Uncorrected, the models NEVER once reached the
# lake's 18 kn floor on any of the 15 hours the lake genuinely blew that
# hard: 0% recall, which is the entire reason runs were being missed.
#
# Backtested at the 18 kn floor, choosing on precision because a false green
# costs a drive while a missed day costs nothing:
#
#   lake   x1.45  precision 73%  recall 73%   (x1.57 was 63/80 - more recall,
#                                              more false calls; x1.35 was
#                                              worse on both)
#   ocean  +3.9   precision 67%  recall 17%   at the run floor, and 66/40 at
#                                              the watch floor against 2% raw
#
# The ocean gets an offset, not a multiplier, deliberately: every multiplier
# tried there sat near 50% precision, which is a coin toss, and the measured
# fit for that site is additive anyway.
#
# The entrance shares the lake's model grid cell (they return identical data,
# 671/671 model-hours), so it takes the lake's correction. There is no
# station at the entrance to verify that against - it is the weakest link
# here, and the first thing to revisit if entrance events start over-firing.
#
# Note this cuts both ways. Entrance mode 1 and Baysurf want LIGHT wind, so
# correcting upward makes them fire less, not more. That is the same fact
# pointing the other way: if the model under-reads, days that looked light
# were not.
#
# Rob's call to apply this on one winter: the lake's wind season IS winter,
# so the sample is the relevant one rather than a biased slice.
WIND_BIAS = {
    "lake": ("scale", 1.45),
    "entrance": ("scale", 1.45),
    "ocean": ("offset", 3.9),
}

# Direction correction, degrees added to the model bearing. Negative backs it
# anticlockwise.
#
# CALIBRATED 11 Aug 2026. At the lake the observed wind sits consistently
# backed of the model: median -12 deg, sd 9, and backed on 27 of 32 hours
# over 15 kn. Physically that is the lake channelling a gradient wind along
# its own axis. It named the wrong crossing 62% of the time; at -10 that
# halves. -10 beat -6 and -12 on the backtest at both 15 and 18 kn.
#
# The ocean measured -10 as well but with sd 25 rather than 9, so the number
# is inside its own noise - and the ocean bands are 55 deg wide, where the
# lake's are 25-45, so a 10 deg shift barely moves band membership. Measured,
# recorded, deliberately not applied.
WIND_DIR_BIAS = {"lake": -10.0, "entrance": -10.0, "ocean": 0.0}


PAT_WARN_DAYS = 14.0
# Inside this, the run fails outright: a red run, GitHub's failure email and
# a SCANNER BROKEN event, which are the loudest channels available and are
# already proven. Better a fortnight of noise than a silent safety net.
PAT_FAIL_DAYS = 3.0

# Google Calendar rejects a description over 8192 characters with a 400, and
# sync does not catch that, so one noisy day would take the whole calendar
# sync down with it. 120 near misses already produced 9867 characters.
WATCH_DIGEST_MAX_LINES = 40
WATCH_DIGEST_MAX_CHARS = 6000

HTTP_TIMEOUT_S = 30
HTTP_RETRIES = 3
# BOM rejects default library user agents with 403 (spec 3.3).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def env(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            from .errors import ConfigError

            raise ConfigError(f"required environment variable {name} is not set")
        return None
    return value


def validate() -> None:
    """Abort on impossible config before any fetch happens."""
    from .errors import ConfigError

    arcs = [arc for _, arc, _, _ in LAKE_RUNS.values()] + [
        ENTRANCE_M1_WIND_ARC,
        ENTRANCE_M1_SWELL_ARC,
        ENTRANCE_M2_WIND_ARC,
        SOUTH_WIND_ARC,
        SOUTH_SWELL_ARC,
        NE_WIND_ARC,
        NE_TRUE_ARC,
        BAYSURF_SWELL_ARC,
        BAYSURF_STRONG_WIND_ARC,
        ENTRANCE_REVERSE_WIND_ARC,
        ENTRANCE_REVERSE_TRUE_ARC,
    ]
    for arc in arcs:
        if not (0 <= arc.lo <= 360 and 0 <= arc.hi <= 360):
            raise ConfigError(f"direction arc out of range: {arc}")

    targets = [t for _, _, t, _ in LAKE_RUNS.values()] + [
        ENTRANCE_M1_SWELL_TARGET_M,
        ENTRANCE_M2_TARGET_KN,
        SOUTH_TARGET_KN,
        HILL60_SWELL_TARGET_M,
        NE_TARGET_KN,
        BAYSURF_SWELL_TARGET_M,
        ENTRANCE_REVERSE_TARGET_KN,
    ]
    if any(t <= 0 for t in targets):
        raise ConfigError("all trigger targets must be positive")

    if not 0 < WATCH_FACTOR < YELLOW_FACTOR < 1 < RED_FACTOR:
        raise ConfigError("grading factors must satisfy watch < yellow < 1 < red")

    if GRADE_ORDER[0] != "watch" or DOWNGRADE_FLOOR not in GRADE_ORDER:
        raise ConfigError("watch must be the lowest grade and the floor a real grade")
    if set(COLOR_IDS) != set(GRADE_ORDER):
        raise ConfigError("every grade needs a calendar colour")

    if not 1 <= MIN_MODELS_WATCH <= MIN_MODELS_AGREE:
        raise ConfigError("watch consensus must be at least 1 and no stricter than a window")

    if ENTRANCE_WIND_NO_GO_KN <= ENTRANCE_M1_WIND_MAX_KN:
        raise ConfigError("the entrance no-go wind must sit above its light-wind ceiling")
    if ENTRANCE_OFF_TIDE_DOWNGRADE < 0:
        raise ConfigError("off-tide downgrade cannot be negative")
    if not 0 <= ENTRANCE_NO_GO_BEFORE_LOW_H < 6:
        raise ConfigError("entrance no-go window must be a sane part of one ebb")

    if not LAKE_ALERT_THRESHOLD_KN < LAKE_ALERT_STRONG_KN < LAKE_ALERT_LOUD_KN:
        raise ConfigError("lake alert tiers must be strictly ascending")

    for tid, (kn, arc, name, station, group) in LIVE_ALERT_TRIGGERS.items():
        if kn <= 0:
            raise ConfigError(f"live alert threshold for {tid} must be positive")
        if not (0 <= arc.lo <= 360 and 0 <= arc.hi <= 360):
            raise ConfigError(f"live alert arc for {tid} out of range")
        if station not in ("lake", "coast", "either"):
            raise ConfigError(f"live alert station for {tid} must be lake|coast|either")
        if not group:
            raise ConfigError(f"live alert group for {tid} must name a body of water")

    for key, (form, value) in WIND_BIAS.items():
        if form not in ("scale", "offset"):
            raise ConfigError(f"wind bias for {key} must be scale or offset")
        if form == "scale" and not 0.5 <= value <= 3.0:
            raise ConfigError(f"wind bias scale for {key} is implausible: {value}")
        if form == "offset" and abs(value) > 20:
            raise ConfigError(f"wind bias offset for {key} is implausible: {value}")
    if {l.key for l in WIND_LOCATIONS} - set(WIND_BIAS):
        raise ConfigError("every wind location needs a bias entry, even a no-op")
    if {l.key for l in WIND_LOCATIONS} - set(WIND_DIR_BIAS):
        raise ConfigError("every wind location needs a direction bias, even a no-op")
    if any(abs(v) > 45 for v in WIND_DIR_BIAS.values()):
        raise ConfigError("a direction correction over 45 deg is not a bias, it is a bug")

    if not 0 < PAT_FAIL_DAYS < PAT_WARN_DAYS:
        raise ConfigError("PAT fail threshold must sit inside the warning window")
    if ALERT_DURATION_H <= 0 or ALERT_LEAD_S < 0 or ALERT_REMINDER_MINUTES < 0:
        raise ConfigError("live alert event timing must be non-negative and non-empty")
    if not 0 < ALERT_TAIL_H <= ALERT_DURATION_H <= ALERT_MAX_H:
        raise ConfigError("alert tail must fit inside the duration, and both inside the cap")

    if not 0 <= BAYSURF_WIND_MIN_KN < BAYSURF_WIND_MAX_KN:
        raise ConfigError("baysurf wind floor must sit below its ceiling")

    ladder = sorted(NE_LADDER, key=lambda r: r[0], reverse=True)
    if list(NE_LADDER) != ladder or len({r[0] for r in NE_LADDER}) != len(NE_LADDER):
        raise ConfigError("NE ladder must be ordered strongest first, no duplicates")
    if NE_FLOOR_KN > min(r[0] for r in NE_LADDER):
        raise ConfigError("NE floor cannot exceed the lowest ladder rung")

    if not 0 < ENTRANCE_REVERSE_YELLOW_KN < ENTRANCE_REVERSE_TARGET_KN:
        raise ConfigError("entrance reverse yellow floor must sit below its target")
    if ENTRANCE_REVERSE_END_BEFORE_HIGH_H < 0 or ENTRANCE_REVERSE_START_AFTER_LOW_H < 0:
        raise ConfigError("entrance reverse tide offsets cannot be negative")

    if not SWELL_IGNORE_BELOW_M < SWELL_CROSS_KILL_M <= SWELL_ALIGNED_MAX_M:
        raise ConfigError("swell compatibility thresholds are out of order")

    if MIN_MODELS_AGREE < 1 or MIN_MODELS_AGREE > len(MODELS):
        raise ConfigError("MIN_MODELS_AGREE must be within the model count")

    if not 0 <= LIVE_FAST_POLL_START_HOUR < LIVE_FAST_POLL_END_HOUR <= 24:
        raise ConfigError("live fast-poll window must be an ascending 24 h local range")
