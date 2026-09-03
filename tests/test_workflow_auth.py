from __future__ import annotations

from pathlib import Path
import unittest


PIPELINE = Path(__file__).parents[1] / ".github" / "workflows" / "pipeline.yml"


class WorkflowAuthenticationTests(unittest.TestCase):
    def test_collection_persistence_uses_scoped_github_app_token(self) -> None:
        workflow = PIPELINE.read_text(encoding="utf-8")
        persist_job = workflow.split("\n  persist:\n", 1)[1]

        self.assertIn("permissions:\n      contents: read", persist_job)
        self.assertNotIn("permissions:\n      contents: write", persist_job)
        self.assertIn("vars.DATA_WRITER_APP_CLIENT_ID", persist_job)
        self.assertIn("secrets.DATA_WRITER_APP_PRIVATE_KEY", persist_job)
        self.assertIn("permission-contents: write", persist_job)
        self.assertIn("token: ${{ steps.data-writer-token.outputs.token }}", persist_job)


if __name__ == "__main__":
    unittest.main()
