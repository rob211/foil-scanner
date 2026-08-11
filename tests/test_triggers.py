import math

import pytest
from datetime import timedelta

from conftest import DAY, NOW, at, mk_marine, mk_sun, mk_wind

from foilscan import config
from foilscan.models import MarineForecast, MarineHour
from foilscan.triggers import (
    HOUR,
    ang_diff,
    baysurf_windows,
    entrance_reverse_windows,
    entrance_windows,
    hill60_windows,
    lake_windows,
    ne_windows,
    south_windows,
    _baysurf_tide_spans,
)


@pytest.fixture(autouse=True)
def _no_tide_calibration(request, monkeypatch):
    """Tide rules are logic; TIDE_TIME_OFFSET_MIN is measured data that moves
    whenever the gauge is re-read. Tests that assert exact gate clock times
    would otherwise have to be rewritten after every recalibration, so the
    offset is zeroed here and proved separately in
    test_tide_offset_shifts_every_tide. Mark a test `real_tide_offset` to
    opt out and see the configured value."""
    if "real_tide_offset" in request.keywords:
        return
    monkeypatch.setattr(config, "TIDE_TIME_OFFSET_MIN", 0.0)


def hours(rng, speed, deg):
    return {h: (speed, deg) for h in rng}


# ------------------------------------------------------------------ lake

def test_lake_south_names_oak_flats_run(sun):
    wind = mk_wind(hours(range(10, 15), 22, 190), location_key="lake")
    windows, _ = lake_windows(wind, sun, NOW)
    assert [w.trigger_id for w in windows] == ["lake_oakflats_berkeley"]
    w = windows[0]
    assert w.run_name == "Oak Flats to Berkeley"
    assert w.grade == "green"
    assert w.start == at(10) and w.end == at(15)


def test_lake_direction_picks_crossing(sun):
    for deg, trigger in [(250, "lake_west"), (275, "lake_west")]:
        wind = mk_wind(hours(range(10, 13), 22, deg), location_key="lake")
        windows, _ = lake_windows(wind, sun, NOW)
        assert [w.trigger_id for w in windows] == [trigger]


def test_lake_grades(sun):
    for speed, grade in [(18.5, "yellow"), (22, "green"), (26, "red")]:
        wind = mk_wind(hours(range(10, 13), speed, 190), location_key="lake")
        windows, _ = lake_windows(wind, sun, NOW)
        assert windows[0].grade == grade, speed


def test_lake_ne_rare_needs_25_and_flags(sun):
    # 21 kn is under the 22.5 yellow floor, so it is no longer a run - but it
    # is inside the watch band (0.75 * 25 = 18.75), so it is flagged rather
    # than dropped (Rob, 10 Aug 2026).
    below_yellow = mk_wind(hours(range(10, 13), 21, 45), location_key="lake")
    marginal_only, _ = lake_windows(below_yellow, sun, NOW)
    assert [w.grade for w in marginal_only] == ["watch"]
    assert marginal_only[0].watch == "21 kn, needs 22"
    marginal = mk_wind(hours(range(10, 13), 23, 45), location_key="lake")
    assert lake_windows(marginal, sun, NOW)[0][0].grade == "yellow"
    strong = mk_wind(hours(range(10, 13), 26, 45), location_key="lake")
    windows, _ = lake_windows(strong, sun, NOW)
    assert windows[0].trigger_id == "lake_ne_rare"
    assert windows[0].grade == "green"
    assert "RARE" in windows[0].title_tags


def test_daylight_clipping(sun):
    wind = mk_wind(hours(range(4, 7), 25, 190), location_key="lake")
    windows, _ = lake_windows(wind, sun, NOW)
    assert windows == []


def test_single_model_is_watch_not_event(sun):
    # One model at full strength is not a run (spec 5 needs 2), but it is
    # exactly the shape of a model bust, so it now surfaces as a watch as
    # well as a near miss instead of only living in the JSON.
    wind = mk_wind(
        hours(range(10, 13), 22, 190),
        models=["gfs_seamless"],
        location_key="lake",
    )
    windows, misses = lake_windows(wind, sun, NOW)
    assert [w.grade for w in windows] == ["watch"]
    assert windows[0].watch == "1 of 4 models only (needs 2)"
    assert any(
        m.reason == "single_model" and m.trigger_id == "lake_oakflats_berkeley"
        for m in misses
    )


