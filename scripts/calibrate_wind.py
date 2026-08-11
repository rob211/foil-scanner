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
    shas = subprocess.run(
        ["git", "log", "--format=%H", f"--since={days} days ago", "--", "data/live.json"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    seen, rows = set(), []
    for sha in shas:
        blob = subprocess.run(
            ["git", "show", f"{sha}:data/live.json"], capture_output=True, text=True
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
    print(f"  -> best fit: {choose(f)}")

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


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    rows = observation_history(days)
    if not rows:
        print("no committed live.json history in that window")
        return 1
    stamps = sorted(d["generated_at"] for d in rows)
    start = stamps[0][:10]
    end = (datetime.fromisoformat(stamps[-1]).date() + timedelta(days=1)).isoformat()
    print(f"observation history: {len(rows)} snapshots, {stamps[0][:16]} -> {stamps[-1][:16]}")
    print(f"model archive: {start} -> {end}, median of {', '.join(config.MODELS.values())}")

    for label, const, key, loc in (
        ("LAKE   Holfuy 366 (0.9-corrected) vs the lake point",
         "lake", "holfuy", config.LAKE),
        ("COAST  BOM Bellambi vs the ocean point",
         "coast", "obs", config.OCEAN),
    ):
        report(label, const, paired(rows, key, model_hours(loc, start, end)))

    print("\nNothing applies these yet. Before wiring one in, re-run across a")
    print("different season: a winter of W/NW gradient days is not a year.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
