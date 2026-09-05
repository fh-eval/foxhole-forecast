from __future__ import annotations

import copy
import unittest
from datetime import UTC, datetime

from foxhole_forecast.comparisons import eligible_pair_rounds, summarize_comparisons


AS_OF = datetime(2026, 1, 3, tzinfo=UTC)


def bet(*, hit=True, tranche="IMMEDIATE", status=None):
    return {
        "eta_utc": "2026-01-01T02:00:00Z",
        "tranche": tranche,
        "status": status or ("hit" if hit else "miss"),
        "crps_minutes": None if status in {"censored", "open"} else 10,
        "selection_capture_observed": hit,
        "selection_transition_observed": hit,
        "selection_exact_outcome": hit,
        "eta_error_minutes": 20 if hit else None,
    }


def round_record(series, **overrides):
    return {
        "series_id": series,
        "model_label": series.upper(),
        "run_id": series,
        "war_id": "war-1",
        "cutoff": "2026-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:01:00Z",
        "protocol": "event_outcome_v5_crps",
        "submission_mode": "live",
        "settlement_updated_at": "2026-01-01T05:00:00Z",
        "predictions": [bet()],
        **overrides,
    }


class ComparisonTests(unittest.TestCase):
    def test_exact_cutoff_war_protocol_and_mode_are_required(self):
        for overrides in (
            {"cutoff": "2026-01-01T00:00:01Z"},
            {"war_id": "war-2"},
            {"protocol": "legacy"},
            {"submission_mode": "delayed_replay"},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(eligible_pair_rounds([
                    round_record("a"), round_record("b", **overrides)
                ], as_of=AS_OF), [])
        groups = eligible_pair_rounds([
            round_record("a"), round_record("b", cutoff="2025-12-31T18:00:00-06:00")
        ], as_of=AS_OF)
        self.assertEqual(groups[0]["mature_shared_rounds"], 1)

    def test_maturity_waits_for_deadlines_and_fresh_settlement_even_for_hits(self):
        for overrides, as_of in (
            ({}, datetime(2026, 1, 1, 4, tzinfo=UTC)),
            ({"settlement_updated_at": "2026-01-01T04:59:59Z"}, AS_OF),
            ({"settlement_updated_at": None}, AS_OF),
            ({"predictions": [bet(status="open")]}, AS_OF),
            ({"predictions": [bet(), {**bet(), "eta_utc": "2026-01-04T00:00:00Z"}]}, AS_OF),
            ({"predictions": []}, AS_OF),
            ({"prediction_count": 2}, AS_OF),
        ):
            with self.subTest(overrides=overrides, as_of=as_of):
                group = eligible_pair_rounds([
                    round_record("a"), round_record("b", **overrides)
                ], as_of=as_of)[0]
                self.assertEqual(group["shared_candidate_cutoffs"], 1)
                self.assertEqual(group["mature_shared_rounds"], 0)
                self.assertEqual(group["pending_excluded_rounds"], 1)

    def test_metrics_weight_rounds_equally_and_report_retention(self):
        rounds = [
            round_record("a", predictions=[bet(), bet(status="censored")], dropped_predictions=[{"reason": "invalid"}]),
            round_record("b", predictions=[bet(hit=False)]),
            round_record("a", cutoff="2026-01-01T01:00:00Z", predictions=[bet(hit=False)] * 3),
            round_record("b", cutoff="2026-01-01T01:00:00Z", predictions=[bet()]),
            round_record("a", cutoff="2026-01-01T02:00:00Z", predictions=[bet(status="censored")]),
            round_record("b", cutoff="2026-01-01T02:00:00Z", predictions=[bet()]),
        ]
        before = copy.deepcopy(rounds)
        summary = summarize_comparisons(rounds, as_of=AS_OF)[0]
        metric = summary["metrics"]["all"]["active_base"]
        self.assertEqual(metric, {
            "left_mean": 0.5, "right_mean": 0.5, "difference": 0,
            "evaluated_rounds": 2, "wins": 1, "ties": 0, "losses": 1,
            "excluded_no_scores_rounds": 1,
        })
        self.assertEqual(summary["left_retention"]["considered_bets"], 7)
        self.assertEqual(summary["left_retention"]["censored_bets"], 2)
        self.assertEqual(summary["left_retention"]["dropped_bets"], 1)
        self.assertEqual(summary["metrics"]["long"]["exact_outcome"]["evaluated_rounds"], 0)
        self.assertEqual(summary["metrics"]["long"]["exact_outcome"]["excluded_no_scores_rounds"], 3)
        self.assertEqual(rounds, before)

    def test_metric_meanings_and_tranches_are_separate(self):
        late = {**bet(tranche="EXTENDED"), "eta_error_minutes": 181}
        wrong = {**bet(), "selection_exact_outcome": False}
        summary = summarize_comparisons([
            round_record("a", predictions=[wrong, late]),
            round_record("b", predictions=[bet(hit=False), bet(hit=False, tranche="EXTENDED")]),
        ], as_of=AS_OF)[0]
        self.assertEqual(summary["metrics"]["all"]["active_base"]["left_mean"], 1)
        self.assertEqual(summary["metrics"]["all"]["exact_outcome"]["left_mean"], 0.5)
        self.assertEqual(summary["metrics"]["all"]["timely_exact_outcome"]["left_mean"], 0)
        self.assertEqual(summary["metrics"]["short"]["exact_outcome"]["left_mean"], 0)
        self.assertEqual(summary["metrics"]["long"]["exact_outcome"]["left_mean"], 1)

    def test_duplicates_choose_creation_then_id_before_maturity(self):
        rounds = [
            round_record("a", run_id="later", created_at="2026-01-01T00:02:00Z"),
            round_record("a", run_id="z", predictions=[bet()]),
            round_record("a", run_id="a", predictions=[bet(status="open")]),
            round_record("b"),
        ]
        selected = eligible_pair_rounds(rounds, as_of=AS_OF)[0]
        self.assertEqual(selected["candidate_pairs"][0][0]["run_id"], "a")
        self.assertEqual(selected["left_duplicate_rounds"], 2)
        self.assertEqual(selected["mature_shared_rounds"], 0)
        self.assertEqual(eligible_pair_rounds(rounds, as_of=AS_OF), eligible_pair_rounds(reversed(rounds), as_of=AS_OF))

    def test_modes_and_wars_stay_separate_and_order_is_stable(self):
        rounds = [round_record(series, war_id=war, submission_mode=mode)
                  for war in ("war-2", "war-1")
                  for mode in ("live", "delayed_replay")
                  for series in ("c", "b", "a")]
        groups = eligible_pair_rounds(rounds, as_of=AS_OF)
        self.assertEqual(len(groups), 6)
        self.assertTrue(all(group["shared_candidate_cutoffs"] == 2 for group in groups))
        self.assertEqual(groups, eligible_pair_rounds(reversed(rounds), as_of=AS_OF))
        self.assertTrue(all(a["war_id"] == b["war_id"] for group in groups for a, b in group["mature_pairs"]))

    def test_unmatched_cutoffs_are_counted_without_including_them_in_rates(self):
        group = summarize_comparisons([
            round_record("a"), round_record("b"),
            round_record("a", cutoff="2026-01-01T01:00:00Z"),
            round_record("b", war_id="war-2"),
        ], as_of=AS_OF)[0]
        self.assertEqual(group["left_unmatched_cutoffs"], 1)
        self.assertEqual(group["right_unmatched_cutoffs"], 1)
        self.assertEqual(group["shared_candidate_cutoffs"], 1)
        self.assertEqual(group["metrics"]["all"]["exact_outcome"]["evaluated_rounds"], 1)


if __name__ == "__main__":
    unittest.main()
