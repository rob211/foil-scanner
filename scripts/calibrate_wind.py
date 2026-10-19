"""Measure how far the wind models sit from the stations.

    python scripts/calibrate_wind.py [days]

Read-only. It changes nothing and nothing applies its output yet - it prints
what a correction would have to be, so the decision to apply one can be made
on evidence and re-checked later.

Ground truth is the observation history already in this repo: every commit of
data/live.json carries a BOM reading and, when the key is configured, a
Holfuy one. Model values come from Open-Meteo's historical forecast archive -
what the models actually said at the time, not a reanalysis.

Why this exists: on 11 Aug 2026, across 475 matched hours, the models were
never once high by 8 kn or more and were low by that much in one hour in six.
Worse, they saturate: when the lake truly blew 18-28 kn the model median sat
at 13. The lake's yellow floor is 18 kn and its watch floor 15, so a genuine
lake day produced neither a window nor a watch. That is not a threshold
slightly out - it is a forecast that cannot reach the threshold at all.

Read the caveats it prints. A multiplier fitted to a handful of windy hours
in one winter will overshoot, and overshooting means green days that are not.
"""
from __future__ import annotations

import json
import statistics as st
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from foilscan import config

ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"
# Below this the gap is dominated by how each side averages, and it is not a
# strength anyone makes a decision at.
DECISION_KN = 15.0


def observation_history(days: int) -> list[dict]:
    """Every distinct live.json ever committed, newest first in git order."""
    # cwd pinned to the repo, not inherited. The history lives in this
    # checkout's git log, so running from anywhere else found no commits -
    # the same shape of bug as the absolute path calibrate_tide.py carried.
    repo = Path(__file__).resolve().parent.parent
    shas = subprocess.run(
        ["git", "log", "--format=%H", f"--since={days} days ago", "--", "data/live.json"],
        capture_output=True, text=True, check=True, cwd=repo,
    ).stdout.split()
    seen, rows = set(), []
    for sha in shas:
        blob = subprocess.run(
            ["git", "show", f"{sha}:data/live.json"],
            capture_output=True, text=True, cwd=repo,
        ).stdout
        if not blob.strip():
            continue
        try:
            d = json.loads(blob)
        except json.JSONDecodeError:
            continue          # a truncated or half-written commit, skip it
        if not d.get("generated_at") or d["generated_at"] in seen:
            continue
        seen.add(d["generated_at"])
        rows.append(d)
    return rows


def model_hours(loc, start: str, end: str) -> dict[datetime, float]:
    """Median across the models for each hour the archive has."""
    r = requests.get(ARCHIVE, params={
        "latitude": loc.lat, "longitude": loc.lon, "hourly": "wind_speed_10m",
        "models": ",".join(config.MODELS), "wind_speed_unit": "kn",
        "timezone": "Australia/Sydney", "start_date": start, "end_date": end,
    }, timeout=90)
    r.raise_for_status()
    h = r.json()["hourly"]
    out = {}
    for i, t in enumerate(h["time"]):
        vals = [h[f"wind_speed_10m_{m}"][i] for m in config.MODELS
                if h[f"wind_speed_10m_{m}"][i] is not None]
        if vals:
            out[datetime.fromisoformat(t).replace(tzinfo=config.TZ)] = st.median(vals)
    return out


def paired(rows, key, model) -> list[tuple[float, float]]:
    """(observed, forecast) once per hour.

    The median of the readings in an hour, not the maximum: taking the
    windiest inflates the observed side and flatters the case for a
    correction. It moved the headline by 0.4 kn when checked.
    """
    by_hour: dict[datetime, list[float]] = {}
    for d in rows:
        o = d.get(key)
        if not o or o.get("speed_kn") is None or not o.get("time"):
            continue
        t = datetime.fromisoformat(o["time"]).replace(minute=0, second=0, microsecond=0)
        if t in model:
            by_hour.setdefault(t, []).append(o["speed_kn"])
    return [(st.median(v), model[t]) for t, v in sorted(by_hour.items())]


def paired_with_dir(rows, key, model) -> list[tuple[float, float, float]]:
    """paired(), but carrying the observed bearing so the fit can be split by
    wind direction. Same hourly-median rule."""
    by_hour: dict[datetime, tuple[list[float], list[float]]] = {}
    for d in rows:
        o = d.get(key)
        if not o or o.get("speed_kn") is None or not o.get("time"):
            continue
        if o.get("dir_deg") is None:
            continue
        t = datetime.fromisoformat(o["time"]).replace(minute=0, second=0, microsecond=0)
        if t in model:
            speeds, dirs = by_hour.setdefault(t, ([], []))
            speeds.append(o["speed_kn"])
            dirs.append(o["dir_deg"])
    return [
        (st.median(sp), model[t], st.median(dr)) for t, (sp, dr) in sorted(by_hour.items())
    ]


