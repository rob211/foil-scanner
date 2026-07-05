"""Untrusted upstream strings must not reach public sinks unbounded
(5 Jul 2026 security review, findings 2/3/4)."""
from datetime import datetime, timedelta

from foilscan import config, fetch, gcal

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=config.TZ)


def test_clean_label_strips_control_chars_and_caps():
    dirty = "Bellambi\r\nBEGIN:VEVENT\tSUMMARY:evil " + "x" * 200
    out = fetch.clean_label(dirty)
    assert "\n" not in out and "\r" not in out and "\t" not in out
    assert len(out) <= 60
    assert out.startswith("Bellambi")


def test_clean_label_leaves_normal_names_intact():
    assert fetch.clean_label("Lake Illawarra") == "Lake Illawarra"
    assert fetch.clean_label("Bellambi") == "Bellambi"


def _bom_payload(when, name="Test Station"):
    return {
        "observations": {
            "data": [
                {
                    "name": name,
                    "local_date_time_full": when.strftime("%Y%m%d%H%M%S"),
                    "wind_spd_kmh": 37,
                    "gust_kmh": 46,
                    "wind_dir": "SSW",
                }
            ]
        }
    }


def test_fetch_bom_sanitises_station_name(monkeypatch):
    hostile = "Bellambi\n\nhttp://evil.example/login " + "z" * 300
    payload = _bom_payload(NOW - timedelta(minutes=10), name=hostile)
    monkeypatch.setattr(fetch, "get_json", lambda *a, **k: payload)
    obs = fetch.fetch_bom(NOW)
    assert "\n" not in obs.station
    assert len(obs.station) <= 60


def test_one_line_flattens_and_caps():
    out = gcal._one_line("line one\nline two\x00\x07 end", cap=140)
    assert "\n" not in out and "\x00" not in out
    out2 = gcal._one_line("y" * 500, cap=120)
    assert len(out2) == 120


class _FakeEvents:
    def __init__(self, captured):
        self._captured = captured

    def list(self, **kwargs):
        return _FakeExec({"items": []})

    def insert(self, calendarId, body):
        self._captured.append(body)
        return _FakeExec({"id": "new"})


class _FakeExec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _FakeSvc:
    def __init__(self, captured):
        self._events = _FakeEvents(captured)

    def events(self):
        return self._events


def test_broken_event_keeps_raw_exception_text_off_the_calendar(monkeypatch):
    captured = []
    monkeypatch.setattr(gcal, "service", lambda: _FakeSvc(captured))
    monkeypatch.setattr(gcal, "calendar_id", lambda: "cal")
    reason = "SchemaError: BOM sent 'http://evil.example/steal'\nsecond line " + "q" * 400
    gcal.write_broken_event(reason, NOW)
    body = captured[0]
    # Raw, unbounded reason must not appear in the published description.
    assert "evil.example" not in body["description"]
    assert "Actions logs" in body["description"]
    # Summary stays a single bounded line for the operator.
    assert "\n" not in body["summary"]
    assert len(body["summary"]) <= len("SCANNER BROKEN: ") + 120
