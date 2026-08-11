# Foil Scanner: build spec

Instructions for programming a downwind foiling conditions scanner. It scans multiple wind and swell models for the week ahead, evaluates Rob's run triggers, and maintains colour-coded events in a dedicated Google Calendar. Failures are loud everywhere. All decisions below were agreed with Rob on 4 Jul 2026.

## 1. Goal and architecture

- GitHub repo `foil-scanner`. Written as "private" on 4 Jul 2026; it is in fact **public** (verified 11 Aug 2026). Nothing sensitive is committed - credentials live in Actions secrets and the Worker's secret store, and the data is station readings and forecasts - but two consequences follow: the repo is world-readable, and **Actions is free and unmetered**, which invalidates any argument that trims cadence to save minutes. Personal project, kept separate from sitework-tools.
- Python 3.12 scanner run by GitHub Actions cron. Two workflows: a full scan every 6 hours, and an hourly live-verification job that only does work on days that already have a trigger event.
- All data and verdicts are written as JSON committed to the repo (`data/latest.json` plus `data/history/YYYY-MM-DD.json`). The calendar is one consumer of that JSON. A future dashboard (GitHub Pages, static page reading the same JSON) is the second consumer, so the verdict schema is a stable contract: version it, and never change field meanings without bumping `schema_version`.
- Output: events on a dedicated "Foiling" Google Calendar, full sync (create, update, move, delete to match the latest verdict).
- Timezone for everything user-facing: `Australia/Sydney` (zoneinfo). Actions cron is UTC, so comment the cron lines with the local times they correspond to and expect them to drift an hour across DST.

Data flow: fetchers -> validated source snapshots -> trigger engine -> verdicts JSON -> calendar sync. Keep these as separate modules so the trigger engine is pure (data in, verdicts out) and fully unit-testable with fixture files.

## 2. Locations

| Point | Lat, Lon | Used for |
|---|---|---|
| Lake Illawarra (mid-lake) | -34.53, 150.84 | Lake run wind forecasts |
| Lake entrance (Windang) | -34.535, 150.874 | Entrance wind forecasts |
| Offshore of entrance | -34.55, 150.90 | Swell and sea level (tides) |
| Wollongong coast (ocean runs) | -34.43, 150.92 | Ocean run wind forecasts (NE and south run families; one point first, split into north/south points in calibration if Bass Point and Bellambi diverge) |

These are first-pass coordinates. During calibration (section 10), nudge them if a point sits over land in a model grid (Open-Meteo will still answer, but land cells read low; compare against the manual sites).

## 3. Data sources

