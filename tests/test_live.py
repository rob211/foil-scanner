import json
from datetime import datetime, timedelta

import pytest

from foilscan import config, fetch, gcal, live, verdict
from foilscan.errors import FetchError, StaleDataError
from foilscan.models import Observation

NOW = datetime(2026, 7, 6, 13, 0, tzinfo=config.TZ)


@pytest.fixture(autouse=True)
def _no_network_sun(monkeypatch):
    """live.run() fetches sunrise/sunset to gate the alerts. Nothing in this
    suite may touch the network, so every test gets a synthetic day: light
    from 07:00 to 17:00, which puts NOW (13:00) inside it. Tests that care
    about the gate call live.alerting_hours directly."""
    from foilscan.models import SunTimes

    days = {
        (NOW + timedelta(days=d)).date(): (
            (NOW + timedelta(days=d)).replace(hour=7),
            (NOW + timedelta(days=d)).replace(hour=17),
        )
        for d in range(-1, 9)
    }
    monkeypatch.setattr(fetch, "fetch_sun", lambda now: SunTimes(days=days))


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
    # Keyed on the water, not the crossing (see LIVE_ALERT_TRIGGERS).
    assert any(key.endswith(":lake") for _, key in seen)


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
    # Near misses ride along on a day that has a watch; they do not create a
    # digest on their own (that put a grey marker on almost every day).
    assert gcal.watch_digest_bodies([], [miss], NOW) == {}
    body = next(iter(gcal.watch_digest_bodies([_watch_window()], [miss], NOW).values()))
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


def test_bias_says_it_could_not_check_rather_than_going_blank():
    # An empty list read as "the models were right". Between midnight and the
    # day's first scan that hid every bust, in the one place built to catch
    # busts.
    rows = live.bias_rows({}, NOW, {"ocean": obs(20.0, 300)})
    assert len(rows) == 1
    assert rows[0]["flagged"] is None
    assert "no expectation" in rows[0]["reason"]

    stale = {"expected_today": [
        {"time": (NOW - timedelta(days=1)).replace(minute=0).isoformat(), "ocean": 12.0}
    ]}
    rows = live.bias_rows(stale, NOW, {"ocean": obs(30.0, 300)})
    assert rows[0]["flagged"] is None and "not " in rows[0]["reason"]


def test_one_alert_per_water_when_wind_sits_on_a_band_boundary():
    # 258 deg matched Kanahooka, 262 matched Berkeley, and each minted its own
    # event with its own popup.
    for deg in (258, 262):
        alerts = live.live_alerts(
            NOW, obs(26.0, deg, station="Bellambi"), None, covered=set(), daylight=True
        )
        lake = [a for a in alerts if a["foil_key"].endswith(":lake")]
        assert len(lake) == 1, deg


def test_alert_end_tracks_the_wind_without_running_away(monkeypatch):
    from datetime import datetime as _dt

    start = NOW
    existing = {
        "id": "e1",
        "extendedProperties": {"private": {"foil_key": "k"}},
        "start": {"dateTime": start.isoformat(), "timeZone": str(config.TZ)},
    }
    patched = {}

    class FakeEvents:
        def list(self, **kw):
            return type("R", (), {"execute": lambda s: {"items": [existing]}})()

        def patch(self, **kw):
            patched.update(kw["body"])
            return type("R", (), {"execute": lambda s: {}})()

    monkeypatch.setattr(gcal, "service", lambda: type("S", (), {"events": lambda s: FakeEvents()})())

    class FrozenNow(_dt):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(hours=5)

    monkeypatch.setattr(gcal, "datetime", FrozenNow)
    gcal.ensure_alert("run", obs(26.0, 275), start, "cal", "k")
    end = _dt.fromisoformat(patched["end"]["dateTime"])
    # 5 h in: end tracks the observation plus the tail, not +2 h per poll.
    assert end == start + timedelta(hours=5 + config.ALERT_TAIL_H)
    assert end - start <= timedelta(hours=config.ALERT_MAX_H)


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


