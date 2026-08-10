import json
from datetime import datetime, timedelta

import pytest

from foilscan import config, fetch, gcal, live, verdict
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


def test_should_run_always_true_on_the_hourly_tick():
    # minute < 30 is the original :23 cron tick - always runs, day or night.
    assert live._should_run(NOW.replace(hour=13, minute=23)) is True
    assert live._should_run(NOW.replace(hour=2, minute=23)) is True


def test_should_run_true_on_half_hour_tick_during_daylight():
    assert live._should_run(NOW.replace(hour=13, minute=53)) is True


def test_should_run_false_on_half_hour_tick_outside_daylight():
    assert live._should_run(NOW.replace(hour=22, minute=53)) is False
    assert live._should_run(NOW.replace(hour=4, minute=53)) is False


def test_should_run_daylight_window_follows_dst_via_local_hour():
    # 19:53 local sits inside the 05:00-20:00 window either way, but AEDT
    # (Jan, DST on) and AEST (Jul, DST off) are different UTC offsets - this
    # only agrees on both dates if the gate compares local hour, not UTC.
    aedt = datetime(2026, 1, 15, 19, 53, tzinfo=config.TZ)
    aest = datetime(2026, 7, 6, 19, 53, tzinfo=config.TZ)
    assert aedt.utcoffset() != aest.utcoffset()
    assert live._should_run(aedt) is True
    assert live._should_run(aest) is True


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


def test_lake_recommendation_appears_above_threshold():
    rec = live.lake_recommendation(obs(27.0, 220, station="Holfuy"))
    assert rec is not None
    assert "27 kn" in rec and "lake" in rec.lower()


def test_alert_body_uses_live_observation():
    body = gcal.alert_body("the lake", obs(27.0, 220, station="Holfuy"), NOW, "k")
    assert body["summary"].startswith("WIND NOW")
    assert "27 kn" in body["summary"]
    assert "Holfuy" in body["description"]


def test_alert_is_timed_with_a_popup_so_it_can_actually_ring():
    # The whole point of the safety net. It used to be an all-day event with
    # an empty overrides list, which cannot notify at all, and whose reminder
    # offset would have been measured from midnight even if populated.
    body = gcal.alert_body("the lake", obs(27.0, 220, station="Holfuy"), NOW, "k")
    assert "dateTime" in body["start"] and "date" not in body["start"]
    assert body["reminders"]["overrides"] == [
        {"method": "popup", "minutes": config.ALERT_REMINDER_MINUTES}
    ]
    # Start must be ahead of now or Google drops the popup on the floor.
    assert datetime.fromisoformat(body["start"]["dateTime"]) > NOW


def test_alert_tiers_are_all_reachable():
    # LAKE_ALERT_LOUD_KN used to equal LAKE_ALERT_STRONG_KN, so the loudest
    # tier could never fire and this function disagreed with
    # live.lake_recommendation about how many tiers there were.
    tiers = [
        gcal._alert_tier(config.LAKE_ALERT_THRESHOLD_KN),
        gcal._alert_tier(config.LAKE_ALERT_STRONG_KN),
        gcal._alert_tier(config.LAKE_ALERT_LOUD_KN),
    ]
    assert tiers == ["", "!!", "!!!"]


def test_all_day_events_end_the_next_day():
    # Google reads an all-day end.date as exclusive; start == end is a
    # zero-length event it rejects.
    assert gcal._next_day("2026-08-10") == "2026-08-11"


def test_ensure_alert_inserts_a_timed_event(monkeypatch):
    # The 10 Aug 2026 alert reached the calendar as an all-day event with
    # start.date == end.date. Google accepted it rather than erroring, so the
    # scanner logged success while the event never rendered anywhere and had
    # no reminder. Timed events cannot land in that state.
    calls = []

    class FakeEvents:
        def list(self, **kw):
            class R:
                def execute(self_):
                    return {"items": []}
            return R()

        def insert(self, **kw):
            calls.append(kw["body"]["summary"])
            class R:
                def execute(self_):
                    return {}
            return R()

    class FakeSvc:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(gcal, "service", lambda: FakeSvc())
    created = gcal.ensure_alert(
        "the lake", obs(22.0, 220, station="Holfuy"), NOW, "cal", "lake-alert:x"
    )
    assert created is True
    assert calls == ["WIND NOW: the lake 22 kn SW"]