def fits(rows: list[tuple[float, float]]) -> dict:
    """Both plausible shapes, and which one explains more.

    A pure multiplier and a slope-plus-offset line describe very different
    physics: one says the model compresses the whole range, the other that it
    is simply low by a constant. They imply different corrections, so report
    both and let the residual decide.
    """
    n = len(rows)
    if n < 2:
        return {}
    mf = st.mean(f for _, f in rows)
    mo = st.mean(o for o, _ in rows)
    sxx = sum((f - mf) ** 2 for _, f in rows)
    sxy = sum((f - mf) * (o - mo) for o, f in rows)
    slope = sxy / sxx if sxx else float("nan")
    intercept = mo - slope * mf
    scale = (sum(o * f for o, f in rows) / sum(f * f for o, f in rows)
             if sum(f * f for o, f in rows) else float("nan"))
    rms = lambda pred: (sum((o - pred(f)) ** 2 for o, f in rows) / n) ** 0.5
    return {
        "n": n,
        "scale": scale,
        "scale_rms": rms(lambda f: scale * f),
        "slope": slope,
        "intercept": intercept,
        "linear_rms": rms(lambda f: slope * f + intercept),
        "offset": st.median(o - f for o, f in rows),
        "offset_rms": rms(lambda f: f + st.median(o - f for o, f in rows)),
    }


# A more complex form has to earn its extra parameter. Without this the
# coast picked `linear` over `offset` by 0.01 kn of residual - fitting noise,
# and recommending a two-parameter correction where a constant does the job.
SIMPLICITY_MARGIN = 0.05
# Past this, what is measured no longer matches what is applied.
DRIFT_LIMIT = 0.15
drifted: list[str] = []


def choose(f: dict) -> str:
    """Simplest correction within SIMPLICITY_MARGIN of the best residual."""
    candidates = [("offset", f["offset_rms"]), ("scale", f["scale_rms"]),
                  ("linear", f["linear_rms"])]           # simplest first
    best = min(r for _, r in candidates)
    for name, rms in candidates:
        if rms <= best * (1 + SIMPLICITY_MARGIN):
            return name
    return candidates[-1][0]


def report(label, const, rows) -> None:
    if not rows:
        print(f"\n{label}: no overlapping hours"); return
    gaps = [o - f for o, f in rows]
    low = sum(1 for g in gaps if g >= config.BIAS_FLAG_KN)
    high = sum(1 for g in gaps if g <= -config.BIAS_FLAG_KN)
    f = fits(rows)
    print(f"\n{label}   {f['n']} matched hours")
    print(f"  observed - forecast: median {st.median(gaps):+.1f} kn, "
          f"mean {st.mean(gaps):+.1f}, sd {st.pstdev(gaps):.1f}")
    print(f"  model low by {config.BIAS_FLAG_KN:.0f} kn+: {low} ({100*low/f['n']:.0f}%)"
          f"   |   high by {config.BIAS_FLAG_KN:.0f} kn+: {high} ({100*high/f['n']:.0f}%)")
    print(f"  candidate corrections (simplest within "
          f"{SIMPLICITY_MARGIN:.0%} of the best residual wins):")
    for name, desc, rms in (
        ("offset", f"forecast {f['offset']:+.1f}", f["offset_rms"]),
        ("scale", f"forecast x {f['scale']:.2f}", f["scale_rms"]),
        ("linear", f"forecast x {f['slope']:.2f} {f['intercept']:+.1f}", f["linear_rms"]),
    ):
        print(f"    {name:<7} {desc:<26} rms {rms:5.2f} kn")
    form, amount = config.WIND_BIAS[const]
    live = f"{form} {amount}"
    print(f"  -> best fit: {choose(f)}   |   currently applied: {live}")
    # The archive is raw, and the correction is applied on ingest rather than
    # here, so these numbers stay absolute rather than compounding: what this
    # prints is what WIND_BIAS should be, not a residual to add to it.
    measured = f["scale"] if form == "scale" else f["offset"]
    drift = abs(measured - amount) / amount if amount else float("inf")
    if drift > DRIFT_LIMIT:
        msg = (f"{const}: measured {measured:.2f} against a configured "
               f"{amount:.2f} ({drift:.0%} off)")
        drifted.append(msg)
        print(f"     DRIFT: {msg} - worth revisiting")

    decisive = [(o, fc) for o, fc in rows if o >= DECISION_KN]
    print(f"  at decision strength (observed {DECISION_KN:.0f} kn+): n={len(decisive)}")
    if decisive:
        print(f"    median gap {st.median([o - fc for o, fc in decisive]):+.1f} kn")
        for lo, hi in ((15, 20), (20, 25), (25, 99)):
            band = [(o, fc) for o, fc in decisive if lo <= o < hi]
            if band:
                print(f"    truth {lo}-{hi if hi < 99 else '+':<2} kn (n={len(band):>2})"
                      f" -> model median {st.median([fc for _, fc in band]):5.1f} kn")
    if len(decisive) < 30:
        print(f"    CAUTION: {len(decisive)} samples above {DECISION_KN:.0f} kn is thin."
              " This is the only band that changes what fires,")
        print("    and a multiplier fitted here will overshoot - green days that are not.")


