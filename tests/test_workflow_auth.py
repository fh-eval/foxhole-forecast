from __future__ import annotations

from pathlib import Path
import unittest


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


if __name__ == "__main__":
    unittest.main()