def test_low_confidence_flag_far_out(sun):
    far = DAY + timedelta(days=5)
    wind = mk_wind(hours(range(10, 13), 22, 190), location_key="lake", day=far)
    windows, _ = lake_windows(wind, sun, NOW)
    assert windows[0].confidence == "low (long range)"


# ------------------------------------------------------------------ south

def test_south_small_swell_all_runs(sun):
    wind = mk_wind(hours(range(10, 14), 22, 185))
    windows, _ = south_windows(wind, mk_marine(0.6, 160), sun, NOW)
    assert len(windows) == 1
    assert windows[0].run_name == "South runs"
    assert windows[0].spots == ["Bass Point", "Hill 60", "Boilers", "Bellambi"]


def test_south_medium_swell_narrows(sun):
    wind = mk_wind(hours(range(10, 14), 22, 185))
    windows, _ = south_windows(wind, mk_marine(1.5, 160), sun, NOW)
    assert windows[0].run_name == "South runs"
    assert windows[0].spots == ["Bellambi red buoy", "Hill 60"]


def test_south_large_swell_hill60_only_no_kill(sun):
    # Hill 60 handles any size south swell and wind (spec 4.3/4.6).
    wind = mk_wind(hours(range(10, 14), 22, 185))
    windows, misses = south_windows(wind, mk_marine(2.8, 180), sun, NOW)
    assert windows[0].run_name == "South runs"
    assert windows[0].spots == ["Hill 60"]
    assert not any(m.reason == "aligned_swell_too_big" for m in misses)


def test_south_cross_swell_kills(sun):
    wind = mk_wind(hours(range(10, 14), 22, 185))
    windows, misses = south_windows(wind, mk_marine(1.2, 90), sun, NOW)
    assert windows == []
    assert any(m.reason == "cross_swell" for m in misses)


def test_south_small_cross_swell_downgrades(sun):
    wind = mk_wind(hours(range(10, 14), 22, 185))
    windows, _ = south_windows(wind, mk_marine(0.8, 90), sun, NOW)
    assert windows[0].grade == "yellow"
    assert any("cross swell" in t for t in windows[0].title_tags)


def test_south_minuscule_swell_ignored(sun):
    wind = mk_wind(hours(range(10, 14), 22, 185))
    windows, _ = south_windows(wind, mk_marine(0.4, 90), sun, NOW)
    assert windows[0].grade == "green"
    assert windows[0].title_tags == []


# ------------------------------------------------------------------ hill 60

def test_hill60_standalone_fires_without_wind(sun):
    marine = mk_marine(swell_by_hour={h: (2.2, 180) for h in range(8, 16)})
    windows = hill60_windows([], marine, sun, NOW)
    assert len(windows) == 1
    assert windows[0].grade == "green"
    assert windows[0].trigger_id == "hill60_swell"


def test_hill60_folds_into_overlapping_south_event(sun):
    wind = mk_wind(hours(range(10, 14), 22, 185))
    marine = mk_marine(2.2, 180)
    south, _ = south_windows(wind, marine, sun, NOW)
    standalone = hill60_windows(south, marine, sun, NOW)
    assert standalone == []
    assert any("standalone Hill 60" in n for n in south[0].notes)


# ------------------------------------------------------------------ NE runs

def test_ne_ladder_15kn_ready_after_2h(sun):
    wind = mk_wind(hours(range(10, 16), 16, 45))
    windows, _ = ne_windows(wind, mk_marine(0.3, 45), sun, NOW)
    assert len(windows) == 1
    assert windows[0].start == at(12)  # 10:00 and 11:00 are the build hours


def test_ne_ladder_11kn_needs_3h(sun):
    wind = mk_wind(hours(range(10, 16), 11, 45))
    windows, _ = ne_windows(wind, mk_marine(0.3, 45), sun, NOW)
    assert windows[0].start == at(13)
    assert windows[0].grade == "yellow"  # 10-15 kn band


def test_ne_too_short_never_fires(sun):
    wind = mk_wind(hours(range(10, 12), 16, 45))
    windows, _ = ne_windows(wind, mk_marine(0.3, 45), sun, NOW)
    assert windows == []


