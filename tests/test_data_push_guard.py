from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


GUARD = Path(__file__).parents[1] / ".github" / "scripts" / "assert-data-only.sh"


class DataPushGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary_directory.name)
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Test Bot")
        self._git("config", "user.email", "test@example.invalid")
        self._write("README.md", "baseline\n")
        self._git("add", "README.md")
        self._git("commit", "-m", "baseline")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *arguments),
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def _write(self, relative_path: str, contents: str) -> None:
        path = self.repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _guard(self, argument: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("bash", os.fspath(GUARD), argument),
            cwd=self.repository,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_staged_data_only_change_is_accepted(self) -> None:
        self._write("data/state.json", "{}\n")
        self._git("add", "data/state.json")

        result = self._guard("--staged")

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staged_source_change_is_rejected(self) -> None:
        self._write("src/broken.py", "undefined_name\n")
        self._git("add", "src/broken.py")

        result = self._guard("--staged")

        self.assertEqual(result.returncode, 1)
        self.assertIn("src/broken.py", result.stderr)

    def test_post_rebase_range_is_checked(self) -> None:
        baseline = self._git("rev-parse", "HEAD").stdout.strip()
        self._write("data/state.json", "{}\n")
        self._git("add", "data/state.json")
        self._git("commit", "-m", "data update")
        self.assertEqual(self._guard(baseline).returncode, 0)

        self._write("config/unsafe.json", "{}\n")
        self._git("add", "config/unsafe.json")
        self._git("commit", "-m", "unsafe update")
        result = self._guard(baseline)

        self.assertEqual(result.returncode, 1)
        self.assertIn("config/unsafe.json", result.stderr)


if __name__ == "__main__":
    unittest.main()
