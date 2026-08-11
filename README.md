# foil-scanner

Downwind foiling conditions scanner for Lake Illawarra and the Wollongong
coast. Scans multiple wind models and swell for the week ahead, evaluates the
run triggers in [docs/SPEC.md](docs/SPEC.md), and maintains colour-coded
events on a dedicated "Foiling" Google Calendar. Banana yellow is marginal,
basil green is on target, tomato red is firing, and graphite is the watch
band: the maybes, collected into one all-day digest per day rather than
cluttering the calendar with events for runs that probably aren't on.

Failures are loud by design: any dead feed, stale reading or schema change
fails the run red, emails via GitHub, and drops a red SCANNER BROKEN event on
today's calendar. A calm-looking week is never allowed to be a broken scanner.

## How it runs

Triggering is done by the Cloudflare Worker in [cron-worker/](cron-worker/),
not by GitHub's own cron: `schedule` events are deprioritised under load and
shed 10 of ~13 live ticks on 10 Aug 2026, which is not something to hang a
wind alert on. The workflows keep a sparse `schedule` block as a backstop, and
a degraded cadence shows up as a `poll gap:` note in `data/live.json`.

- `scan` (every 2 h): fetch forecasts, evaluate triggers,
  write `data/latest.json` (and `data/history/`), sync the calendar.
- `live` (every 30 min from 5am-8pm local, hourly overnight): on days with
  events, verify against the BOM station and the Holfuy lake station
  (corrected by 0.9 for its known overread). Confirmed events get a tick and
  a 30-minute popup reminder; misses get flagged. Every run, window or not,
  it also checks live wind against every run's direction and strength and
  fires a timed red `WIND NOW` event with a popup when one matches - the
  safety net for the days the models miss entirely.

Data sources: Open-Meteo forecast and marine APIs (GFS, ECMWF, ICON, UKMO;
swell and modelled sea level for tide timing), BOM observations JSON,
Holfuy station 366. All free, official, no scraping.

Manual cross-checks (not used by the code):
[Windguru](https://www.windguru.cz/768215),
[WillyWeather](https://wind.willyweather.com.au/nsw/illawarra/wollongong-harbour.html),
[BOM station page](https://www.bom.gov.au/products/IDN60801/IDN60801.94749.shtml),
[Holfuy 366](https://holfuy.com/en/data/366).

## Snapshot

`python -m foilscan snapshot` prints what it is doing right now: observed wind
from both stations, the median model wind for the same hour beside it (so a
bust is visible rather than implied), swell height/direction/period with a
3 h trend, tide height above chart datum with flood/ebb and the next high and
low, today's tide gates, and daylight remaining. Read-only - no calendar
writes, nothing committed, so it is safe to run any time.

Marine data is hourly; the tide and swell figures are interpolated to the
current minute rather than snapped to the last sample. `HOLFUY_KEY` in the
environment adds the lake station line.

## Dashboard

A single self-contained page (`index.html`) on GitHub Pages reads
`data/latest.json`: verdict up top, 7-day strip, tap a window for the model
spread. Same loud-failure philosophy; a stale or broken scanner shows an
alarm banner, never a calm page. Setup in [SETUP.md](SETUP.md).

## Tide calibration

The modelled tide is checked against the real Port Kembla gauge (IOC station
`pkem`, 1 km from the marine point):

```
python scripts/calibrate_tide.py [days]
```

Read-only. It prints suggested values for `TIDE_TIME_OFFSET_MIN` and
`TIDE_HEIGHT_OFFSET_M`; paste them into config with the date. Re-run it if the
tide gates start looking off — the suggestion accounts for the offset already
configured, so a healthy calibration reports back roughly what is set.

## Wind calibration

```
python scripts/calibrate_wind.py [days]
```

Read-only, and **nothing applies its output**. It compares every observation
ever committed in `data/live.json` against what the models actually said at
the time (Open-Meteo's historical forecast archive, not a reanalysis), and
prints what a correction would have to be.

First run, 11 Aug 2026, 5 weeks: across 475 matched hours the models were
never once high by 8 kn or more, and low by that much in one hour in six.
They also saturate - when the lake truly blew 18-28 kn the model median sat
at 13, against a yellow floor of 18 and a watch floor of 15. Lake fits a
multiplier (about 1.57x), the coast a flat offset (about +3.9 kn).

Applied since 11 Aug 2026 as `config.WIND_BIAS` (lake and entrance x1.45,
ocean +3.9), on ingest so everything downstream agrees. Backtested at 73%
precision and 73% recall on the lake, against 0% recall uncorrected.

The script reports absolute values rather than residuals, so re-running
cannot compound the correction; it flags drift over 15% against what is
configured. Re-run across a summer before trusting it year-round.

## Local dev

```
pip install -r requirements.txt
pytest
python -m foilscan scan --dry-run --data-dir /tmp/foil-data
```

Dry run prints the event plan without touching the calendar and needs no
credentials. See [SETUP.md](SETUP.md) for the one-time credential setup.

`data/latest.json` is the stable contract for the dashboard (spec section 9);
bump `schema_version` before changing any field meaning.
