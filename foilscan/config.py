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
    "lake_kanahooka": ("Kanahooka run", Arc(215, 260), 20.0, False),
    "lake_berkeley": ("Berkeley run", Arc(260, 285), 20.0, False),
    "lake_ne_rare": ("Sailing Club to Oak Flats", Arc(20, 70), 25.0, True),
}

# Lake Entrance (spec 4.2)
ENTRANCE_TIDE_WINDOW_H = 2.0
ENTRANCE_M1_WIND_MAX_KN = 10.0
ENTRANCE_M1_WIND_ARC = Arc(200, 340)
ENTRANCE_M1_CALM_KN = 5.0
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

BOM_JSON_URL = "http://www.bom.gov.au/fwo/IDN60801/IDN60801.94749.json"
BOM_JSON_URL_FALLBACK = "http://reg.bom.gov.au/fwo/IDN60801/IDN60801.94749.json"

# Live verification (spec 7)
LIVE_CONFIRM_FACTOR = 0.9
LIVE_MISS_FACTOR = 0.7
LIVE_REMINDER_MINUTES = 30

# Google Calendar colour ids (spec 6)
COLOR_IDS = {"yellow": "5", "green": "10", "red": "11"}
GRADE_ORDER = ("yellow", "green", "red")

DATA_DIR = "data"
SCHEMA_VERSION = 1

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
    ]
    if any(t <= 0 for t in targets):
        raise ConfigError("all trigger targets must be positive")

    if not 0 < YELLOW_FACTOR < 1 < RED_FACTOR:
        raise ConfigError("grading factors must satisfy yellow < 1 < red")

    ladder = sorted(NE_LADDER, key=lambda r: r[0], reverse=True)
    if list(NE_LADDER) != ladder or len({r[0] for r in NE_LADDER}) != len(NE_LADDER):
        raise ConfigError("NE ladder must be ordered strongest first, no duplicates")
    if NE_FLOOR_KN > min(r[0] for r in NE_LADDER):
        raise ConfigError("NE floor cannot exceed the lowest ladder rung")

    if not SWELL_IGNORE_BELOW_M < SWELL_CROSS_KILL_M <= SWELL_ALIGNED_MAX_M:
        raise ConfigError("swell compatibility thresholds are out of order")

    if MIN_MODELS_AGREE < 1 or MIN_MODELS_AGREE > len(MODELS):
        raise ConfigError("MIN_MODELS_AGREE must be within the model count")