# ------------------------------------------------------- alert daylight gate

def _sun(sunrise_h=7, sunset_h=17):
    from foilscan.models import SunTimes

    return SunTimes(
        days={NOW.date(): (NOW.replace(hour=sunrise_h), NOW.replace(hour=sunset_h))}
    )


def test_no_alerts_after_dark():
    # 26 kn on the lake at 21:00 is real wind and completely unrunnable, and
    # the per-day alert key would mint a fresh 0-minute popup after midnight.
    night = NOW.replace(hour=21)
    assert live.alerting_hours(night, _sun()) is False
    assert live.live_alerts(
        night, obs(26.0, 275, station="Bellambi"), None, covered=set(), daylight=False
    ) == []


def test_alerts_fire_in_daylight():
    noon = NOW.replace(hour=12)
    assert live.alerting_hours(noon, _sun()) is True
    alerts = live.live_alerts(
        noon, obs(26.0, 275, station="Bellambi"), None, covered=set(), daylight=True
    )
    assert "lake_berkeley" in {a["trigger_id"] for a in alerts}


def test_missing_sun_narrows_the_window_rather_than_alerting_all_night():
    # Fallback is the fast-poll window, so the failure mode is a missed ping,
    # never a 3am one.
    assert live.alerting_hours(NOW.replace(hour=3), None) is False
    assert live.alerting_hours(NOW.replace(hour=12), None) is True


# ------------------------------------------------ stale live-alert cleanup

def _managed(monkeypatch, existing):
    """sync() against a fake calendar holding `existing` foil_key -> event."""
    deleted, svc_calls = [], []

    class FakeEvents:
        def list(self, **kw):
            items = [
                {"id": k, "extendedProperties": {"private": {"foil_key": k}},
                 "summary": v}
                for k, v in existing.items()
            ]
            return type("R", (), {"execute": lambda s: {"items": items}})()

        def insert(self, **kw):
            svc_calls.append(("insert", kw["body"]["summary"]))
            return type("R", (), {"execute": lambda s: {"id": "new"}})()

        def patch(self, **kw):
            svc_calls.append(("patch", kw.get("eventId")))
            return type("R", (), {"execute": lambda s: {}})()

        def delete(self, **kw):
            deleted.append(kw["eventId"])
            return type("R", (), {"execute": lambda s: {}})()

    monkeypatch.setattr(gcal, "service", lambda: type("S", (), {"events": lambda s: FakeEvents()})())
    monkeypatch.setattr(gcal, "calendar_id", lambda: "cal")
    return deleted


def test_todays_live_alert_survives_a_scan(monkeypatch):
    today = f"live-alert:{NOW.date().isoformat()}:lake_kanahooka"
    deleted = _managed(monkeypatch, {today: "WIND NOW: Kanahooka"})
    gcal.sync([], NOW, [])
    assert today not in deleted


def test_yesterdays_live_alert_is_swept(monkeypatch):
    old = f"live-alert:{(NOW - timedelta(days=1)).date().isoformat()}:lake_berkeley"
    deleted = _managed(monkeypatch, {old: "WIND NOW!!: Berkeley run 26 kn W"})
    gcal.sync([], NOW, [])
    assert old in deleted


# ------------------------------------------ daylight gate: season and DST

