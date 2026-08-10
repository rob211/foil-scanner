"""`python -m foilscan snapshot`: what it is doing right now.

Read-only. No calendar writes, no JSON committed, nothing cached - it exists
to answer "what are the actual conditions, and what did the models think they
would be" in one place, which on 10 Aug 2026 took four browser tabs and a
guess. Every number is sourced and stamped so a disagreement between the
stations and the models is visible rather than averaged away.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median

from . import config, fetch
from .models import MarineHour
from .triggers import compass


def _lerp_marine(marine, now: datetime) -> tuple[MarineHour, MarineHour, float]:
    """The two hourly samples bracketing `now`, and how far between them we
    are. Marine data is hourly; the tide moves enough inside an hour that
    reporting the last sample as "now" is misleading."""
    hours = sorted(marine.hours, key=lambda h: h.time)
    before = [h for h in hours if h.time <= now]
    after = [h for h in hours if h.time > now]
    if not before or not after:
        raise ValueError("marine forecast does not bracket the current time")
    lo, hi = before[-1], after[0]
    span = (hi.time - lo.time).total_seconds()
    frac = ((now - lo.time).total_seconds() / span) if span else 0.0
    return lo, hi, frac


def _at_now(lo: MarineHour, hi: MarineHour, frac: float, attr: str) -> float:
    a, b = getattr(lo, attr), getattr(hi, attr)
    return a + (b - a) * frac


def _tide_state(marine, now: datetime) -> dict:
    lo, hi, frac = _lerp_marine(marine, now)
    level_msl = _at_now(lo, hi, frac, "sea_level_m")
    rate_m_per_h = hi.sea_level_m - lo.sea_level_m

    highs = [t for t in marine.high_tides() if t.time > now]
    lows = [t for t in marine.low_tides() if t.time > now]
    next_high = min(highs, key=lambda t: t.time) if highs else None
    next_low = min(lows, key=lambda t: t.time) if lows else None
    upcoming = [t for t in (next_high, next_low) if t is not None]
    nxt = min(upcoming, key=lambda t: t.time) if upcoming else None

    if abs(rate_m_per_h) < 0.01:
        moving = "slack"
    elif rate_m_per_h > 0:
        moving = "rising (flood)"
    else:
        moving = "falling (ebb)"

    return {
        "height_cd_m": level_msl + config.TIDE_HEIGHT_OFFSET_M,
        "rate_m_per_h": rate_m_per_h,
        "moving": moving,
        "next_high": next_high,
        "next_low": next_low,
        "next": nxt,
        "next_is_high": nxt is not None and next_high is not None and nxt is next_high,
    }


def _swell_now(marine, now: datetime) -> dict:
    lo, hi, frac = _lerp_marine(marine, now)
    height = _at_now(lo, hi, frac, "swell_m")
    # Direction is interpolated the short way round so a swell sitting near
    # 360 does not average to due south.
    a, b = lo.swell_dir_deg, hi.swell_dir_deg
    direction = (a + ((b - a + 180) % 360 - 180) * frac) % 360
    three_ago = [h for h in marine.hours if h.time <= now - timedelta(hours=3)]
    trend = None
    if three_ago:
        delta = height - three_ago[-1].swell_m
        if abs(delta) < 0.05:
            trend = "steady"
        else:
            trend = f"{'building' if delta > 0 else 'easing'} {abs(delta):.2f} m/3 h"
    return {
        "height_m": height,
        "dir_deg": direction,
        "period_s": _at_now(lo, hi, frac, "swell_period_s"),
        "trend": trend,
    }


def _model_now(wind, now: datetime) -> dict | None:
    """Median model wind for the current hour, so a bust is visible against
    the stations rather than only showing up as an empty calendar."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    speeds, dirs = [], []
    for series in wind.models.values():
        for hw in series:
            if hw.time == hour:
                speeds.append(hw.speed_kn)
                dirs.append(hw.dir_deg)
    if not speeds:
        return None
    from .triggers import vector_mean

    return {"speed_kn": median(speeds), "dir_deg": vector_mean(dirs)}


def _wind_line(label: str, obs, note: str = "") -> str:
    if obs is None:
        # `note` describes the reading (e.g. "0.9-corrected"), so it is
        # meaningless when there is no reading; PROBLEMS carries the reason.
        return f"  {label:<26} unavailable (see PROBLEMS)"
    where = compass(obs.dir_deg) if obs.dir_deg is not None else "calm"
    deg = f"{obs.dir_deg:.0f}" if obs.dir_deg is not None else "--"
    return (
        f"  {label:<26} {obs.speed_kn:>4.0f} kn {where:<3} ({deg:>3})"
        f"  gust {obs.gust_kn:>4.0f}   {obs.time:%H:%M}{('  ' + note) if note else ''}"
    )


