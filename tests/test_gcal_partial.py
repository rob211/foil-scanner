"""A partial pass must not read an absent window as a cancelled one.

6 Sep 2026: the ocean wind fetch timed out, main.py skipped the whole ocean
trigger family, and the stale sweep deleted a real Hill 60 run off the
calendar - "deleted stale hill60_swell:2026-09-10:13" - before the job went
red. The events were never wrong; they were never evaluated.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from foilscan import config, gcal
from foilscan.models import Window

NOW = datetime(2026, 9, 6, 8, 0, tzinfo=config.TZ)


class FakeEvents:
    def __init__(self, log):
        self.log = log

    def _op(self, name):
        def call(**kwargs):
            self.log.append((name, kwargs.get("eventId") or kwargs.get("body")))

            class R:
                @staticmethod
                def execute():
                    return {"id": "new-event-id"}

            return R()

        return call

    def __getattr__(self, name):
        return self._op(name)


class FakeService:
    def __init__(self, log):
        self._events = FakeEvents(log)

    def events(self):
        return self._events


def mk_window(trigger_id="lake_west", hour=12, grade="green", watch=None) -> Window:
    start = NOW.replace(hour=hour, minute=0)
    return Window(
        trigger_id=trigger_id,
        run_name="Oak Flats to Berkeley",
        start=start,
        end=start + timedelta(hours=2),
        grade=grade,
        peak_time=start,
        peak_median_kn=22.0,
        direction_deg=250.0,
        models_agreeing=4,
        model_values={m: 22.0 for m in config.MODELS},
        watch=watch,
    )


@pytest.fixture
def calendar(monkeypatch):
    """Returns (ops_log, set_existing). No network, no credentials."""
    log: list[tuple[str, object]] = []
    state: dict[str, dict] = {}
    monkeypatch.setattr(gcal, "service", lambda: FakeService(log))
    monkeypatch.setattr(gcal, "calendar_id", lambda: "cal-id")
    monkeypatch.setattr(gcal, "list_managed", lambda svc, cal_id, now: dict(state))
    return log, state


def deletes(log):
    return [event_id for name, event_id in log if name == "delete"]


def inserted_keys(log):
    return [
        body["extendedProperties"]["private"]["foil_key"]
        for name, body in log
        if name == "insert" and isinstance(body, dict)
    ]


def test_complete_pass_sweeps_an_unmatched_event(calendar):
    log, state = calendar
    state["hill60_swell:2026-09-10:13"] = {"id": "ev-hill60", "summary": "Hill 60"}

    gcal.sync([mk_window()], NOW, [], complete=True)

    assert deletes(log) == ["ev-hill60"]


def test_partial_pass_keeps_an_unmatched_event(calendar):
    log, state = calendar
    state["hill60_swell:2026-09-10:13"] = {"id": "ev-hill60", "summary": "Hill 60"}

    plan = gcal.sync([mk_window()], NOW, [], complete=False)

    assert deletes(log) == []
    assert any("stale sweep deferred" in line for line in plan)


def test_partial_pass_still_publishes_what_it_computed(calendar):
    """The lake answered; its window is as good as on any other run."""
    log, _ = calendar

    gcal.sync([mk_window()], NOW, [], complete=False)

    assert inserted_keys(log) == ["lake_west:2026-09-06:12"]


def test_partial_pass_does_not_republish_a_truncated_digest(calendar):
    """A digest is rebuilt wholesale, so a partial one silently drops the
    families that never ran. Yesterday's complete digest is the better answer."""
    log, _ = calendar
    watch = mk_window(grade="watch", watch="one model only")

    gcal.sync([watch], NOW, [], complete=False)

    assert not any(key.startswith("watch:") for key in inserted_keys(log))
    assert deletes(log) == []


def test_complete_pass_still_writes_the_digest(calendar):
    log, _ = calendar
    watch = mk_window(grade="watch", watch="one model only")

    gcal.sync([watch], NOW, [], complete=True)

    assert any(key.startswith("watch:") for key in inserted_keys(log))


def test_dry_run_reports_the_deferral(calendar):
    plan = gcal.sync([mk_window()], NOW, [], dry_run=True, complete=False)

    assert any("stale sweep would be skipped" in line for line in plan)
