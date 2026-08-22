import assert from "node:assert/strict";
import test from "node:test";

import { forecastSlot, observationIsStale } from "../src/index.mjs";

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
