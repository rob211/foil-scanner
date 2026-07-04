"""Fetchers. Each returns a validated snapshot or raises (spec sections 3, 8).

Wind and gust values are validated to 0-80 kn and swell to 0-15 m; anything
outside physical range means a unit or schema problem upstream, so it fails.
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta

import requests

from . import config
from .errors import FetchError, SchemaError, StaleDataError
from .models import (
    HourWind,
    MarineForecast,
    MarineHour,
    Observation,
    SunTimes,
    WindForecast,
)

KMH_PER_KN = 1.852

COMPASS = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5, "E": 90.0, "ESE": 112.5,
    "SE": 135.0, "SSE": 157.5, "S": 180.0, "SSW": 202.5, "SW": 225.0,
    "WSW": 247.5, "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
}


def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> dict:
    last = None
    for attempt in range(config.HTTP_RETRIES):
        try:
            resp = requests.get(
                url, params=params, headers=headers, timeout=config.HTTP_TIMEOUT_S
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - re-raised as FetchError below
            last = exc
            if attempt < config.HTTP_RETRIES - 1:
                _time.sleep(2**attempt)
    raise FetchError(f"GET {url} failed after {config.HTTP_RETRIES} attempts: {last}")


def _require(payload: dict, key: str, source: str):
    if key not in payload:
        raise SchemaError(f"{source}: missing key {key!r}")
    return payload[key]


def _parse_local(t: str) -> datetime:
    # Open-Meteo returns naive local ISO strings when timezone= is passed.
    return datetime.fromisoformat(t).replace(tzinfo=config.TZ)


def _check_coverage(times: list[datetime], source: str, now: datetime) -> None:
    """Open-Meteo's time axis starts at local midnight today, so freshness
    means coverage: the axis must include now and reach well ahead."""
    if times[0] > now:
        raise StaleDataError(
            f"{source}: forecast starts at {times[0].isoformat()}, after now; misaligned"
        )
    if times[-1] < now:
        raise StaleDataError(
            f"{source}: forecast ends at {times[-1].isoformat()}, before now; stale"
        )
    horizon = now + timedelta(days=config.FORECAST_MIN_HORIZON_DAYS)
    if times[-1] < horizon:
        raise StaleDataError(
            f"{source}: forecast only reaches {times[-1].isoformat()}, expected at "
            f"least {config.FORECAST_MIN_HORIZON_DAYS} days ahead; response is stale or truncated"
        )


def _check_range(value: float, lo: float, hi: float, what: str, source: str) -> float:
    if not isinstance(value, (int, float)):
        raise SchemaError(f"{source}: {what} is not numeric: {value!r}")
    if not lo <= value <= hi:
        raise SchemaError(f"{source}: {what}={value} outside physical range {lo}-{hi}")
    return float(value)


def fetch_wind(location, now: datetime) -> WindForecast:
    source = f"open-meteo wind ({location.key})"
    payload = get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": location.lat,
            "longitude": location.lon,
            "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "models": ",".join(config.MODELS),
            "wind_speed_unit": "kn",
            "timezone": "Australia/Sydney",
            "forecast_days": config.FORECAST_DAYS,
        },
    )
    hourly = _require(payload, "hourly", source)
    times = [_parse_local(t) for t in _require(hourly, "time", source)]
    if not times:
        raise SchemaError(f"{source}: empty time axis")
    _check_coverage(times, source, now)

    models: dict[str, list[HourWind]] = {}
    for model_id in config.MODELS:
        speeds = _require(hourly, f"wind_speed_10m_{model_id}", source)
        dirs = _require(hourly, f"wind_direction_10m_{model_id}", source)
        gusts = _require(hourly, f"wind_gusts_10m_{model_id}", source)
        if not (len(speeds) == len(dirs) == len(gusts) == len(times)):
            raise SchemaError(f"{source}: ragged arrays for model {model_id}")
        series = []
        for t, s, d, g in zip(times, speeds, dirs, gusts):
            # Nulls are legitimate outside a model's own span: before its
            # first step earlier today, and past its horizon at the tail.
            # A null between now and now+5d is a real problem.
            if s is None or d is None or g is None:
                if now <= t <= now + timedelta(days=5):
                    raise SchemaError(
                        f"{source}: model {model_id} has null data at {t.isoformat()}"
                    )
                continue
            series.append(
                HourWind(
                    time=t,
                    speed_kn=_check_range(s, 0, 80, "wind speed kn", source),
                    dir_deg=_check_range(d, 0, 360, "wind direction", source),
                    gust_kn=_check_range(g, 0, 120, "gust kn", source),
                )
            )
        if not series:
            raise SchemaError(f"{source}: model {model_id} returned no usable hours")
        models[model_id] = series
    return WindForecast(location_key=location.key, fetched_at=now, models=models)


def fetch_sun(now: datetime) -> SunTimes:
    # Astronomy does not vary by model, so one plain call at the lake point
    # serves every location (they are within ~20 km).
    source = "open-meteo sun"
    payload = get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": config.LAKE.lat,
            "longitude": config.LAKE.lon,
            "daily": "sunrise,sunset",
            "timezone": "Australia/Sydney",
            "forecast_days": config.FORECAST_DAYS,
        },
    )
    daily = _require(payload, "daily", source)
    days = {}
    for d, sr, ss in zip(
        _require(daily, "time", source),
        _require(daily, "sunrise", source),
        _require(daily, "sunset", source),
    ):
        days[date.fromisoformat(d)] = (_parse_local(sr), _parse_local(ss))
    if not days:
        raise SchemaError(f"{source}: no daily data")
    return SunTimes(days=days)


def fetch_marine(now: datetime) -> MarineForecast:
    source = "open-meteo marine"
    payload = get_json(
        "https://marine-api.open-meteo.com/v1/marine",
        params={
            "latitude": config.MARINE_POINT.lat,
            "longitude": config.MARINE_POINT.lon,
            "hourly": (
                "swell_wave_height,swell_wave_direction,"
                "swell_wave_period,sea_level_height_msl"
            ),
            "timezone": "Australia/Sydney",
            "forecast_days": config.FORECAST_DAYS,
        },
    )
    hourly = _require(payload, "hourly", source)
    times = [_parse_local(t) for t in _require(hourly, "time", source)]
    if not times:
        raise SchemaError(f"{source}: empty time axis")
    _check_coverage(times, source, now)

    heights = _require(hourly, "swell_wave_height", source)
    dirs = _require(hourly, "swell_wave_direction", source)
    periods = _require(hourly, "swell_wave_period", source)
    levels = _require(hourly, "sea_level_height_msl", source)
    if not (len(heights) == len(dirs) == len(periods) == len(levels) == len(times)):
        raise SchemaError(f"{source}: ragged arrays")

    hours = []
    for t, h, d, p, lvl in zip(times, heights, dirs, periods, levels):
        if h is None or d is None or p is None or lvl is None:
            if now <= t <= now + timedelta(days=5):
                raise SchemaError(f"{source}: null data at {t.isoformat()}")
            continue
        hours.append(
            MarineHour(
                time=t,
                swell_m=_check_range(h, 0, 15, "swell height m", source),
                swell_dir_deg=_check_range(d, 0, 360, "swell direction", source),
                swell_period_s=_check_range(p, 0, 30, "swell period s", source),
                sea_level_m=_check_range(lvl, -5, 5, "sea level m", source),
            )
        )
    return MarineForecast(fetched_at=now, hours=hours)


def fetch_bom(now: datetime) -> Observation:
    source = "BOM observations"
    headers = {"User-Agent": config.BROWSER_UA}
    try:
        payload = get_json(config.BOM_JSON_URL, headers=headers)
    except FetchError:
        payload = get_json(config.BOM_JSON_URL_FALLBACK, headers=headers)

    obs = _require(_require(payload, "observations", source), "data", source)
    if not obs:
        raise SchemaError(f"{source}: empty observation list")
    latest = obs[0]
    station = _require(latest, "name", source)

    when = datetime.strptime(
        _require(latest, "local_date_time_full", source), "%Y%m%d%H%M%S"
    ).replace(tzinfo=config.TZ)
    age_min = (now - when).total_seconds() / 60
    if age_min > config.BOM_MAX_AGE_MIN:
        raise StaleDataError(
            f"{source} ({station}): latest reading is {age_min:.0f} min old, "
            f"cap is {config.BOM_MAX_AGE_MIN} min"
        )

    speed_kmh = _require(latest, "wind_spd_kmh", source)
    gust_kmh = latest.get("gust_kmh")
    dir_txt = _require(latest, "wind_dir", source)
    if dir_txt == "CALM":
        dir_deg = None
    elif dir_txt in COMPASS:
        dir_deg = COMPASS[dir_txt]
    else:
        raise SchemaError(f"{source}: unknown wind_dir {dir_txt!r}")

    return Observation(
        station=station,
        time=when,
        speed_kn=_check_range(speed_kmh, 0, 150, "wind kmh", source) / KMH_PER_KN,
        gust_kn=(
            _check_range(gust_kmh, 0, 220, "gust kmh", source) / KMH_PER_KN
            if gust_kmh is not None
            else 0.0
        ),
        dir_deg=dir_deg,
    )


def fetch_holfuy(key: str, now: datetime) -> Observation:
    """Holfuy station 366. Values are corrected by 0.9 here, once, so every
    consumer sees corrected knots (spec 3.4)."""
    source = f"Holfuy {config.HOLFUY_STATION}"
    payload = get_json(
        "https://api.holfuy.com/live/",
        params={
            "s": config.HOLFUY_STATION,
            "pw": key,
            "m": "JSON",
            "tu": "C",
            "su": "knots",
        },
    )
    wind = _require(payload, "wind", source)
    when = datetime.fromisoformat(_require(payload, "dateTime", source)).replace(
        tzinfo=config.TZ
    )
    age_min = (now - when).total_seconds() / 60
    if age_min > config.HOLFUY_MAX_AGE_MIN:
        raise StaleDataError(
            f"{source}: latest reading is {age_min:.0f} min old, "
            f"cap is {config.HOLFUY_MAX_AGE_MIN} min"
        )
    return Observation(
        station=payload.get("stationName", f"Holfuy {config.HOLFUY_STATION}"),
        time=when,
        speed_kn=_check_range(_require(wind, "speed", source), 0, 80, "speed", source)
        * config.HOLFUY_CORRECTION,
        gust_kn=_check_range(_require(wind, "gust", source), 0, 120, "gust", source)
        * config.HOLFUY_CORRECTION,
        dir_deg=_check_range(
            _require(wind, "direction", source), 0, 360, "direction", source
        ),
    )
