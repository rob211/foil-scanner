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
    elif w.trigger_id == "baysurf":
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
        height = f", {w.high_tide_m:.1f} m" if w.high_tide_m is not None else ""
        if w.trigger_id == "entrance_reverse":
            lines.append(
                f"High tide: {ht:%H:%M}{height} "
                f"(window is low tide +{config.ENTRANCE_REVERSE_START_AFTER_LOW_H:.0f} h "
                f"to high tide -{config.ENTRANCE_REVERSE_END_BEFORE_HIGH_H:.0f} h)"
            )
        else:
            lines.append(f"High tide: {ht:%H:%M}{height} (window is high tide to +2 h)")
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


def _alert_tier(speed: float) -> str:
    """Three real tiers now. LAKE_ALERT_LOUD_KN used to equal
    LAKE_ALERT_STRONG_KN, which made the loudest unreachable and left
    live.lake_recommendation's three tiers disagreeing with this function's
    two (10 Aug 2026 review)."""
    if speed >= config.LAKE_ALERT_LOUD_KN:
        return "!!!"
    if speed >= config.LAKE_ALERT_STRONG_KN:
        return "!!"
    return ""


def alert_body(
    run_name: str,
    obs: object,
    now: datetime,
    key: str,
    detail: str = "",
) -> dict:
    """A live wind alert, as a TIMED event with a popup.

    It was an all-day event with `reminders.overrides` set to an empty list,
    which is two separate reasons it could never reach a phone: an empty
    overrides list with useDefault off means no notification at all, and an
    all-day event's reminder offset is measured from local midnight anyway, so
    even a populated one could not have fired at the moment the wind was
    blowing (10 Aug 2026: 32 kn NW all morning, nothing rang).

    Start sits config.ALERT_LEAD_S ahead of now so the 0-minute popup is still
    in the future when Google receives it; a popup on a past start never
    fires. ensure_alert never moves the start once set, so re-patching it on
    later polls cannot re-notify.
    """
    speed = getattr(obs, "speed_kn", 0.0) or 0.0
    gust = getattr(obs, "gust_kn", None)
    station = getattr(obs, "station", "live") or "live"
    dir_deg = getattr(obs, "dir_deg", None)
    where = f" {compass(dir_deg)}" if dir_deg is not None else ""
    start = now + timedelta(seconds=config.ALERT_LEAD_S)
    end = start + timedelta(hours=config.ALERT_DURATION_H)
    lines = [
        f"Live wind matches {run_name} right now, whatever the forecast said.",
        f"{speed:.0f} kn{where}"
        + (f", gusting {gust:.0f}" if gust is not None else "")
        + f" at {station}, observed {now:%H:%M}.",
    ]
    if detail:
        lines.append(_one_line(detail))
    lines.append("Fired from live observations, not from a forecast window.")
    return {
        "summary": f"WIND NOW{_alert_tier(speed)}: {run_name} {speed:.0f} kn{where}",
        "description": "\n".join(lines),
        "start": {"dateTime": start.isoformat(), "timeZone": str(config.TZ)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(config.TZ)},
        "colorId": config.COLOR_IDS["red"],
        "extendedProperties": {"private": {"foil_key": key}},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": config.ALERT_REMINDER_MINUTES}
            ],
        },
    }


def ensure_alert(
    run_name: str,
    obs: object,
    now: datetime,
    cal_id: str,
    key: str,
    detail: str = "",
) -> bool:
    """Create or refresh one live alert. Returns True when it was created.

    An existing alert for the same key keeps its original start and reminder -
    only the title, description and end move - so a blow that lasts four
    hours pings once, not once per poll.
    """
    svc = service()
    body = alert_body(run_name, obs, now, key, detail)
    for ev in list_managed(svc, cal_id, now).values():
        ep = ev.get("extendedProperties", {}).get("private", {}).get("foil_key", "")
        if ep != key:
            continue
        keep_start = ev.get("start", {}).get("dateTime")
        if keep_start:
            body["start"] = ev["start"]
            end = max(
                datetime.fromisoformat(keep_start)
                + timedelta(hours=config.ALERT_DURATION_H),
                datetime.fromisoformat(body["end"]["dateTime"]),
            )
            body["end"] = {"dateTime": end.isoformat(), "timeZone": str(config.TZ)}
        body.pop("reminders", None)
        svc.events().patch(calendarId=cal_id, eventId=ev["id"], body=body).execute()
        return False
    svc.events().insert(calendarId=cal_id, body=body).execute()
    return True


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


