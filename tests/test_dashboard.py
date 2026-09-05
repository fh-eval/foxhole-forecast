from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foxhole_forecast.dashboard import (
    build_dashboard_data,
    _build_war_api_snapshot,
    _behavior_summary,
    _dashboard_family_rounds,
    _forecast_status,
    _latest_round_groups,
    _metric_label,
    _predicted_outcome,
    _present_evidence,
    _present_strategic_advice,
    _round_slot,
    _write_dashboard_shards,
)
from foxhole_forecast.forecasting import _freeze_evidence
from foxhole_forecast.config import Settings
from foxhole_forecast.storage import append_jsonl, read_json, read_jsonl


class DashboardTests(unittest.TestCase):
    def test_dashboard_sanitizes_malformed_dropped_ids_without_publishing_raw(self) -> None:
        run = {
            "run_id": "run-1",
            "cohort_id": "cohort-1",
            "series_id": "model-1",
            "war_id": "war-1",
            "cutoff": "2026-01-01T00:00:00Z",
            "status": "valid",
            "forecast": {"predictions": [{"base_id": "Map:valid"}]},
            "dropped_predictions": [
                {"base_id": ["invalid"], "reason": "invalid base", "raw_prediction": {"base_id": ["invalid"]}},
                {"base_id": "Map:missing", "reason": "unknown base"},
            ],
            "dropped_strategic_advice": [
                {"base_id": {"invalid": True}, "reason": "invalid base", "raw_recommendation": {"base_id": {"invalid": True}}},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            append_jsonl(data / "model_runs.jsonl", run)
            with (
                patch("foxhole_forecast.dashboard.ROOT", root),
                patch("foxhole_forecast.dashboard.DATA_DIR", data),
            ):
                output = build_dashboard_data(Settings.load())

            round_record = output["rounds"][0]
            self.assertIsNone(round_record["dropped_predictions"][0]["base_id"])
            self.assertEqual(round_record["dropped_predictions"][1]["base_id"], "Map:missing")
            self.assertIsNone(round_record["dropped_strategic_advice"][0]["base_id"])
            for path in (root / "web" / "public" / "data").glob("*.json"):
                payload = path.read_text()
                self.assertNotIn("raw_prediction", payload)
                self.assertNotIn("raw_recommendation", payload)
            self.assertEqual(read_jsonl(data / "model_runs.jsonl"), [run])

    def test_dashboard_family_rounds_preserve_source_records(self) -> None:
        rounds = [
            {"series_id": "deepseek-v4", "predictions": [{"rank": 1}]},
            {"series_id": "deepseek-v5", "predictions": [{"rank": 2}]},
        ]

        displayed = _dashboard_family_rounds(
            rounds, {"deepseek-v4": "deepseek-v5"}
        )

        self.assertEqual([row["series_id"] for row in displayed], ["deepseek-v5"] * 2)
        self.assertEqual(rounds[0]["series_id"], "deepseek-v4")

    def test_dashboard_shards_replace_the_unused_monolith(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = {
                "schema_version": 12,
                "generated_at": "2026-01-01T09:00:00Z",
                "methodology": {"current_protocol": "current"},
                "models": [
                    {
                        "series_id": "model-a",
                        "label": "Model A",
                        "latest": {"run_id": "latest"},
                        "latest_all_time": {"run_id": "all-time"},
                        "history": [{"run_id": "historical"}],
                    },
                    {
                        "series_id": "model-hidden",
                        "label": "Retired Model",
                        "latest": {"run_id": "hidden-latest"},
                        "latest_all_time": {"run_id": "hidden-all-time"},
                        "history": [{"run_id": "hidden-historical"}],
                    },
                ],
                "model_behavior": {
                    "current_war": [
                        {"series_id": "model-a"},
                        {"series_id": "model-hidden"},
                    ],
                    "all_time": [
                        {"series_id": "model-a"},
                        {"series_id": "model-hidden"},
                    ],
                    "by_war": {
                        "war-1": [
                            {"series_id": "model-a"},
                            {"series_id": "model-hidden"},
                        ]
                    },
                },
                "rounds": [
                    {
                        "war_id": "war-1",
                        "round_slot": f"2026-01-01T0{hour}:00:00Z",
                        "protocol": "current",
                    }
                    for hour in (9, 6, 3, 0)
                ]
                + [
                    {
                        "war_id": "war-1",
                        "round_slot": "2026-01-01T09:00:00Z",
                        "protocol": "current",
                        "series_id": "model-hidden",
                    }
                ],
                "base_forecasts": [{"base_id": "unused"}],
            }
            with patch("foxhole_forecast.dashboard.ROOT", root):
                _write_dashboard_shards(output, {"model-hidden"})

            data = root / "web" / "public" / "data"
            main = read_json(data / "dashboard-main.json")
            rounds = read_json(data / "round-history.json")
            summaries = read_json(data / "summary-history.json")
            self.assertFalse((data / "dashboard.json").exists())
            self.assertNotIn("history", main["models"][0])
            self.assertNotIn("latest_all_time", main["models"][0])
            self.assertEqual(
                [model["series_id"] for model in main["models"]], ["model-a"]
            )
            self.assertEqual(
                [row["series_id"] for row in main["model_behavior"]["all_time"]],
                ["model-a"],
            )
            self.assertEqual(
                [row["series_id"] for row in main["model_behavior"]["by_war"]["war-1"]],
                ["model-a"],
            )
            self.assertEqual(len(main["rounds"]), 3)
            self.assertNotIn(
                "model-hidden", {row.get("series_id") for row in main["rounds"]}
            )
            self.assertEqual(main["base_forecasts"], [])
            self.assertEqual(len(rounds["rounds"]), 5)
            self.assertIn(
                "model-hidden", {row.get("series_id") for row in rounds["rounds"]}
            )
            self.assertEqual(
                summaries["models"][0]["history"], [{"run_id": "historical"}]
            )
            self.assertEqual(len(summaries["models"]), 2)

    def test_latest_round_groups_keeps_every_participant_in_three_newest_slots(self) -> None:
        rounds = [
            {
                "war_id": "war-2",
                "round_slot": slot,
                "protocol": protocol,
                "series_id": model,
            }
            for slot, protocol, model in (
                ("2026-01-01T09:00:00Z", "event_outcome_v5_crps", "a"),
                ("2026-01-01T09:00:00Z", "event_outcome_v5_crps", "b"),
                ("2026-01-01T06:00:00Z", "event_outcome_v5_crps", "a"),
                ("2026-01-01T03:00:00Z", "event_outcome_v5_crps", "a"),
                ("2026-01-01T00:00:00Z", "event_outcome_v5_crps", "a"),
                ("2026-01-01T12:00:00Z", "legacy", "old"),
            )
        ]

        selected = _latest_round_groups(rounds, "event_outcome_v5_crps")

        self.assertEqual([row["series_id"] for row in selected], ["a", "b", "a", "a"])
        self.assertNotIn("2026-01-01T00:00:00Z", {row["round_slot"] for row in selected})

    def test_behavior_summary_can_separate_current_war_from_all_time(self) -> None:
        def round_for(war_id: str, credit: float) -> dict:
            return {
                "war_id": war_id,
                "protocol": "event_outcome_v5_crps",
                "series_id": "model-1",
                "model_label": "Model One",
                "predictions": [
                    {
                        "status": "hit" if credit else "miss",
                        "timing_credit": credit,
                        "crps_minutes": 1 - credit,
                        "confidence": 0.5,
                        "sigma_minutes": 90,
                        "tranche": "IMMEDIATE",
                        "cutoff": "2026-01-01T00:00:00Z",
                        "eta_utc": "2026-01-01T02:00:00Z",
                        "eta_error_minutes": 15,
                    }
                ],
            }

        rounds = [round_for("war-1", 0), round_for("war-2", 1)]
        rounds[0]["dropped_predictions"] = [{"reason": "invalid base"}]
        rounds[1]["submission_mode"] = "delayed_replay"

        current = _behavior_summary(rounds, "war-2", 1)[0]
        all_time = _behavior_summary(rounds, None, 1)[0]
        self.assertEqual(current["crps_minutes"], 0)
        self.assertEqual(current["short_crps_minutes"], 0)
        self.assertIsNone(current["long_crps_minutes"])
        self.assertIsNone(current["forecast_score"])
        self.assertEqual(current["published_bets"], 1)
        self.assertEqual(all_time["crps_minutes"], 0.5)
        self.assertEqual(all_time["published_bets"], 2)
        self.assertEqual(current["retention"]["considered_bets"], 1)
        self.assertEqual(current["retention"]["dropped_bets"], 0)
        self.assertEqual(current["retention"]["by_submission_mode"]["delayed_replay"]["scored_bets"], 1)
        self.assertEqual(all_time["retention"]["considered_bets"], 3)
        self.assertEqual(all_time["retention"]["dropped_reasons"], {"invalid base": 1})
        self.assertEqual(all_time["retention"]["by_submission_mode"]["live"]["scored_bets"], 1)

    def test_behavior_summary_scopes_actionable_outcomes_by_war(self) -> None:
        def round_for(war_id: str, eta_error: float | None) -> dict:
            return {
                "war_id": war_id,
                "protocol": "event_outcome_v5_crps",
                "series_id": "model-1",
                "model_label": "Model One",
                "predictions": [
                    {
                        "status": "partial",
                        "crps_minutes": 10,
                        "confidence": 0.5,
                        "sigma_minutes": 90,
                        "tranche": "IMMEDIATE",
                        "cutoff": "2026-01-01T00:00:00Z",
                        "eta_utc": "2026-01-01T02:00:00Z",
                        "eta_error_minutes": eta_error,
                        "selection_capture_observed": True,
                        "selection_transition_observed": True,
                        "selection_exact_outcome": True,
                    }
                ],
            }

        rounds = [round_for("war-1", 181), round_for("war-2", 180)]

        current = _behavior_summary(rounds, "war-2", 1)[0]
        all_time = _behavior_summary(rounds, None, 1)[0]
        self.assertEqual(current["actionable_exact_outcome_hits"], 1)
        self.assertEqual(current["actionable_exact_outcome_bets"], 1)
        self.assertEqual(current["actionable_exact_outcome_rate"], 1)
        self.assertEqual(all_time["actionable_exact_outcome_hits"], 1)
        self.assertEqual(all_time["actionable_exact_outcome_bets"], 2)
        self.assertEqual(all_time["actionable_exact_outcome_rate"], 0.5)

    def test_forecast_status_respects_war_end_and_history_warmup(self) -> None:
        active = {"warId": "war-1", "winner": "NONE"}
        ended = {"warId": "war-1", "winner": "WARDENS"}

        self.assertEqual(_forecast_status(active, 1.5, 2), "warming_up")
        self.assertEqual(_forecast_status(active, 2, 2), "ready")
        self.assertEqual(_forecast_status(ended, 24, 2), "war_inactive")

    def test_round_slot_is_shared_across_a_three_hour_block(self) -> None:
        self.assertEqual(
            _round_slot("2026-08-22T07:59:59Z", 3),
            "2026-08-22T06:00:00Z",
        )

    def test_predicted_outcome_is_recovered_from_archived_forecast(self) -> None:
        settled_bet = {"outcome": None}
        forecast_bet = {"outcome": "CAPTURED"}

        self.assertEqual(
            _predicted_outcome(settled_bet, forecast_bet), "CAPTURED"
        )

    def test_same_faction_capture_is_presented_as_self_capture(self) -> None:
        self.assertEqual(
            _predicted_outcome(
                {"current_team": "WARDENS", "predicted_outcome": "CAPTURED_BY_WARDENS"},
                {},
            ),
            "SELF_CAPTURE",
        )

    def test_timed_prediction_freezes_base_state_and_evidence(self) -> None:
        metric_id = "region.TestHex.totalEnlistments.rate_1h_per_hour"
        forecast = {
            "predictions": [
                {
                    "base_id": "base-1",
                    "evidence": [{"metric_id": metric_id, "relevance": 6}],
                }
            ],
            "strategic_advice": {
                "warden_reinforce": {
                    "base_id": "base-1",
                    "reason": "Hold this base.",
                    "evidence": [{"metric_id": metric_id, "relevance": 8}],
                }
            },
        }
        packet = {
            "strategic_bases": [
                {
                    "base_id": "base-1",
                    "name": "Test Base",
                    "map_name": "TestHex",
                    "current_owner": "WARDENS",
                    "icon_type": 45,
                    "base_type": "Relic Base",
                }
            ],
            "selected_metrics": [
                {
                    "metric_id": metric_id,
                    "value": 42,
                    "observed_at": "2026-08-22T03:00:00Z",
                }
            ],
        }

        frozen = _freeze_evidence(forecast, packet)

        prediction = frozen["predictions"][0]
        self.assertEqual(prediction["current_team"], "WARDENS")
        self.assertEqual(prediction["base_name"], "Test Base")
        self.assertEqual(prediction["icon_type"], 45)
        self.assertEqual(prediction["base_type"], "Relic Base")
        self.assertEqual(prediction["evidence"][0]["value"], 42)
        advice = frozen["strategic_advice"]["warden_reinforce"]
        self.assertEqual(advice["base_name"], "Test Base")
        self.assertEqual(advice["current_team"], "WARDENS")
        self.assertEqual(advice["evidence"][0]["value"], 42)

    def test_strategic_advice_is_presented_with_readable_frozen_evidence(self) -> None:
        metric_id = "region.TestHex.totalEnlistments.rate_1h_per_hour"
        advice = {
            "warden_reinforce": {
                "base_id": "base-1",
                "reason": "Activity is rising.",
                "evidence": [
                    {"metric_id": metric_id, "relevance": 8, "value": 42}
                ],
            }
        }

        presented = _present_strategic_advice(
            advice,
            {
                "base-1": {
                    "name": "Test Base",
                    "base_type": "Relic Base",
                    "map_name": "TestHex",
                    "current_owner": "WARDENS",
                }
            },
            {},
        )

        self.assertEqual(presented["warden_reinforce"]["base_name"], "Test Base")
        self.assertEqual(
            presented["warden_reinforce"]["evidence"][0]["label"],
            "Test · Enlistments rate, last 1h (per hour)",
        )

    def test_war_api_snapshot_summarizes_current_official_inputs(self) -> None:
        latest = {
            "observed_at": "2026-08-22T03:00:00Z",
            "maps": {
                "QuietHex": {
                    "bases": {
                        "q1": {"team": "WARDENS"},
                    },
                    "report": {
                        "dayOfWar": 83,
                        "wardenCasualties": 10,
                        "colonialCasualties": 20,
                        "totalEnlistments": 30,
                    },
                },
                "ActiveHex": {
                    "bases": {
                        "a1": {"team": "COLONIALS"},
                        "a2": {"team": "NONE"},
                    },
                    "report": {
                        "dayOfWar": 83,
                        "wardenCasualties": 100,
                        "colonialCasualties": 200,
                        "totalEnlistments": 300,
                    },
                },
            },
        }
        events = [
            {
                "observed_to": "2026-08-22T02:00:00Z",
                "map_name": "ActiveHex",
                "base_name": "Test Base",
                "event_type": "CAPTURED_BY_WARDENS",
                "actor": "WARDENS",
            },
            {
                "observed_to": "2026-08-20T02:00:00Z",
                "map_name": "QuietHex",
                "base_name": "Old Base",
                "event_type": "OWNER_LOSES",
                "actor": "WARDENS",
            },
        ]

        snapshot = _build_war_api_snapshot(latest, events)

        self.assertEqual(snapshot["day_of_war"], 83)
        self.assertEqual(
            snapshot["strategic_base_ownership"],
            {"WARDENS": 1, "COLONIALS": 1, "NONE": 1},
        )
        self.assertEqual(snapshot["casualties"], {"WARDENS": 110, "COLONIALS": 220})
        self.assertEqual(snapshot["active_regions"][0]["map_name"], "ActiveHex")
        self.assertEqual(snapshot["active_regions"][0]["activity"]["events_24h"], 1)
        self.assertEqual(len(snapshot["recent_ownership_events"]), 1)

    def test_war_api_snapshot_keeps_twenty_four_complete_transitions(self) -> None:
        latest = {"observed_at": "2026-08-22T23:59:00Z", "maps": {}}
        events = []
        for minute in range(30):
            observed_to = f"2026-08-22T23:{59 - minute:02d}:00Z"
            events.extend(
                [
                    {
                        "observed_to": observed_to,
                        "map_name": "TestHex",
                        "base_name": f"Base {minute}",
                        "event_type": "OWNER_LOSES",
                        "actor": "WARDENS",
                    },
                    {
                        "observed_to": observed_to,
                        "map_name": "TestHex",
                        "base_name": f"Base {minute}",
                        "event_type": "CAPTURED_BY_COLONIALS",
                        "actor": "COLONIALS",
                    },
                ]
            )

        displayed = _build_war_api_snapshot(latest, events)["recent_ownership_events"]

        grouped = {
            (event["observed_at"], event["map_name"], event["base_name"])
            for event in displayed
        }
        self.assertEqual(len(grouped), 24)
        self.assertEqual(len(displayed), 48)
        self.assertEqual({event["base_name"] for event in displayed[-2:]}, {"Base 23"})

    def test_evidence_value_is_frozen_and_presented_readably(self) -> None:
        metric_id = "region.OarbreakerHex.casualties.ratio_colonial_to_warden"
        forecast = {
            "base_forecasts": [
                {"events": [{"evidence": [{"metric_id": metric_id, "relevance": 9}]}]}
            ]
        }
        packet = {
            "selected_metrics": [
                {
                    "metric_id": metric_id,
                    "value": 0.9234,
                    "observed_at": "2026-08-22T03:00:00Z",
                }
            ]
        }

        frozen = _freeze_evidence(forecast, packet)
        evidence = frozen["base_forecasts"][0]["events"][0]["evidence"][0]
        presented = _present_evidence(evidence, {})

        self.assertNotIn("value", forecast["base_forecasts"][0]["events"][0]["evidence"][0])
        self.assertEqual(presented["value"], 0.9234)
        self.assertEqual(presented["observed_at"], "2026-08-22T03:00:00Z")
        self.assertEqual(
            _metric_label(metric_id),
            "Oarbreaker · Colonial/Warden casualty ratio",
        )
        self.assertEqual(
            _metric_label(
                "region.OarbreakerHex.wardenCasualties.rate_change_1h_vs_previous"
            ),
            "Oarbreaker · Warden casualties rate change, last 1h vs prior 1h",
        )
        self.assertEqual(
            _metric_label("region.CallahansPassageHex.totalEnlistments.raw"),
            "Callahan's Passage · Enlistments",
        )

    def test_war_api_snapshot_reuses_the_exact_scout_region_metrics(self) -> None:
        latest = {
            "observed_at": "2026-08-22T03:00:00Z",
            "maps": {
                "TestHex": {
                    "bases": {"a": {"team": "WARDENS"}},
                    "report": {
                        "dayOfWar": 83,
                        "wardenCasualties": 100,
                        "colonialCasualties": 200,
                        "totalEnlistments": 300,
                    },
                }
            },
        }
        scout_region = {
            "map_name": "TestHex",
            "strategic_base_count": 1,
            "ownership": {"WARDENS": 1, "COLONIALS": 0, "NONE": 0},
            "report": {"warden_casualties": 100, "colonial_casualties": 200},
            "report_deltas": {"2h": {"warden_casualties": 20}},
            "rate_trends": {
                "1h_vs_prior_1h": {
                    "warden_casualties": [12, 8, 4, "accelerating"]
                }
            },
            "activity": {
                "events_2h": 1,
                "events_6h": 2,
                "events_24h": 3,
                "latest_event_at": "2026-08-22T02:30:00Z",
            },
        }

        snapshot = _build_war_api_snapshot(
            latest,
            [],
            {
                "packet_version": 2,
                "history_hours_available": 24,
                "data_dictionary": {"total_enlistments": "activity proxy"},
                "regions": [scout_region],
            },
        )

        self.assertEqual(
            snapshot["active_regions"][0]["activity"], scout_region["activity"]
        )
        self.assertEqual(
            snapshot["active_regions"][0]["rate_trends"],
            scout_region["rate_trends"],
        )
        self.assertEqual(snapshot["history_hours_available"], 24)


if __name__ == "__main__":
    unittest.main()
