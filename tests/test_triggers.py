import math
from datetime import timedelta

from conftest import DAY, NOW, at, mk_marine, mk_sun, mk_wind

from foilscan import config
from foilscan.models import MarineForecast, MarineHour
from foilscan.triggers import (
    ang_diff,
    baysurf_windows,
    entrance_reverse_windows,
    entrance_windows,
    hill60_windows,
    lake_windows,
    ne_windows,
    south_windows,
)


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
    for deg, trigger in [(250, "lake_kanahooka"), (275, "lake_berkeley")]:
        wind = mk_wind(hours(range(10, 13), 22, deg), location_key="lake")
        windows, _ = lake_windows(wind, sun, NOW)
        assert [w.trigger_id for w in windows] == [trigger]


def test_lake_grades(sun):
    for speed, grade in [(18.5, "yellow"), (22, "green"), (26, "red")]:
        wind = mk_wind(hours(range(10, 13), speed, 190), location_key="lake")
        windows, _ = lake_windows(wind, sun, NOW)
        assert windows[0].grade == grade, speed


def test_lake_ne_rare_needs_25_and_flags(sun):
    below_yellow = mk_wind(hours(range(10, 13), 21, 45), location_key="lake")
    assert lake_windows(below_yellow, sun, NOW)[0] == []
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


def test_single_model_is_near_miss_not_event(sun):
    wind = mk_wind(
        hours(range(10, 13), 22, 190),
        models=["gfs_seamless"],
        location_key="lake",
    )
    windows, misses = lake_windows(wind, sun, NOW)
    assert windows == []
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


def test_baysurf_downgrades_outside_ideal_tide_window(sun):
    wind = mk_wind(hours(range(10, 13), 8, 270), location_key="ocean")
    marine = _marine_with_tide(10, 17)
    windows, _ = baysurf_windows(wind, marine, sun, NOW)
    assert windows[0].grade == "yellow"
    assert any("tide" in t.lower() for t in windows[0].title_tags)


# ------------------------------------------------------------------ entrance

def test_entrance_mode1_needs_tide_overlap(sun):
    wind = mk_wind(hours(range(8, 16), 6, 270), location_key="entrance")
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert len(windows) == 1
    w = windows[0]
    assert w.trigger_id == "entrance_swell"
    assert w.grade == "green"
    # Clamped to the 2 h after the 13:00 high (run-out only, not before).
    assert w.start == at(13) and w.end == at(15)
    assert w.high_tide == at(13).isoformat()
    # Height is the modelled sea level (0.0 at the peak here) plus the datum offset.
    assert w.high_tide_m == config.PORT_KEMBLA_MSL_ABOVE_CD_M


def test_entrance_mode1_swell_direction_matters(sun):
    wind = mk_wind(hours(range(8, 16), 6, 270), location_key="entrance")
    marine = mk_marine(0.9, 180, high_tide_hour=13)  # south swell, not E/NE
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert windows == []


def test_entrance_mode1_wind_too_strong(sun):
    wind = mk_wind(hours(range(8, 16), 14, 270), location_key="entrance")
    marine = mk_marine(0.9, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert windows == []


def test_entrance_mode2_strong_ne(sun):
    wind = mk_wind(hours(range(8, 16), 20, 50), location_key="entrance")
    marine = mk_marine(0.2, 90, high_tide_hour=13)
    windows, _ = entrance_windows(wind, marine, sun, NOW)
    assert len(windows) == 1
    assert windows[0].trigger_id == "entrance_ne"
    assert windows[0].grade == "green"


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
    assert len(windows) == 1
    assert any("also fires as" in n for n in windows[0].notes)


# -------------------------------------------------------- entrance reverse

def _marine_low_then_high(low_hour: int) -> MarineForecast:
    """One clean tide cycle: low at low_hour, high 12 h later. A single
    cosine period sampled hourly has exactly one min and one max, so
    high_tides()/low_tides() are unambiguous."""
    hours = []
    for h in range(24):
        level = -math.cos(2 * math.pi * (h - low_hour) / 24)
        hours.append(
            MarineHour(
                time=at(h),
                swell_m=0.3,
                swell_dir_deg=90.0,
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
    for speed, grade in [(19.0, None), (20.0, "yellow"), (25.0, "green"), (32.0, "red")]:
        wind = mk_wind(hours(range(10, 15), speed, 315), location_key="entrance")
        marine = _marine_low_then_high(low_hour=8)
        windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
        if grade is None:
            assert windows == [], speed
        else:
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


def test_entrance_reverse_needs_the_tide_gate(sun):
    # Blows before the gate opens: low tide is at 08:00, gate opens 10:00.
    wind = mk_wind(hours(range(8, 10), 25, 315), location_key="entrance")
    marine = _marine_low_then_high(low_hour=8)
    windows, _ = entrance_reverse_windows(wind, marine, sun, NOW)
    assert windows == []


def test_entrance_reverse_no_false_miss_outside_tide_gate(sun):
    # All 4 models agree on a clean NW blow entirely before the gate opens
    # (low tide 08:00 -> gate opens 10:00): this is a tide-gate rejection,
    # not a model-agreement problem, and must not be reported as one.
    wind = mk_wind(hours(range(7, 10), 26, 315), location_key="entrance")
    marine = _marine_low_then_high(low_hour=8)
    windows, misses = entrance_reverse_windows(wind, marine, sun, NOW)
    assert windows == []
    assert misses == []


def test_entrance_reverse_single_model_is_near_miss(sun):
    wind = mk_wind(
        hours(range(10, 15), 25, 315),
        models=["gfs_seamless"],
        location_key="entrance",
    )
    marine = _marine_low_then_high(low_hour=8)
    windows, misses = entrance_reverse_windows(wind, marine, sun, NOW)
    assert windows == []
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
