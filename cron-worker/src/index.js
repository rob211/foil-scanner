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

const LIVE_CRON = "*/30 * * * *";
const SCAN_CRON = "7 */2 * * *";

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

async function dispatch(workflow, env) {
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
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "foil-scanner-cron",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: env.GITHUB_REF || "main" }),
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
    const work = async () => {
      if (event.cron === SCAN_CRON) {
        console.log(JSON.stringify(await dispatch("scan.yml", env)));
        return;
      }
      if (!shouldDispatchLive(now)) {
        console.log(
          JSON.stringify({ skipped: "live", reason: "outside daylight half-hour tick" })
        );
        return;
      }
      console.log(JSON.stringify(await dispatch("live.yml", env)));
    };
    // waitUntil so a slow GitHub response cannot truncate the dispatch.
    ctx.waitUntil(work());
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
