from datetime import datetime, timedelta

import pytest

from foilscan import config, live
from foilscan.errors import StaleDataError
from foilscan.models import Observation

NOW = datetime(2026, 7, 6, 13, 0, tzinfo=config.TZ)


def window(trigger_id="south_ocean", start_h=12, end_h=16):
    day = NOW.date().isoformat()
    return {
        "trigger_id": trigger_id,
        "foil_key": f"{trigger_id}:{day}:{start_h:02d}",
        "start": NOW.replace(hour=start_h).isoformat(),
        "end": NOW.replace(hour=end_h).isoformat(),
        "peak_median_kn": 22.0,
        "event_id": "ev1",
    }


def obs(speed, deg, station="Test"):
    return Observation(
        station=station, time=NOW - timedelta(minutes=10),
        speed_kn=speed, gust_kn=speed + 3, dir_deg=deg,
    )


def test_heartbeat_raises_when_scan_cron_dead():
    latest = {"generated_at": (NOW - timedelta(hours=9)).isoformat()}
    with pytest.raises(StaleDataError, match="cron looks dead"):
        live.heartbeat(latest, NOW)
    live.heartbeat({"generated_at": (NOW - timedelta(hours=5)).isoformat()}, NOW)


def test_confirm_at_90pct_of_target():
    state, _ = live.status_for(window(), obs(18.5, 185), NOW)
    assert state == "confirmed"


def test_miss_when_started_and_under_70pct():
    state, _ = live.status_for(window(), obs(10.0, 185), NOW)
    assert state == "miss"


def test_miss_when_direction_out_of_band():
    state, _ = live.status_for(window(), obs(25.0, 45), NOW)
    assert state == "miss"


def test_pending_between_thresholds():
    state, _ = live.status_for(window(), obs(16.0, 185), NOW)
    assert state == "pending"


def test_pending_not_miss_before_window_starts():
    w = window(start_h=15, end_h=17)
    state, _ = live.status_for(w, obs(5.0, 185), NOW)
    assert state == "pending"


def test_lake_prefers_holfuy():
    w = window(trigger_id="lake_kanahooka")
    holfuy = obs(20, 250, station="Holfuy")
    picked, note = live.pick_obs(w, obs(15, 250, station="BOM"), holfuy)
    assert picked is holfuy and note is None
    picked, note = live.pick_obs(w, obs(15, 250, station="BOM"), None)
    assert picked.station == "BOM" and "BOM only" in note


def test_relevant_windows_selects_near_now():
    latest = {
        "windows": [
            window(start_h=12, end_h=16),  # in progress
            window(trigger_id="ne_ocean", start_h=14, end_h=16),  # within 1 h
            {**window(trigger_id="lake_berkeley", start_h=8, end_h=10)},  # done
        ]
    }
    got = [w["trigger_id"] for w in live.relevant_windows(latest, NOW)]
    assert got == ["south_ocean", "ne_ocean"]
