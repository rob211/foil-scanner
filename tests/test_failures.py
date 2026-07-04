"""Every failure rule must actually raise (spec section 8)."""
from datetime import datetime, timedelta

import pytest

from foilscan import config, fetch
from foilscan.errors import ConfigError, FetchError, SchemaError, StaleDataError

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=config.TZ)


def om_wind_payload(now, null_model=None, drop_model=None, bad_speed=None):
    n = 24 * 7
    start = now.replace(hour=0)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(n)]
    hourly = {"time": times}
    for model_id in config.MODELS:
        if model_id == drop_model:
            continue
        speeds = [15.0] * n
        if model_id == null_model:
            speeds[30] = None  # tomorrow morning, inside the live range
        if bad_speed is not None:
            speeds[0] = bad_speed
        hourly[f"wind_speed_10m_{model_id}"] = speeds
        hourly[f"wind_direction_10m_{model_id}"] = [180.0] * n
        hourly[f"wind_gusts_10m_{model_id}"] = [20.0] * n
    return {"hourly": hourly}


def test_missing_model_is_schema_error(monkeypatch):
    payload = om_wind_payload(NOW, drop_model="ecmwf_ifs025")
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    with pytest.raises(SchemaError, match="ecmwf"):
        fetch.fetch_wind(config.LAKE, NOW)


def test_null_inside_five_days_is_schema_error(monkeypatch):
    payload = om_wind_payload(NOW, null_model="gfs_seamless")
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    with pytest.raises(SchemaError, match="null data"):
        fetch.fetch_wind(config.LAKE, NOW)


def test_unphysical_wind_is_schema_error(monkeypatch):
    payload = om_wind_payload(NOW, bad_speed=300.0)
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    with pytest.raises(SchemaError, match="physical range"):
        fetch.fetch_wind(config.LAKE, NOW)


def test_stale_forecast_is_stale_error(monkeypatch):
    # A response generated days ago ends before it can cover the horizon.
    payload = om_wind_payload(NOW - timedelta(days=4))
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    with pytest.raises(StaleDataError, match="stale or truncated"):
        fetch.fetch_wind(config.LAKE, NOW)


def test_forecast_not_covering_now_is_stale_error(monkeypatch):
    payload = om_wind_payload(NOW - timedelta(days=10))
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    with pytest.raises(StaleDataError, match="before now"):
        fetch.fetch_wind(config.LAKE, NOW)


def test_http_failure_becomes_fetch_error(monkeypatch):
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise OSError("connection refused")

    monkeypatch.setattr(fetch.requests, "get", boom)
    monkeypatch.setattr(fetch._time, "sleep", lambda s: None)
    with pytest.raises(FetchError, match="after 3 attempts"):
        fetch.get_json("https://example.invalid/x")
    assert calls["n"] == config.HTTP_RETRIES


def bom_payload(when):
    return {
        "observations": {
            "data": [
                {
                    "name": "Test Station",
                    "local_date_time_full": when.strftime("%Y%m%d%H%M%S"),
                    "wind_spd_kmh": 37,
                    "gust_kmh": 46,
                    "wind_dir": "SSW",
                }
            ]
        }
    }


def test_stale_bom_raises(monkeypatch):
    payload = bom_payload(NOW - timedelta(minutes=90))
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    with pytest.raises(StaleDataError, match="min old"):
        fetch.fetch_bom(NOW)


def test_bom_converts_kmh_to_knots(monkeypatch):
    payload = bom_payload(NOW - timedelta(minutes=10))
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    obs = fetch.fetch_bom(NOW)
    assert obs.speed_kn == pytest.approx(37 / 1.852, abs=0.01)
    assert obs.dir_deg == 202.5


def test_bom_unknown_direction_is_schema_error(monkeypatch):
    payload = bom_payload(NOW - timedelta(minutes=10))
    payload["observations"]["data"][0]["wind_dir"] = "??"
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    with pytest.raises(SchemaError, match="wind_dir"):
        fetch.fetch_bom(NOW)


def test_holfuy_applies_correction(monkeypatch):
    payload = {
        "stationName": "Lake Illawarra",
        "dateTime": (NOW - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S"),
        "wind": {"speed": 20.0, "gust": 26.0, "direction": 200.0},
    }
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    obs = fetch.fetch_holfuy("pw", NOW)
    assert obs.speed_kn == pytest.approx(18.0)
    assert obs.gust_kn == pytest.approx(26.0 * 0.9)


def test_holfuy_stale_raises(monkeypatch):
    payload = {
        "stationName": "Lake Illawarra",
        "dateTime": (NOW - timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S"),
        "wind": {"speed": 20.0, "gust": 26.0, "direction": 200.0},
    }
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    with pytest.raises(StaleDataError):
        fetch.fetch_holfuy("pw", NOW)


def test_config_validates(monkeypatch):
    config.validate()
    monkeypatch.setattr(config, "YELLOW_FACTOR", 1.5)
    with pytest.raises(ConfigError):
        config.validate()


def test_missing_env_is_config_error(monkeypatch):
    monkeypatch.delenv("FOIL_CALENDAR_ID", raising=False)
    with pytest.raises(ConfigError, match="FOIL_CALENDAR_ID"):
        config.env("FOIL_CALENDAR_ID")
