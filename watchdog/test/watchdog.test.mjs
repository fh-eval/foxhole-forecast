import assert from "node:assert/strict";
import test from "node:test";

import {
  cacheBustedUrl,
  checkAndDispatch,
  forecastSlot,
  observationIsStale,
  successfulRunSince,
} from "../src/index.mjs";

const now = Date.parse("2026-08-22T12:00:00Z");

test("an observation inside the threshold is fresh", () => {
  assert.equal(observationIsStale("2026-08-22T11:47:00Z", now, 14), false);
});

test("an observation beyond the threshold is stale", () => {
  assert.equal(observationIsStale("2026-08-22T11:45:00Z", now, 14), true);
});

test("an invalid observation timestamp is rejected", () => {
  assert.throws(() => observationIsStale("unknown", now, 14), /valid observed_at/);
});

test("forecast slots are aligned to three-hour UTC boundaries", () => {
  assert.equal(
    forecastSlot(Date.parse("2026-08-22T10:47:12Z")).toISOString(),
    "2026-08-22T09:00:00.000Z",
  );
});

test("repository data URLs are cache-busted for each watchdog check", () => {
  assert.equal(
    cacheBustedUrl("https://example.test/latest.json?raw=1", now),
    `https://example.test/latest.json?raw=1&watchdog_time=${now}`,
  );
});

test("a fresh status document avoids large state downloads and dispatches", async () => {
  const requests = [];
  const result = await checkAndDispatch(
    {
      GITHUB_TOKEN: "test-token",
      STATUS_DATA_URL: "https://example.test/watchdog.json",
      STALE_AFTER_MINUTES: "14",
    },
    async (url) => {
      requests.push(url);
      return Response.json({
        observed_at: "2026-08-22T11:47:00Z",
        last_forecast_slot: "2026-08-22T12:00:00Z",
      });
    },
    now,
  );

  assert.equal(requests.length, 1);
  assert.equal(requests[0].startsWith("https://example.test/watchdog.json?"), true);
  assert.deepEqual(result.actions, [{ action: "fresh" }]);
});

test("a successful run inside the guard window suppresses a duplicate", () => {
  const runs = [
    { status: "completed", conclusion: "failure", created_at: "2026-08-22T12:30:00Z" },
    { id: 42, status: "completed", conclusion: "success", created_at: "2026-08-22T12:15:00Z" },
  ];
  assert.equal(successfulRunSince(runs, Date.parse("2026-08-22T12:00:00Z"))?.id, 42);
  assert.equal(successfulRunSince(runs, Date.parse("2026-08-22T12:20:00Z")), undefined);
});