def test_ne_off_angle_downgrade(sun):
    wind = mk_wind(hours(range(10, 16), 17, 70))
    windows, _ = ne_windows(wind, mk_marine(0.3, 45), sun, NOW)
    assert windows[0].grade == "yellow"  # green downgraded
    assert any("off-angle" in t for t in windows[0].title_tags)


def test_ne_aligned_swell_ok_to_1_5(sun):
    wind = mk_wind(hours(range(10, 16), 17, 45))
    windows, _ = ne_windows(wind, mk_marine(1.4, 50), sun, NOW)
    assert windows[0].grade == "green"


def test_ne_aligned_swell_over_1_5_kills(sun):
    wind = mk_wind(hours(range(10, 16), 17, 45))
    windows, misses = ne_windows(wind, mk_marine(1.7, 50), sun, NOW)
    assert windows == []
    assert any(m.reason == "aligned_swell_too_big" for m in misses)


def test_ne_cross_south_swell_kills(sun):
    # Nuking NE over a medium south swell creates no event (spec 4.6).
    wind = mk_wind(hours(range(10, 16), 25, 45))
    windows, misses = ne_windows(wind, mk_marine(1.2, 180), sun, NOW)
    assert windows == []
    assert any(m.reason == "cross_swell" for m in misses)


def test_ne_minuscule_south_swell_full_value(sun):
    wind = mk_wind(hours(range(10, 16), 25, 45))
    windows, _ = ne_windows(wind, mk_marine(0.4, 180), sun, NOW)
    assert windows[0].grade == "red"


# --------------------------------------------------------------- tide helper

def _marine_with_tide(high_tide_hour: int, low_tide_hour: int) -> MarineForecast:
    hours = []
    for h in range(24):
        sea_level_m = 0.0
        if h == high_tide_hour:
            sea_level_m = 1.0
        elif h == low_tide_hour:
            sea_level_m = -1.0
        elif high_tide_hour < low_tide_hour:
            if high_tide_hour < h < low_tide_hour:
                sea_level_m = -0.2 * (h - high_tide_hour)
            else:
                sea_level_m = -0.2 * (high_tide_hour + 24 - h)
        else:
            if h <= high_tide_hour and h >= low_tide_hour:
                sea_level_m = -0.2 * (high_tide_hour - h)
            else:
                sea_level_m = -0.2 * (h + 24 - high_tide_hour)
        hours.append(
            MarineHour(
                time=at(h),
                swell_m=1.6,
                swell_dir_deg=60,
                swell_period_s=9.0,
                sea_level_m=sea_level_m,
            )
        )
    return MarineForecast(fetched_at=NOW, hours=hours)


# ------------------------------------------------------------------ baysurf

def test_baysurf_triggers_on_east_ne_swell_and_light_wind(sun):
    wind = mk_wind(hours(range(10, 14), 8, 270), location_key="ocean")
    marine = _marine_with_tide(10, 13)
    windows, _ = baysurf_windows(wind, marine, sun, NOW)
    assert len(windows) == 1
    assert windows[0].trigger_id == "baysurf"
    assert windows[0].grade == "green"


def test_baysurf_rejects_strong_wrong_direction_wind(sun):
    wind = mk_wind(hours(range(10, 14), 12, 90), location_key="ocean")
    marine = _marine_with_tide(10, 13)
    windows, _ = baysurf_windows(wind, marine, sun, NOW)
    assert windows == []


