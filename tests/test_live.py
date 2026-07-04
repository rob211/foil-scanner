import json
from datetime import datetime, timedelta

import pytest

from foilscan import config, fetch, live, verdict
from foilscan.errors import FetchError, StaleDataError
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


# --- live.json contract -----------------------------------------------------


def _latest_on_disk(tmp_path, windows=()):
    payload = {
        "generated_at": (NOW - timedelta(hours=1)).isoformat(),
        "windows": list(windows),
    }
    (tmp_path / "latest.json").write_text(json.dumps(payload))
    return tmp_path


def _live_json(tmp_path):
    return json.loads((tmp_path / "live.json").read_text())


def test_build_live_serialises_obs_and_calm():
    calm = Observation(station="Bellambi", time=NOW, speed_kn=1.2, gust_kn=2.0, dir_deg=None)
    payload = verdict.build_live(NOW, calm, None, [], ["a note"])
    assert payload["obs"] == {
        "station": "Bellambi",
        "time": NOW.isoformat(),
        "speed_kn": 1.2,
        "gust_kn": 2.0,
        "dir_deg": None,
    }
    assert payload["holfuy"] is None
    assert payload["notes"] == ["a note"]
    assert payload["schema_version"] == config.SCHEMA_VERSION


def test_run_writes_live_json_even_without_windows(tmp_path, monkeypatch):
    data_dir = _latest_on_disk(tmp_path)
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5, station="Bellambi"))
    log = live.run(NOW, dry_run=False, data_dir=data_dir)
    got = _live_json(tmp_path)
    assert got["obs"]["station"] == "Bellambi"
    assert got["checks"] == []
    assert any("no windows near now" in l for l in log)
    assert any("live.json" in l for l in log)


def test_run_dry_run_does_not_write_live_json(tmp_path, monkeypatch):
    data_dir = _latest_on_disk(tmp_path)
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5))
    live.run(NOW, dry_run=True, data_dir=data_dir)
    assert not (tmp_path / "live.json").exists()


def test_run_records_checks_and_patches_calendar(tmp_path, monkeypatch):
    data_dir = _latest_on_disk(tmp_path, [window(start_h=12, end_h=16)])
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(21.0, 185, station="Bellambi"))
    monkeypatch.setattr(live.gcal, "service", lambda: object())
    monkeypatch.setattr(live.gcal, "calendar_id", lambda: "cal")
    patched = []
    monkeypatch.setattr(
        live, "apply_status", lambda svc, cal, w, state, line, dry: patched.append(state) or "ok"
    )
    live.run(NOW, dry_run=False, data_dir=data_dir)
    got = _live_json(tmp_path)
    assert patched == ["confirmed"]
    assert got["checks"] == [
        {
            "foil_key": f"south_ocean:{NOW.date().isoformat()}:12",
            "state": "confirmed",
            "live_line": got["checks"][0]["live_line"],
        }
    ]
    assert "21 kn" in got["checks"][0]["live_line"]


def test_run_bom_failure_still_publishes_obsless_live_json(tmp_path, monkeypatch):
    data_dir = _latest_on_disk(tmp_path)

    def boom(now):
        raise FetchError("GET bom failed after 3 attempts")

    monkeypatch.setattr(fetch, "fetch_bom", boom)
    with pytest.raises(FetchError):
        live.run(NOW, dry_run=False, data_dir=data_dir)
    got = _live_json(tmp_path)
    assert got["obs"] is None
    assert got["checks"] == []
    assert any("BOM fetch failed" in n for n in got["notes"])
