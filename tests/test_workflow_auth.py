from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"


class WorkflowAuthenticationTests(unittest.TestCase):
    def test_model_triage_uses_gpt_6_luna_at_high_reasoning(self) -> None:
        workflow = (WORKFLOWS / "model-triage.yml").read_text(encoding="utf-8")
        registration = (WORKFLOWS.parents[1] / "opencode.json").read_text(
            encoding="utf-8"
        )
        self.assertIn("--model openrouter/openai/gpt-6-luna", workflow)
        self.assertIn('"openai/gpt-6-luna"', registration)
        self.assertIn('"model": "openrouter/openai/gpt-6-luna"', registration)
        self.assertIn('"reasoningEffort": "high"', registration)
        self.assertNotIn("openai/gpt-5.6-luna", workflow)

    def assert_scoped_app_authentication(self, job: str) -> None:
        self.assertIn("permissions:\n      contents: read", job)
        self.assertNotIn("permissions:\n      contents: write", job)
        self.assertIn("vars.DATA_WRITER_APP_CLIENT_ID", job)
        self.assertIn("secrets.DATA_WRITER_APP_PRIVATE_KEY", job)
        self.assertIn("permission-contents: write", job)
        self.assertIn("token: ${{ steps.data-writer-token.outputs.token }}", job)
        self.assertIn(
            "PERSIST_GIT_USER_NAME: ${{ steps.data-writer-token.outputs.app-slug }}[bot]",
            job,
        )
        self.assertIn("id: data-writer-identity", job)
        self.assertIn("GH_TOKEN: ${{ steps.data-writer-token.outputs.token }}", job)
        self.assertIn("PERSIST_GIT_USER_EMAIL:", job)
        self.assertIn("steps.data-writer-identity.outputs.user-id", job)

    def test_collection_persistence_uses_scoped_github_app_token(self) -> None:
        workflow = (WORKFLOWS / "pipeline.yml").read_text(encoding="utf-8")
        self.assert_scoped_app_authentication(workflow.split("\n  persist:\n", 1)[1])

    def test_forecast_persistence_uses_scoped_github_app_token(self) -> None:
        workflow = (WORKFLOWS / "forecast.yml").read_text(encoding="utf-8")
        persist_job = workflow.split("\n  persist:\n", 1)[1].split("\n  audit:\n", 1)[0]
        self.assert_scoped_app_authentication(persist_job)

    def test_archive_persistence_uses_scoped_github_app_token(self) -> None:
        workflow = (WORKFLOWS / "archive-maintenance.yml").read_text(encoding="utf-8")
        self.assert_scoped_app_authentication(workflow.split("\n  maintain:\n", 1)[1])

    def test_model_triage_validates_incident_before_agent_or_comment(self) -> None:
        workflow = (WORKFLOWS / "model-triage.yml").read_text(encoding="utf-8")
        validation = workflow.index("- name: Validate the model-failure incident packet")
        agent = workflow.index("- name: Ask Luna for a read-only diagnosis")
        comment = workflow.index("- name: Post the report using a credential the agent never received")

        self.assertLess(validation, agent)
        self.assertLess(validation, comment)
        self.assertIn('"model-failure" not in label_names', workflow)
        self.assertIn('"<!-- foxhole-model-failure -->" not in body', workflow)
        self.assertIn('r"/actions/runs/(\\d+)"', workflow)
        self.assertIn('r"^- Run: `([^`]+)`\\s*$"', workflow)
        self.assertIn("if isinstance(label, dict) and isinstance(label.get(\"name\"), str)", workflow)

    def test_model_triage_validator_accepts_github_labels_and_rejects_other_issues(self) -> None:
        workflow = (WORKFLOWS / "model-triage.yml").read_text(encoding="utf-8")
        validator = textwrap.dedent(
            workflow.split("          python3 - <<'PY'\n", 1)[1].split(
                "\n          PY", 1
            )[0]
        )
        valid = {
            "labels": [{"name": "model-failure", "color": "b60205"}],
            "body": (
                "<!-- foxhole-model-failure -->\n"
                "- Source workflow: https://github.com/example/repo/actions/runs/123\n"
                "- Run: `cohort:model-a`\n"
            ),
        }
        invalid = {"labels": [{"name": "bug"}], "body": "ordinary issue"}
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory) / ".opencode"
            packet.mkdir()
            incident = packet / "runtime-incident.json"
            for payload, expected in ((valid, 0), (invalid, 1)):
                incident.write_text(json.dumps(payload), encoding="utf-8")
                result = subprocess.run(
                    ["python3", "-c", validator],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, expected, result.stderr)


