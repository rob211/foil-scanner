# One-time setup

Three credentials, in rough order of lead time.

## 1. Holfuy API password (has lead time, start first)

Email info@holfuy.hu asking for actual-data API access to station 366
(Lake Illawarra), personal non-commercial use. When the password arrives,
add it as the Actions secret `HOLFUY_KEY`. Until then the scanner runs
BOM-only for live checks and says so in event descriptions.

## 2. Google Calendar service account

1. In Google Cloud Console create a project (e.g. `foil-scanner`), enable
   the **Google Calendar API**.
2. Create a **service account** (no roles needed), then create a JSON key
   for it and download it.
3. In Google Calendar (Rob's account) create a new calendar named
   **Foiling**.
4. Share the Foiling calendar with the service account's email address
   (the `...@...iam.gserviceaccount.com` one) with permission
   **Make changes to events**. This works on a personal account, no domain
   delegation involved.
5. Copy the calendar ID from the Foiling calendar's settings
   (Integrate calendar section, looks like `...@group.calendar.google.com`).

## 3. GitHub Actions secrets

In the repo settings add:

| Secret | Value |
|---|---|
| `GCAL_SERVICE_ACCOUNT_JSON` | full contents of the service account JSON key |
| `FOIL_CALENDAR_ID` | the Foiling calendar ID |
| `HOLFUY_KEY` | Holfuy API password (add when it arrives) |

Then run the **scan** workflow once by hand (Actions tab, Run workflow) and
check events appear on the Foiling calendar.

## 4. Dashboard (GitHub Pages)

The dashboard is `index.html` at the repo root reading `data/latest.json`.

1. Make the repo public (Pages on private repos needs a paid plan). Secrets
   stay secret either way; the only data published is wind forecasts.
2. Repo Settings, Pages, Source: **Deploy from a branch**, branch `main`,
   folder `/ (root)`.
3. The dashboard comes up at `https://rob211.github.io/foil-scanner/` and
   refreshes whenever a scan commits new data. Add it to the phone home
   screen for the 6am check.

## Calibration fortnight (spec section 10, phase 5)

- Compare the modelled high-tide times in event descriptions against the
  BOM Port Kembla tide predictions; if consistently off by more than about
  30 minutes, add a fixed offset in `config.py`.
- On a windy lake day, compare Holfuy (corrected) against BOM to sanity
  check the 0.9 factor.
- Eyeball events against Windguru and WillyWeather; tune coordinates or
  direction bands in `config.py` if a location point reads low.
