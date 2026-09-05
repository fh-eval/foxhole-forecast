import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const page = fs.readFileSync(new URL("../src/pages/comparisons.astro", import.meta.url), "utf8");
const pairingStart = page.indexOf("      function pairedEvaluationCount(");
const pairingEnd = page.indexOf("      // Stored pairs", pairingStart);
const { pairedEvaluationCount, defaultPair } = eval(`(() => { ${page.slice(pairingStart, pairingEnd)} return { pairedEvaluationCount, defaultPair }; })()`);
const start = page.indexOf("      function orientPair(");
const end = page.indexOf("      function findPair(", start);
const orientPair = eval(`(${page.slice(start, end).trim()})`);
const stateStart = page.indexOf("      function metricHasEvaluations(");
const stateEnd = page.indexOf("      function summaryCard(", stateStart);
const { metricHasEvaluations, comparisonEmptyMessage, successfulMetric } = eval(`(() => { ${page.slice(stateStart, stateEnd)} return { metricHasEvaluations, comparisonEmptyMessage, successfulMetric }; })()`);
const gapStart = page.indexOf("      function citationGap(");
const gapEnd = page.indexOf("      function evidenceFamilyCard(", gapStart);
const { citationGap } = eval(`(() => { ${page.slice(gapStart, gapEnd)} return { citationGap }; })()`);

const storedPair = () => ({
  left_series_id: "model-a", right_series_id: "model-b", left_label: "A", right_label: "B",
  left_duplicate_rounds: 1, right_duplicate_rounds: 2,
  left_unmatched_cutoffs: 3, right_unmatched_cutoffs: 4,
  left_retention: { scored_bets: 10 }, right_retention: { scored_bets: 20 },
  left_mature_retention: { scored_bets: 8 }, right_mature_retention: { scored_bets: 18 },
  metrics: { all: { exact_outcome: { left_mean: 0.25, right_mean: 0.5, difference: -0.25, wins: 2, ties: 1, losses: 4 } } },
  evidence: { models: [{ series_id: "model-a" }, { series_id: "model-b" }], round_refs: [{ left_run_id: "run-a", right_run_id: "run-b" }] },
});

test("reverse selection swaps all sides and negates paired differences", () => {
  const oriented = orientPair(storedPair(), "model-b", "model-a");
  assert.equal(oriented.left_series_id, "model-b");
  assert.equal(oriented.right_series_id, "model-a");
  assert.equal(oriented.metrics.all.exact_outcome.left_mean, 0.5);
  assert.equal(oriented.metrics.all.exact_outcome.right_mean, 0.25);
  assert.equal(oriented.metrics.all.exact_outcome.difference, 0.25);
  assert.equal(oriented.metrics.all.exact_outcome.wins, 4);
  assert.equal(oriented.metrics.all.exact_outcome.losses, 2);
  assert.deepEqual(oriented.evidence.models.map(({ series_id }) => series_id), ["model-b", "model-a"]);
  assert.deepEqual(oriented.evidence.round_refs[0], { left_run_id: "run-b", right_run_id: "run-a" });
  assert.equal(oriented.left_duplicate_rounds, 2);
  assert.equal(oriented.right_unmatched_cutoffs, 3);
});

test("page keeps explicit identical and unavailable pair states", () => {
  assert.match(page, /Choose two different model series/);
  assert.match(page, /No shared candidate cutoff is recorded/);
  assert.match(page, /no timely-score result/);
  assert.match(page, /no mature rounds yet/);
});

test("producer-shaped zero metric objects show no-score versus no-mature states", () => {
  const zeroMetrics = { active_base: { evaluated_rounds: 0, left_mean: null, right_mean: null, difference: null }, exact_outcome: { evaluated_rounds: 0 }, timely_exact_outcome: { evaluated_rounds: 0 } };
  assert.equal(metricHasEvaluations(zeroMetrics.active_base), false);
  assert.equal(comparisonEmptyMessage({ mature_shared_rounds: 4, metrics: { all: zeroMetrics }}), "This pair has mature rounds, but no timely-score result is available for this performance window yet; the named outcome must occur within 3 hours of the predicted time.");
  assert.equal(comparisonEmptyMessage({ mature_shared_rounds: 0, metrics: { all: zeroMetrics }}), "A shared candidate cutoff exists, but this pair has no mature rounds yet; a timely score requires a settled named outcome within 3 hours of the predicted time.");
  assert.match(page, /if \(!metricHasEvaluations\(value\)\) return/);
});

test("headline result requires a timely score and never falls back to exact outcome", () => {
  const exactOnly = { metrics: { all: {
    exact_outcome: { evaluated_rounds: 4, left_mean: 0.9, right_mean: 0.2 },
    timely_exact_outcome: { evaluated_rounds: 0, left_mean: null, right_mean: null },
  } } };
  assert.equal(successfulMetric(exactOnly, "all"), null);
  assert.match(page, /named an outcome that occurred within 3 hours of the predicted time/);
  assert.match(page, /the named outcome occurred within 3 hours of the predicted time/);
});

test("evidence gap stays missing when a citation rate or denominator is unavailable", () => {
  assert.equal(citationGap({ citation_rate: null, denominator: 4 }, { citation_rate: 0.2, denominator: 4 }), null);
  assert.equal(citationGap({ citation_rate: 0.2, denominator: 0 }, { citation_rate: 0.1, denominator: 4 }), null);
  assert.ok(Math.abs(citationGap({ citation_rate: 0.6, denominator: 4 }, { citation_rate: 0.2, denominator: 4 }) - 0.4) < 1e-12);
  assert.match(page, /Not enough calls in one group/);
});

test("default pair prefers the largest evaluated mature sample, not result size", () => {
  const pair = (left, right, evaluated, mature, leftMean) => ({
    left_series_id: left, right_series_id: right, mature_shared_rounds: mature,
    metrics: { all: { timely_exact_outcome: { evaluated_rounds: evaluated, left_mean: leftMean } } },
  });
  const chosen = defaultPair([
    pair("alpha", "winner", 4, 99, 0.99),
    pair("beta", "gamma", 8, 8, 0.01),
    pair("delta", "epsilon", 8, 12, 0.02),
  ]);
  assert.equal(pairedEvaluationCount(chosen), 8);
  assert.deepEqual([chosen.left_series_id, chosen.right_series_id], ["delta", "epsilon"], "mature rounds break an evaluated-count tie");
});

test("comparison keeps readable selector labels and exact IDs in audit copy", () => {
  assert.match(page, /Model 1 <select/);
  assert.match(page, /Model 2 <select/);
  assert.match(page, /collection v\$\{match\[1\]\}/);
  assert.match(page, /Exact model identities/);
  assert.doesNotMatch(page, /Choose a pair/);
});

test("result and evidence summaries stay outside the collapsed audit view", () => {
  assert.match(page, /Shared-round result/);
  assert.match(page, /What did they cite\?/);
  assert.match(page, /Did successful calls cite it more often\?/);
  assert.match(page, /Audit view: metrics, retention, evidence, IDs, and round references/);
  assert.match(page, /table-shell comparison-audit-scroll/);
});

test("round references stay preserved inside a nested bounded disclosure", () => {
  assert.match(page, /refs\.className = "comparison-refs"/);
  assert.match(page, /Round references \(\$\{number\(evidence\.round_refs\?\.length/);
  assert.match(page, /comparison-refs__body/);
  assert.match(page, /data:image\/svg\+xml/);
});
