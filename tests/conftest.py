"""Synthetic snapshot builders. Tests drive the pure trigger engine with
these; no network anywhere in the suite."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from foilscan import config
from foilscan.models import (
    HourWind,
    MarineForecast,
    MarineHour,
    SunTimes,
    WindForecast,
)

DAY = date(2026, 7, 6)
NOW = datetime(2026, 7, 6, 6, 0, tzinfo=config.TZ)


def at(hour: int, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=config.TZ)


def mk_sun(days: int = 7, sunrise: int = 7, sunset: int = 17) -> SunTimes:
    out = {}
    for offset in range(days):
        d = DAY + timedelta(days=offset)
        out[d] = (at(sunrise, d), at(sunset, d))
    return SunTimes(days=out)


def mk_wind(
    hours: dict[int, tuple[float, float]],
    models: list[str] | None = None,
    location_key: str = "ocean",
    day: date = DAY,
) -> WindForecast:
    """hours: hour-of-day -> (speed_kn, dir_deg). Same series for each model
    unless a subset of model ids is given; other models stay near calm."""
    active = models if models is not None else list(config.MODELS)
    series: dict[str, list[HourWind]] = {}
    for model_id in config.MODELS:
        rows = []
        for h in range(0, 24):
            if model_id in active and h in hours:
                speed, deg = hours[h]
            else:
                speed, deg = 2.0, 315.0
            rows.append(
                HourWind(time=at(h, day), speed_kn=speed, dir_deg=deg, gust_kn=speed + 3)
            )
        series[model_id] = rows
    return WindForecast(location_key=location_key, fetched_at=NOW, models=series)


def mk_marine(
    swell_m: float = 0.3,
    swell_dir: float = 135.0,
    high_tide_hour: int | None = 13,
    day: date = DAY,
    swell_by_hour: dict[int, tuple[float, float]] | None = None,
) -> MarineForecast:
    hours = []
    for h in range(0, 24):
        if swell_by_hour and h in swell_by_hour:
            sm, sd = swell_by_hour[h]
        else:
            sm, sd = swell_m, swell_dir
        # Simple hump so exactly one local maximum sits at high_tide_hour.
        level = -abs(h - high_tide_hour) * 0.1 if high_tide_hour is not None else 0.0
        hours.append(
            MarineHour(
                time=at(h, day),
                swell_m=sm,
                swell_dir_deg=sd,
                swell_period_s=9.0,
                sea_level_m=level,
            )
        )
    return MarineForecast(fetched_at=NOW, hours=hours)


@pytest.fixture
def sun():
    return mk_sun()


@pytest.fixture
def calm_marine():
    return mk_marine()
