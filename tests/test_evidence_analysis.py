import unittest

from foxhole_forecast.evidence_analysis import evidence_family, summarize_pair_evidence


def bet(evidence=None, **changes):
    return {"status": "hit", "crps_minutes": 30, "selection_exact_outcome": True,
            "selection_capture_observed": False,
            "eta_error_minutes": 180, "evidence": evidence, **changes}


def cite(metric_id, relevance=8):
    return {"metric_id": metric_id, "relevance": relevance}


def group(left, right):
    return {"left_series_id": "a", "right_series_id": "b", "left_label": "A",
            "right_label": "B", "mature_pairs": [(left, right)]}


class EvidenceAnalysisTests(unittest.TestCase):
    def test_family_preserves_scope_faction_and_window(self):
        self.assertEqual(evidence_family("region.A.wardenCasualties.delta_2h")[0],
                         evidence_family("region.B.wardenCasualties.delta_2h")[0])
        keys = {evidence_family(value)[0] for value in (
            "region.A.wardenCasualties.delta_2h", "region.A.wardenCasualties.delta_6h",
            "region.A.colonialCasualties.delta_2h", "base.A.wardenCasualties.delta_2h",
            "region.A.wardenCasualties.rate_2h_per_hour",
        )}
        self.assertEqual(len(keys), 5)
        self.assertEqual(evidence_family("unknown.a.b.c")[0], "unknown.a.b.c")

    def test_per_bet_frequency_and_relevance_do_not_overweight_duplicates(self):
        one = "region.A.wardenCasualties.delta_2h"
        two = "region.B.wardenCasualties.delta_2h"
        left = {"bets": [bet([cite(one, 2), cite(one, 2), cite(two, 10)]),
                          bet([cite(one, 10)]), bet(None)]}
        result = summarize_pair_evidence(group(left, {"bets": []}))["models"][0]
        summary = result["families"][0]["success"]
        self.assertEqual(summary["bets"], 2)
        self.assertEqual(summary["denominator"], 3)
        self.assertAlmostEqual(summary["citation_rate"], 2 / 3, places=6)
        self.assertEqual(summary["mean_relevance"], 8)  # mean of 6 and 10
        self.assertEqual(result["missing_evidence_bets"], 1)

    def test_missing_and_invalid_ratings_are_not_zero_ratings(self):
        metric = "region.A.wardenCasualties.raw"
        left = {"bets": [bet([cite(metric, value)]) for value in (None, 0, True, 11, 4)]}
        row = summarize_pair_evidence(group(left, {"bets": []}))["models"][0]["families"][0]["success"]
        self.assertEqual(row["bets"], 5)
        self.assertEqual(row["rated_bets"], 1)
        self.assertEqual(row["missing_rating_bets"], 4)
        self.assertEqual(row["mean_relevance"], 4)

    def test_exact_timely_success_and_excluded_observations(self):
        left = {"bets": [bet(), bet(eta_error_minutes=181),
                          bet(selection_exact_outcome=False),
                          bet(eta_error_minutes=None, eta_error_max_minutes=180),
                          bet(status="open", crps_minutes=None),
                          bet(status="censored", crps_minutes=None),
                          bet(selection_capture_observed=None)]}
        row = summarize_pair_evidence(group(left, {"bets": []}))["models"][0]
        self.assertEqual((row["success_bets"], row["other_bets"], row["excluded_bets"]), (2, 2, 3))

    def test_overlap_distinguishes_same_signal_from_same_place_and_omits_empty_sides(self):
        left = {"run_id": "one", "cutoff": "cutoff", "war_id": "war",
                "bets": [bet([cite("region.A.wardenCasualties.raw")])]}
        right = {"run_id": "two", "bets": [bet([cite("region.B.wardenCasualties.raw")])]}
        selected = group(left, right)
        selected["mature_pairs"].extend([(left, {"bets": []}), ({"bets": []}, {"bets": []})])
        result = summarize_pair_evidence(selected)
        self.assertEqual(result["similarity"]["family_jaccard_mean"], 1)
        self.assertEqual(result["similarity"]["exact_metric_jaccard_mean"], 0)
        self.assertEqual(result["similarity"]["family_rounds"], 1)
        self.assertEqual(result["similarity"]["empty_side_rounds"], 2)
        self.assertEqual(result["round_refs"][0]["left_run_id"], "one")

    def test_only_supplied_mature_pairs_are_used(self):
        selected = group({"bets": [bet()]}, {"bets": [bet()]})
        selected["candidate_pairs"] = selected["mature_pairs"]
        selected["mature_pairs"] = []
        result = summarize_pair_evidence(selected)
        self.assertEqual(result["mature_shared_rounds"], 0)
        self.assertEqual(result["models"][0]["scoreable_bets"], 0)
        self.assertIsNone(result["similarity"]["family_jaccard_mean"])

    def test_dashboard_prediction_contract_is_selected_over_legacy_bets(self):
        metric = "region.A.wardenCasualties.delta_2h"
        selected = group(
            {
                "predictions": [bet([cite(metric, 9)])],
                # A stale alias must not make an explicitly empty prediction
                # list look scoreable or cited.
                "bets": [bet([cite("region.A.colonialCasualties.raw", 1)])],
            },
            {"predictions": [], "bets": [bet([cite(metric)])]},
        )
        result = summarize_pair_evidence(selected)
        left, right = result["models"]
        self.assertEqual(left["scoreable_bets"], 1)
        self.assertEqual(right["scoreable_bets"], 0)
        self.assertEqual(left["families"][0]["success"]["mean_relevance"], 9)

    def test_censored_predictions_never_become_successes(self):
        selected = group(
            {"predictions": [
                bet([cite("region.A.wardenCasualties.raw")], status="censored", crps_minutes=None),
                bet([cite("region.A.wardenCasualties.raw")], status="censored", crps_minutes=None),
            ]},
            {"predictions": []},
        )
        model = summarize_pair_evidence(selected)["models"][0]
        self.assertEqual(model["scoreable_bets"], 0)
        self.assertEqual(model["success_bets"], 0)
        self.assertEqual(model["other_bets"], 0)
        self.assertEqual(model["excluded_bets"], 2)
        self.assertEqual(model["families"], [])
