"""Google Calendar sync (spec section 7).

Diff-based full sync keyed on extendedProperties.private.foil_key. Events
without a foil_key are never touched. Auth is a service account that the
Foiling calendar has been shared with (see SETUP.md).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from . import config
from .errors import CalendarError
from .models import Window
from .triggers import compass

SCOPES = ["https://www.googleapis.com/auth/calendar"]

LIVE_PREFIXES = ("LIVE NOW ✅ ", "⚠ NOT VERIFYING ")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _one_line(text: object, cap: int = 140) -> str:
    """Bound text that ends up on the PUBLIC calendar. Notes and error
    strings can carry upstream fragments; flatten to one line, drop control
    characters and cap length so nothing unbounded reaches subscribers."""
    return _CONTROL_CHARS.sub(" ", str(text)).strip()[:cap]


def service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = config.env("GCAL_SERVICE_ACCOUNT_JSON")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CalendarError(f"GCAL_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from exc
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def calendar_id() -> str:
    return config.env("FOIL_CALENDAR_ID")


def title_for(w: Window) -> str:
    # Fold the individual spots back into the calendar title (dashboard shows
    # them as separate chips; the calendar keeps them on one line).
    name = w.run_name + (f" ({', '.join(w.spots)})" if w.spots else "")
    if w.trigger_id == "hill60_swell":
        core = f"{name}: {w.swell_m:.1f} m {compass(w.swell_dir_deg)}"
    elif w.trigger_id == "entrance_swell":
        core = f"{name}: {w.swell_m:.1f} m {compass(w.swell_dir_deg)} swell"
    else:
        core = f"{name}: {w.peak_median_kn:.0f} kn {compass(w.direction_deg)}"
    rare = "RARE" in w.title_tags
    tags = [t for t in w.title_tags if t != "RARE"]
    if tags:
        core += " (" + ", ".join(tags) + ")"
    return ("RARE: " if rare else "") + core


def description_for(w: Window, generated_at: datetime, source_notes: list[str]) -> str:
    lines = []
    if w.model_values:
        vals = ", ".join(f"{m} {v:.0f} kn" for m, v in w.model_values.items())
        lines.append(
            f"Models at peak ({w.peak_time:%H:%M}): {vals} "
            f"({w.models_agreeing}/{len(config.MODELS)} agree)"
        )
        lines.append(f"Direction: {w.direction_deg:.0f} ({compass(w.direction_deg)})")
    if w.swell_m is not None:
        lines.append(f"Swell: {w.swell_m:.1f} m from {compass(w.swell_dir_deg)}")
    if w.high_tide:
        ht = datetime.fromisoformat(w.high_tide)
        lines.append(f"High tide: {ht:%H:%M} (window is high tide to +2 h)")
    for note in w.notes:
        lines.append(f"Note: {_one_line(note)}")
    lines.append(f"Confidence: {w.confidence}")
    lines.append(f"Live: {w.live_status}")
    for note in source_notes:
        lines.append(f"SOURCE PROBLEM: {_one_line(note)}")
    lines.append(f"Generated: {generated_at:%Y-%m-%d %H:%M %Z} by foil-scanner")
    return "\n".join(lines)


def desired_body(w: Window, generated_at: datetime, source_notes: list[str]) -> dict:
    return {
        "summary": title_for(w),
        "description": description_for(w, generated_at, source_notes),
        "start": {"dateTime": w.start.isoformat(), "timeZone": str(config.TZ)},
        "end": {"dateTime": w.end.isoformat(), "timeZone": str(config.TZ)},
        "colorId": config.COLOR_IDS[w.grade],
        "extendedProperties": {"private": {"foil_key": w.foil_key}},
        "reminders": {"useDefault": False, "overrides": []},
    }


def _strip_live_prefix(summary: str) -> str:
    for p in LIVE_PREFIXES:
        if summary.startswith(p):
            return summary[len(p) :]
    return summary


def list_managed(svc, cal_id: str, now: datetime) -> dict[str, dict]:
    """All events in the horizon carrying a foil_key, keyed by it."""
    time_min = (now - timedelta(days=1)).isoformat()
    time_max = (now + timedelta(days=config.FORECAST_DAYS + 1)).isoformat()
    out: dict[str, dict] = {}
    token = None
    while True:
        resp = (
            svc.events()
            .list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                maxResults=2500,
                pageToken=token,
            )
            .execute()
        )
        for ev in resp.get("items", []):
            key = ev.get("extendedProperties", {}).get("private", {}).get("foil_key")
            if key:
                if key in out:
                    raise CalendarError(f"duplicate foil_key on calendar: {key}")
                out[key] = ev
        token = resp.get("nextPageToken")
        if not token:
            return out


def _needs_patch(existing: dict, want: dict) -> bool:
    if _strip_live_prefix(existing.get("summary", "")) != want["summary"]:
        return True
    if existing.get("colorId") != want["colorId"]:
        return True
    for edge in ("start", "end"):
        have = existing.get(edge, {}).get("dateTime", "")
        if datetime.fromisoformat(have) != datetime.fromisoformat(want[edge]["dateTime"]):
            return True
    # Live lines are owned by the live job; compare everything else.
    have_desc = [
        l for l in existing.get("description", "").splitlines() if not l.startswith("Live:")
    ]
    want_desc = [l for l in want["description"].splitlines() if not l.startswith("Live:")]
    return have_desc != want_desc


def sync(
    windows: list[Window],
    generated_at: datetime,
    source_notes: list[str],
    dry_run: bool = False,
) -> list[str]:
    """Returns a plan log. Fills window.event_id on the way through."""
    plan: list[str] = []
    desired = {w.foil_key: w for w in windows}
    if len(desired) != len(windows):
        raise CalendarError("duplicate foil_key among computed windows")

    if dry_run:
        for key, w in sorted(desired.items()):
            plan.append(f"DRY RUN would ensure: [{w.grade}] {title_for(w)} ({key})")
        return plan

    svc = service()
    cal_id = calendar_id()
    existing = list_managed(svc, cal_id, generated_at)

    for key, w in sorted(desired.items()):
        body = desired_body(w, generated_at, source_notes)
        have = existing.pop(key, None)
        if have is None:
            created = svc.events().insert(calendarId=cal_id, body=body).execute()
            w.event_id = created["id"]
            plan.append(f"created {key}: {body['summary']}")
        else:
            w.event_id = have["id"]
            if _needs_patch(have, body):
                svc.events().patch(
                    calendarId=cal_id, eventId=have["id"], body=body
                ).execute()
                plan.append(f"updated {key}: {body['summary']}")
            else:
                plan.append(f"unchanged {key}")

    for key, ev in existing.items():
        # Anything left is stale, including recovered broken:* flags.
        svc.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
        plan.append(f"deleted stale {key}: {ev.get('summary', '')}")
    return plan


def write_broken_event(reason: str, now: datetime) -> None:
    """Best-effort red flag on today (spec 8.6). Callers swallow errors from
    this only after the run is already failing."""
    svc = service()
    cal_id = calendar_id()
    day = now.date().isoformat()
    key = f"broken:{day}"
    body = {
        "summary": f"SCANNER BROKEN: {_one_line(reason, 120)}",
        # Keep raw exception text off the public calendar; it can embed
        # upstream response fragments. Full detail stays in the Actions logs.
        "description": (
            f"foil-scanner failed at {now.isoformat()}.\n"
            "Full details are in the repo's Actions logs, not published here."
        ),
        "start": {"date": day},
        "end": {"date": day},
        "colorId": config.COLOR_IDS["red"],
        "extendedProperties": {"private": {"foil_key": key}},
    }
    for ev in list_managed(svc, cal_id, now).values():
        ep = ev.get("extendedProperties", {}).get("private", {}).get("foil_key", "")
        if ep == key:
            svc.events().patch(calendarId=cal_id, eventId=ev["id"], body=body).execute()
            return
    svc.events().insert(calendarId=cal_id, body=body).execute()
