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

import worker, { shouldDispatchLive } from "./src/index.js";

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
