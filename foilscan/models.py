"""Typed snapshots and verdict shapes passed between fetchers, the trigger
engine and the outputs. The trigger engine only sees these, never raw HTTP."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class HourWind:
    time: datetime  # tz-aware, Australia/Sydney
    speed_kn: float
    gust_kn: float
    dir_deg: float


@dataclass
class WindForecast:
    location_key: str
    fetched_at: datetime
    # model id -> hourly series, all models same timestamps
    models: dict[str, list[HourWind]]


@dataclass(frozen=True)
class MarineHour:
    time: datetime
    swell_m: float
    swell_dir_deg: float
    swell_period_s: float
    sea_level_m: float


@dataclass
class MarineForecast:
    fetched_at: datetime
    hours: list[MarineHour]

    def at(self, t: datetime) -> MarineHour:
        for h in self.hours:
            if h.time == t:
                return h
        from .errors import SchemaError

        raise SchemaError(f"no marine data for {t.isoformat()}")

    def high_tides(self) -> list[MarineHour]:
        """Local maxima of modelled sea level (spec 3.2). Hourly resolution,
        so timing is +/- 30 min until calibrated."""
        highs = []
        s = self.hours
        for i in range(1, len(s) - 1):
            if s[i].sea_level_m > s[i - 1].sea_level_m and s[i].sea_level_m >= s[i + 1].sea_level_m:
                highs.append(s[i])
        return highs


@dataclass
class SunTimes:
    # date -> (sunrise, sunset), tz-aware
    days: dict[date, tuple[datetime, datetime]]

    def daylight(self, t: datetime) -> bool:
        d = self.days.get(t.date())
        if d is None:
            from .errors import SchemaError

            raise SchemaError(f"no sunrise/sunset for {t.date().isoformat()}")
        sunrise, sunset = d
        return sunrise <= t < sunset


@dataclass(frozen=True)
class Observation:
    station: str
    time: datetime
    speed_kn: float
    gust_kn: float
    dir_deg: float | None  # None when calm


@dataclass
class SourceStatus:
    ok: bool
    fetched_at: str | None = None
    detail: str | None = None
    error: str | None = None


@dataclass
class Window:
    trigger_id: str
    run_name: str
    start: datetime
    end: datetime
    grade: str  # yellow | green | red
    peak_time: datetime
    peak_median_kn: float
    direction_deg: float
    models_agreeing: int
    model_values: dict[str, float | None]
    title_tags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    swell_m: float | None = None
    swell_dir_deg: float | None = None
    high_tide: str | None = None
    confidence: str = "normal"
    live_status: str = "pending"
    event_id: str | None = None

    @property
    def foil_key(self) -> str:
        return f"{self.trigger_id}:{self.start.date().isoformat()}:{self.start:%H}"


@dataclass
class NearMiss:
    trigger_id: str
    date: str
    start: str
    end: str
    reason: str
    detail: str
