"""Every failure rule must actually raise (spec section 8)."""
from datetime import datetime, timedelta, timezone

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
        # dateTime is UTC here (we pass utc=1), not Sydney local time.
        "dateTime": (NOW - timedelta(minutes=5)).astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "wind": {"speed": 20.0, "gust": 26.0, "direction": 200.0},
    }
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    obs = fetch.fetch_holfuy("pw", NOW)
    assert obs.speed_kn == pytest.approx(18.0)
    assert obs.gust_kn == pytest.approx(26.0 * 0.9)


def test_holfuy_stale_raises(monkeypatch):
    payload = {
        "stationName": "Lake Illawarra",
        "dateTime": (NOW - timedelta(minutes=60)).astimezone(timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
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


# ------------------------------------------------ model wind bias (11 Aug)

def test_bias_scales_the_lake_and_offsets_the_ocean():
    from foilscan import config, fetch

    assert fetch.apply_bias("lake", 10.0) == pytest.approx(14.5)
    assert fetch.apply_bias("entrance", 10.0) == pytest.approx(14.5)
    assert fetch.apply_bias("ocean", 10.0) == pytest.approx(13.9)
    # The entrance shares the lake's grid cell, so it must share its number.
    assert config.WIND_BIAS["entrance"] == config.WIND_BIAS["lake"]


def test_a_correction_never_produces_negative_wind():
    from foilscan import fetch

    # An offset does lift a modelled calm a little, which matches the
    # stations - they rarely read a true zero. What it must never do is go
    # below it, which a negative offset would.
    assert fetch.apply_bias("ocean", 0.0) >= 0.0
    assert fetch.apply_bias("lake", 0.0) == 0.0


def test_the_correction_also_tightens_the_light_wind_triggers():
    # Entrance mode 1 and Baysurf want light wind, so correcting upward makes
    # them FIRE LESS, not more. That is the same fact pointing the other way:
    # if the model under-reads, days that looked light were not.
    from foilscan import config, fetch

    # 10 Aug, entrance mode 1 at 07:00: ECMWF read 9.5 kn, under the 10 kn
    # ceiling. Corrected it is over, and BOM measured ~20 kn that morning.
    assert fetch.apply_bias("entrance", 9.5) > config.ENTRANCE_M1_WIND_MAX_KN


def test_the_correction_lifts_a_real_lake_day_over_its_floor():
    # The case the calibration exists for: on 10 Aug the models read ~12-13 kn
    # at the lake while it genuinely blew over 20, so nothing fired - not even
    # a watch. 18 kn is the lake's yellow floor.
    from foilscan import config, fetch

    assert fetch.apply_bias("lake", 12.5) >= 18.0
    # ...without dragging an ordinary breeze over it as well.
    assert fetch.apply_bias("lake", 9.0) < 15.0


def test_every_wind_location_has_a_bias_entry():
    # A location without one would raise a KeyError deep inside a fetch.
    from foilscan import config, fetch

    for loc in config.WIND_LOCATIONS:
        assert fetch.apply_bias(loc.key, 10.0) > 0


def test_direction_correction_backs_the_lake_and_leaves_the_ocean():
    from foilscan import config, fetch

    assert fetch.apply_dir_bias("lake", 264.0) == pytest.approx(254.0)
    assert fetch.apply_dir_bias("entrance", 264.0) == pytest.approx(254.0)
    # Measured at -10 too, but with sd 25 against the lake's 9 - inside its
    # own noise, and the ocean bands are wide enough that it barely moves
    # band membership. Recorded, not applied.
    assert fetch.apply_dir_bias("ocean", 264.0) == pytest.approx(264.0)


def test_direction_correction_wraps_through_north():
    from foilscan import fetch

    assert fetch.apply_dir_bias("lake", 5.0) == pytest.approx(355.0)
    assert 0 <= fetch.apply_dir_bias("lake", 0.0) < 360


# --------------------------------------------- Port Kembla wave buoy (3.6)

def _wave_payload(stamp="2026-08-11 12:00:00", hs=1.92, extra=None):
    row = {config.WAVE_PARAM_HS: hs, config.WAVE_PARAM_HMAX: 3.3,
           config.WAVE_PARAM_DIR: 186.0, config.WAVE_PARAM_TP: 8.9}
    if extra is not None:
        row.update(extra)
    return {"readings": {stamp: row}}


def test_wave_buoy_reads_the_newest_complete_reading(monkeypatch):
    # Trailing hours can be present but empty; the last key is not
    # necessarily the last reading.
    payload = {"readings": {
        "2026-08-11 11:00:00": {config.WAVE_PARAM_HS: 1.5, config.WAVE_PARAM_DIR: 90.0},
        "2026-08-11 12:00:00": {config.WAVE_PARAM_HS: 1.9, config.WAVE_PARAM_DIR: 186.0},
        "2026-08-11 13:00:00": {config.WAVE_PARAM_HS: None},
    }}
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    got = fetch.fetch_wave(datetime(2026, 8, 11, 12, 30, tzinfo=config.TZ))
    assert got.hs_m == 1.9 and got.dir_deg == 186.0


def test_wave_buoy_staleness_is_a_failure(monkeypatch):
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: _wave_payload())
    with pytest.raises(StaleDataError):
        fetch.fetch_wave(datetime(2026, 8, 11, 20, 0, tzinfo=config.TZ))


def test_wave_buoy_rejects_an_impossible_height(monkeypatch):
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: _wave_payload(hs=99.0))
    with pytest.raises(SchemaError):
        fetch.fetch_wave(datetime(2026, 8, 11, 12, 30, tzinfo=config.TZ))


def test_wave_buoy_rejects_an_empty_feed(monkeypatch):
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: {"readings": {}})
    with pytest.raises(SchemaError):
        fetch.fetch_wave(datetime(2026, 8, 11, 12, 30, tzinfo=config.TZ))


def test_wave_buoy_rejects_an_unreadable_timestamp(monkeypatch):
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: _wave_payload(stamp="whenever"))
    with pytest.raises(SchemaError):
        fetch.fetch_wave(datetime(2026, 8, 11, 12, 30, tzinfo=config.TZ))


def test_wave_buoy_tolerates_missing_optional_fields(monkeypatch):
    # Direction and period drop out sometimes; height alone is still useful.
    payload = {"readings": {"2026-08-11 12:00:00": {config.WAVE_PARAM_HS: 1.2}}}
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    got = fetch.fetch_wave(datetime(2026, 8, 11, 12, 30, tzinfo=config.TZ))
    assert got.hs_m == 1.2 and got.dir_deg is None and got.peak_period_s is None