class DataWriteLockTests(unittest.TestCase):
    """The `data/` single-writer lock covers the commit, not the whole run."""

    WRITERS = (
        ("pipeline.yml", "persist"),
        ("forecast.yml", "persist"),
        ("archive-maintenance.yml", "maintain"),
    )

    def test_only_the_committing_job_holds_the_data_write_lock(self) -> None:
        block = (
            "    concurrency:\n"
            "      group: foxhole-data-pipeline\n"
            "      cancel-in-progress: false\n"
            "      queue: max\n"
        )
        for filename, job in self.WRITERS:
            workflow = (WORKFLOWS / filename).read_text(encoding="utf-8")
            self.assertEqual(
                workflow.count("group: foxhole-data-pipeline"),
                1,
                f"{filename} must declare the write lock exactly once",
            )
            self.assertNotIn(
                "concurrency:\n  group: foxhole-data-pipeline",
                workflow,
                f"{filename} must not hold the write lock at workflow level",
            )
            before_job, _, after_job = workflow.partition(f"\n  {job}:\n")
            self.assertTrue(after_job, f"{filename} has no '{job}' job")
            self.assertNotIn(block, before_job, f"{filename} locks a job other than '{job}'")
            self.assertIn(block, after_job, f"{filename} must lock the '{job}' job")

    def test_forecast_runs_queue_on_their_own_group(self) -> None:
        workflow = (WORKFLOWS / "forecast.yml").read_text(encoding="utf-8")
        self.assertIn(
            "concurrency:\n"
            "  group: foxhole-forecast-cohort\n"
            "  cancel-in-progress: false\n"
            "  queue: max\n",
            workflow,
        )
        # The serialising group must not be the shared write lock, and no other
        # workflow may take it, so only forecast and replay runs contend there
        # while collection runs stay free to proceed.
        for filename in (
            "pipeline.yml",
            "archive-maintenance.yml",
            "ci.yml",
            "pages.yml",
            "watchdog.yml",
            "model-triage.yml",
            "notification-test.yml",
        ):
            self.assertNotIn(
                "foxhole-forecast-cohort",
                (WORKFLOWS / filename).read_text(encoding="utf-8"),
                f"{filename} must not take the forecast serialising group",
            )


class RecoveryLabelTests(unittest.TestCase):
    """Recovery labels must express one state, never a contradictory pair."""

    def test_recovery_report_clears_the_opposite_and_dispatch_labels(self) -> None:
        workflow = (WORKFLOWS / "forecast.yml").read_text(encoding="utf-8")
        report = workflow.split("- name: Report the exact recovered run", 1)[1].split(
            "- name: Report recovery workflow failure", 1
        )[0]
        failure = workflow.split("- name: Report recovery workflow failure", 1)[1]
        for body in (report, failure):
            self.assertIn("github.rest.issues.removeLabel", body)
            self.assertLess(
                body.index("github.rest.issues.removeLabel"),
                body.index("github.rest.issues.addLabels"),
                "stale labels must be removed before the new one is added",
            )
            self.assertIn("if (error.status !== 404) throw error;", body)
        # Both transitions clear the opposite outcome and the dispatch marker
        # that the triage workflow set.
        self.assertIn("? ['agent-recovery-failed', 'agent-recovery-dispatched']", report)
        self.assertIn(": ['agent-recovered', 'agent-recovery-dispatched'];", report)
        self.assertIn(
            "for (const stale of ['agent-recovered', 'agent-recovery-dispatched'])",
            failure,
        )


if __name__ == "__main__":
    unittest.main()
