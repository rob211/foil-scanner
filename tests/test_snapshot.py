"""Snapshot report: the pure helpers only, no network."""
import math
from datetime import datetime, timedelta

import pytest
from conftest import DAY, NOW, at

from foilscan import config, snapshot
from foilscan.models import MarineForecast, MarineHour, Observation


def _cosine_marine(low_hour: int = 4, swell=1.5, swell_dir=60.0) -> MarineForecast:
    """One clean tide cycle so highs and lows are unambiguous."""
    return MarineForecast(
        fetched_at=NOW,
        hours=[
            MarineHour(
                time=at(h),
                swell_m=swell + 0.02 * h,
                swell_dir_deg=swell_dir,
                swell_period_s=9.0,
                sea_level_m=-math.cos(2 * math.pi * (h - low_hour) / 24),
            )
            for h in range(24)
        ],
    )


def test_tide_reports_height_above_chart_datum():
    marine = _cosine_marine(low_hour=4)
    # 16:00 is on the flood between the 04:00 low and the 16:00 high.
    td = snapshot._tide_state(marine, at(10) + timedelta(minutes=30))
    expected_msl = snapshot._at_now(*snapshot._lerp_marine(marine, at(10) + timedelta(minutes=30)), "sea_level_m")
    assert td["height_cd_m"] == pytest.approx(
        expected_msl + config.TIDE_HEIGHT_OFFSET_M
    )


def test_tide_direction_is_flood_then_ebb():
    marine = _cosine_marine(low_hour=4)
    rising = snapshot._tide_state(marine, at(10))
    falling = snapshot._tide_state(marine, at(20))
    assert "rising" in rising["moving"]
    assert "falling" in falling["moving"]


def test_next_high_and_low_are_both_ahead_of_now():
    marine = _cosine_marine(low_hour=4)
    td = snapshot._tide_state(marine, at(10))
    assert td["next_high"] is not None and td["next_high"].time > at(10)
    if td["next_low"] is not None:
        assert td["next_low"].time > at(10)


def test_values_are_interpolated_not_snapped_to_the_hour():
    # Hourly marine data moves enough inside an hour that reporting the last
    # sample as "now" misleads - especially the tide.
    marine = _cosine_marine(low_hour=4)
    on_hour = snapshot._tide_state(marine, at(10))["height_cd_m"]
    half_past = snapshot._tide_state(marine, at(10) + timedelta(minutes=30))["height_cd_m"]
    assert on_hour != half_past


def test_swell_direction_interpolates_the_short_way_round():
    # A swell either side of north must not average to due south.
    hours = [
        MarineHour(time=at(h), swell_m=1.0, swell_dir_deg=d, swell_period_s=9.0,
                   sea_level_m=0.1 * h)
        for h, d in [(9, 350.0), (10, 10.0)] + [(h, 0.0) for h in range(11, 24)]
    ]
    marine = MarineForecast(fetched_at=NOW, hours=hours)
    sw = snapshot._swell_now(marine, at(9) + timedelta(minutes=30))
    assert sw["dir_deg"] > 355 or sw["dir_deg"] < 5


def test_swell_trend_reports_building():
    marine = _cosine_marine()
    sw = snapshot._swell_now(marine, at(12))
    assert "building" in sw["trend"]


def test_missing_station_says_so_without_a_stray_note():
    line = snapshot._wind_line("Lake Illawarra (Holfuy)", None, "0.9-corrected")
    assert "unavailable" in line
    assert "0.9-corrected" not in line


def test_wind_line_shows_speed_direction_and_gust():
    o = Observation(
        station="Bellambi",
        time=datetime(2026, 7, 6, 16, 0, tzinfo=config.TZ),
        speed_kn=24.0,
        gust_kn=34.0,
        dir_deg=292.0,
    )
    line = snapshot._wind_line("Bellambi (BOM, coast)", o)
    assert "24 kn" in line and "WNW" in line and "292" in line and "34" in line


def test_marine_must_bracket_now():
    marine = _cosine_marine()
    with pytest.raises(ValueError):
        snapshot._lerp_marine(marine, at(23) + timedelta(hours=2))
