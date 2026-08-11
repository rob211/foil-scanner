"""Calibrate the modelled tide against the real Port Kembla gauge.

    python scripts/calibrate_tide.py [days]

Prints suggested values for config.TIDE_TIME_OFFSET_MIN and
config.TIDE_HEIGHT_OFFSET_M. Read-only: it changes nothing, so re-run it
whenever the tide gates start looking off and paste the numbers into config
with the date.

Ground truth is IOC Sea Level Monitoring station "pkem" - the actual Port
Kembla gauge, 1 km from config.MARINE_POINT. Spec section 10 asked for a
fortnight of BOM tide predictions before trusting the modelled tide; this
gets the same answer from history in one run, and repeatably.

Observed data is ~1 min and carries wind waves and seiche, so it is smoothed
before peak-picking. The model is hourly and goes through the scanner's own
interpolation, so like is compared with like.

First run, 11 Aug 2026, 14 days: model early by 28 min on highs and 32 on
lows (sd ~9), heights low by 0.826 m (sd 0.03).
"""
import json, math, sys, statistics as st
from datetime import datetime, timedelta, timezone
from pathlib import Path
# Relative to this file, not an absolute path. This was hardcoded to one
# machine's checkout and worked everywhere it was ever run by hand - until
# the weekly workflow ran it in CI and it could not import foilscan at all.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from foilscan import config
from foilscan.models import MarineForecast, MarineHour
import requests

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 14
IOC = "https://www.ioc-sealevelmonitoring.org/service.php"
UA = {"User-Agent": "foil-scanner-calibration"}

# The service caps how much it returns per call, so walk the range in weekly
# chunks rather than asking for the lot and silently getting 18 hours.
end = datetime.now(timezone.utc).date()
rows, seen = [], set()
for chunk in range(0, DAYS, 7):
    lo = end - timedelta(days=DAYS - chunk)
    hi = min(lo + timedelta(days=7), end)
    r = requests.get(IOC, params={"query": "data", "code": "pkem",
                                  "timestart": lo.isoformat(),
                                  "timestop": hi.isoformat(), "format": "json"},
                     timeout=120, headers=UA)
    r.raise_for_status()
    for x in r.json():
        if x["stime"] not in seen:
            seen.add(x["stime"]); rows.append(x)
rows.sort(key=lambda x: x["stime"])