Free, official APIs only. No scraping, no paid APIs. Windguru (https://www.windguru.cz/768215) and WillyWeather (https://wind.willyweather.com.au/nsw/illawarra/wollongong-harbour.html) stay as manual eyeball cross-checks and belong in the README, not the code.

### 3.1 Open-Meteo forecast API (multi-model wind)

One request per location:

```
https://api.open-meteo.com/v1/forecast
  ?latitude=..&longitude=..
  &hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m
  &daily=sunrise,sunset
  &models=gfs_seamless,ecmwf_ifs025,icon_seamless,ukmo_seamless
  &wind_speed_unit=kn
  &timezone=Australia/Sydney
  &forecast_days=7
```

That returns each model's hourly wind separately (fields come back suffixed per model). These are the same underlying models Windguru displays. BOM's ACCESS-G was the original fourth model but stopped returning data on Open-Meteo (verified dead 4 Jul 2026, all-null for at least a week); UKMO global (10 km) replaces it and covers Australia well. Take sunrise and sunset from the `daily` block rather than adding an astronomy dependency.

### 3.2 Open-Meteo marine API (swell and tide timing)

```
https://marine-api.open-meteo.com/v1/marine
  ?latitude=-34.55&longitude=150.90
  &hourly=swell_wave_height,swell_wave_direction,swell_wave_period,sea_level_height_msl
  &timezone=Australia/Sydney
  &forecast_days=7
```

- Swell rules use `swell_wave_*` (not total `wave_height`, which mixes in wind chop).
- High tide times: find local maxima of `sea_level_height_msl` per day. The series is hourly, so a raw local maximum is only accurate to ±30 min; fit a parabola through the three samples bracketing each extremum and take its vertex for a sub-hourly time. That half hour decides gate membership at the edges (see 4.2).
- This is modelled, not the official tide table. **Calibrated 11 Aug 2026** against IOC Sea Level Monitoring station `pkem` — the real Port Kembla gauge, 1 km from `MARINE_POINT` — using 14 days of 1-minute observations and 54 matched tides (`scripts/calibrate_tide.py`, re-runnable):
  - the model runs **~30 min early**: `TIDE_TIME_OFFSET_MIN = 30.0` (highs +28, lows +32, sd ~9)
  - its "MSL" carries its own offset (+0.148 m mean over the fortnight), so the height constant is the combined **model-to-gauge-datum** shift, not a textbook datum separation: `TIDE_HEIGHT_OFFSET_M = 0.83` (was a 0.95 guess, over-reporting every height by ~12 cm)
- Section 10's original plan was a fortnight of BOM tide predictions. The gauge gets the same answer from history in one run, and repeatably.

### 3.3 BOM observations (live verification, primary)

Rob's station page: https://www.bom.gov.au/products/IDN60801/IDN60801.94749.shtml. The JSON feed is:

```
http://www.bom.gov.au/fwo/IDN60801/IDN60801.94749.json
```

- BOM returns 403 to default Python user agents. Send a browser-like `User-Agent` header. If 403 persists, try the `reg.bom.gov.au` host with the same path. A 403 from both is a hard failure, not a skip.
- Read the station name from the JSON header and log it, do not hardcode a name for it.
- Wind fields are km/h; convert to knots (divide by 1.852). Use the most recent observation and its timestamp.

### 3.4 Holfuy station 366 (live verification, lake)

Live lake wind station: https://holfuy.com/en/data/366. The API needs a per-station password which Holfuy grants free for personal use on request (do this early, it can take days):

```
https://api.holfuy.com/live/?s=366&pw=<HOLFUY_KEY>&m=JSON&tu=C&su=knots
```

- Known bias: this station reads roughly 10% high. Multiply speed and gust by 0.9 before comparing to any threshold, and show both raw and corrected values in output.
- `HOLFUY_KEY` is an optional secret. If it is not configured, the scanner must say so explicitly in the verdict JSON and in event descriptions ("lake live check: Holfuy key not configured, BOM only"). Absence is allowed; silence is not.

### 3.5 Freshness rules

Every source snapshot records `fetched_at` and the newest data timestamp inside it. Staleness is a failure (section 8):

| Source | Max age of newest data |
|---|---|
| Open-Meteo forecast/marine | time axis must include now and reach at least 5 days ahead (the axis starts at local midnight, so a start-of-axis check is wrong) |
| BOM observation | 45 minutes |
| Holfuy observation (when key configured) | 30 minutes |

## 4. Trigger definitions

All triggers evaluate hourly forecast steps, clipped to daylight (sunrise to sunset). A window is a run of consecutive qualifying hours; minimum window one hour unless a duration ladder says more. Direction bands use degrees-from (meteorological, wind coming from).

### 4.1 Lake runs (Lake Illawarra), 20 kn+

One trigger family; the direction picks the crossing, and the event title names it:

| Direction band | Run name | Threshold |
|---|---|---|
| 170-215 (S/SSW) | Oak Flats to Berkeley | 20 kn+ |
| 215-260 (SW/WSW) | Kanahooka run | 20 kn+ |
| 260-285 (W) | Berkeley run | 20 kn+ |
| 20-70 (NE) | Sailing Club to Oak Flats | 25 kn+, rare |

Stronger is better on the lake. The NE lake run is a rare event: prefix its title with "RARE:" so it stands out.

### 4.2 Lake Entrance, two modes

Both modes prefer the window to overlap the period from high tide to 2 h after it (the run-out only, not before the high), and require daylight.

The tide has three phases, not a gate (Rob, 11 Aug 2026 — refining the 10 Aug call that it should not be a veto at all):

| Phase | When | Effect |
|---|---|---|
| **No go** | the last `ENTRANCE_NO_GO_BEFORE_LOW_H` (4 h) before dead low | Too much outflow — the lake draining through a narrow entrance. Cut out of the window before a `Window` is built. The only tide state that removes a run. |
| **Preferred** | overlapping high tide to +`ENTRANCE_TIDE_WINDOW_H` (2 h), the run-out | Full rating. |
| **Workable** | any other tide | The run is on, just not at its best: keeps its event, drops `ENTRANCE_OFF_TIDE_DOWNGRADE` colour steps, gains an "off-tide" tag, and is recorded in `near_misses` with reason `off_tide`. |

A qualifying stretch is **split at the preferred boundaries into separate events** (Rob, 11 Aug 2026), so the calendar shows when it is actually good rather than one long block carrying a single colour:

```
07:00-13:00  yellow   workable   (off-tide)
13:00-15:00  green    preferred
15:00-17:00  yellow   workable   (off-tide)
```

A shoulder shorter than an hour is dropped rather than emitted as a stub, matching the minimum window everywhere else. Two modes firing over the same piece still merge into one event (4.2); the merge only joins windows sharing a `tide_state`, so a workable window can never be promoted by folding a preferred one into it.

Both entrance modes (4.2) and the reverse run (4.8) share the no-go: peak ebb is peak ebb whichever direction you are running. The reverse run keeps its own preferred window, the run-in (4.8).

This replaced a distance-from-the-gate tolerance, which was a guess standing in for a physical constraint.

This mattered: on 10 Aug 2026 wind and swell both qualified for 07:00–09:00 with 1.4 m of ENE swell, the gate ran to 07:00, the overlap was zero minutes, and a good morning produced no event and — because `entrance_windows` returned a hardcoded empty near-miss list — no record anywhere of why.

`high_tide_m` on those windows is the modelled high-tide height referenced to chart datum (tide-table style): the Open-Meteo sea level (relative to mean sea level) plus `config.TIDE_HEIGHT_OFFSET_M`. It is modelled, not an official prediction, so treat it as approximate and calibrate the offset against a BOM Port Kembla tide reading (section 10).

Mode 1 (swell): wind 0-10 kn with a westerly component (direction 200-340), or under 5 kn from any direction; swell from 35-110 degrees (NE through E, ENE explicitly included) at 0.8 m or more.

Mode 2 (strong NE, no swell needed): wind from 20-80 (NE/ENE) at 18 kn or more. Swell ignored in this mode.

If both modes fire in the same window, keep one event and note both in the description.

### 4.3 South wind ocean runs (Bass Point / Hill 60 / Boilers / Bellambi)

Wind from the south (155-210, SSE through SSW edges; first-pass band, tune in calibration) at 20 kn or more. Swell size then narrows which runs are on, and the event title lists exactly those:

| Swell | Runs on |
|---|---|
| Small: under 1.0 m, any direction | Bass Point, Hill 60, Boilers, Bellambi |
| Medium: 1.0-2.0 m from S/SSE/SE (135-205) | Bellambi red buoy, Hill 60 |
| Large: over 2.0 m from S/SSE/SE (135-205) | Hill 60 only |

Medium or large swell from outside the S/SSE/SE band during 20 kn+ south wind is governed by the swell compatibility rule in 4.6, which will usually kill it (cross swell of 1.0 m or more is a no go, 0.5-1.0 m downgrades the small-swell row one step).

### 4.4 Hill 60 swell run (standalone, no wind required)

Large south swell alone fires a Hill 60 event even on light-wind days (a swell run rather than a downwinder): swell over 2.0 m from S/SSE/SE (135-205), any wind. Daylight clipping applies as usual. If the south-wind trigger (4.3, large-swell row) fires in the same window, keep one Hill 60 event and note both the wind and swell numbers in the description.

### 4.5 NE ocean runs (Easty / South Beach / Sandon)

Direction 20-75 (NNE through ENE). Sliding duration ladder on sustained wind:

| Strength | Hours required before it counts |
|---|---|
| 10-12 kn | 3 h |
| 13-15 kn | 2.5 h |
| 15 kn+ | 2 h |

The event starts when the required hours have already blown (it marks when the bump is ready, not when the wind starts) and ends when the wind drops below 10 kn or at sunset. Required build-up hours must themselves be in daylight.

Off-angle downgrade: true NE (34-56) gets full rating. NNE (20-34) and ENE (56-75) are not as good on this stretch of coast, so downgrade the colour one step (red becomes green, green becomes yellow, yellow stays yellow) and add "off-angle NNE" or "off-angle ENE" to the title. Exception: ENE is fine for the Lake Entrance triggers, which have their own bands above; the downgrade applies to ocean runs only.

### 4.6 Swell compatibility (all ocean downwind runs)

Ocean downwinders prefer less swell, and swell that crosses the wind is the killer. For every ocean downwind window (the south wind runs in 4.3 and the NE runs in 4.5), compute the angular difference d between the swell-from direction and the wind-from direction (0-180), then apply the first matching row:

| Swell | Effect |
|---|---|
| Minuscule: under 0.5 m | Ignore the swell entirely, full rating |
| Aligned (d up to 25) and up to 1.5 m | Fine, full rating; note swell in the description |
| Aligned (d up to 25) and over 1.5 m | No go: unreasonably hard even lined up. Kill the window |
| Cross (d over 25) and 0.5-1.0 m | Downgrade one colour step, add "cross swell X.X m SE" style note to the title |
| Cross (d over 25) and 1.0 m or more | No go: too challenging to downwind. Kill the window |

So a nuking NE day with a medium (1.0 m+) south swell creates no event, while the same wind over minuscule south swell rates full value. Killed windows are recorded in `near_misses` with reason `cross_swell` or `aligned_swell_too_big` so the dashboard shows why a windy day went quiet, and the daily verdict is never silently empty.

- The explicit swell table in 4.3 wins over this rule for the south wind family: south wind with S/SSE/SE swell follows that table with no upper swell limit, because Hill 60 can handle any size south swell and wind (confirmed by Rob). This rule handles everything else.
- The cross-swell downgrade stacks with the NE off-angle downgrade (4.5) but never drops below yellow; a yellow event stays on the calendar.
- Does not apply to the lake runs (no ocean swell on the lake), the Lake Entrance triggers including the reverse run (E/NE swell is the point for the standard modes; the reverse run is gated on wind and tide only), or the standalone Hill 60 swell run (a swell event, not a downwinder).

### 4.7 Baysurf (custom event)

Baysurf is a custom event for clean E/NE-swell windows. It triggers when the marine forecast shows at least 1.5 m of swell from 35-90 degrees (E through NE) while the wind is either:

- light, up to 10 kn, or
- stronger only if it is from the west, south-west or north-west (the wind must be favourably off the back for the run).

The event must also overlap the falling-tide window from high tide toward low tide. The middle-to-low part of that fall is the ideal window; if the event only lands in the broader high-to-low span, it drops one colour and gets a tide tag in the event title. Baysurf is not part of the standard ocean-downwinder swell-compatibility rule; it is treated as its own trigger family.

### 4.8 Entrance reverse run (Boronia Ave, custom event)

A reverse-direction run near Boronia Ave, gated to the opposite tide phase from the standard Lake Entrance triggers (4.2): those want the run-out (high tide to +2 h), this one wants the run-in, so the window opens 2 h after low tide and closes 1 h before the next high tide. Uses the same entrance wind forecast as 4.2.

Wind from the west or north-west (270-315). 20 kn or more to fire at all; north-west (295-315) is prime and gets the full rating, west of that (270-295) is off-angle and downgrades one colour step (same treatment as the NE ocean off-angle rule in 4.5), with an "off-angle <compass>" tag added to the title.

Not part of the ocean swell-compatibility rule (4.6) — like the standard entrance triggers, it is gated on wind and tide, not swell.

### 4.9 Watch band (the "maybe" tier)

Below yellow sits `watch`, a deliberately wide net whose only job is to say "worth looking at the models" (Rob, 10 Aug 2026). A window is a watch when either:

- its strength falls between `config.watch_floor_for(yellow_floor)` and the yellow floor — a blanket `WATCH_FACTOR` (0.75) measured **down from the trigger's yellow floor**, not from its target, so every trigger gets the same proportional band regardless of whether its yellow floor is derived or explicit; or
- its strength clears the yellow floor but fewer models agree than section 5 requires, down to `config.MIN_MODELS_WATCH`. A lone model calling 18 kn while the coast does 32 is what a model bust looks like from inside the forecast.

A watch never overrules a deliberate veto: if a span is strong enough *and* agreed on but still has no window, something downstream killed it on purpose (the 4.6 swell rules), and it stays killed and stays in `near_misses`.

Watch windows do not get their own calendar events. Each day's collect into one graphite all-day digest keyed `watch:<date>`, which also lists that day's near misses. A day with near misses but no watch window gets **no** digest — a lone single-model miss is not worth a calendar entry, and a marker on almost every day would stop meaning anything. The digest is capped at `WATCH_DIGEST_MAX_LINES` and hard-truncated at `WATCH_DIGEST_MAX_CHARS`, well under Google's 8192-character limit; exceeding it returns a 400 that would take the whole calendar sync with it. They are excluded from live verification (they have no event to patch) and never take the dashboard's headline verdict while a real window exists.

## 5. Model consensus

- A forecast window fires only when at least 2 of the 4 models meet the trigger for that hour.
- Strength for colour grading is the median across the agreeing models at the window's peak hour.
- The event description lists each model's numbers so the spread is visible.
- Single-model hits are recorded in the verdict JSON (for the dashboard and for tuning) but create no calendar event.
- Days 5-7 of the horizon get a "low confidence, long range" line in the description.

## 6. Colour grading

Three levels per Rob's scheme: roughly 10% under desired, at desired, well over (25%+ over). Google Calendar `colorId`: Banana = 5, Basil = 10, Tomato = 11.

Watch (4.9) sits below yellow and is graphite (`colorId` 8). Grades in ascending order: `watch`, `yellow`, `green`, `red`. The off-angle (4.5) and cross-swell (4.6) downgrades still never drop below yellow — only the off-tide penalty (4.2) may reach watch.

| Trigger | Yellow (marginal) | Green (on target) | Red (firing) |
|---|---|---|---|
| Lake runs (S/SSW, SW/WSW, W) | 18-20 kn | 20-25 kn | over 25 kn |
| Lake NE rare run | 22.5-25 kn | 25-31 kn | over 31 kn |
| Entrance mode 1 (graded on swell) | 0.7-0.8 m | 0.8-1.0 m | over 1.0 m |
| Entrance mode 2 (NE wind) | 16-18 kn | 18-22.5 kn | over 22.5 kn |
| South wind ocean runs | 18-20 kn | 20-25 kn | over 25 kn |
| Hill 60 swell run (graded on swell) | 1.8-2.0 m | 2.0-2.5 m | over 2.5 m |
| NE ocean runs | 10-15 kn (ladder hours met) | 15-19 kn | over 19 kn |
| Entrance reverse run (Boronia Ave) | 20-25 kn | 25-31.25 kn | over 31.25 kn |

Yellow windows are "worth watching" and do create events. Entrance mode 1 is graded on swell height because the wind there is a constraint, not the quality driver. Ocean downwind runs then apply the off-angle downgrade (4.5) and the swell compatibility rule (4.6), which can downgrade or kill a window; downgrades stack but never drop below yellow. The Hill 60 swell run yellow band (1.8-2.0 m) only exists when the south-wind trigger has not already fired; with 20 kn+ south wind, 1.8-2.0 m is simply the medium-swell row of 4.3.

## 7. Calendar sync

- Dedicated calendar named "Foiling" in Rob's Google account. Never write to the primary calendar.
- Auth for GitHub Actions: GCP project, enable the Calendar API, create a service account, download its JSON key into the Actions secret `GCAL_SERVICE_ACCOUNT_JSON`. Then share the Foiling calendar with the service account's email address with "Make changes to events" permission. This works on a personal Google account with no domain delegation. Put the calendar ID in config.
- Event shape: timed event spanning the window (e.g. "Oak Flats to Berkeley 1pm-5pm"). Title: run name, peak median knots and direction, e.g. `Kanahooka run: 24 kn WSW`. Description: per-model numbers, gusts, swell/tide details where relevant, confidence note, live verification status, and a generated-at stamp.
- Dedup and sync: stamp every event with `extendedProperties.private.foil_key = "<trigger_id>:<date>:<window_start_hour>"`. Each scan lists existing events in the horizon carrying any `foil_key`, diffs against the new verdicts, then patches changed events, inserts new ones, and deletes events whose window no longer exists. Never touch events without a `foil_key`.
- Live wind alerts (every live run, forecast or not). The whole notification path used to hang off a forecast window, so when every model missed a 32 kn NW blow on 10 Aug 2026 there was no window, nothing to verify, and no notification of any kind. `config.LIVE_ALERT_TRIGGERS` maps each run to its yellow-floor strength, direction arc and preferred station; any live observation clearing both fires a **timed** red event keyed `live-alert:<date>:<trigger_id>` with a 0-minute popup override.
  - Timed, not all-day: an all-day event's reminder offset is measured from local midnight, so it cannot ring when the wind is actually blowing. The event starts `config.ALERT_LEAD_S` ahead of now, because Google drops a popup whose start is already past.
  - Re-polling patches the title, description and end but never the start or the reminder, so a four-hour blow pings once.
  - Station choice takes the **windier** of Holfuy and BOM for lake and entrance runs. `holfuy or bom` meant the coastal reading was discarded whenever Holfuy answered; on 10 Aug Holfuy read 19 kn mid-lake while Bellambi did 31.9, and nothing fired for another 2.5 h.
  - Alerts are keyed on the **body of water**, not the individual crossing (`config.LIVE_ALERT_TRIGGERS`'s `group`). The lake bands abut exactly, so a wind hunting either side of a boundary otherwise minted an event, and a popup, per band. Where two triggers on one water both match, the one clearing its own bar by the widest margin wins.
- A refreshed alert's end tracks the latest observation plus `ALERT_TAIL_H`, capped at `ALERT_MAX_H` from the start. Adding a full `ALERT_DURATION_H` on every poll grew a 5 h blow into a 7 h event.
- Alerts are suppressed for any trigger that already has a live window on the calendar — that is the verification job's business, and double-notifying is worse than not notifying.
  - `sync` must never delete the current day's `live-alert:*` as stale; the point of them is that no forecast window exists. Older ones are swept.
- The alert's start is computed from the clock **at the moment of the calendar write**, not from the run's start time. A run that has spent a minute on fetches would otherwise post a start already in the past, and Google silently declines to fire a popup for one (11 Aug 2026 audit).
- The two workflows use **separate** concurrency groups. A shared one let GitHub cancel a pending run that a newer one displaced — silently, with no non-zero exit, no failure email and no data written. They write different files; the push retry in each workflow handles the race that remains.
- `sync` finishes its pass before failing: a per-event error is collected and raised at the end, never mid-loop. Aborting halfway left later windows with no `event_id` on disk, which the next live run then died on. The live check loop contains per-window failures the same way, so one unverifiable window cannot take the safety-net alerts down with it.
- Every all-day event body (`broken:*`, `watch:*`) must set `end.date` to the **day after** `start.date`. Google treats it as exclusive, so `start == end` is a zero-length event.
- Live verification updates (hourly job, trigger days only):
  - Confirmed (live reading at 90% of threshold or better, direction in band): prepend a tick to the title (`LIVE NOW:` plus tick emoji) and arm a 30-minute popup reminder via `reminders.overrides`, so the phone pings through the calendar itself.
  - Not verifying (window has started, live under 70% of threshold or direction out of band): prepend a warning to the title and put "forecast X kn vs live Y kn at HH:MM (station)" in the description.
  - Lake events check Holfuy (corrected by 0.9) and BOM; ocean and entrance events check BOM.

## 8. Failures are loud (hard rules for the coder)

The prime directive: this scanner must never quietly show a calm week because something broke. A missing event because of a dead feed is the worst failure mode.

1. No `except: pass`, no default values, no "assume 0 kn", no fallback numbers of any kind.
2. Every HTTP call: 30 s timeout, `raise_for_status()`, up to 3 retries with exponential backoff, then raise.
3. Every response is schema-checked: required keys present, units as expected, arrays same length, values in physical range (wind 0-80 kn, swell 0-15 m). A model missing from the Open-Meteo response is a failure, not a shrug.
4. Staleness limits from section 3.5 are enforced; stale equals failed.
5. Any failure exits non-zero, which makes the Actions run red and triggers GitHub's failure email to Rob.
6. On failure the scanner also writes (best effort) a Tomato all-day event on today: `SCANNER BROKEN: <one-line reason>`, with the traceback summary in the description, keyed `foil_key=broken:<date>` so it dedupes and gets cleaned up by the first healthy run. If the calendar write itself is what failed, the red run and email still happen.
7. Config is validated at startup (bands are within 0-360, thresholds positive, ladder ordered, calendar ID present). Bad config fails before any fetch.
8. Heartbeat: the hourly live job checks the committed `data/latest.json` age. If the last successful full scan is older than 8 hours, that is a failure too (catches silently disabled crons).
9. Partial-failure policy: fail per source but finish the pass. One dead source does not abort evaluation of triggers that do not need it; every event description gains a loud "BOM feed down since HH:MM" style line, the verdict JSON records the source as failed, and the run still exits non-zero at the end so the red run and email fire.

## 9. Verdict JSON (dashboard contract)

`data/latest.json`, committed every run, mirrored into `data/history/`:

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-04T06:10:00+10:00",
  "sources": {
    "open_meteo_wind": {"ok": true, "fetched_at": "..."},
    "open_meteo_marine": {"ok": true, "fetched_at": "..."},
    "bom_obs": {"ok": true, "station": "...", "latest_obs": "..."},
    "holfuy_366": {"ok": false, "reason": "key not configured"}
  },
  "windows": [
    {
      "trigger_id": "lake_kanahooka",
      "run_name": "Kanahooka run",
      "date": "2026-07-06",
      "start": "13:00", "end": "17:00",
      "grade": "green",
      "peak_median_kn": 24, "direction_deg": 250,
      "models_agreeing": 3, "model_values": {"GFS": 25, "ECMWF": 24, "ICON": 22, "UKMO": 18},
      "swell_m": null, "high_tide": null, "high_tide_m": null,
      "spots": null,
      "confidence": "normal",
      "live_status": "pending",
      "event_id": "google-event-id"
    }
  ],
  "near_misses": []
}
```

`near_misses` holds single-model hits and windows that failed exactly one condition (useful on the dashboard and for tuning thresholds later).

`spots` is set only on south-ocean windows (`run_name` "South runs"): the list of individual launch spots that qualify at that swell size, e.g. `["Bass Point", "Hill 60", "Boilers", "Bellambi"]`. The dashboard shows them as separate chips; the calendar folds them back into the event title. It is `null` on every other trigger.

`windows[]` also carries `watch` (why it only reached the watch tier, else null) and `tide_state` ("preferred" | "workable" | null). `latest.json` carries `expected_today`: one row per hour with the median model wind per location, published so the live job can diff observations against it.

`data/live.json`, committed by the hourly live job (absent until the first live run after deploy):

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-05T14:23:00+10:00",
  "obs": {"station": "Bellambi", "time": "...", "speed_kn": 12.4, "gust_kn": 16.2, "dir_deg": 157.5},
  "holfuy": null,
  "checks": [{"foil_key": "south_ocean:2026-07-06:12", "state": "confirmed", "live_line": "18 kn S at 14:00 (Bellambi), forecast 22 kn"}],
  "notes": []
}
```

`live.json` also carries `alerts` (live wind matching a trigger with no forecast window behind it) and `bias` (observed minus forecast for the current hour, with `flagged` set past `config.BIAS_FLAG_KN`). When the hour cannot be checked — no scan has run yet today, so `expected_today` is yesterday's — `bias` carries rows with `flagged: null` and a `reason` instead of being empty, because an empty list read as "the models were right".

`latest.json` carries `heartbeat_max_age_h` so the dashboard uses `config.HEARTBEAT_MAX_AGE_H` rather than its own copy of the staleness rule. A poll gap longer than `config.LIVE_POLL_GAP_WARN_MIN` is recorded in `notes`: GitHub sheds scheduled runs, and on 10 Aug it shed three consecutive live ticks across the peak of the blow, which looked identical to a quiet morning.

`obs.dir_deg` is null when BOM reports CALM. `obs` itself is null only when the BOM fetch failed; the reason lands in `notes` and the run still exits non-zero (section 8). The dashboard overlays `checks` onto the `latest.json` windows by `foil_key` and renders `obs` as the live tile. BOM is fetched every live run, window or not, so the tile stays current all day.

## 10. Build order (1-2 day chunks)

1. Repo scaffold, config module with validation, all four fetchers with the failure rules, fixture capture script that saves real responses into `tests/fixtures/`. Unit tests that every failure rule actually raises.
2. Trigger engine: pure functions from source snapshots to windows. Tests per trigger against hand-built fixtures (including the WSW boundary at 215/260, the entrance dual-mode overlap, the NE ladder edge at exactly 2 h, the off-angle downgrade, the south-run swell narrowing at exactly 1.0 m and 2.0 m, the standalone Hill 60 swell run deduping against the south-wind large-swell case, the swell compatibility boundaries (d at exactly 25 degrees, aligned swell at exactly 1.5 m, cross swell at exactly 0.5 m and 1.0 m, the 4.3-table-wins precedence), and daylight clipping in July).
3. Calendar sync: service account setup, Foiling calendar creation and sharing, diff-based sync, `SCANNER BROKEN` path. A `--dry-run` flag that prints the event plan without writing is required and is also the local dev mode.
4. Workflows: `scan.yml` (cron every 6 h) and `live.yml` (hourly; skips calendar work when today has no events but still fetches BOM and publishes `data/live.json` for the dashboard tile), both with the secret plumbing. Request the Holfuy key in parallel since it has lead time.
5. Calibration fortnight: compare modelled high-tide times against BOM Port Kembla tide predictions, sanity-check the Holfuy 0.9 factor against BOM on a windy lake day, eyeball events against Windguru and WillyWeather, and tune coordinates or thresholds. Log findings in the repo.
6. Later: GitHub Pages dashboard reading `data/latest.json` and `data/history/`. Nothing in phases 1-5 may assume the calendar is the only consumer.

## 14. Credential expiry

The Worker's GitHub PAT expires, and an expired one is the quietest failure
in the system: dispatches stop, the schedule backstop keeps the scanner alive
at a reduced rate, and the only trace is a poll-gap note that reads like
GitHub shedding runs rather than a credential to renew.

Only the Worker holds the token, and GitHub reveals the expiry only to
whoever holds it - as the `github-authentication-token-expiration` header on
every response. So the Worker reads it from the run-list GET it already
makes and forwards it as a `workflow_dispatch` input; `live.yml` puts it in
`FOIL_TOKEN_EXPIRES_AT` and the live job escalates:

| Time left | Effect |
|---|---|
| over `PAT_WARN_DAYS` | silent |
| inside `PAT_WARN_DAYS` | a note in `live.json`, rendered as a `TOKEN EXPIRING` alarm on the dashboard |
| inside `PAT_FAIL_DAYS`, or expired | the run fails: red run, failure email, `SCANNER BROKEN` event |

`live.json` is written **before** the run is failed, so the warning reaches
the dashboard rather than going down with the run. It also carries
`token_expires_at` on every run, not only once the date is close: a healthy
token that says nothing leaves "when does this expire" answerable only by
going and looking, which is the habit this replaces. Null on a scheduled
backstop run, which carries no token and so cannot know.

An absent value means the run came from the schedule backstop, which needs no
PAT. That is "unknown", not "healthy", and is silent by design - the backstop
is exactly the path that still works when the token is dead.

A 401 or 403 from GitHub is not retried. Retrying a credential rejection
gets the same answer three times and buries the reason under what look like
transport errors.

## 13. Model bias

The wind models are not calibrated against the stations, and measurement says
they should be. `scripts/calibrate_wind.py` compares every committed
observation against the archived forecast for the same hour and reports what
a correction would be. It applies nothing: the decision is deliberately not
automatic.

First run, 11 Aug 2026, 5 weeks, 91 lake hours and 384 coast hours:

- observed minus forecast, median +4.0 kn (lake) and +3.9 kn (coast)
- the model was **never** high by `BIAS_FLAG_KN` or more, in 475 hours
- it saturates: with the lake truly at 18-28 kn the model median was 13.0,
  against a yellow floor of 18 and a watch floor of 15. A genuine lake day
  produced neither a window nor a watch.
- lake fits a multiplier (~1.57x), coast a flat offset (~+3.9 kn) - the
  script prefers the simpler form unless a more complex one is meaningfully
  better, or it would recommend two parameters where a constant serves

Before any of this is applied it needs a second season. Fitting a multiplier
to 25 windy hours in one winter will overshoot, and overshooting fires green
days that are not - which is worse than the current miss, because a missed
day costs nothing and a false green costs a drive.

## 12. Snapshot command

`python -m foilscan snapshot` is a read-only report of current conditions:
observed wind (both stations), median model wind for the same hour alongside
it, swell (height, direction, period, 3 h trend), tide (height above chart
datum, flood/ebb, next high and low), today's tide gates for 4.2 and 4.8, and
daylight remaining.

- It writes nothing - no calendar, no JSON - so the loud-failure rules in
  section 8 do not apply and a dead source degrades its own line into a
  PROBLEMS list rather than failing the process.
- Hourly marine samples are interpolated to the current minute. The tide moves
  enough within an hour that reporting the last sample as "now" misleads, and
  swell direction is interpolated the short way round so a swell near north
  does not average to south.

## 11. Dependencies

Keep it lean: `requests`, `google-api-python-client`, `google-auth`, `pytest`. Stdlib `zoneinfo` for timezones. No pandas, no scraping libraries.