def test_sync_does_not_delete_todays_lake_alert(monkeypatch):
    # sync()'s cleanup treats anything not in this scan's computed windows
    # as stale, which used to sweep up live alerts too - a real alert from
    # live.py's hourly safety net would be gone by the next 2-hourly scan
    # cron, regardless of whether the wind was still blowing. broken:* must
    # still be swept (that is how it self-heals on the next good scan).
    existing = {
        "live-alert:2026-07-06:lake_kanahooka": {
            "id": "ev-lake",
            "summary": "LAKE ALERT: 27 kn at Holfuy",
            "extendedProperties": {"private": {"foil_key": "live-alert:2026-07-06:lake_kanahooka"}},
        },
        "broken:2026-07-06": {
            "id": "ev-broken",
            "summary": "SCANNER BROKEN: ...",
            "extendedProperties": {"private": {"foil_key": "broken:2026-07-06"}},
        },
    }
    deleted = []

    class FakeEvents:
        def list(self, **kw):
            class R:
                def execute(self_):
                    return {"items": list(existing.values())}
            return R()

        def delete(self, calendarId, eventId):
            deleted.append(eventId)
            class R:
                def execute(self_):
                    return {}
            return R()

    class FakeSvc:
        def events(self):
            return FakeEvents()

    monkeypatch.setattr(gcal, "service", lambda: FakeSvc())
    monkeypatch.setattr(gcal, "calendar_id", lambda: "cal")
    gcal.sync([], NOW, [], dry_run=False)
    assert "ev-lake" not in deleted
    assert "ev-broken" in deleted


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


def test_run_fetches_holfuy_even_without_a_lake_window(tmp_path, monkeypatch):
    # The safety-net alert needs a live reading on every hour, not just the
    # hours the forecast already called a lake window for.
    data_dir = _latest_on_disk(tmp_path)
    monkeypatch.setenv("HOLFUY_KEY", "testkey")
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5, station="Bellambi"))
    monkeypatch.setattr(fetch, "fetch_holfuy", lambda key, now: obs(14.0, 250, station="Holfuy"))
    live.run(NOW, dry_run=False, data_dir=data_dir)
    got = _live_json(tmp_path)
    assert got["holfuy"]["station"] == "Holfuy"


def test_run_noop_on_off_hours_half_hour_tick_touches_nothing(tmp_path, monkeypatch):
    # tmp_path has no latest.json at all - if this didn't short-circuit
    # before verdict.load_latest, it would raise instead of skipping clean.
    night_tick = NOW.replace(hour=22, minute=53)
    fetch_calls = []
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: fetch_calls.append("bom"))
    log = live.run(night_tick, dry_run=False, data_dir=tmp_path)
    assert fetch_calls == []
    assert not (tmp_path / "live.json").exists()
    assert any("skipped" in l and "22:53" in l for l in log)


def test_run_fires_lake_alert_with_no_forecast_window_active(tmp_path, monkeypatch):
    # The exact case the safety net exists for: strong live wind the
    # forecast never called, so `todays` is empty and cal_id must not stay
    # None (it did: the alert silently failed every time this fired).
    data_dir = _latest_on_disk(tmp_path)
    monkeypatch.setenv("HOLFUY_KEY", "testkey")
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5, station="Bellambi"))
    monkeypatch.setattr(fetch, "fetch_holfuy", lambda key, now: obs(30.0, 250, station="Holfuy"))
    monkeypatch.setattr(live.gcal, "service", lambda: object())
    monkeypatch.setattr(live.gcal, "calendar_id", lambda: "cal")
    seen = []
    monkeypatch.setattr(
        live.gcal,
        "ensure_alert",
        lambda run_name, o, now, cal_id, key, detail="": seen.append((cal_id, key))
        or True,
    )
    live.run(NOW, dry_run=False, data_dir=data_dir)
    # 30 kn WSW on the lake with nothing forecast: the Kanahooka run's live
    # alert fires, and cal_id must not stay None (it did - the alert silently
    # failed every time this fired).
    assert seen
    assert all(cal_id == "cal" for cal_id, _ in seen)
    assert any(key.endswith("lake_kanahooka") for _, key in seen)