# IOC serves UTC.
obs = []
for r in rows:
    if r.get("slevel") is None:
        continue
    t = datetime.strptime(r["stime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    obs.append((t.astimezone(config.TZ), float(r["slevel"])))
obs.sort()
print(f"observations: {len(obs)}  {obs[0][0]:%Y-%m-%d %H:%M} -> {obs[-1][0]:%Y-%m-%d %H:%M} (local)")
print(f"gauge mean level: {st.mean(v for _, v in obs):.3f} m  "
      f"(range {min(v for _,v in obs):.2f} - {max(v for _,v in obs):.2f})")

# 61-minute centred moving average: kills waves/seiche, negligible phase shift.
W = 30
sm = []
for i in range(W, len(obs) - W):
    window = obs[i - W:i + W + 1]
    if (window[-1][0] - window[0][0]) > timedelta(minutes=90):
        continue          # gap in the record, don't smooth across it
    sm.append((obs[i][0], sum(v for _, v in window) / len(window)))

def extrema(series, high=True, min_sep_h=4):
    out = []
    for i in range(1, len(series) - 1):
        a, b, c = series[i-1][1], series[i][1], series[i+1][1]
        if (b > a and b >= c) if high else (b < a and b <= c):
            if out and (series[i][0] - out[-1][0]) < timedelta(hours=min_sep_h):
                # keep the more extreme of the pair
                if (b > out[-1][1]) if high else (b < out[-1][1]):
                    out[-1] = (series[i][0], b)
                continue
            out.append((series[i][0], b))
    return out

obs_highs = extrema(sm, True)
obs_lows = extrema(sm, False)
print(f"observed highs: {len(obs_highs)}, lows: {len(obs_lows)}")

# Modelled: same endpoint the scanner uses, with history.
p = requests.get("https://marine-api.open-meteo.com/v1/marine", params={
    "latitude": config.MARINE_POINT.lat, "longitude": config.MARINE_POINT.lon,
    "hourly": "sea_level_height_msl", "timezone": "Australia/Sydney",
    "past_days": min(DAYS, 92), "forecast_days": 1}, timeout=60).json()
h = p["hourly"]
hours = [MarineHour(time=datetime.fromisoformat(t).replace(tzinfo=config.TZ),
                    swell_m=0.0, swell_dir_deg=0.0, swell_period_s=0.0, sea_level_m=lv)
         for t, lv in zip(h["time"], h["sea_level_height_msl"]) if lv is not None]
marine = MarineForecast(fetched_at=datetime.now(config.TZ), hours=hours)
mod_highs = [(t.time, t.sea_level_m) for t in marine.high_tides()]
mod_lows = [(t.time, t.sea_level_m) for t in marine.low_tides()]
print(f"modelled highs: {len(mod_highs)}, lows: {len(mod_lows)}")
print(f"model mean level: {st.mean(x.sea_level_m for x in hours):.3f} m (MSL datum)")

def match(model, observed, label):
    diffs, hts = [], []
    for mt, mv in model:
        near = [o for o in observed if abs((o[0] - mt).total_seconds()) < 3 * 3600]
        if not near:
            continue
        ot, ov = min(near, key=lambda o: abs((o[0] - mt).total_seconds()))
        diffs.append((ot - mt).total_seconds() / 60.0)
        hts.append(ov - mv)
    if not diffs:
        print(f"{label}: no matches"); return None, None
    diffs.sort()
    print(f"\n{label}: {len(diffs)} matched pairs")
    print(f"  time  observed-minus-model: median {st.median(diffs):+.1f} min, "
          f"mean {st.mean(diffs):+.1f}, sd {st.pstdev(diffs):.1f}, "
          f"range {diffs[0]:+.0f} to {diffs[-1]:+.0f}")
    print(f"  height observed-minus-model: median {st.median(hts):+.3f} m, "
          f"sd {st.pstdev(hts):.3f}")
    return st.median(diffs), st.median(hts)

# Past these, what is measured no longer matches what is applied.
DRIFT_MIN = 15.0
DRIFT_M = 0.10

th, hh = match(mod_highs, obs_highs, "HIGH TIDES")
tl, hl = match(mod_lows, obs_lows, "LOW TIDES")
if th is not None and tl is not None:
    # marine.high_tides() already applies the configured offset, so what is
    # measured above is the RESIDUAL. The suggestion has to add the offset
    # back, or re-running this after a good calibration would tell you to
    # reset it to zero and undo the fix.
    residual = st.median([th, tl])
    print(f"\n   residual with the current offset applied: {residual:+.1f} min")
    print(f"=> TIDE_TIME_OFFSET_MIN  suggestion: "
          f"{config.TIDE_TIME_OFFSET_MIN + residual:+.0f}  "
          f"(configured: {config.TIDE_TIME_OFFSET_MIN:+.0f})")
    height = st.median([hh, hl])
    print(f"=> TIDE_HEIGHT_OFFSET_M  suggestion: {height:+.3f}  "
          f"(configured: {config.TIDE_HEIGHT_OFFSET_M})")

    drifted = []
    if abs(residual) > DRIFT_MIN:
        drifted.append(f"tide timing residual {residual:+.0f} min")
    if abs(height - config.TIDE_HEIGHT_OFFSET_M) > DRIFT_M:
        drifted.append(
            f"tide height {height:+.3f} against a configured "
            f"{config.TIDE_HEIGHT_OFFSET_M}"
        )
    if "--check" in sys.argv and drifted:
        # Non-zero so a scheduled run goes red and emails.
        print("\nDRIFTED: " + "; ".join(drifted), file=sys.stderr)
        sys.exit(1)
