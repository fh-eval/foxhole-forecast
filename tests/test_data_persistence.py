from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PERSIST = Path(__file__).parents[1] / ".github" / "scripts" / "persist-data.sh"


class DataPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.remote = root / "remote.git"
        self.repository = root / "work"
        self._run("git", "init", "--bare", "--initial-branch=main", os.fspath(self.remote), cwd=root)
        self._run("git", "init", "--initial-branch=main", os.fspath(self.repository), cwd=root)
        self._git("config", "user.name", "Seed User")
        self._git("config", "user.email", "seed@example.invalid")
        self._write("data/state.json", "{\"version\":1}\n")
        self._git("add", "data/state.json")
        self._git("commit", "-m", "seed")
        self._git("remote", "add", "origin", os.fspath(self.remote))
        self._git("push", "-u", "origin", "main")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run(self, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, cwd=cwd, check=True, capture_output=True, text=True)

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._run("git", *arguments, cwd=self.repository)

    def _write(self, relative_path: str, contents: str) -> None:
        path = self.repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _persist(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("bash", os.fspath(PERSIST), "data: test update"),
            cwd=self.repository,
            check=False,
            capture_output=True,
            text=True,
        )

    def _remote_file(self, relative_path: str) -> str:
        return self._run(
            "git",
            f"--git-dir={self.remote}",
            "show",
            f"main:{relative_path}",
            cwd=self.repository,
        ).stdout

    def test_data_change_is_committed_and_pushed(self) -> None:
        self._write("data/state.json", "{\"version\":2}\n")

        result = self._persist()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._remote_file("data/state.json"), "{\"version\":2}\n")

    def test_staged_source_change_blocks_push(self) -> None:
        self._write("data/state.json", "{\"version\":2}\n")
        self._write("src/unsafe.py", "unsafe = True\n")
        self._git("add", "src/unsafe.py")

        result = self._persist()

        self.assertEqual(result.returncode, 1)
        self.assertIn("src/unsafe.py", result.stderr)
        self.assertEqual(self._remote_file("data/state.json"), "{\"version\":1}\n")

    def test_no_change_exits_without_a_commit(self) -> None:
        before = self._git("rev-parse", "HEAD").stdout

        result = self._persist()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No persisted changes", result.stdout)
        self.assertEqual(self._git("rev-parse", "HEAD").stdout, before)


if __name__ == "__main__":
    unittest.main()
