from datetime import timedelta

from conftest import DAY, NOW, at, mk_marine, mk_sun, mk_wind

from foilscan import config
from foilscan.triggers import (
    ang_diff,
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


# ------------------------------------------------------------------ helpers

def test_ang_diff_wraps():
    assert ang_diff(350, 10) == 20
    assert ang_diff(180, 45) == 135


def test_arc_wrap():
    arc = config.Arc(300, 60)
    assert arc.contains(350) and arc.contains(30)
    assert not arc.contains(180)