def watch_digest_bodies(
    watches: list[Window], near_misses: list, generated_at: datetime
) -> dict[str, dict]:
    """One graphite all-day event per day collecting that day's maybes.

    Rob asked for the wider band to be "flagged in calendar" so the models
    could be looked at if needed. A timed event per watch window would bury
    the real runs, so each day gets a single digest instead: the title says
    how many, the description says which, when, and why each one only reached
    watch. Near misses ride along - a window the 4.6 swell rule vetoed is
    worth seeing on the day even though it is deliberately not a maybe.
    """
    by_day: dict[str, list[str]] = {}
    names: dict[str, list[str]] = {}
    for w in sorted(watches, key=lambda w: (w.start, w.trigger_id)):
        day = w.start.date().isoformat()
        by_day.setdefault(day, []).append(
            f"{w.start:%H:%M}-{w.end:%H:%M}  {w.run_name}: "
            f"{w.peak_median_kn:.0f} kn {compass(w.direction_deg)} - {w.watch}"
        )
        names.setdefault(day, []).append(w.run_name)
    for m in sorted(near_misses, key=lambda m: (m.date, m.trigger_id)):
        start, end = datetime.fromisoformat(m.start), datetime.fromisoformat(m.end)
        by_day.setdefault(m.date, []).append(
            f"{start:%H:%M}-{end:%H:%M}  {m.trigger_id}: "
            f"{m.reason.replace('_', ' ')} - {_one_line(m.detail, 100)}"
        )
    out = {}
    for day, lines in by_day.items():
        uniq = list(dict.fromkeys(names.get(day, [])))
        head = ", ".join(uniq[:2]) + (f" +{len(uniq) - 2}" if len(uniq) > 2 else "")
        out[f"watch:{day}"] = {
            "summary": f"WATCH: {head}" if uniq else "WATCH: near misses only",
            "description": "\n".join(
                lines
                + [
                    "",
                    "Not a call to go - these missed on strength, model "
                    "agreement or tide. Worth a look at the models.",
                    f"Generated: {generated_at:%Y-%m-%d %H:%M %Z} by foil-scanner",
                ]
            ),
            "start": {"date": day},
            "end": {"date": _next_day(day)},
            "colorId": config.COLOR_IDS["watch"],
            "extendedProperties": {"private": {"foil_key": f"watch:{day}"}},
            "reminders": {"useDefault": False, "overrides": []},
        }
    return out


def _next_day(day: str) -> str:
    """Google treats an all-day event's end.date as exclusive, so a body with
    start.date == end.date describes a zero-length event. Every all-day event
    here had that bug (10 Aug 2026 review); the tests mock the API, so it was
    never exercised against Google."""
    return (datetime.fromisoformat(day).date() + timedelta(days=1)).isoformat()


def _needs_patch_allday(existing: dict, want: dict) -> bool:
    return (
        existing.get("summary", "") != want["summary"]
        or existing.get("description", "") != want["description"]
        or existing.get("colorId") != want["colorId"]
    )


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
    near_misses: list | None = None,
) -> list[str]:
    """Returns a plan log. Fills window.event_id on the way through."""
    plan: list[str] = []
    # Watch windows do not get their own timed events; they are collected into
    # one all-day digest per day so the maybes never crowd out the real runs.
    runs = [w for w in windows if w.grade != "watch"]
    watches = [w for w in windows if w.grade == "watch"]
    desired = {w.foil_key: w for w in runs}
    if len(desired) != len(runs):
        raise CalendarError("duplicate foil_key among computed windows")
    digests = watch_digest_bodies(watches, near_misses or [], generated_at)

    if dry_run:
        for key, w in sorted(desired.items()):
            plan.append(f"DRY RUN would ensure: [{w.grade}] {title_for(w)} ({key})")
        for key, body in sorted(digests.items()):
            plan.append(f"DRY RUN would ensure: [watch] {body['summary']} ({key})")
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

    for key, body in sorted(digests.items()):
        have = existing.pop(key, None)
        if have is None:
            svc.events().insert(calendarId=cal_id, body=body).execute()
            plan.append(f"created {key}: {body['summary']}")
        elif _needs_patch_allday(have, body):
            svc.events().patch(
                calendarId=cal_id, eventId=have["id"], body=body
            ).execute()
            plan.append(f"updated {key}: {body['summary']}")
        else:
            plan.append(f"unchanged {key}")

    for key, ev in existing.items():
        # Anything left is stale, including recovered broken:* flags - but
        # not live-alert:*: those are same-day live notifications owned by
        # live.py's safety net, not forecast windows, and the whole point of
        # them is that no forecast window exists, so every scan would
        # otherwise delete them on sight (found 2026-08-04, when this
        # deleted every alert the cal_id/threshold fixes had just made
        # possible again).
        #
        # lake-alert:* is deliberately NOT protected any more. Nothing writes
        # that key since the safety net went trigger-wide on 10 Aug 2026, so
        # the only ones left are the old zero-length all-day events that
        # Google accepted but never rendered - letting the sweep collect them
        # is the cleanup.
        if key.startswith("live-alert:"):
            # Today's belong to the live job, which is still refreshing them,
            # and no forecast window backs them so `desired` will never hold
            # them. Older ones are dead weight: without this they accumulate
            # on the calendar forever (10 Aug's 19:53 alerts were still
            # sitting there the next morning).
            parts = key.split(":")
            if len(parts) >= 2 and parts[1] == generated_at.date().isoformat():
                continue
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
        # Exclusive end date - see _next_day. This was `day`, a zero-length
        # all-day range that Google rejects.
        "end": {"date": _next_day(day)},
        "colorId": config.COLOR_IDS["red"],
        "extendedProperties": {"private": {"foil_key": key}},
    }
    for ev in list_managed(svc, cal_id, now).values():
        ep = ev.get("extendedProperties", {}).get("private", {}).get("foil_key", "")
        if ep == key:
            svc.events().patch(calendarId=cal_id, eventId=ev["id"], body=body).execute()
            return
    svc.events().insert(calendarId=cal_id, body=body).execute()