# Below this a bearing has too little strong wind behind it to say anything;
# a scale fitted on light air describes light air.
BEARING_MIN_STRONG = 15


def bearing_report(rows3: list[tuple[float, float, float]]) -> None:
    """Does each lake run's own arc want its own multiplier?

    One scale for the whole lake is an assumption, not a measurement. The
    physical story behind the correction is the lake channelling a gradient
    wind along its own axis, and channelling is a function of direction - so
    if the assumption is going to break, it breaks here.

    Reporting only. It changes no constant and fails no run; the sample that
    would settle it does not exist yet. Printed every week so that when summer
    fills the NE arc in, the answer is already on the page.
    """
    if not rows3:
        return
    print("\n  per-bearing fit (one lake scale is an assumption, not a measurement)")
    bands = [(name, arc) for _, (name, arc, _, _) in config.LAKE_RUNS.items()]
    covered = set()
    for name, arc in bands:
        band = [(o, f) for o, f, d in rows3 if arc.contains(d)]
        covered |= {id(r) for r in rows3 if arc.contains(r[2])}
        strong = sum(1 for o, _ in band if o >= DECISION_KN)
        label = f"{name} ({arc.lo:.0f}-{arc.hi:.0f})"
        if len(band) < 2 or strong < BEARING_MIN_STRONG:
            print(f"    {label:<38} n={len(band):<4} {strong:>2} strong hr(s)"
                  f" - too thin to fit")
            continue
        f = fits(band)
        print(f"    {label:<38} n={len(band):<4} {strong:>2} strong hrs"
              f"  -> x{f['scale']:.2f}")
    rest = [(o, f) for o, f, d in rows3 if not any(a.contains(d) for _, a in bands)]
    if len(rest) >= 2:
        f = fits(rest)
        print(f"    {'outside every run arc':<38} n={len(rest):<4}"
              f"    -> x{f['scale']:.2f}")
    applied = config.WIND_BIAS["lake"][1]
    print(f"    currently applied across all of them: x{applied:.2f}")


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--check"]
    check = "--check" in sys.argv
    days = int(args[0]) if args else 60
    rows = observation_history(days)
    if not rows:
        print("no committed live.json history in that window")
        return 1
    stamps = sorted(d["generated_at"] for d in rows)
    start = stamps[0][:10]
    end = (datetime.fromisoformat(stamps[-1]).date() + timedelta(days=1)).isoformat()
    print(f"observation history: {len(rows)} snapshots, {stamps[0][:16]} -> {stamps[-1][:16]}")
    print(f"model archive: {start} -> {end}, median of {', '.join(config.MODELS.values())}")

    # loc.key, not a hand-written string: the label and the config key drifted
    # apart once already and only blew up on the second location.
    for label, key, loc in (
        ("LAKE   Holfuy 366 (0.9-corrected) vs the lake point", "holfuy", config.LAKE),
        ("COAST  BOM Bellambi vs the ocean point", "obs", config.OCEAN),
    ):
        model = model_hours(loc, start, end)
        report(label, loc.key, paired(rows, key, model))
        if loc.key == "lake":
            bearing_report(paired_with_dir(rows, key, model))

    print("\nThese are absolute, not residuals: the archive is raw and the")
    print("correction is applied on ingest, so re-running cannot compound it.")
    print("Re-run across a summer before trusting the numbers year-round.")
    if check and drifted:
        # Non-zero so a scheduled run goes red and emails, rather than
        # printing a warning into a log nobody opens.
        print("\nDRIFTED: " + "; ".join(drifted), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
