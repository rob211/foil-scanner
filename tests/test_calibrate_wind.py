"""The wind calibration's fitting maths.

The script is read-only, but its output is what a config constant would be
set from, so the arithmetic and the model choice are worth pinning.
"""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "calibrate_wind", Path(__file__).resolve().parent.parent / "scripts" / "calibrate_wind.py"
)
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)


def test_recovers_a_pure_offset():
    rows = [(f + 4.0, f) for f in range(5, 30)]
    f = cw.fits(rows)
    assert f["offset"] == pytest.approx(4.0)
    assert f["slope"] == pytest.approx(1.0, abs=0.01)
    assert cw.choose(f) == "offset"


def test_recovers_a_pure_multiplier():
    rows = [(f * 1.5, f) for f in range(5, 30)]
    f = cw.fits(rows)
    assert f["scale"] == pytest.approx(1.5, abs=0.01)
    assert cw.choose(f) in ("scale", "linear")
    assert f["offset_rms"] > f["scale_rms"], "an offset should fit a multiplier worse"


def test_a_complex_form_must_earn_its_extra_parameter():
    # Offset and linear within a hair of each other: take the constant.
    f = {"offset_rms": 3.70, "scale_rms": 4.17, "linear_rms": 3.69}
    assert cw.choose(f) == "offset"
    # A genuinely better linear fit still wins.
    f = {"offset_rms": 6.00, "scale_rms": 5.50, "linear_rms": 3.00}
    assert cw.choose(f) == "linear"


def test_hourly_pairs_use_the_median_not_the_windiest():
    # Taking the maximum reading in an hour inflates the observed side and
    # flatters the case for a correction.
    from datetime import datetime
    from foilscan import config

    t = datetime(2026, 8, 10, 12, 0, tzinfo=config.TZ)
    rows = [
        {"generated_at": f"x{i}", "obs": {"speed_kn": v, "time": t.isoformat()}}
        for i, v in enumerate((10.0, 20.0, 30.0))
    ]
    (observed, forecast), = cw.paired(rows, "obs", {t: 15.0})
    assert observed == 20.0 and forecast == 15.0


def test_missing_or_malformed_readings_are_skipped():
    from datetime import datetime
    from foilscan import config

    t = datetime(2026, 8, 10, 12, 0, tzinfo=config.TZ)
    rows = [
        {"generated_at": "a", "obs": None},
        {"generated_at": "b", "obs": {"speed_kn": None, "time": t.isoformat()}},
        {"generated_at": "c", "obs": {"speed_kn": 12.0, "time": None}},
        {"generated_at": "d", "obs": {"speed_kn": 12.0, "time": t.isoformat()}},
    ]
    assert cw.paired(rows, "obs", {t: 9.0}) == [(12.0, 9.0)]


def test_too_few_points_returns_nothing_rather_than_a_bogus_fit():
    assert cw.fits([(10.0, 8.0)]) == {}


def test_every_reported_location_has_a_configured_bias():
    # report() looks the location up in config.WIND_BIAS. A hand-written
    # label once drifted from the key and only failed on the second site,
    # after the first had already printed.
    from foilscan import config

    for loc in (config.LAKE, config.OCEAN):
        assert loc.key in config.WIND_BIAS
