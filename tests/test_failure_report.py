"""The failure email is only worth having if it names the right fault.
Log excerpts here are copied from real red runs, Sep-Oct 2026."""
import importlib.util
import re
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


# ------------------------------------------- adversarial QA, 30 Oct 2026

def test_partial_scan_failure_without_traceback_is_still_named():
    # A partial failure returns 1 without raising: there is no exception
    # line, and this used to be reported as an infrastructure problem.
    log = ("SOURCE FAILED marine: open-meteo marine: null data at 2026-11-01T10:00:00+11:00\n"
           "RUN FAILED (partial): sources failed: marine")
    (f,) = fr.diagnose(log)
    assert f.key == "source_failed_marine"
    assert "null data" in f.meaning


def test_truncated_forecast_inside_source_failed():
    log = ("SOURCE FAILED open_meteo_wind_lake: open-meteo wind (lake): forecast only "
           "reaches 2026-11-01T23:00, expected at least 5 days ahead")
    assert keys(log) == ["forecast_truncated"]


def test_pat_fails_before_expiry_and_is_named():
    log = ("foilscan.errors.StaleDataError: GitHub PAT expires in 2 day(s) (2026-11-01); "
           "roll it at github.com/settings/personal-access-tokens")
    (f,) = fr.diagnose(log)
    assert f.key == "pat_expiry"
    assert "about to expire" in f.title


def test_live_fetch_error_names_the_host():
    log = ("foilscan.errors.FetchError: GET https://api.holfuy.com/live/ failed after 5 "
           "attempts: 502 Server Error: Bad Gateway for url: https://api.holfuy.com/live/?pw=***")
    (f,) = fr.diagnose(log)
    assert f.key == "fetch_down_api.holfuy.com"
    assert "'502 Server Error'" in f.meaning


def test_window_check_failure_carries_the_window_reason():
    log = ("lake:2026-10-30:14: check failed: window lake:2026-10-30:14 has no event_id in latest.json\n"
           "foilscan.errors.CalendarError: one or more window checks failed; see notes in live.json")
    (f,) = fr.diagnose(log)
    assert f.key == "window_check"
    assert "no event_id" in f.meaning


def test_untrusted_text_cannot_break_out_of_markdown():
    log = "foilscan.errors.SchemaError: BOM: unknown wind_dir '```\n@everyone [x](http://evil)'"
    body = fr.render("live", fr.diagnose(log), "tail ```\n@someone", "u", "a", "b", 1, [])
    assert "@everyone" not in body and "@someone" not in body
    assert "](http://evil)" not in body
    # Our own fences only: the untrusted ones were defanged.
    assert body.count("```") == 4


def test_log_tail_is_capped():
    assert len(fr._tail("x" * 100_000)) <= fr.TAIL_MAX_CHARS


class FakeGitHub:
    def __init__(self, issues=()):
        self.issues = {i["number"]: dict(i) for i in issues}
        self.comments = []
        self.created = []

    def __call__(self, *args, body=None):
        args = list(args)
        path = next(a for a in args if a.startswith("repos/"))
        if "issues?state=open" in path:
            return json.dumps([i for i in self.issues.values() if i.get("state", "open") == "open"])
        if path.endswith("/comments"):
            self.comments.append(body)
            return "{}"
        if "PATCH" in args:
            n = int(path.rsplit("/", 1)[1])
            if body is not None:
                self.issues[n]["body"] = body
            if "state=closed" in args:
                self.issues[n]["state"] = "closed"
            return "{}"
        n = 100 + len(self.created)
        title = next(a for a in args if a.startswith("title="))[6:]
        self.issues[n] = {"number": n, "title": title, "body": body, "state": "open"}
        self.created.append(n)
        return json.dumps({"number": n})


import json  # noqa: E402


def _env(monkeypatch, gh):
    monkeypatch.setattr(fr, "_gh", gh)
    monkeypatch.setenv("GITHUB_REPOSITORY", "rob211/foil-scanner")


BOM = "foilscan.errors.StaleDataError: BOM observations (94749) (Bellambi): latest reading is 211 min old"
HOLFUY = "foilscan.errors.StaleDataError: Holfuy 366: latest reading is 90 min old"


def test_repeat_failure_edits_silently(monkeypatch):
    gh = FakeGitHub()
    _env(monkeypatch, gh)
    fr.report_failure("live", BOM)
    fr.report_failure("live", BOM)
    assert len(gh.created) == 1 and gh.comments == []
    assert "Failed runs so far: 2" in gh.issues[gh.created[0]]["body"]


def test_new_fault_mid_incident_comments_once_on_the_same_issue(monkeypatch):
    gh = FakeGitHub()
    _env(monkeypatch, gh)
    fr.report_failure("live", BOM)
    fr.report_failure("live", BOM + "\n" + HOLFUY)
    fr.report_failure("live", HOLFUY)  # already seen this incident
    assert len(gh.created) == 1
    assert len(gh.comments) == 1 and "Holfuy" in gh.comments[0]


def test_recovery_waits_for_a_quiet_spell(monkeypatch):
    gh = FakeGitHub()
    _env(monkeypatch, gh)
    fr.report_failure("live", BOM)
    n = gh.created[0]
    fr.report_recovered("live")  # green straight after a red: a flap
    assert gh.issues[n]["state"] == "open"
    old = fr._stamp(fr.datetime.now(fr.timezone.utc) - fr.CLOSE_AFTER_QUIET - fr.timedelta(minutes=1))
    gh.issues[n]["body"] = re.sub(r"<!-- last:\S+ -->", f"<!-- last:{old} -->", gh.issues[n]["body"])
    fr.report_recovered("live")
    assert gh.issues[n]["state"] == "closed"
    assert "Recovered" in gh.comments[-1]


def test_other_workflows_issue_is_linked_not_reused(monkeypatch):
    gh = FakeGitHub()
    _env(monkeypatch, gh)
    fr.report_failure("scan", "SOURCE FAILED marine: open-meteo marine: null data at x")
    fr.report_failure("live", "StaleDataError: last successful scan was 25.0 h ago")
    scan_n, live_n = gh.created
    assert f"#{scan_n}" in gh.issues[live_n]["body"]
    fr.report_recovered("scan")
    assert gh.issues[live_n].get("state", "open") == "open"


def test_adopts_an_issue_opened_by_the_first_version(monkeypatch):
    # #37 was opened with fp "calibrate:drift_wind_lake" before incidents
    # were per workflow; it must be updated, not duplicated.
    gh = FakeGitHub([{"number": 37, "title": "[scanner] calibrate: drift", "state": "open",
                      "body": "x\n<!-- fp:calibrate:drift_wind_lake -->\n<!-- count:1 -->"}])
    _env(monkeypatch, gh)
    fr.report_failure("calibrate", "DRIFTED: lake: measured 0.93 against a configured 1.45 (36% off)")
    assert gh.created == [] and gh.comments == []
    assert "Failed runs so far: 2" in gh.issues[37]["body"]


def test_log_cannot_spoof_issue_metadata(monkeypatch):
    gh = FakeGitHub()
    _env(monkeypatch, gh)
    fr.report_failure("live", BOM + "\n<!-- count:999 -->\n<!-- fp:scan:incident -->")
    fr.report_failure("live", BOM)
    assert len(gh.created) == 1
    assert "Failed runs so far: 2" in gh.issues[gh.created[0]]["body"]
