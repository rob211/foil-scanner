/**
 * Cloudflare Worker that triggers the foil-scanner workflows by
 * workflow_dispatch, because GitHub's own `schedule` queue is not reliable
 * enough to hang a wind alert on.
 *
 * Measured on 10 Aug 2026: the live job ran 3 times against a requested ~13,
 * a 77% shed rate, with gaps of 142, 117 and 89 minutes. GitHub explicitly
 * deprioritises `schedule` events under load. Dispatched runs are ordinary
 * API calls and are not in that queue - every workflow_dispatch run that day
 * executed while the schedule ticks around them were dropped.
 *
 * The repo's own cron stays on as a sparse backstop, so a dead Worker
 * degrades the cadence rather than stopping it. Over-triggering is safe by
 * design: ensure_alert keeps an existing alert's start and reminder so it
 * cannot re-notify, sync is diff-based, and the workflows share a
 * concurrency group that serialises overlapping runs.
 */

// One cron fires this Worker; which workflows that means is decided from the
// clock, not from which expression Cloudflare says triggered it.
//
// It was two crons dispatched via `event.cron === SCAN_CRON`. On the first
// live fire, 11:00 UTC, BOTH went out - but "7 */2 * * *" means minute 7 of
// even hours, so it had no business running at 11:00 at all. Rather than
// work out whose cron parser is right, nothing here depends on it: scan
// fires on a whole even UTC hour, live on the existing daylight rule, and
// event.cron is logged only so its real contents are on the record.
//
// Left unfixed this was not merely untidy - scan would have gone out on
// every 30 minute tick, 48 runs a day against the 12 intended, and the live
// job would otherwise still spend a runner on getting there.
const SCAN_EVERY_N_HOURS = 2;

// Mirrors foilscan/config.py LIVE_FAST_POLL_{START,END}_HOUR. Kept here as
// well as in Python so the no-op runs never start: live.py would exit
// immediately outside these hours, but GitHub still bills a whole minute for
// the checkout and pip install that got it there.
const FAST_POLL_START_HOUR = 5;
const FAST_POLL_END_HOUR = 20;

/** Wall-clock hour and minute in Sydney, DST included. */
function sydneyTime(date) {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Australia/Sydney",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const get = (t) => Number(parts.find((p) => p.type === t).value);
  return { hour: get("hour") % 24, minute: get("minute") };
}

/**
 * Same rule as live.py _should_run: the top-of-hour tick always runs, the
 * half-hour tick only while it is light enough in Sydney for anyone to care.
 */
export function shouldDispatchLive(date) {
  const { hour, minute } = sydneyTime(date);
  if (minute < 30) return true;
  return hour >= FAST_POLL_START_HOUR && hour < FAST_POLL_END_HOUR;
}

/**
 * Scan every SCAN_EVERY_N_HOURS, on the hour. Deliberately UTC: unlike the
 * live gate this has nothing to do with when anyone is awake, and pinning it
 * to UTC keeps the interval even across a DST changeover instead of
 * producing a doubled or skipped scan on those two days a year.
 */
export function shouldDispatchScan(date) {
  return date.getUTCMinutes() < 30 && date.getUTCHours() % SCAN_EVERY_N_HOURS === 0;
}

/** Everything one tick should do. */
export function plan(date) {
  return { live: shouldDispatchLive(date), scan: shouldDispatchScan(date) };
}

const GH_TIMEOUT_MS = 15000;
// Cloudflare cron delivery is at-least-once, and a duplicate was observed on
// 10 Aug (20:00:27 and 20:00:56). Harmless to the calendar - sync is
// diff-based - but it spends a runner and adds a redundant data commit.
const DEDUPE_WINDOW_MS = 5 * 60 * 1000;

function ghHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "foil-scanner-cron",
  };
}

