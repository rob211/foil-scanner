# cron-worker

A Cloudflare Worker that triggers the scanner's workflows by
`workflow_dispatch`, because GitHub's own `schedule` queue is not reliable
enough to hang a wind alert on.

## Why

Measured on 10 Aug 2026, live job, requested every 30 min:

```
live scheduled runs, UTC 00:00-06:27:  3      expected: ~13

  00:39 UTC / 10:39 AEST   gap -
  03:01 UTC / 13:01 AEST   gap 142 min
  04:58 UTC / 14:58 AEST   gap 117 min
  --- 06:27 UTC / 16:27 AEST     gap 89 min
```

A 77% shed rate. GitHub deprioritises `schedule` events under load; this is
documented behaviour, not an outage. The cost was real: at 16:15 that day the
entrance reverse run was live at 24 kn WNW inside its tide gate, and no alert
fired because the job had not run since 14:58.

Every `workflow_dispatch` run that day executed normally. Dispatches are
ordinary API calls and are not in the shed queue, so moving the trigger out of
GitHub is the fix; the scanner code does not change.

## Design

- Worker cron drives the real cadence: live every 30 min in daylight and
  hourly overnight, scan every 2 h.
- The repo's own `schedule` blocks stay on as a **sparse backstop** (live
  3-hourly, scan 4-hourly) so a dead Worker degrades the cadence rather than
  stopping it. `live.py`'s `poll_gap_note` reports the degraded cadence.
- The daylight gate is applied **in the Worker**, not just in `live.py`. The
  Python gate exits in milliseconds, but GitHub bills a whole minute per run
  regardless, so gating at the trigger is what actually saves the minutes.
- Over-triggering is safe by design: `ensure_alert` keeps an existing alert's
  start and reminder so it cannot re-notify, `sync` is diff-based, and both
  workflows share a `concurrency` group that serialises overlapping runs.

## Setup

1. Create a **fine-grained** personal access token:
   - Repository access: only `rob211/foil-scanner`
   - Permissions: **Actions: Read and write**. Nothing else.
   - Give it an expiry you will actually notice, and diarise the renewal - an
     expired token is a silent trigger failure, caught only by the poll-gap
     note.

2. From this directory:

   ```
   npm install -g wrangler        # if you don't have it
   wrangler login
   wrangler secret put GITHUB_TOKEN
   wrangler secret put CRON_SHARED_SECRET    # any long random string
   wrangler deploy
   ```

3. Check it fired: at the next :00 or :30 a `workflow_dispatch` run should
   appear in the repo's Actions tab, and `wrangler tail` shows one JSON line
   per fire.

   The Worker deploys with `workers_dev = false`, so it has no public URL -
   cron triggers do not need one, and the manual POST endpoint in
   `src/index.js` is therefore unreachable by default. If you want it for
   debugging, register a workers.dev subdomain, flip `workers_dev` to true,
   and then:

   ```
   curl -X POST https://foil-scanner-cron.<subdomain>.workers.dev/live \
     -H "x-foil-key: <CRON_SHARED_SECRET>"
   ```

   It is secret-gated because it can start jobs that write to a real calendar.

4. Watch it: `wrangler tail`, or the Workers dashboard. Each fire logs one
   JSON line - dispatched workflow, or why it skipped.

## If the Worker dies

The backstop keeps the scanner alive at a reduced rate, and the first live run
after a gap over `config.LIVE_POLL_GAP_WARN_MIN` records a `poll gap:` note in
`data/live.json`, which the dashboard renders as a `LIVE CHECK LATE` alarm.
That is the intended detection path - the Worker is not silently load-bearing.