def test_baysurf_tide_span_uses_the_next_low_not_the_deepest_one():
    # _baysurf_tide_spans used to search every remaining hour in the whole
    # forecast for the single lowest sea level, instead of just the next low
    # tide - a single-cycle fixture can't catch that (soonest == lowest when
    # there's only one), so this builds several days with a deliberately
    # deeper low on day 3 to tell them apart.
    hours_list = []
    for h in range(24 * 4):
        t = at(h % 24, DAY + timedelta(days=h // 24))
        base = 0.5 * math.sin(2 * math.pi * h / 12.42)
        depth_bonus = -0.4 if 58 <= h <= 66 else 0.0
        hours_list.append(
            MarineHour(time=t, swell_m=1.6, swell_dir_deg=60.0, swell_period_s=10.0,
                       sea_level_m=base + depth_bonus)
        )
    marine = MarineForecast(fetched_at=NOW, hours=hours_list)
    highs = marine.high_tides()
    lows = marine.low_tides()
    soonest_low = min((lt for lt in lows if lt.time > highs[0].time), key=lambda lt: lt.time)

    spans = _baysurf_tide_spans(marine)
    full_start, full_end, _, _ = spans[0]
    assert full_start == highs[0].time
    assert full_end == soonest_low.time + HOUR
    assert full_end - full_start < timedelta(hours=12)


def test_baysurf_downgrades_outside_ideal_tide_window(sun):
    wind = mk_wind(hours(range(10, 13), 8, 270), location_key="ocean")
    marine = _marine_with_tide(10, 17)
    windows, _ = baysurf_windows(wind, marine, sun, NOW)
    assert windows[0].grade == "yellow"
    assert any("tide" in t.lower() for t in windows[0].title_tags)


# ------------------------------------------------------------------ entrance

def test_entrance_splits_the_run_out_from_its_shoulders(sun):
    # Rob, 11 Aug 2026: the run-out gets its own full-rating event and the
    # rest of the workable stretch is downgraded around it, so the calendar
    # shows when it is actually good rather than one long single-coloured
    # block.
    wind = mk_wind(hours(range(8, 16), 6, 270), location_key="entrance")
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    by_start = sorted(windows, key=lambda w: w.start)
    assert [(w.start.hour, w.end.hour, w.grade, w.tide_state) for w in by_start] == [
        (7, 13, "yellow", "workable"),
        (13, 15, "green", "preferred"),
        (15, 17, "yellow", "workable"),
    ]
    run_out = by_start[1]
    assert run_out.trigger_id == "entrance_swell"
    assert run_out.title_tags == []                 # only the shoulders are tagged
    assert all("off-tide" in w.title_tags for w in (by_start[0], by_start[2]))
    assert run_out.high_tide == at(13).isoformat()
    # Height is the modelled sea level (0.0 at the peak here) plus the offset.
    assert run_out.high_tide_m == config.TIDE_HEIGHT_OFFSET_M


def test_entrance_sub_hour_shoulder_is_dropped_not_stubbed(sun):
    # A 30-minute leftover either side of the run-out is below the minimum
    # window used everywhere else; it should vanish, not become a stub event.
    wind = mk_wind(hours(range(12, 16), 6, 270), location_key="entrance")
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert all((w.end - w.start) >= HOUR for w in windows)


def test_entrance_mode1_swell_direction_matters(sun):
    wind = mk_wind(hours(range(8, 16), 6, 270), location_key="entrance")
    marine = mk_marine(0.9, 180, high_tide_hour=13)  # south swell, not E/NE
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert windows == []


def test_entrance_mode1_wind_too_strong(sun):
    # Onshore AND over the no-go: the only combination that deletes a swell
    # window now. All 24 h, not just the daylight block - mk_wind's filler
    # hours are 2 kn, which qualifies through the calm clause.
    wind = mk_wind(hours(range(0, 24), 30, 90), location_key="entrance")
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert windows == []


def test_entrance_strong_offshore_is_still_a_run(sun):
    # The entrance is open to the ocean and this is a swell run: a hard
    # offshore grooms the face rather than ruining it, so it is not a no-go
    # the way the same strength onshore would be.
    wind = mk_wind(hours(range(0, 24), 22, 270), location_key="entrance")
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    swell = [w for w in windows if w.trigger_id == "entrance_swell"]
    assert swell and all(w.grade != "watch" for w in swell)


def test_entrance_unfavourable_wind_is_a_watch_not_a_deletion(sun):
    # Rob, 11 Aug 2026: "it's not a wind only place, so that shouldn't be the
    # golden gate holding it all back." Swell and tide are present; the wind
    # is merely wrong.
    wind = mk_wind(hours(range(0, 24), 16, 90), location_key="entrance")
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    swell = [w for w in windows if w.trigger_id == "entrance_swell"]
    assert swell, "an onshore breeze deleted a swell window"
    assert all(w.grade == "watch" for w in swell)
    assert all("wind not favourable" in (w.watch or "") for w in swell)
    # ...and a window that is also off-tide reports both reasons, not one.
    both = [w for w in swell if w.tide_state == "workable"]
    assert both and all("off tide" in w.watch for w in both)


def test_entrance_mode2_strong_ne(sun):
    wind = mk_wind(hours(range(8, 16), 20, 50), location_key="entrance")
    marine = mk_marine(0.2, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    run_out = [w for w in windows if w.tide_state == "preferred"]
    assert len(run_out) == 1
    assert run_out[0].trigger_id == "entrance_ne"
    assert run_out[0].grade == "green"


def test_entrance_both_modes_merge(sun):
    # Two models see 20 kn NE (mode 2), the other two stay near calm which
    # qualifies for mode 1 alongside the E swell: one merged event, not two.
    wind = mk_wind(
        hours(range(8, 16), 20, 50),
        models=["gfs_seamless", "ecmwf_ifs025"],
        location_key="entrance",
    )
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    # One event per tide phase, not one per mode: the run-out piece carries
    # both modes rather than appearing twice.
    run_out = [w for w in windows if w.tide_state == "preferred"]
    assert len(run_out) == 1
    windows = run_out
    assert any("also fires as" in n for n in windows[0].notes)
    # entrance_ne (mode 2) sorts first and survives as the merged event here,
    # but never sets swell fields itself - the swell numbers that justified
    # mode 1 firing used to vanish entirely except for a bare grade mention.
    assert windows[0].swell_m == 0.9
    assert windows[0].swell_dir_deg == 90.0


# -------------------------------------------------------- entrance reverse

def _marine_low_then_high(
    low_hour: int, swell_m: float = 0.3, swell_dir: float = 90.0
) -> MarineForecast:
    """One clean tide cycle: low at low_hour, high 12 h later. A single
    cosine period sampled hourly has exactly one min and one max, so
    high_tides()/low_tides() are unambiguous.

    Swell defaults below the entrance mode 1 floor, so wind-only tests are
    not accidentally firing on swell; pass swell_m to exercise mode 1."""
    hours = []
    for h in range(24):
        level = -math.cos(2 * math.pi * (h - low_hour) / 24)
        hours.append(
            MarineHour(
                time=at(h),
                swell_m=swell_m,
                swell_dir_deg=swell_dir,
                swell_period_s=9.0,
                sea_level_m=level,
            )
        )
    return MarineForecast(fetched_at=NOW, hours=hours)


def test_entrance_reverse_fires_between_low_plus_2_and_high_minus_1(sun):
    wind = mk_wind(hours(range(10, 15), 25, 315), location_key="entrance")
    marine = _marine_low_then_high(low_hour=8)  # high at 20:00
    windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
    assert len(windows) == 1
    w = windows[0]
    assert w.trigger_id == "entrance_reverse"
    assert w.grade == "green"
    assert w.start == at(10) and w.end == at(15)
    assert w.high_tide == at(20).isoformat()
    assert any("low tide 08:00" in n for n in w.notes)


def test_entrance_reverse_grades(sun):
    # 19 kn is below the 20 kn yellow floor but inside the watch band
    # (0.75 * 25 = 18.75), so it is flagged rather than dropped.
    for speed, grade in [(19.0, "watch"), (20.0, "yellow"), (25.0, "green"), (32.0, "red")]:
        wind = mk_wind(hours(range(10, 15), speed, 315), location_key="entrance")
        marine = _marine_low_then_high(low_hour=8)
        windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
        assert windows[0].grade == grade, speed


def test_entrance_reverse_nw_is_prime_west_is_off_angle(sun):
    wind = mk_wind(hours(range(10, 15), 25, 280), location_key="entrance")
    marine = _marine_low_then_high(low_hour=8)
    windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
    assert windows[0].grade == "yellow"  # green downgraded one step
    assert any("off-angle" in t for t in windows[0].title_tags)


def test_entrance_reverse_rejects_wrong_direction(sun):
    wind = mk_wind(hours(range(10, 15), 25, 90), location_key="entrance")
    marine = _marine_low_then_high(low_hour=8)
    windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
    assert windows == []


def test_entrance_reverse_off_tide_is_downgraded_not_dropped(sun):
    # Blows before the gate opens: low tide is at 08:00, gate opens 10:00.
    # The tide is a penalty now, not a veto (Rob, 10 Aug 2026) - a clean
    # 25 kn NW an hour early is still worth knowing about.
    wind = mk_wind(hours(range(8, 10), 25, 315), location_key="entrance")
    marine = _marine_low_then_high(low_hour=8)
    windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
    assert len(windows) == 1
    assert windows[0].tide_state == "workable"
    assert windows[0].grade == "yellow"  # green, downgraded one step
    assert "off-tide" in windows[0].title_tags


def test_window_survives_when_no_tide_can_be_labelled(sun):
    # A 24 h fixture whose next high falls off the end leaves no preferred
    # window to name. The run is still not in doubt - the no-go has already
    # been subtracted - so it is workable, not dropped.
    #
    # This replaces a test that asserted the opposite. It passed only because
    # an empty preferred list fell off the end of an elif and silently binned
    # the window; the tolerance rule it was really testing was removed when
    # the tide became three phases.
    wind = mk_wind(hours(range(8, 10), 25, 315), location_key="entrance")
    marine = _marine_low_then_high(low_hour=20)
    windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
    real = [w for w in windows if w.grade != "watch"]
    assert [w.tide_state for w in real] == ["workable"]
    assert real[0].high_tide is None


def test_entrance_without_a_detectable_high_still_reports(sun):
    # The F3 case on the standard modes: perfect conditions, flat sea level,
    # nothing for high_tides() to find. Must not vanish silently.
    flat = MarineForecast(fetched_at=NOW, hours=[
        MarineHour(time=at(h), swell_m=1.2, swell_dir_deg=70.0,
                   swell_period_s=9.0, sea_level_m=0.0)
        for h in range(24)
    ])
    wind = mk_wind(hours(range(8, 16), 6, 270), location_key="entrance")
    windows, _ = entrance_windows(wind, flat, sun, NOW)
    assert windows, "qualifying conditions produced no window at all"
    assert all(w.tide_state == "workable" for w in windows)
    assert all(w.high_tide is None for w in windows)


def test_entrance_reverse_no_false_miss_outside_tide_gate(sun):
    # All 4 models agree on a clean NW blow entirely before the gate opens
    # (low tide 08:00 -> gate opens 10:00): this is a tide-gate rejection,
    # not a model-agreement problem, and must not be reported as one.
    wind = mk_wind(hours(range(7, 10), 26, 315), location_key="entrance")
    marine = _marine_low_then_high(low_hour=8)
    windows, misses = entrance_reverse_windows(wind, marine, sun, NOW)
    assert [w.tide_state for w in windows] == ["workable"]
    assert misses == []


def test_entrance_reverse_single_model_is_near_miss(sun):
    wind = mk_wind(
        hours(range(10, 15), 25, 315),
        models=["gfs_seamless"],
        location_key="entrance",
    )
    marine = _marine_low_then_high(low_hour=8)
    windows, misses = entrance_reverse_windows(wind, marine, sun, NOW)
    assert [w.grade for w in windows] == ["watch"]
    assert any(
        m.reason == "single_model" and m.trigger_id == "entrance_reverse"
        for m in misses
    )


# ------------------------------------------------------------------ helpers

def test_ang_diff_wraps():
    assert ang_diff(350, 10) == 20
    assert ang_diff(180, 45) == 135


def test_arc_wrap():
    arc = config.Arc(300, 60)
    assert arc.contains(350) and arc.contains(30)
    assert not arc.contains(180)


# ------------------------------------------------------- watch / maybe band

def test_watch_band_flags_below_yellow(sun):
    # 16 kn against a 20 kn lake target: under the 18 kn yellow floor, inside
    # the 15 kn watch floor.
    wind = mk_wind(hours(range(10, 13), 16, 190), location_key="lake")
    windows, _ = lake_windows(wind, sun, NOW)
    assert [w.grade for w in windows] == ["watch"]
    assert windows[0].watch == "16 kn, needs 18"


def test_watch_band_has_a_floor(sun):
    # 14 kn is below the 15 kn watch floor: still nothing at all.
    wind = mk_wind(hours(range(10, 13), 14, 190), location_key="lake")
    windows, _ = lake_windows(wind, sun, NOW)
    assert windows == []


def test_watch_never_resurrects_a_swell_veto(sun):
    # 4.6 kills this window on cross swell. The watch pass sees uncovered
    # hours at full strength and full consensus and must leave them alone -
    # re-adding it as a maybe would quietly overrule a safety rule.
    wind = mk_wind(hours(range(10, 14), 22, 185))
    windows, misses = south_windows(wind, mk_marine(1.2, 90), sun, NOW)
    assert windows == []
    assert any(m.reason == "cross_swell" for m in misses)


def test_watch_band_is_proportional_for_an_explicit_yellow_floor():
    # The reverse run's yellow floor is an explicit 20 against a 25 target
    # (spec 4.8), so measuring the band down from the target gave it 1.25 kn
    # where every other trigger gets 15%. Anchored to the yellow floor now.
    assert config.watch_floor_for(20.0) == pytest.approx(16.667, abs=0.01)
    assert config.watch_floor_for(18.0) == pytest.approx(15.0, abs=0.01)


def test_downgrades_still_floor_at_yellow(sun):
    # Adding watch below yellow must not let the 4.5/4.6 downgrades through
    # it; spec 6 says they never drop below yellow.
    from foilscan.triggers import downgrade

    assert downgrade("yellow") == "yellow"
    assert downgrade("green", 5) == "yellow"
    assert downgrade("green", 5, floor="watch") == "watch"


# ------------------------------------------------------------ tide accuracy

def test_high_tide_is_interpolated_between_hourly_samples():
    # A peak skewed towards the later sample must land after the sample hour,
    # not on it. The 10 Aug entrance miss was decided by 20 minutes.
    hours_list = [
        MarineHour(time=at(h), swell_m=1.0, swell_dir_deg=60.0, swell_period_s=9.0,
                   sea_level_m=lvl)
        for h, lvl in enumerate([0.0, 0.30, 0.50, 0.45, 0.10] + [0.0] * 19)
    ]
    marine = MarineForecast(fetched_at=NOW, hours=hours_list)
    high = marine.high_tides()[0]
    assert high.sample.time == at(2)
    assert at(2) < high.time < at(3)


def test_symmetric_peak_is_not_moved():
    hours_list = [
        MarineHour(time=at(h), swell_m=1.0, swell_dir_deg=60.0, swell_period_s=9.0,
                   sea_level_m=lvl)
        for h, lvl in enumerate([0.0, 0.4, 0.5, 0.4, 0.0] + [0.0] * 19)
    ]
    marine = MarineForecast(fetched_at=NOW, hours=hours_list)
    assert marine.high_tides()[0].time == at(2)


def test_entrance_off_tide_survives_and_is_recorded(sun):
    # The 10 Aug shape: wind and swell qualify right as the run-out gate
    # closes. It used to vanish with no near miss to explain it.
    # Every hour spelled out: mk_wind's 2 kn filler would itself qualify
    # mode 1 through the calm clause and blur the window boundary.
    # Filler must be unfavourable AND over the no-go now: 14 kn offshore is
    # perfectly good conditions for a swell run.
    spec = {h: (30.0, 90.0) for h in range(24)}
    spec.update({h: (6.0, 270.0) for h in range(9, 12)})
    wind = mk_wind(spec, location_key="entrance")
    marine = mk_marine(1.2, 70, high_tide_hour=7)
    windows, misses = entrance_windows(wind, marine, sun, NOW)
    assert [w.tide_state for w in windows] == ["workable"]
    assert "off-tide" in windows[0].title_tags
    assert any(m.reason == "off_tide" for m in misses)


def test_tide_offset_shifts_every_tide(monkeypatch):
    # The calibrated offset (Open-Meteo runs ~30 min early at Port Kembla)
    # must move highs and lows together, or the gates derived from them drift
    # apart.
    marine = mk_marine(1.0, 90, high_tide_hour=13)
    monkeypatch.setattr(config, "TIDE_TIME_OFFSET_MIN", 0.0)
    base = marine.high_tides()[0].time
    monkeypatch.setattr(config, "TIDE_TIME_OFFSET_MIN", 30.0)
    assert marine.high_tides()[0].time == base + timedelta(minutes=30)
    monkeypatch.setattr(config, "TIDE_TIME_OFFSET_MIN", -45.0)
    assert marine.high_tides()[0].time == base - timedelta(minutes=45)


@pytest.mark.real_tide_offset
def test_calibrated_offset_is_applied_by_default():
    # Guards against the constant being reset to 0 without the note in
    # config.py being revisited.
    assert config.TIDE_TIME_OFFSET_MIN != 0.0


# ------------------------------------------- entrance tide phases (11 Aug)

def test_entrance_no_go_in_the_last_hours_before_low(sun):
    # "Can still work on any tide except the last 4 hours before dead low -
    # water flow is too much." Low at 16:00 makes 12:00-16:00 a hard no.
    marine = _marine_low_then_high(low_hour=16, swell_m=1.2)
    spec = {h: (30.0, 90.0) for h in range(24)}   # onshore and over the no-go       # never qualifies
    spec.update({h: (6.0, 270.0) for h in range(13, 16)})   # only inside the no-go
    wind = mk_wind(spec, location_key="entrance")
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert windows == []


def test_entrance_window_is_clipped_at_the_no_go_boundary(sun):
    marine = _marine_low_then_high(low_hour=16, swell_m=1.2)
    spec = {h: (30.0, 90.0) for h in range(24)}   # onshore and over the no-go
    spec.update({h: (6.0, 270.0) for h in range(10, 15)})   # straddles 12:00
    wind = mk_wind(spec, location_key="entrance")
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert len(windows) == 1
    # 10:00-15:00 minus the 12:00-16:00 no-go leaves 10:00-12:00.
    assert windows[0].start == at(10) and windows[0].end == at(12)


def test_entrance_workable_tide_is_downgraded_not_dropped(sun):
    # Neither the run-out nor the no-go: the run is on, just not at its best.
    marine = _marine_low_then_high(low_hour=16, swell_m=1.2)  # no-go 12:00-16:00
    spec = {h: (30.0, 90.0) for h in range(24)}   # onshore and over the no-go
    spec.update({h: (6.0, 270.0) for h in range(8, 12)})
    wind = mk_wind(spec, location_key="entrance")
    windows, misses = entrance_windows(wind, marine, sun, NOW)
    assert len(windows) == 1
    w = windows[0]
    assert w.tide_state == "workable"
    assert "off-tide" in w.title_tags
    assert w.grade == "green"      # red on swell, downgraded one step
    assert any(m.reason == "off_tide" for m in misses)


def test_no_go_applies_to_the_reverse_run_too(sun):
    # Peak ebb is peak ebb whichever direction you are running.
    marine = _marine_low_then_high(low_hour=16)
    spec = {h: (25.0, 315.0) for h in range(13, 16)}   # inside the no-go only
    wind = mk_wind(spec, location_key="entrance")
    windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
    assert [w for w in windows if w.grade != "watch"] == []



def test_the_entrance_no_go_sits_exactly_where_rob_put_it(sun):
    # 20 kn onshore, his number. Just under is a watch, at or over is gone.
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    for speed, expect in ((19.0, "watch"), (20.0, None), (24.0, None)):
        wind = mk_wind(hours(range(0, 24), speed, 90), location_key="entrance")
        got = [w for w in entrance_windows(wind, marine, sun, NOW)[0]
               if w.trigger_id == "entrance_swell"]
        if expect is None:
            assert got == [], f"{speed} kn onshore should be a no-go"
        else:
            assert got and all(w.grade == expect for w in got), speed


# ----------------------------------------- thin consensus flag (11 Aug)

def _win(agreeing, grade="green"):
    from foilscan.models import Window

    return Window(trigger_id="lake_west", run_name="Kanahooka / Berkeley",
                  start=at(10), end=at(12), grade=grade, peak_time=at(10),
                  peak_median_kn=22.0, direction_deg=250.0,
                  models_agreeing=agreeing, model_values={"ICON": 22.0})


def test_a_bare_minimum_window_is_flagged():
    from foilscan.triggers import flag_thin_consensus

    w = flag_thin_consensus([_win(config.MIN_MODELS_AGREE)])[0]
    assert any("of 4 models" in t for t in w.title_tags)
    assert any("the others did not see it" in n for n in w.notes)


def test_a_well_agreed_window_is_not_flagged():
    from foilscan.triggers import flag_thin_consensus

    w = flag_thin_consensus([_win(4)])[0]
    assert w.title_tags == [] and w.notes == []


def test_a_watch_is_left_alone():
    # It already carries its own reason; "2 of 4 models" beside "1 of 4
    # models only" reads as a contradiction.
    from foilscan.triggers import flag_thin_consensus

    w = flag_thin_consensus([_win(config.MIN_MODELS_AGREE, grade="watch")])[0]
    assert w.title_tags == []