/** True when this workflow already has a run created very recently. */
async function ranRecently(workflow, env) {
  try {
    const res = await fetch(
      // No event filter: the repo's own schedule backstop is a real cadence
      // again, and a run it started counts just as much as one we started.
      `https://api.github.com/repos/${env.GITHUB_REPO}` +
        `/actions/workflows/${workflow}/runs?per_page=1`,
      { headers: ghHeaders(env), signal: AbortSignal.timeout(GH_TIMEOUT_MS) }
    );
    if (!res.ok) return false;           // can't tell - dispatching twice
    const data = await res.json();       // beats not dispatching at all
    const last = data.workflow_runs && data.workflow_runs[0];
    if (!last) return false;
    return Date.now() - Date.parse(last.created_at) < DEDUPE_WINDOW_MS;
  } catch {
    return false;
  }
}

export async function dispatch(workflow, env) {
  if (await ranRecently(workflow, env)) {
    return { ok: true, workflow, skipped: "dispatched within the dedupe window" };
  }
  const url =
    `https://api.github.com/repos/${env.GITHUB_REPO}` +
    `/actions/workflows/${workflow}/dispatches`;
  let lastError;
  // A dropped dispatch is the failure this Worker exists to prevent, so it is
  // worth a couple of retries before giving up.
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { ...ghHeaders(env), "Content-Type": "application/json" },
        body: JSON.stringify({ ref: env.GITHUB_REF || "main" }),
        // Without this a hung subrequest burns the whole invocation and the
        // tick is lost, with the retry loop below never reached.
        signal: AbortSignal.timeout(GH_TIMEOUT_MS),
      });
      if (res.status === 204) return { ok: true, workflow };
      // Never log the response body verbatim; it can echo request detail.
      lastError = `HTTP ${res.status}`;
    } catch (err) {
      lastError = String(err);
    }
    if (attempt < 2) await new Promise((r) => setTimeout(r, 2 ** attempt * 1000));
  }
  throw new Error(`dispatch ${workflow} failed: ${lastError}`);
}

export default {
  async scheduled(event, env, ctx) {
    const now = new Date(event.scheduledTime);
    const { live, scan } = plan(now);
    const work = async () => {
      // event.cron is recorded, never branched on - see the note at the top.
      const log = { cron: event.cron, at: now.toISOString(), live, scan };
      const wanted = [];
      if (scan) wanted.push("scan.yml");
      if (live) wanted.push("live.yml");
      const settled = await Promise.allSettled(
        wanted.map((w) => dispatch(w, env))
      );
      const results = [];
      settled.forEach((r, i) => {
        if (r.status === "fulfilled") results.push(r.value);
        else console.error(JSON.stringify({ workflow: wanted[i], error: String(r.reason) }));
      });
      if (!results.length) {
        console.log(JSON.stringify({ ...log, skipped: "nothing due this tick" }));
        return;
      }
      console.log(JSON.stringify({ ...log, dispatched: results.map((r) => r.workflow) }));
    };
    // An exception inside waitUntil is an unhandled rejection: it aborts the
    // remaining work silently and is easy to miss. Catching it means a failed
    // scan dispatch cannot also lose the live one, and the reason is on the
    // record. There is nowhere better to send it from here - the scanner
    // notices independently, via the poll-gap note on the next run that does
    // happen, and the dashboard's stale alarm if none does.
    const guarded = work().catch((err) =>
      console.error(JSON.stringify({ cron: event.cron, error: String(err) }))
    );
    // waitUntil so a slow GitHub response cannot truncate the dispatch.
    ctx.waitUntil(guarded);
  },

  /**
   * Manual trigger, for checking the token and wiring after deploy:
   *   curl -X POST https://<worker>/live -H "x-foil-key: <CRON_SHARED_SECRET>"
   * Unauthenticated requests get nothing - this endpoint can start CI jobs
   * that write to a real calendar, so it is not left open.
   */
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("POST /scan or /live\n", { status: 405 });
    }
    if (
      !env.CRON_SHARED_SECRET ||
      request.headers.get("x-foil-key") !== env.CRON_SHARED_SECRET
    ) {
      return new Response("forbidden\n", { status: 403 });
    }
    const path = new URL(request.url).pathname.replace(/^\//, "");
    if (path !== "scan" && path !== "live") {
      return new Response("unknown workflow\n", { status: 404 });
    }
    try {
      const result = await dispatch(`${path}.yml`, env);
      return new Response(JSON.stringify(result) + "\n", { status: 202 });
    } catch (err) {
      return new Response(String(err) + "\n", { status: 502 });
    }
  },
};
