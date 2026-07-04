# foil-scanner

Downwind foiling conditions scanner for Lake Illawarra and the Wollongong
coast. Scans multiple wind models and swell for the week ahead, evaluates the
run triggers in [docs/SPEC.md](docs/SPEC.md), and maintains colour-coded
events on a dedicated "Foiling" Google Calendar. Banana yellow is marginal,
basil green is on target, tomato red is firing.

Failures are loud by design: any dead feed, stale reading or schema change
fails the run red, emails via GitHub, and drops a red SCANNER BROKEN event on
today's calendar. A calm-looking week is never allowed to be a broken scanner.

## How it runs

- `scan` (GitHub Actions, every 6 h): fetch forecasts, evaluate triggers,
  write `data/latest.json` (and `data/history/`), sync the calendar.
- `live` (hourly): on days with events, verify against the BOM station and
  the Holfuy lake station (corrected by 0.9 for its known overread). Confirmed
  events get a tick and a 30-minute popup reminder; misses get flagged.

Data sources: Open-Meteo forecast and marine APIs (GFS, ECMWF, ICON, UKMO;
swell and modelled sea level for tide timing), BOM observations JSON,
Holfuy station 366. All free, official, no scraping.

Manual cross-checks (not used by the code):
[Windguru](https://www.windguru.cz/768215),
[WillyWeather](https://wind.willyweather.com.au/nsw/illawarra/wollongong-harbour.html),
[BOM station page](https://www.bom.gov.au/products/IDN60801/IDN60801.94749.shtml),
[Holfuy 366](https://holfuy.com/en/data/366).

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