def build(now: datetime) -> list[str]:
    """Returns the report as lines. Sources are fetched independently and a
    dead one degrades its own line rather than the whole report - this is a
    look-out-the-window tool, not a decision the loud-failure rules govern."""
    out: list[str] = [
        f"FOIL SNAPSHOT   {now:%a %d %b %Y, %H:%M %Z}",
        "",
    ]

    bom = holfuy = marine = sun = lake_wind = ocean_wind = None
    problems: list[str] = []

    def grab(fn, name):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    bom = grab(lambda: fetch.fetch_bom(now), "BOM")
    key = config.env("HOLFUY_KEY", required=False)
    if key:
        holfuy = grab(lambda: fetch.fetch_holfuy(key, now), "Holfuy")
    else:
        problems.append("Holfuy: HOLFUY_KEY not configured")
    marine = grab(lambda: fetch.fetch_marine(now), "marine")
    sun = grab(lambda: fetch.fetch_sun(now), "sun")
    lake_wind = grab(lambda: fetch.fetch_wind(config.LAKE, now), "lake models")
    ocean_wind = grab(lambda: fetch.fetch_wind(config.OCEAN, now), "ocean models")

    out.append("WIND (observed)")
    out.append(_wind_line("Bellambi (BOM, coast)", bom))
    out.append(_wind_line("Lake Illawarra (Holfuy)", holfuy, "0.9-corrected"))

    model_lines = []
    for label, wind, station in (
        ("lake", lake_wind, holfuy),
        ("ocean", ocean_wind, bom),
    ):
        if wind is None:
            continue
        m = _model_now(wind, now)
        if m is None:
            continue
        gap = f"{station.speed_kn - m['speed_kn']:+.0f} kn" if station else "n/a"
        model_lines.append(
            f"  {label:<26} {m['speed_kn']:>4.0f} kn {compass(m['dir_deg']):<3} "
            f"({m['dir_deg']:>3.0f})  observed {gap}"
        )
    if model_lines:
        out += ["", "WIND (models, median for this hour)"] + model_lines

    if marine is not None:
        sw = _swell_now(marine, now)
        out += [
            "",
            "SWELL (offshore of the entrance)",
            f"  {sw['height_m']:.2f} m from {compass(sw['dir_deg'])} "
            f"({sw['dir_deg']:.0f}), {sw['period_s']:.1f} s period"
            + (f"   {sw['trend']}" if sw["trend"] else ""),
        ]

        td = _tide_state(marine, now)
        out += [
            "",
            "TIDE (modelled, Port Kembla chart datum)",
            f"  {td['height_cd_m']:.2f} m, {td['moving']} "
            f"({td['rate_m_per_h']:+.2f} m/h)",
        ]
        for label, tide in (("next high", td["next_high"]), ("next low", td["next_low"])):
            if tide is not None:
                mins = (tide.time - now).total_seconds() / 60
                out.append(
                    f"  {label:<10} {tide.time:%H:%M}  "
                    f"{tide.sea_level_m + config.TIDE_HEIGHT_OFFSET_M:.2f} m "
                    f"(in {mins / 60:.0f} h {mins % 60:.0f} m)"
                )

        from .triggers import _entrance_reverse_tide_spans, _tide_spans

        def gate(spans):
            today = [
                f"{lo:%H:%M}-{hi:%H:%M}"
                for lo, hi, *_ in spans
                if lo.date() == now.date()
            ]
            return ", ".join(today) if today else "none today"

        out += [
            "",
            "TIDE GATES today",
            f"  entrance run-out (4.2)     {gate(_tide_spans(marine))}",
            f"  reverse run-in (4.8)       {gate(_entrance_reverse_tide_spans(marine))}",
        ]

    if sun is not None:
        sunrise, sunset = sun.days[now.date()]
        left = (sunset - now).total_seconds() / 60
        out += [
            "",
            f"DAYLIGHT  {sunrise:%H:%M} - {sunset:%H:%M}"
            + (
                f"   {left / 60:.0f} h {left % 60:.0f} m left"
                if left > 0
                else "   after sunset"
            ),
        ]

    if problems:
        out += ["", "PROBLEMS"] + [f"  {p}" for p in problems]
    return out


def run(now: datetime) -> list[str]:
    return build(now)
