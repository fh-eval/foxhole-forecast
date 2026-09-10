from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"


class WorkflowAuthenticationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
