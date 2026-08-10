/**
 * node --test cron-worker/test.mjs
 *
 * No dependencies and no network - this only exercises the daylight gate,
 * which is the one piece of real logic in the Worker and the one that costs
 * money when it is wrong (every needless dispatch bills a full Actions
 * minute).
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import worker, { plan, shouldDispatchLive, shouldDispatchScan } from "./src/index.js";

// AEST (UTC+10) in August, AEDT (UTC+11) in January. Both are whole-hour
// offsets, so a UTC :00/:30 cron is always :00/:30 local.
const aest = (h, m) => new Date(Date.UTC(2026, 7, 10, h - 10, m));
const aedt = (h, m) => new Date(Date.UTC(2026, 0, 15, h - 11, m));

test("top-of-hour tick always dispatches", () => {
  for (const t of [aest(0, 0), aest(3, 0), aest(12, 0), aest(23, 0)]) {
    assert.equal(shouldDispatchLive(t), true, t.toISOString());
  }
});

test("half-hour tick only inside the daylight window", () => {
  assert.equal(shouldDispatchLive(aest(4, 30)), false); // before 05:00
  assert.equal(shouldDispatchLive(aest(5, 30)), true); // window opens
  assert.equal(shouldDispatchLive(aest(12, 30)), true);
  assert.equal(shouldDispatchLive(aest(19, 30)), true); // last one in
  assert.equal(shouldDispatchLive(aest(20, 30)), false); // window closes
  assert.equal(shouldDispatchLive(aest(23, 30)), false);
});

test("the window follows local time across DST, not UTC", () => {
  // 19:30 AEDT is 08:30 UTC; 19:30 AEST is 09:30 UTC. Both must dispatch,
  // which a UTC-based gate would get wrong for half the year.
  assert.equal(shouldDispatchLive(aedt(19, 30)), true);
  assert.equal(shouldDispatchLive(aest(19, 30)), true);
  // ...and 04:30 local must not, in either season.
  assert.equal(shouldDispatchLive(aedt(4, 30)), false);
  assert.equal(shouldDispatchLive(aest(4, 30)), false);
});

test("manual endpoint refuses anything without the shared secret", async () => {
  const env = { CRON_SHARED_SECRET: "s3cret", GITHUB_REPO: "x/y" };
  const post = (headers = {}) =>
    worker.fetch(new Request("https://w/live", { method: "POST", headers }), env);

  assert.equal((await post()).status, 403);
  assert.equal((await post({ "x-foil-key": "wrong" })).status, 403);
  // GET is rejected before auth is even considered.
  const get = await worker.fetch(new Request("https://w/live"), env);
  assert.equal(get.status, 405);
});

test("manual endpoint refuses an unknown workflow even when authorised", async () => {
  const env = { CRON_SHARED_SECRET: "s3cret", GITHUB_REPO: "x/y" };
  const res = await worker.fetch(
    new Request("https://w/deploy", {
      method: "POST",
      headers: { "x-foil-key": "s3cret" },
    }),
    env
  );
  assert.equal(res.status, 404);
});

test("an unset shared secret locks the endpoint rather than opening it", async () => {
  const res = await worker.fetch(
    new Request("https://w/live", {
      method: "POST",
      headers: { "x-foil-key": "undefined" },
    }),
    { GITHUB_REPO: "x/y" }
  );
  assert.equal(res.status, 403);
});

// --------------------------------------------------------------- scan gate

test("scan fires every 2 h on the hour, in UTC", () => {
  const utc = (h, m) => new Date(Date.UTC(2026, 7, 10, h, m));
  assert.equal(shouldDispatchScan(utc(10, 0)), true);
  assert.equal(shouldDispatchScan(utc(12, 0)), true);
  assert.equal(shouldDispatchScan(utc(11, 0)), false); // odd hour
  assert.equal(shouldDispatchScan(utc(10, 30)), false); // half past
});

test("scan stays evenly spaced across a DST changeover", () => {
  // 5 Apr 2026, AEDT -> AEST. A local-time rule would double or skip a scan
  // here; a UTC rule cannot.
  const before = new Date(Date.UTC(2026, 3, 4, 16, 0));
  const after = new Date(Date.UTC(2026, 3, 4, 18, 0));
  assert.equal(shouldDispatchScan(before), true);
  assert.equal(shouldDispatchScan(after), true);
  assert.equal(shouldDispatchScan(new Date(Date.UTC(2026, 3, 4, 17, 0))), false);
});

test("a single tick can mean both workflows, or neither", () => {
  // 10:00 UTC = 20:00 AEST: even hour and top of hour, so both.
  assert.deepEqual(plan(new Date(Date.UTC(2026, 7, 10, 10, 0))), { live: true, scan: true });
  // 18:30 UTC = 04:30 AEST: half past the hour, so scan is out, and 04:30
  // local is before the daylight window, so live is too. Nothing due.
  assert.deepEqual(plan(new Date(Date.UTC(2026, 7, 10, 18, 30))), { live: false, scan: false });
});

// ------------------------------------------------- QA fixes, 11 Aug 2026

import { dispatch } from "./src/index.js";

function fakeFetch(handlers) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || "GET", signal: init.signal });
    for (const [match, respond] of handlers) {
      if (String(url).includes(match)) return respond();
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  return calls;
}

const ENV = { GITHUB_REPO: "x/y", GITHUB_TOKEN: "t", GITHUB_REF: "main" };
const noRecentRuns = ["/runs?", () => new Response(JSON.stringify({ workflow_runs: [] }), { status: 200 })];

test("every GitHub request carries a timeout signal", async () => {
  const calls = fakeFetch([noRecentRuns, ["/dispatches", () => new Response(null, { status: 204 })]]);
  await dispatch("live.yml", ENV);
  assert.ok(calls.length >= 2);
  for (const c of calls) {
    assert.ok(c.signal, `no AbortSignal on ${c.method} ${c.url}`);
  }
});

test("a run dispatched moments ago is not dispatched again", async () => {
  const justNow = new Date(Date.now() - 30_000).toISOString();
  const calls = fakeFetch([
    ["/runs?", () => new Response(JSON.stringify({ workflow_runs: [{ created_at: justNow }] }), { status: 200 })],
    ["/dispatches", () => new Response(null, { status: 204 })],
  ]);
  const res = await dispatch("scan.yml", ENV);
  assert.equal(res.skipped, "dispatched within the dedupe window");
  assert.equal(calls.filter((c) => c.method === "POST").length, 0);
});

test("an old run does not suppress a new dispatch", async () => {
  const longAgo = new Date(Date.now() - 90 * 60_000).toISOString();
  const calls = fakeFetch([
    ["/runs?", () => new Response(JSON.stringify({ workflow_runs: [{ created_at: longAgo }] }), { status: 200 })],
    ["/dispatches", () => new Response(null, { status: 204 })],
  ]);
  await dispatch("scan.yml", ENV);
  assert.equal(calls.filter((c) => c.method === "POST").length, 1);
});

test("an unreadable run list dispatches rather than staying silent", async () => {
  // Failing to check must never be a reason to skip the safety net.
  const calls = fakeFetch([
    ["/runs?", () => new Response("nope", { status: 500 })],
    ["/dispatches", () => new Response(null, { status: 204 })],
  ]);
  await dispatch("live.yml", ENV);
  assert.equal(calls.filter((c) => c.method === "POST").length, 1);
});

test("one workflow failing does not lose the other", async () => {
  fakeFetch([
    noRecentRuns,
    ["scan.yml/dispatches", () => new Response("boom", { status: 500 })],
    ["live.yml/dispatches", () => new Response(null, { status: 204 })],
  ]);
  const logged = [];
  const realErr = console.error, realLog = console.log;
  console.error = (m) => logged.push(m); console.log = (m) => logged.push(m);
  const pending = [];
  await worker.scheduled(
    { cron: "*/30 * * * *", scheduledTime: Date.UTC(2026, 7, 10, 10, 0) },
    ENV,
    { waitUntil: (p) => pending.push(p) }
  );
  await Promise.all(pending);
  console.error = realErr; console.log = realLog;
  const blob = logged.join(" ");
  assert.ok(blob.includes("scan.yml"), "the scan failure was not recorded");
  assert.ok(blob.includes("live.yml"), "the live dispatch was lost with it");
});
