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


@dataclass(frozen=True)
class Tide:
    """A tide extremum. `time` and `sea_level_m` are interpolated between the
    hourly samples, so they are not tied to a sample boundary; `sample` is the
    hour the extremum was detected on, kept for traceability."""

    time: datetime
    sea_level_m: float
    sample: MarineHour


def _vertex(y0: float, y1: float, y2: float) -> float:
    """Offset in hours of a parabola's vertex from the middle of three evenly
    spaced samples. Zero when the curve is flat or the fit is degenerate."""
    denom = y0 - 2.0 * y1 + y2
    if denom == 0:
        return 0.0
    offset = 0.5 * (y0 - y2) / denom
    # A true interior extremum sits within half a step either side; anything
    # further means the three points aren't bracketing a peak, so don't move.
    return offset if -0.5 <= offset <= 0.5 else 0.0


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

    def _extrema(self, want_high: bool) -> list[Tide]:
        from datetime import timedelta

        from . import config

        s = self.hours
        out: list[Tide] = []
        offset = timedelta(minutes=config.TIDE_TIME_OFFSET_MIN)
        for i in range(1, len(s) - 1):
            a, b, c = s[i - 1].sea_level_m, s[i].sea_level_m, s[i + 1].sea_level_m
            hit = (b > a and b >= c) if want_high else (b < a and b <= c)
            if not hit:
                continue
            # Sub-hourly vertex: the hourly sample is only the bracket, the
            # real peak is somewhere inside +/- 30 min of it (10 Aug 2026).
            dx = _vertex(a, b, c)
            level = b - 0.25 * (a - c) * dx
            out.append(
                Tide(
                    time=s[i].time + timedelta(hours=dx) + offset,
                    sea_level_m=level,
                    sample=s[i],
                )
            )
        return out

    def high_tides(self) -> list[Tide]:
        """Local maxima of modelled sea level (spec 3.2), interpolated to
        sub-hourly times and shifted by config.TIDE_TIME_OFFSET_MIN."""
        return self._extrema(want_high=True)

    def low_tides(self) -> list[Tide]:
        """Local minima of modelled sea level (spec 4.8), interpolated to
        sub-hourly times and shifted by config.TIDE_TIME_OFFSET_MIN."""
        return self._extrema(want_high=False)


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
    high_tide_m: float | None = None  # tide-table height (chart datum), modelled
    confidence: str = "normal"
    live_status: str = "pending"
    event_id: str | None = None
    # Individual launch spots when one window covers several (south ocean runs).
    spots: list[str] | None = None
    # Why this only reached the watch tier ("one model only", "18 kn, needs
    # 20"). None on a real window.
    watch: str | None = None
    # "in gate" | "off tide" for the tide-gated entrance families, None where
    # the tide is irrelevant. Off-tide windows are downgraded, not deleted.
    tide_state: str | None = None

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
