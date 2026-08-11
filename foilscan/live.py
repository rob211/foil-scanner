"""Live verification against BOM (and Holfuy for the lake), spec 7.

Reads the committed verdict, checks today's events around their windows,
and patches titles, descriptions and reminders in place. Runs hourly
overnight, every 30 min during local daylight hours - see _should_run.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import config, fetch, gcal, verdict
from .errors import CalendarError, StaleDataError
from .models import Observation

# trigger_id -> (green-target kn, direction arc) for wind-verifiable events
WIND_TARGETS = {
    "lake_oakflats_berkeley": (20.0, config.LAKE_RUNS["lake_oakflats_berkeley"][1]),
    "lake_west": (20.0, config.LAKE_RUNS["lake_west"][1]),
    "lake_ne_rare": (25.0, config.LAKE_RUNS["lake_ne_rare"][1]),
    "entrance_ne": (config.ENTRANCE_M2_TARGET_KN, config.ENTRANCE_M2_WIND_ARC),
    # Missing since the reverse run was added (spec 4.8): the first live
    # reverse-run window would have crashed the whole live job on a KeyError
    # here, taking every other check on the day down with it (found 10 Aug
    # 2026 running the live job against a real reverse-run watch).
    "entrance_reverse": (
        config.ENTRANCE_REVERSE_TARGET_KN,
        config.ENTRANCE_REVERSE_WIND_ARC,
    ),
    "south_ocean": (config.SOUTH_TARGET_KN, config.SOUTH_WIND_ARC),
    "ne_ocean": (config.NE_TARGET_KN, config.NE_WIND_ARC),
    "baysurf": (config.BAYSURF_WIND_MAX_KN, config.BAYSURF_STRONG_WIND_ARC),
}


def _should_run(now: datetime, force: bool = False) -> bool:
    """Whether this tick should do any work.

    `force` short-circuits it for a dispatched run. The Cloudflare Worker
    applies this same rule before dispatching, so re-applying it here can
    only produce false negatives: the gate is evaluated against the moment
    GitHub *starts* the job, not the moment the Worker asked for it, and a
    :00 dispatch that starts after :30 was being dropped overnight without
    writing live.json at all.

    The repo's own schedule backstop still arrives ungated, so the rule has
    to stay for that path."""
    if force:
        return True
    if now.minute < 30:
        return True
    return config.LIVE_FAST_POLL_START_HOUR <= now.hour < config.LIVE_FAST_POLL_END_HOUR


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
        # Watch windows are maybes: they live in the day's digest event, not
        # as events of their own, so they have no event_id to patch and
        # nothing to verify against.
        if w.get("grade") == "watch":
            continue
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
        # A swell run on an ocean-facing entrance: the live wind can only
        # spoil it, never make it. Offshore is fine at any strength - it
        # grooms the face - and light is fine from anywhere. Only an onshore
        # blow hard enough to wreck it counts as a miss.
        offshore = obs.dir_deg is not None and config.ENTRANCE_M1_WIND_ARC.contains(obs.dir_deg)
        if offshore or obs.speed_kn <= config.ENTRANCE_M1_WIND_MAX_KN:
            return "confirmed", f"wind favourable for the swell: {live}"
        if obs.speed_kn >= config.ENTRANCE_WIND_NO_GO_KN:
            return "miss", f"onshore and too strong for the swell: {live}"
        return "pending", f"onshore but not wrecking it: {live}"

    if w["trigger_id"] == "baysurf":
        if obs.speed_kn <= config.BAYSURF_WIND_MAX_KN:
            return "confirmed", f"{live}, forecast {w['peak_median_kn']} kn"
        if obs.dir_deg is not None and config.BAYSURF_STRONG_WIND_ARC.contains(obs.dir_deg):
            return "confirmed", f"{live}, forecast {w['peak_median_kn']} kn"
        started = now >= datetime.fromisoformat(w["start"])
        if started and obs.speed_kn > config.BAYSURF_WIND_MAX_KN:
            return "miss", f"{live} vs forecast {w['peak_median_kn']} kn"
        return "pending", live

    if w["trigger_id"] not in WIND_TARGETS:
        # Loud, not a KeyError three frames down: an unknown trigger id here
        # means a new trigger family shipped without its live check.
        raise CalendarError(
            f"no live wind target configured for trigger {w['trigger_id']!r}"
        )
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
    """Station for verifying a forecast window. Deliberately not `stronger()`:
    verification asks whether the forecast for *this spot* was right, so the
    spot's own station is the authority. `stronger()` belongs to the alert
    path, which asks the different question of whether anything is blowing
    anywhere that the forecast missed."""
    if w["trigger_id"].startswith("lake"):
        if holfuy is not None:
            return holfuy, None
        return bom, "Holfuy unavailable, verified with BOM only"
    return bom, None


# Triggers whose alert is worth annotating with today's tide gate.
_TIDE_GATED = ("entrance_reverse", "entrance_ne")


def _tide_notes(marine, now: datetime) -> dict[str, str]:
    """Today's tide gates, as a line per tide-gated trigger.

    An alert saying "32 kn NW right now" is more use when it also says the
    run-in gate is 13:00-17:00, so the tide can be judged rather than
    guessed - especially now that a window outside the gate is downgraded
    rather than deleted."""
    from .triggers import _entrance_reverse_tide_spans, _tide_spans

    def fmt(spans):
        today = [
            f"{lo:%H:%M}-{hi:%H:%M}"
            for lo, hi, *_ in spans
            if lo.date() == now.date()
        ]
        return ", ".join(today) if today else "none today"

    return {
        "entrance_reverse": f"Run-in gate today: {fmt(_entrance_reverse_tide_spans(marine))}.",
        "entrance_ne": f"Run-out gate today: {fmt(_tide_spans(marine))}.",
    }


def token_expiry_note(now: datetime) -> tuple[str | None, float | None]:
    """How long the Worker's GitHub PAT has left, and how loudly to say so.

    Returns (note, days_left). The Worker passes GitHub's
    github-authentication-token-expiration header through as a dispatch
    input; the workflow puts it in FOIL_TOKEN_EXPIRES_AT. Absent means this
    run came from the schedule backstop, which needs no PAT - not that the
    token is fine - so absence is silent by design.

    An expired PAT stops dispatches dead while the backstop quietly carries
    on at a reduced rate. Without this the only trace is a poll-gap note,
    which reads like GitHub shedding runs rather than a credential to renew.
    """
    raw = config.env("FOIL_TOKEN_EXPIRES_AT", required=False)
    if not raw:
        return None, None
    text = raw.strip().replace(" UTC", "+00:00")
    try:
        expires = datetime.fromisoformat(text)
    except ValueError:
        return f"could not read the token expiry {raw!r}", None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    days = (expires - now).total_seconds() / 86400
    if days <= 0:
        return (
            f"GitHub PAT EXPIRED {abs(days):.0f} day(s) ago "
            f"({expires:%Y-%m-%d}); the Worker cannot dispatch - roll it at "
            "github.com/settings/personal-access-tokens",
            days,
        )
    if days <= config.PAT_WARN_DAYS:
        return (
            f"GitHub PAT expires in {days:.0f} day(s) ({expires:%Y-%m-%d}); "
            "roll it at github.com/settings/personal-access-tokens and run "
            "`wrangler secret put GITHUB_TOKEN`",
            days,
        )
    return None, days


def poll_gap_note(data_dir, now: datetime) -> str | None:
    """Flag a gap since the previous live poll.

    GitHub sheds scheduled runs under load, and it shed three consecutive
    live ticks on 10 Aug 2026 - the last poll before 13:01 was 10:39, right
    across the peak of a 32 kn blow. Nothing recorded that, so a two and a
    half hour hole in the safety net looked identical to a quiet morning.
    Raising the cron rate does not fix shedding (a busier schedule gets shed
    harder) and costs Actions minutes, so this makes the hole visible
    instead of pretending it isn't there."""
    from pathlib import Path

    path = Path(data_dir) / "live.json"
    if not path.exists():
        return None
    try:
        previous = json.loads(path.read_text())["generated_at"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None
    gap_min = (now - datetime.fromisoformat(previous)).total_seconds() / 60
    if gap_min <= config.LIVE_POLL_GAP_WARN_MIN:
        return None
    return (
        f"poll gap: {gap_min:.0f} min since the last live check "
        f"({previous[11:16]}); GitHub shed scheduled runs, live alerts "
        f"could be that late"
    )


def stronger(a: Observation | None, b: Observation | None) -> Observation | None:
    """The windier of two readings.

    `holfuy or bom` used to decide this, so Holfuy always won when it
    answered and BOM was never consulted. On 10 Aug 2026 the 10:39 poll had
    Holfuy at 19.0 kn (under the 22 kn alert threshold, mid-lake and still
    sheltered from the NW) while Bellambi was reading 31.9. Nothing fired for
    another two and a half hours."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a.speed_kn >= b.speed_kn else b


def alert_obs(
    station_pref: str, bom: Observation | None, holfuy: Observation | None
) -> Observation | None:
    """Which reading speaks for a trigger. "lake" and "either" take whichever
    station is windier; the ocean runs only ever trust the coastal station."""
    if station_pref == "coast":
        return bom
    return stronger(holfuy, bom)


def alerting_hours(now: datetime, sun) -> bool:
    """Whether a live alert is allowed to ring at all.

    Every forecast trigger clips to daylight (spec 4); the live alerts did
    not, and the alert key is per-day, so a blow running through midnight
    minted a fresh alert - and a fresh 0-minute popup - at 3am (found the
    evening of 10 Aug 2026, 26 kn on the lake well after dark).

    A failed sun fetch falls back to the fast-poll window rather than
    alerting around the clock. That is not a fabricated measurement, it is a
    narrower notification window, so spec 8.1 is not in play: the failure
    mode is a missed ping, never a 3am one.
    """
    if sun is not None:
        try:
            return sun.daylight(now)
        except Exception:  # noqa: BLE001 - no entry for today, fall through
            pass
    return config.LIVE_FAST_POLL_START_HOUR <= now.hour < config.LIVE_FAST_POLL_END_HOUR


def live_alerts(
    now: datetime,
    bom: Observation | None,
    holfuy: Observation | None,
    covered: set[str],
    tide_notes: dict[str, str] | None = None,
    daylight: bool = True,
) -> list[dict]:
    """Live wind matching a trigger, with no forecast window behind it.

    This is the gap that let 10 Aug pass in silence: every alerting path in
    the scanner hung off a forecast window, so when all four models missed a
    32 kn NW blow there was no window, therefore nothing to verify, therefore
    no notification of any kind. These fire off the observation alone.

    `covered` holds trigger ids that already have a live window on the
    calendar - those are the live-verification job's business, not the safety
    net's, and double-notifying is worse than not notifying."""
    out = []
    if not daylight:
        return out
    best: dict[str, dict] = {}
    for tid, (threshold, arc, run_name, pref, group) in config.LIVE_ALERT_TRIGGERS.items():
        if tid in covered:
            continue
        obs = alert_obs(pref, bom, holfuy)
        if obs is None or obs.dir_deg is None:
            continue
        if obs.speed_kn < threshold or not arc.contains(obs.dir_deg):
            continue
        # One alert per body of water. The lake bands abut exactly, so a wind
        # hunting either side of a boundary used to mint an event per band,
        # each with its own popup. Where two triggers on the same water both
        # match, the one clearing its own bar by the widest margin wins.
        margin = obs.speed_kn / threshold
        if group in best and best[group]["_margin"] >= margin:
            continue
        best[group] = {
                "_margin": margin,
                "trigger_id": tid,
                "run_name": run_name,
                # The reading this decision was made on, so the calendar
                # writer never has to re-derive which station won.
                "obs": obs,
                "station": obs.station,
                "speed_kn": round(obs.speed_kn, 1),
                "dir_deg": obs.dir_deg,
                "threshold_kn": threshold,
                "detail": (tide_notes or {}).get(tid, ""),
                "foil_key": f"live-alert:{now.date().isoformat()}:{group}",
        }
    for row in best.values():
        row.pop("_margin", None)
        out.append(row)
    return out


def bias_rows(latest: dict, now: datetime, obs_by_key: dict) -> list[dict]:
    """Observed minus forecast for the current hour (spec 9, added 10 Aug
    2026). `expected_today` is written by the scan; an older latest.json from
    before this landed simply has none, which is not an error."""
    expected = latest.get("expected_today") or []
    hour = now.replace(minute=0, second=0, microsecond=0)
    row = next(
        (r for r in expected if datetime.fromisoformat(r["time"]) == hour), None
    )
    if row is None:
        # Returning [] made "the models were right" and "nobody checked"
        # identical. Between midnight and the day's first scan, expected_today
        # is yesterday's, and a bust left no trace at all - in the one place
        # built to catch busts.
        why = (
            "no expectation published yet"
            if not expected
            else f"expectation is from {expected[0]['time'][:10]}, not {hour.date()}"
        )
        return [
            {
                "location": key,
                "station": obs.station,
                "observed_kn": round(obs.speed_kn, 1),
                "forecast_kn": None,
                "gap_kn": None,
                "flagged": None,
                "reason": why,
            }
            for key, obs in obs_by_key.items()
            if obs is not None
        ]
    out = []
    for key, obs in obs_by_key.items():
        if obs is None or key not in row:
            continue
        gap = obs.speed_kn - row[key]
        out.append(
            {
                "location": key,
                "station": obs.station,
                "observed_kn": round(obs.speed_kn, 1),
                "forecast_kn": row[key],
                "gap_kn": round(gap, 1),
                "flagged": abs(gap) >= config.BIAS_FLAG_KN,
            }
        )
    return out


def lake_recommendation(obs: Observation | None) -> str | None:
    if obs is None:
        return None
    if obs.speed_kn < config.LAKE_ALERT_THRESHOLD_KN:
        return None
    if obs.speed_kn < config.LAKE_ALERT_STRONG_KN:
        tier = "first notification for the lake today"
    elif obs.speed_kn < config.LAKE_ALERT_LOUD_KN:
        tier = "stronger lake notification"
    else:
        tier = "loudest lake notification"
    return (
        f"Lake recommendation: {obs.speed_kn:.0f} kn at {obs.time:%H:%M} "
        f"({obs.station}) — {tier}"
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


def run(
    now: datetime,
    dry_run: bool = False,
    data_dir: str = config.DATA_DIR,
    force: bool = False,
) -> list[str]:
    if not _should_run(now, force):
        return [
            f"skipped: {now:%H:%M} fast-poll tick is outside local daylight hours "
            f"({config.LIVE_FAST_POLL_START_HOUR:02d}:00-{config.LIVE_FAST_POLL_END_HOUR:02d}:00), "
            "hourly tick still runs overnight"
        ]
    latest = verdict.load_latest(data_dir)
    heartbeat(latest, now)
    gap = poll_gap_note(data_dir, now)
    pat_note, pat_days = token_expiry_note(now)
    todays = relevant_windows(latest, now)

    log: list[str] = []
    notes: list[str] = [gap] if gap else []
    if pat_note:
        notes.append(pat_note)
        log.append(pat_note)
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
    # Fetched every run, not just when a lake window is forecast: the
    # lake_recommendation/live_alerts safety net below exists precisely
    # to catch wind the forecast didn't call, so it needs live data on every
    # hour, not only the hours the forecast already agreed with.
    holfuy = None
    key = config.env("HOLFUY_KEY", required=False)
    needs_holfuy = any(w["trigger_id"].startswith("lake") for w in todays)
    if key:
        try:
            holfuy = fetch.fetch_holfuy(key, now)
        except Exception as exc:
            # A live lake window needs this reading to verify itself, so a
            # dead feed there fails loud like any other needed source (spec
            # 8.9). Outside a lake window it is only feeding the safety net,
            # so note it and keep going rather than blocking unrelated checks.
            if needs_holfuy:
                raise
            notes.append(f"Holfuy fetch failed, lake safety-net check skipped this hour: {exc}")
    else:
        notes.append("Holfuy key not configured, BOM only")
    log += notes

    checks: list[dict] = []
    svc = None if dry_run or not todays else gcal.service()
    cal_id = None if dry_run or not todays else gcal.calendar_id()

    # cal_id above is None whenever nothing is forecast right now - exactly
    # the case the lake safety net exists for (wind the forecast missed), so
    # it cannot reuse that None here. Fetch it lazily instead of unlocking it
    # unconditionally at startup, so runs with nothing to do still don't
    # need calendar secrets configured (spec 8.9 / test_run_writes_live_json
    # _even_without_windows).
    # Daylight gate for the alerts (spec 4 applies it to every forecast
    # trigger; the live path skipped it). Best effort - a dead sun feed
    # narrows the window, it does not silence the safety net.
    sun = None
    try:
        sun = fetch.fetch_sun(now)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"sunrise/sunset unavailable, alerting on clock hours only: {exc}")
    daylight = alerting_hours(now, sun)

    # Logged every run, alongside Bellambi, and used for nothing yet. It is
    # 4 km from the entrance where Bellambi is 15, so it should eventually be
    # the entrance's station - but Rob's shelter caveat has to be measurable
    # before anything depends on it. Best effort: this is data collection,
    # not a decision, and it must never fail a run.
    port_kembla = None
    try:
        port_kembla = fetch.fetch_bom(now, config.BOM_STATION_PORT_KEMBLA)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Port Kembla station unavailable (logging only): {exc}")

    covered = {w["trigger_id"] for w in todays}
    alerts = live_alerts(now, bom, holfuy, covered, daylight=daylight)
    # Tide context, only when an entrance alert is actually going out - this
    # runs every 30 min all day and most polls have nothing to say. Best
    # effort either way: a dead marine feed must not stop a wind alert, it
    # only costs the gate times in the description.
    if any(a["trigger_id"] in _TIDE_GATED for a in alerts):
        try:
            tide_notes = _tide_notes(fetch.fetch_marine(now), now)
            for a in alerts:
                a["detail"] = tide_notes.get(a["trigger_id"], "")
        except Exception as exc:
            notes.append(f"tide context unavailable for alerts: {exc}")
    lake_rec = lake_recommendation(stronger(holfuy, bom))
    if not dry_run:
        for a in alerts:
            try:
                alert_cal_id = cal_id if cal_id is not None else gcal.calendar_id()
                created = gcal.ensure_alert(
                    a["run_name"],
                    # Same call live_alerts already made. Re-deriving it here
                    # is how the lake alert tiers drifted apart before (see
                    # config.LAKE_ALERT_*), so reuse the decision, don't
                    # repeat the rule.
                    a["obs"],
                    now,
                    alert_cal_id,
                    a["foil_key"],
                    a["detail"],
                )
                a["created"] = created
                log.append(
                    f"{'ALERT' if created else 'alert refreshed'}: {a['run_name']} "
                    f"{a['speed_kn']:.0f} kn at {a['station']}"
                )
            except Exception as exc:
                notes.append(f"live alert event failed for {a['trigger_id']}: {exc}")
    elif alerts:
        log += [
            f"DRY RUN alert: {a['run_name']} {a['speed_kn']:.0f} kn at {a['station']}"
            for a in alerts
        ]

    bias = bias_rows(latest, now, {"lake": holfuy, "ocean": bom})
    unchecked = [b for b in bias if b.get("flagged") is None]
    if unchecked:
        notes.append(f"model bias not checked: {unchecked[0]['reason']}")
    for b in bias:
        if b["flagged"]:
            line = (
                f"MODEL BUST ({b['location']}): observed {b['observed_kn']:.0f} kn "
                f"vs forecast {b['forecast_kn']:.0f} kn at {b['station']}"
            )
            notes.append(line)
            log.append(line)

    checked_ok = True
    for w in todays:
        try:
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
        except Exception as exc:  # noqa: BLE001 - noted, and re-raised below
            # One window with no event_id (a sync that failed part-way) used
            # to raise straight out of here and take every other check on the
            # day down with it - including the safety-net alerts, which are
            # the part that matters most when something is already wrong.
            checked_ok = False
            msg = f"{w.get('foil_key', w['trigger_id'])}: check failed: {exc}"
            notes.append(msg)
            log.append(msg)
    if not todays:
        log.append("no windows near now; nothing to verify on the calendar")

    if dry_run:
        log.append("DRY RUN: not writing live.json")
    else:
        verdict.write_live(
            verdict.build_live(
                now, bom, holfuy, checks, notes, alerts, bias,
                token_expires_at=config.env("FOIL_TOKEN_EXPIRES_AT", required=False),
                port_kembla=port_kembla,
            ),
            data_dir,
        )
        if lake_rec is not None:
            log.append(lake_rec)
        log.append(
            f"live.json: {bom.station} {bom.speed_kn:.0f} kn at {bom.time:%H:%M}, "
            f"{len(checks)} check(s)"
        )
    if pat_days is not None and pat_days <= config.PAT_FAIL_DAYS:
        # Everything else has finished and live.json is written, so the note
        # is on the dashboard before this makes the run red (spec 8.9).
        raise StaleDataError(pat_note)
    if not checked_ok:
        # Everything else finished first, but the run still fails loudly
        # (spec 8.9): a window that cannot be verified is a real problem.
        raise CalendarError(
            "one or more window checks failed; see notes in live.json"
        )
    return log