def test_daylight_gate_follows_the_season():
    from foilscan.models import SunTimes

    # Sunset moves ~4 h between a Wollongong winter and summer. A fixed
    # clock-hour rule would be wrong at both ends; the gate uses the real
    # sunrise/sunset for the date, so it tracks automatically.
    winter_day = datetime(2026, 6, 21, 12, 0, tzinfo=config.TZ)
    summer_day = datetime(2026, 12, 21, 12, 0, tzinfo=config.TZ)
    winter = SunTimes(days={winter_day.date(): (
        winter_day.replace(hour=6, minute=58), winter_day.replace(hour=16, minute=55))})
    summer = SunTimes(days={summer_day.date(): (
        summer_day.replace(hour=5, minute=37), summer_day.replace(hour=19, minute=57))})

    # 18:30 is dark midwinter and broad daylight midsummer.
    assert live.alerting_hours(winter_day.replace(hour=18, minute=30), winter) is False
    assert live.alerting_hours(summer_day.replace(hour=18, minute=30), summer) is True
    # 06:00 is before a winter sunrise and after a summer one.
    assert live.alerting_hours(winter_day.replace(hour=6, minute=0), winter) is False
    assert live.alerting_hours(summer_day.replace(hour=6, minute=0), summer) is True


def test_daylight_gate_is_local_time_so_dst_is_free():
    # Sun times come back tagged Australia/Sydney, and `now` is built from
    # config.TZ, so the comparison is local-vs-local on both sides of a DST
    # changeover. 19:00 AEDT in January and 19:00 AEST in July are different
    # UTC instants; neither needs a seasonal code change.
    from foilscan.models import SunTimes

    for month, sunset_h, expected in ((1, 20, True), (7, 17, False)):
        day = datetime(2026, month, 15, 19, 0, tzinfo=config.TZ)
        sun = SunTimes(days={day.date(): (day.replace(hour=6), day.replace(hour=sunset_h))})
        assert live.alerting_hours(day, sun) is expected, month
        assert day.tzinfo is config.TZ


# ---------------------------------------------- QA fixes, 11 Aug 2026

def test_alert_start_is_measured_from_the_insert_not_the_run_start():
    # The popup is a 0-minute override, so a start already in the past never
    # fires. `now` is captured when the process begins; by the time the
    # insert happens the run has fetched BOM (3 retries x 30 s), Holfuy, sun,
    # marine and calendar credentials.
    stale_now = NOW - timedelta(minutes=5)
    body = gcal.alert_body("run", obs(26.0, 275), stale_now, "k", issued_at=NOW)
    start = datetime.fromisoformat(body["start"]["dateTime"])
    assert start > NOW
    # The observation text still uses the run's clock, which is what it is for.
    assert f"{stale_now:%H:%M}" in body["description"]


def test_watch_digest_stays_under_googles_description_limit():
    from foilscan.models import NearMiss

    misses = [
        NearMiss(trigger_id="south_ocean", date=NOW.date().isoformat(),
                 start=NOW.isoformat(), end=(NOW + timedelta(hours=2)).isoformat(),
                 reason="cross_swell", detail="cross swell 1.2 m E, 95 deg off the wind")
        for _ in range(400)
    ]
    body = next(iter(gcal.watch_digest_bodies([_watch_window()], misses, NOW).values()))
    assert len(body["description"]) <= config.WATCH_DIGEST_MAX_CHARS
    assert "more, see the dashboard" in body["description"]


def test_sync_finishes_the_pass_when_one_event_fails(monkeypatch):
    from foilscan.models import Window

    def win(h):
        return Window(
            trigger_id="lake_kanahooka", run_name="Kanahooka run",
            start=NOW.replace(hour=h), end=NOW.replace(hour=h + 1), grade="green",
            peak_time=NOW.replace(hour=h), peak_median_kn=22.0, direction_deg=240.0,
            models_agreeing=3, model_values={"ICON": 22.0},
        )

    windows, attempts, inserted = [win(9), win(11), win(13)], [], []

    class FakeEvents:
        def list(self, **kw):
            return type("R", (), {"execute": lambda s: {"items": []}})()

        def insert(self, **kw):
            attempts.append(kw["body"]["summary"])
            if len(attempts) == 2:          # blow up on the second one only
                raise RuntimeError("backendError")
            inserted.append(kw["body"]["summary"])
            return type("R", (), {"execute": lambda s: {"id": f"id{len(inserted)}"}})()

    monkeypatch.setattr(gcal, "service", lambda: type("S", (), {"events": lambda s: FakeEvents()})())
    monkeypatch.setattr(gcal, "calendar_id", lambda: "cal")

    with pytest.raises(gcal.CalendarError):
        gcal.sync(windows, NOW, [])
    # All three were attempted rather than the third being stranded behind
    # the second - that stranding is what poisoned the next live run.
    assert len(attempts) == 3
    assert len(inserted) == 2


