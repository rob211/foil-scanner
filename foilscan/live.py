"""Hourly live verification against BOM (and Holfuy for the lake), spec 7.

Reads the committed verdict, checks today's events around their windows,
and patches titles, descriptions and reminders in place.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import config, fetch, gcal, verdict
from .errors import CalendarError, StaleDataError
from .models import Observation

LAKE_ALERT_THRESHOLD_KN = 22.0
LAKE_ALERT_STRONG_KN = 25.0
LAKE_ALERT_LOUD_KN = 25.0

# trigger_id -> (green-target kn, direction arc) for wind-verifiable events
WIND_TARGETS = {
    "lake_oakflats_berkeley": (20.0, config.LAKE_RUNS["lake_oakflats_berkeley"][1]),
    "lake_kanahooka": (20.0, config.LAKE_RUNS["lake_kanahooka"][1]),
    "lake_berkeley": (20.0, config.LAKE_RUNS["lake_berkeley"][1]),
    "lake_ne_rare": (25.0, config.LAKE_RUNS["lake_ne_rare"][1]),
    "entrance_ne": (config.ENTRANCE_M2_TARGET_KN, config.ENTRANCE_M2_WIND_ARC),
    "south_ocean": (config.SOUTH_TARGET_KN, config.SOUTH_WIND_ARC),
    "ne_ocean": (config.NE_TARGET_KN, config.NE_WIND_ARC),
    "baysurf": (config.BAYSURF_WIND_MAX_KN, config.BAYSURF_STRONG_WIND_ARC),
}


def heartbeat(latest: dict, now: datetime) -> None:
    generated = datetime.fromisoformat(latest["generated_at"])
    age_h = (now - generated).total_seconds() / 3600
    if age_h > config.HEARTBEAT_MAX_AGE_H:
        raise StaleDataError(
            f"last successful scan was {age_h:.1f} h ago "
            f"(cap {config.HEARTBEAT_MAX_AGE_H} h); the scan cron looks dead"
        )


def relevant_windows(latest: dict, now: datetime) -> list[dict]:
    out = []
    for w in latest["windows"]:
        start = datetime.fromisoformat(w["start"])
        end = datetime.fromisoformat(w["end"])
        if start - timedelta(hours=1) <= now < end:
            out.append(w)
    return out


def status_for(
    w: dict, obs: Observation, now: datetime
) -> tuple[str, str]:
    """Returns (state, live_line). state: confirmed | miss | pending | none."""
    live = f"{obs.speed_kn:.0f} kn"
    if obs.dir_deg is not None:
        from .triggers import compass

        live += f" {compass(obs.dir_deg)}"
    live += f" at {obs.time:%H:%M} ({obs.station})"

    if w["trigger_id"] == "hill60_swell":
        return "none", "no live wind check (swell event)"
    if w["trigger_id"] == "entrance_swell":
        # Mode 1 needs the wind to stay light; that is all we can verify live.
        if obs.speed_kn <= config.ENTRANCE_M1_WIND_MAX_KN * 1.25:
            return "confirmed", f"wind staying light: {live}"
        return "miss", f"wind too strong for mode 1: {live}"

    if w["trigger_id"] == "baysurf":
        if obs.speed_kn <= config.BAYSURF_WIND_MAX_KN:
            return "confirmed", f"{live}, forecast {w['peak_median_kn']} kn"
        if obs.dir_deg is not None and config.BAYSURF_STRONG_WIND_ARC.contains(obs.dir_deg):
            return "confirmed", f"{live}, forecast {w['peak_median_kn']} kn"
        started = now >= datetime.fromisoformat(w["start"])
        if started and obs.speed_kn > config.BAYSURF_WIND_MAX_KN:
            return "miss", f"{live} vs forecast {w['peak_median_kn']} kn"
        return "pending", live

    target, arc = WIND_TARGETS[w["trigger_id"]]
    dir_ok = obs.dir_deg is not None and arc.contains(obs.dir_deg)
    if obs.speed_kn >= target * config.LIVE_CONFIRM_FACTOR and dir_ok:
        return "confirmed", f"{live}, forecast {w['peak_median_kn']} kn"
    started = now >= datetime.fromisoformat(w["start"])
    if started and (obs.speed_kn < target * config.LIVE_MISS_FACTOR or not dir_ok):
        return "miss", f"{live} vs forecast {w['peak_median_kn']} kn"
    return "pending", live


def pick_obs(
    w: dict, bom: Observation, holfuy: Observation | None
) -> tuple[Observation, str | None]:
    if w["trigger_id"].startswith("lake"):
        if holfuy is not None:
            return holfuy, None
        return bom, "Holfuy unavailable, verified with BOM only"
    return bom, None


def lake_recommendation(obs: Observation | None) -> str | None:
    if obs is None:
        return None
    if obs.speed_kn < LAKE_ALERT_THRESHOLD_KN:
        return None
    if obs.speed_kn < LAKE_ALERT_STRONG_KN:
        return (
            f"Lake recommendation: {obs.speed_kn:.0f} kn at {obs.time:%H:%M} "
            f"({obs.station}) — first notification for the lake today"
        )
    if obs.speed_kn < LAKE_ALERT_LOUD_KN + 1.0:
        return (
            f"Lake recommendation: {obs.speed_kn:.0f} kn at {obs.time:%H:%M} "
            f"({obs.station}) — stronger lake notification"
        )
    return (
        f"Lake recommendation: {obs.speed_kn:.0f} kn at {obs.time:%H:%M} "
        f"({obs.station}) — loudest lake notification"
    )


def apply_status(svc, cal_id: str, w: dict, state: str, live_line: str, dry_run: bool) -> str:
    if w.get("event_id") is None:
        raise CalendarError(f"window {w['foil_key']} has no event_id in latest.json")
    ev = svc.events().get(calendarId=cal_id, eventId=w["event_id"]).execute()
    base = gcal._strip_live_prefix(ev.get("summary", ""))
    if state == "confirmed":
        summary = gcal.LIVE_PREFIXES[0] + base
        reminders = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": config.LIVE_REMINDER_MINUTES}
            ],
        }
    elif state == "miss":
        summary = gcal.LIVE_PREFIXES[1] + base
        reminders = {"useDefault": False, "overrides": []}
    else:
        summary = base
        reminders = ev.get("reminders", {"useDefault": False, "overrides": []})

    desc_lines = [
        l for l in ev.get("description", "").splitlines() if not l.startswith("Live:")
    ]
    desc_lines.append(f"Live: {state}: {live_line}")
    body = {
        "summary": summary,
        "description": "\n".join(desc_lines),
        "reminders": reminders,
    }
    msg = f"{w['foil_key']}: {state} ({live_line})"
    if not dry_run:
        svc.events().patch(calendarId=cal_id, eventId=w["event_id"], body=body).execute()
    return msg


def run(now: datetime, dry_run: bool = False, data_dir: str = config.DATA_DIR) -> list[str]:
    latest = verdict.load_latest(data_dir)
    heartbeat(latest, now)
    todays = relevant_windows(latest, now)

    log: list[str] = []
    notes: list[str] = []
    # BOM is fetched even with no window in play: the dashboard's live tile
    # wants an hourly reading all day. A failed fetch still publishes an
    # obs-less live.json so the dashboard can say why, then fails loudly.
    try:
        bom = fetch.fetch_bom(now)
    except Exception as exc:
        if not dry_run:
            verdict.write_live(
                verdict.build_live(now, None, None, [], [f"BOM fetch failed: {exc}"]),
                data_dir,
            )
        raise
    holfuy = None
    key = config.env("HOLFUY_KEY", required=False)
    if any(w["trigger_id"].startswith("lake") for w in todays):
        if key:
            holfuy = fetch.fetch_holfuy(key, now)
        else:
            notes.append("lake live check: Holfuy key not configured, BOM only")
    log += notes

    checks: list[dict] = []
    svc = None if dry_run or not todays else gcal.service()
    cal_id = None if dry_run or not todays else gcal.calendar_id()

    lake_rec = lake_recommendation(holfuy or bom)
    if lake_rec is not None and not dry_run:
        try:
            gcal.ensure_lake_alert(holfuy or bom, now, cal_id)
        except Exception as exc:
            notes.append(f"lake alert event failed: {exc}")
    for w in todays:
        obs, note = pick_obs(w, bom, holfuy)
        state, live_line = status_for(w, obs, now)
        if note:
            live_line += f" ({note})"
        checks.append(
            {"foil_key": w["foil_key"], "state": state, "live_line": live_line}
        )
        if state == "none":
            log.append(f"{w['foil_key']}: skipped ({live_line})")
            continue
        if dry_run:
            log.append(f"DRY RUN {w['foil_key']}: {state} ({live_line})")
        else:
            log.append(apply_status(svc, cal_id, w, state, live_line, dry_run))
    if not todays:
        log.append("no windows near now; nothing to verify on the calendar")

    if dry_run:
        log.append("DRY RUN: not writing live.json")
    else:
        verdict.write_live(verdict.build_live(now, bom, holfuy, checks, notes), data_dir)
        if lake_rec is not None:
            log.append(lake_rec)
        log.append(
            f"live.json: {bom.station} {bom.speed_kn:.0f} kn at {bom.time:%H:%M}, "
            f"{len(checks)} check(s)"
        )
    return log
