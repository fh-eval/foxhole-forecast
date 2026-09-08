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
        """Stand-in that performs no patching: production code stays in place."""

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

    def absent_target(target: object, attribute: object = "") -> bool:
        """True when a patch target resolves to a string matching ``substring``.

        Handles both ``patch("pkg.attr")``-style string targets and
        ``patch.object(module, "attr")``-style object targets (labelled
        ``"<module __name__>.<attribute>"``).
        """
        if isinstance(target, str):
            label = target
        elif attribute:
            name = getattr(target, "__name__", None)
            if not isinstance(name, str):
                return False
            label = f"{name}.{attribute}"
        else:
            return False
        return substring in label

    def factory(*args: object, **kwargs: object) -> object:
        target = args[0] if args else kwargs.get("target", "")
        if absent_target(target):
            return _AbsentPatch()
        return real_patch(*args, **kwargs)  # type: ignore[arg-type]

    def patch_object(*args: object, **kwargs: object) -> object:
        target = args[0] if args else kwargs.get("target")
        attribute = args[1] if len(args) > 1 else kwargs.get("attribute", "")
        if absent_target(target, attribute):
            return _AbsentPatch()
        return real_patch.object(*args, **kwargs)  # type: ignore[arg-type]

    def patch_multiple(*args: object, **kwargs: object) -> object:
        target = args[0] if args else kwargs.get("target", "")
        if absent_target(target):
            return _AbsentPatch()
        return real_patch.multiple(*args, **kwargs)  # type: ignore[arg-type]

    # mock.patch is a function whose patch-object/multiple/dict entry points
    # hang off its attributes; rebind ONLY the dispatch while keeping every
    # other entry point of the patch machinery intact, so absence mode never
    # breaks unrelated tests (patch.dict, patch.object on other targets, ...).
    factory.object = patch_object  # type: ignore[attr-defined]
    factory.multiple = patch_multiple  # type: ignore[attr-defined]
    factory.dict = real_patch.dict  # type: ignore[attr-defined]
    mock.patch = factory  # type: ignore[assignment]


def _error_kind(traceback_text: str) -> str:
    """Exception class name from a formatted unittest traceback string."""
    lines = traceback_text.strip().splitlines()
    if not lines:
        return "unknown"
    return lines[-1].split(":", 1)[0].strip() or "unknown"


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
        f"{_error_kind(traceback_text)}: {test}"
        for test, traceback_text in getattr(result, "failures", []) + getattr(result, "errors", [])
    )
    print(f"ABSENCE_TARGET={substring}")
    print(f"run={result.testsRun} failed_or_errored={len(flipped)}")
    for line in flipped:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