def test_one_bad_window_does_not_kill_the_other_live_checks(tmp_path, monkeypatch):
    good = window(trigger_id="south_ocean")
    good["event_id"] = "ev-good"
    bad = dict(good, trigger_id="lake_kanahooka",
               foil_key="lake_kanahooka:bad", event_id=None)   # never synced
    data_dir = _latest_on_disk(tmp_path, windows=[bad, good])

    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(22.0, 180, station="Bellambi"))
    monkeypatch.setattr(live.gcal, "service", lambda: object())
    monkeypatch.setattr(live.gcal, "calendar_id", lambda: "cal")
    monkeypatch.setattr(live.gcal, "ensure_alert",
                        lambda *a, **k: True)
    seen = []

    def fake_apply(svc, cal, w, state, line, dry):
        # Same guard as the real apply_status: a window sync never got to
        # has no event to patch.
        if w.get("event_id") is None:
            raise gcal.CalendarError(f"window {w['foil_key']} has no event_id")
        seen.append(w["foil_key"])
        return "ok"

    monkeypatch.setattr(live, "apply_status", fake_apply)

    with pytest.raises(gcal.CalendarError):
        live.run(NOW, dry_run=False, data_dir=data_dir)
    assert good["foil_key"] in seen, "the healthy window was skipped"


def test_force_skips_the_cadence_gate():
    night_half_past = NOW.replace(hour=3, minute=45)
    assert live._should_run(night_half_past) is False
    assert live._should_run(night_half_past, force=True) is True


def test_verdict_reaches_disk_even_when_sync_fails(tmp_path, monkeypatch):
    # sync finishes its pass and reports failures at the end, so some windows
    # genuinely did get event ids. Skipping the re-write threw those away and
    # left the next live run unable to verify anything - the same failure the
    # sync change existed to stop, one layer up.
    from foilscan import main as scanner_main
    from foilscan.models import Window

    w = Window(
        trigger_id="lake_kanahooka", run_name="Kanahooka run",
        start=NOW.replace(hour=10), end=NOW.replace(hour=12), grade="green",
        peak_time=NOW.replace(hour=10), peak_median_kn=22.0, direction_deg=240.0,
        models_agreeing=3, model_values={"ICON": 22.0},
    )

    def boom(windows, now, notes, dry_run=False, near_misses=None):
        windows[0].event_id = "ev-that-synced-fine"   # this one worked
        raise gcal.CalendarError("1 calendar operation(s) failed")

    # Every source "succeeds" with a sentinel; only the lake family returns a
    # window, and the trigger engine itself is not under test here.
    monkeypatch.setattr(scanner_main, "_capture", lambda src, name, fn: object())
    monkeypatch.setattr(scanner_main, "_identical", lambda a, b: False)
    monkeypatch.setattr(scanner_main, "lake_windows", lambda *a: ([w], []))
    for fn in ("entrance_windows", "entrance_reverse_windows", "south_windows",
               "ne_windows", "baysurf_windows"):
        monkeypatch.setattr(scanner_main, fn, lambda *a: ([], []))
    monkeypatch.setattr(scanner_main, "hill60_windows", lambda *a: [])
    monkeypatch.setattr(scanner_main.verdict, "build_expected", lambda now, fc: [])
    monkeypatch.setattr(scanner_main.gcal, "sync", boom)

    with pytest.raises(gcal.CalendarError):
        scanner_main.scan(NOW, dry_run=False, data_dir=str(tmp_path))

    on_disk = json.loads((tmp_path / "latest.json").read_text())
    assert on_disk["windows"][0]["event_id"] == "ev-that-synced-fine"


