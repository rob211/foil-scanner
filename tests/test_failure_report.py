"""The failure email is only worth having if it names the right fault.
Log excerpts here are copied from real red runs, Sep-Oct 2026."""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "failure_report", Path(__file__).resolve().parent.parent / "scripts" / "failure_report.py"
)
fr = importlib.util.module_from_spec(spec)
sys.modules["failure_report"] = fr  # dataclass needs the module registered
spec.loader.exec_module(fr)


def keys(log):
    return [f.key for f in fr.diagnose(log)]


def test_marine_gap_reports_the_value_not_the_traceback_source():
    log = '''  File "/home/runner/work/foil-scanner/foil-scanner/foilscan/models.py", line 68, in at
    raise SchemaError(f"no marine data for {t.isoformat()}")
foilscan.errors.SchemaError: no marine data for 2026-10-29T11:00:00+11:00
wrote SCANNER BROKEN calendar flag'''
    (f,) = fr.diagnose(log)
    assert f.key == "marine_gap"
    assert "2026-10-29T11:00:00+11:00" in f.meaning
    assert f.action == fr.CODE_BUG


def test_dead_scan_seen_from_live_is_flagged_as_a_symptom():
    log = ("foilscan.errors.StaleDataError: last successful scan was 25.0 h ago "
           "(cap 8.0 h); the scan cron looks dead")
    (f,) = fr.diagnose(log)
    assert f.key == "scan_dead"
    assert "25 h" in f.title


def test_bom_stale_self_heals():
    log = ("foilscan.errors.StaleDataError: BOM observations (94749) (Bellambi): "
           "latest reading is 211 min old, cap is 75 min")
    (f,) = fr.diagnose(log)
    assert f.key == "bom_stale"
    assert f.action == fr.SELF_HEALS


def test_source_down_names_the_innermost_cause():
    timeout = ("SOURCE FAILED open_meteo_wind_ocean: GET https://api.open-meteo.com/v1/forecast "
               "failed after 5 attempts: HTTPSConnectionPool(host='api.open-meteo.com', "
               "port=443): Read timed out. (read timeout=30)")
    http = ("SOURCE FAILED open_meteo_wind_lake: GET https://api.open-meteo.com/v1/forecast "
            "failed after 5 attempts: 503 Server Error: Service Unavailable for url: "
            "https://api.open-meteo.com/v1/forecast?latitude=-34.53")
    assert "'timed out'" in fr.diagnose(timeout)[0].meaning
    assert "'503 Server Error'" in fr.diagnose(http)[0].meaning


def test_each_drifted_site_is_its_own_finding():
    log = ("DRIFTED: lake: measured 1.00 against a configured 1.45 (31% off); "
           "coast: measured 2.10 against a configured 3.9 (46% off)")
    assert sorted(keys(log)) == ["drift_wind_coast", "drift_wind_lake"]


def test_tide_drift():
    assert keys("DRIFTED: tide timing residual -53 min") == ["drift_tide"]


def test_unknown_falls_back_to_the_last_exception():
    log = "Traceback (most recent call last):\nKeyError: 'wind'\nValueError: bad thing"
    (f,) = fr.diagnose(log)
    assert f.key == "unknown_valueerror"
    assert "bad thing" in f.meaning


def test_no_output_at_all_still_says_something():
    assert keys("") == ["unknown"]


def test_redact_strips_keys_and_known_secrets():
    text = ("GET https://api.holfuy.com/live/?pw=abcd1234&s=366 failed; "
            "sa foil@proj.iam.gserviceaccount.com cal abc@group.calendar.google.com SEKRET99")
    out = fr.redact(text, ["SEKRET99"])
    for leaked in ("abcd1234", "foil@proj", "abc@group", "SEKRET99"):
        assert leaked not in out


def test_render_mentions_owner_and_carries_the_action():
    findings = fr.diagnose("DRIFTED: tide timing residual -53 min")
    body = fr.render("calibrate", findings, "log", "https://run", "a", "b", 3, [7])
    assert body.startswith("@rob211")
    assert "**Action:**" in body
    assert "#7" in body
    assert "Failed runs so far: 3" in body