def test_alert_prefers_the_windier_station_not_just_holfuy(tmp_path, monkeypatch):
    # 10 Aug 2026, 10:39: Holfuy read 19 kn mid-lake while Bellambi was doing
    # 31.9. `holfuy or bom` meant the coastal reading was discarded and
    # nothing fired for another two and a half hours.
    data_dir = _latest_on_disk(tmp_path)
    monkeypatch.setenv("HOLFUY_KEY", "testkey")
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(31.9, 300, station="Bellambi"))
    monkeypatch.setattr(fetch, "fetch_holfuy", lambda key, now: obs(19.0, 300, station="Holfuy"))
    monkeypatch.setattr(live.gcal, "service", lambda: object())
    monkeypatch.setattr(live.gcal, "calendar_id", lambda: "cal")
    monkeypatch.setattr(live, "_tide_notes", lambda marine, now: {})
    monkeypatch.setattr(fetch, "fetch_marine", lambda now: None)
    fired = []
    monkeypatch.setattr(
        live.gcal,
        "ensure_alert",
        lambda run_name, o, now, cal_id, key, detail="": fired.append(run_name) or True,
    )
    live.run(NOW, dry_run=False, data_dir=data_dir)
    assert "Entrance reverse run (Boronia Ave)" in fired


def test_no_alert_when_a_forecast_window_already_covers_it(tmp_path, monkeypatch):
    # A live window is the verification job's business; double-notifying is
    # worse than not notifying.
    alerts = live.live_alerts(
        NOW,
        obs(31.9, 300, station="Bellambi"),
        obs(31.6, 300, station="Holfuy"),
        covered={"entrance_reverse"},
    )
    assert "entrance_reverse" not in {a["trigger_id"] for a in alerts}


def test_run_holfuy_failure_without_lake_window_is_soft(tmp_path, monkeypatch):
    data_dir = _latest_on_disk(tmp_path)
    monkeypatch.setenv("HOLFUY_KEY", "testkey")
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5, station="Bellambi"))

    def boom(key, now):
        raise FetchError("holfuy down")

    monkeypatch.setattr(fetch, "fetch_holfuy", boom)
    log = live.run(NOW, dry_run=False, data_dir=data_dir)
    got = _live_json(tmp_path)
    assert got["holfuy"] is None
    assert any("Holfuy fetch failed" in n for n in got["notes"])
    assert any("Holfuy fetch failed" in l for l in log)


def test_run_holfuy_failure_with_live_lake_window_raises(tmp_path, monkeypatch):
    data_dir = _latest_on_disk(tmp_path, [window(trigger_id="lake_kanahooka", start_h=12, end_h=16)])
    monkeypatch.setenv("HOLFUY_KEY", "testkey")
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5, station="Bellambi"))

    def boom(key, now):
        raise FetchError("holfuy down")

    monkeypatch.setattr(fetch, "fetch_holfuy", boom)
    with pytest.raises(FetchError):
        live.run(NOW, dry_run=False, data_dir=data_dir)


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


# ---------------------------------------------------------- watch digest

def _watch_window(start_h=10, run_name="Kanahooka run", trigger_id="lake_kanahooka"):
    from foilscan.models import Window

    return Window(
        trigger_id=trigger_id,
        run_name=run_name,
        start=NOW.replace(hour=start_h),
        end=NOW.replace(hour=start_h + 2),
        grade="watch",
        peak_time=NOW.replace(hour=start_h),
        peak_median_kn=17.0,
        direction_deg=240.0,
        models_agreeing=1,
        model_values={"ICON": 17.0},
        watch="1 of 4 models only (needs 2)",
    )


def test_watch_windows_become_one_digest_not_events(monkeypatch):
    plan = gcal.sync(
        [_watch_window(10), _watch_window(14, "Berkeley run", "lake_berkeley")],
        NOW,
        [],
        dry_run=True,
    )
    assert len(plan) == 1
    assert "watch:" in plan[0]
    assert "Kanahooka run" in plan[0] and "Berkeley run" in plan[0]