# ------------------------------------------------------- PAT expiry alarm

def _expiry_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("FOIL_TOKEN_EXPIRES_AT", raising=False)
    else:
        monkeypatch.setenv("FOIL_TOKEN_EXPIRES_AT", value)


def test_pat_expiry_is_silent_while_there_is_plenty_of_time(monkeypatch):
    far = (NOW + timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S UTC")
    _expiry_env(monkeypatch, far)
    note, days = live.token_expiry_note(NOW)
    assert note is None and days == pytest.approx(90, abs=1)


def test_pat_expiry_warns_inside_the_window(monkeypatch):
    soon = (NOW + timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S UTC")
    _expiry_env(monkeypatch, soon)
    note, days = live.token_expiry_note(NOW)
    assert note is not None and "expires in 10 day" in note
    assert "wrangler secret put" in note          # says how to fix it
    assert config.PAT_FAIL_DAYS < days <= config.PAT_WARN_DAYS


def test_an_expired_pat_says_so_plainly(monkeypatch):
    gone = (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S UTC")
    _expiry_env(monkeypatch, gone)
    note, days = live.token_expiry_note(NOW)
    assert "EXPIRED" in note and days < 0


def test_an_absent_expiry_is_unknown_not_healthy(monkeypatch):
    # A scheduled backstop run carries no input. That means "nobody told us",
    # which must not be reported as "the token is fine".
    _expiry_env(monkeypatch, None)
    assert live.token_expiry_note(NOW) == (None, None)


def test_an_unreadable_expiry_is_reported_rather_than_ignored(monkeypatch):
    _expiry_env(monkeypatch, "next Tuesday")
    note, days = live.token_expiry_note(NOW)
    assert note is not None and days is None


def test_a_nearly_dead_pat_fails_the_run(tmp_path, monkeypatch):
    # The loudest channels available are a red run, GitHub's failure email
    # and a SCANNER BROKEN event; this reuses all three.
    data_dir = _latest_on_disk(tmp_path)
    _expiry_env(monkeypatch, (NOW + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S UTC"))
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5, station="Bellambi"))
    monkeypatch.setattr(live.gcal, "ensure_alert", lambda *a, **k: True)

    with pytest.raises(StaleDataError, match="PAT"):
        live.run(NOW, dry_run=False, data_dir=data_dir)

    # ...but live.json is written first, so the warning is on the dashboard
    # rather than lost with the failing run.
    assert any("PAT" in n for n in _live_json(tmp_path)["notes"])


def test_the_token_expiry_is_recorded_even_when_healthy(tmp_path, monkeypatch):
    # A healthy token that says nothing leaves "when does this expire?"
    # answerable only by going and looking, which is the habit this replaces.
    data_dir = _latest_on_disk(tmp_path)
    _expiry_env(monkeypatch, "2026-11-09 00:00:00 UTC")
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5, station="Bellambi"))
    monkeypatch.setattr(live.gcal, "ensure_alert", lambda *a, **k: True)
    live.run(NOW, dry_run=False, data_dir=data_dir)
    got = _live_json(tmp_path)
    assert got["token_expires_at"] == "2026-11-09 00:00:00 UTC"
    assert not any("PAT" in n for n in got["notes"])   # healthy: recorded, not shouted


def test_a_backstop_run_records_no_expiry_rather_than_a_stale_one(tmp_path, monkeypatch):
    data_dir = _latest_on_disk(tmp_path)
    _expiry_env(monkeypatch, None)
    monkeypatch.setattr(fetch, "fetch_bom", lambda now: obs(12.0, 157.5, station="Bellambi"))
    monkeypatch.setattr(live.gcal, "ensure_alert", lambda *a, **k: True)
    live.run(NOW, dry_run=False, data_dir=data_dir)
    assert _live_json(tmp_path)["token_expires_at"] is None
