#!/usr/bin/env python3
"""The installed pre-commit hook must tell blocked users the CURRENT review command.

When the diff review moved from a bare `review` / `review --staged` to the `review diff`
subcommand, and later gained a required task code, the pre-commit gate's "how to fix" text
had to move with it: a user blocked by the gate is told
`run: review diff --staged --task TASK-CODE`. The OLD `review --staged` text would itself
hit the "no subcommand given" migration error, and `review diff --staged` would now fail the
missing-task gate, so this pins the hook body to the current command. Pure string assertions
on the `_PRECOMMIT` template — no global hook is installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.install import _PRECOMMIT  # noqa: E402


def test_precommit_hook_tells_user_to_run_review_diff_staged():
    assert "review diff --staged --task TASK-CODE" in _PRECOMMIT, _PRECOMMIT
    # The bare `review --staged` (no subcommand) is GONE from the hint — it would now hit
    # the "no subcommand" migration error instead of satisfying the gate.
    assert "review --staged" not in _PRECOMMIT, _PRECOMMIT


def test_precommit_hook_keeps_the_skip_escape_hatches():
    assert "REVIEW_SKIP=1" in _PRECOMMIT
    assert "--no-verify" in _PRECOMMIT


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
