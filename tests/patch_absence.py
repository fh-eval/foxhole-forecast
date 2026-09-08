"""Patch-absence harness: verify monkeypatch targets still reach production code.

Pattern from PR #47: tests patch package-namespace names
(``foxhole_forecast.forecasting.DATA_DIR``, ``...dashboard.DATA_DIR``/``ROOT``,
``...foxholestats.paths.DATA_DIR``, and the storage helper names re-exported on
those namespaces).  A patch that silently stops taking effect is a blocker: the
test then exercises different I/O than production while still passing.

This harness makes ``unittest.mock.patch`` a no-op for targets whose string
contains a given substring, so the patched name falls back to production code,
then runs the requested tests.  A test that still passes while its patch is
absent is NOT mediated by that patch.

Usage::

    PYTHONPATH=src python3 -m tests.patch_absence <target-substring> [test ...]

Run it only inside a scratch worktree/clone: absence removes test isolation and
may write into the checkout's ``data/`` directory.
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock


def install_absence(substring: str) -> None:
    real_patch = mock.patch

    class _AbsentPatch:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: object) -> bool:
            return False

        def __call__(self, func):
            return func

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def attribute(self, *args: object, **kwargs: object) -> "_AbsentPatch":
            return self

        def assert_not_called(self) -> None:
            pass

    def factory(*args: object, **kwargs: object) -> object:
        target = args[0] if args else kwargs.get("target", "")
        if isinstance(target, str) and substring in target:
            return _AbsentPatch()
        return real_patch(*args, **kwargs)  # type: ignore[arg-type]

    mock.patch = factory  # type: ignore[assignment]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    substring, test_names = argv[1], argv[2:]
    install_absence(substring)
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromNames(test_names) if test_names else (
        loader.discover("tests")
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    flipped = sorted(
        f"{type(error).__name__}: {test}"
        for test, error in getattr(result, "failures", []) + getattr(result, "errors", [])
    )
    print(f"ABSENCE_TARGET={substring}")
    print(f"run={result.testsRun} failed_or_errored={len(flipped)}")
    for line in flipped:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