def test_watch_digest_is_a_valid_all_day_range():
    bodies = gcal.watch_digest_bodies([_watch_window()], [], NOW)
    body = next(iter(bodies.values()))
    assert body["start"]["date"] == NOW.date().isoformat()
    assert body["end"]["date"] == (NOW.date() + timedelta(days=1)).isoformat()
    assert body["colorId"] == config.COLOR_IDS["watch"]
    assert "1 of 4 models only" in body["description"]


def test_watch_digest_carries_near_misses_too():
    from foilscan.models import NearMiss

    miss = NearMiss(
        trigger_id="south_ocean",
        date=NOW.date().isoformat(),
        start=NOW.replace(hour=11).isoformat(),
        end=NOW.replace(hour=13).isoformat(),
        reason="cross_swell",
        detail="cross swell 1.2 m E, 95 deg off the wind",
    )
    bodies = gcal.watch_digest_bodies([], [miss], NOW)
    body = next(iter(bodies.values()))
    assert "cross swell" in body["description"]


def test_real_windows_are_not_folded_into_the_digest():
    from foilscan.models import Window

    real = Window(
        trigger_id="lake_kanahooka", run_name="Kanahooka run",
        start=NOW.replace(hour=10), end=NOW.replace(hour=12), grade="green",
        peak_time=NOW.replace(hour=10), peak_median_kn=22.0, direction_deg=240.0,
        models_agreeing=3, model_values={"ICON": 22.0},
    )
    plan = gcal.sync([real, _watch_window(14)], NOW, [], dry_run=True)
    assert len(plan) == 2
    assert any("[green]" in line for line in plan)
    assert any("watch:" in line for line in plan)


# ------------------------------------------------------------- model bias

def test_bias_flags_a_model_bust():
    latest = {
        "expected_today": [
            {"time": NOW.replace(minute=0).isoformat(), "lake": 15.0, "ocean": 15.2}
        ]
    }
    rows = live.bias_rows(latest, NOW, {"ocean": obs(31.9, 300, station="Bellambi")})
    assert len(rows) == 1
    assert rows[0]["gap_kn"] == pytest.approx(16.7, abs=0.05)
    assert rows[0]["flagged"] is True


def test_bias_is_quiet_when_the_models_are_right():
    latest = {
        "expected_today": [
            {"time": NOW.replace(minute=0).isoformat(), "ocean": 20.0}
        ]
    }
    rows = live.bias_rows(latest, NOW, {"ocean": obs(21.5, 300, station="Bellambi")})
    assert rows[0]["flagged"] is False


def test_bias_survives_a_latest_json_from_before_the_field_existed():
    assert live.bias_rows({}, NOW, {"ocean": obs(20.0, 300)}) == []


# ----------------------------------------------------------- poll gap

def test_poll_gap_is_reported(tmp_path):
    verdict.write_live(
        verdict.build_live(NOW - timedelta(minutes=142), None, None, [], []), tmp_path
    )
    note = live.poll_gap_note(tmp_path, NOW)
    assert note is not None and "142 min" in note


def test_no_poll_gap_note_on_a_normal_cadence(tmp_path):
    verdict.write_live(
        verdict.build_live(NOW - timedelta(minutes=30), None, None, [], []), tmp_path
    )
    assert live.poll_gap_note(tmp_path, NOW) is None


def test_watch_windows_are_not_live_verified():
    latest = {
        "windows": [
            {
                "trigger_id": "lake_kanahooka",
                "grade": "watch",
                "start": NOW.replace(hour=12).isoformat(),
                "end": NOW.replace(hour=16).isoformat(),
            }
        ]
    }
    # No event_id to patch, nothing to verify: it must not reach the loop.
    assert live.relevant_windows(latest, NOW) == []


def test_alert_rows_stay_json_serialisable():
    # live_alerts carries the chosen Observation in memory for the calendar
    # writer; live.json must not choke on it.
    alerts = live.live_alerts(
        NOW, obs(31.9, 300, station="Bellambi"), None, covered=set()
    )
    assert alerts and "obs" in alerts[0]
    payload = verdict.build_live(NOW, None, None, [], [], alerts, [])
    json.dumps(payload)
    assert "obs" not in payload["alerts"][0]
    assert payload["alerts"][0]["station"] == "Bellambi"
