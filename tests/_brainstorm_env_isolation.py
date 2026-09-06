"""Shared env-isolation helper for tests that drive the REAL `mode_brainstorm`.

Root cause of the dashboard's "TOPIC: topic" / generic model-label junk (Alex,
2026-09-02): several tests in `test_brainstorm_diff.py` and `test_diff_cap.py` call
the REAL `reviewlib.modes.brainstorm.mode_brainstorm`, which writes a
`{ts}-brainstorm.md` discussion log via `log_dir()` even though `run_panel`/
`run_moderator` are stubbed. Some call sites never redirected `$REVIEW_LOG_DIR`, so
running the file directly (`python tests/test_diff_cap.py`, which the `__main__`
block below the tests supports and `tests/smoke.py`'s `run_unit` drives as a
subprocess) or under a plain `pytest` invocation that skips conftest wrote real
junk straight into the dashboard's live `~/Library/Logs/review-cli` and a matching
`~/.config/review-cli/run-stats.jsonl` record.

`conftest.py`'s autouse fixture now defaults every test to isolated paths, but that
only fires under pytest. This helper is the belt-and-suspenders half: it works for
BOTH invocation styles (no pytest fixture machinery), so a direct-script run is
covered too. `mode_brainstorm` itself never calls `stats.record_run` (only `cli.py`'s
dispatch does), so `REVIEW_STATS_FILE` isn't reachable from these particular calls
today — redirected anyway so this helper stays correct if that ever changes.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, TypeVar

_T = TypeVar("_T")

_ISOLATED_ENV_KEYS = ("REVIEW_LOG_DIR", "REVIEW_STATS_FILE")


def with_isolated_brainstorm_paths(fn: Callable[[], _T]) -> _T:
    """Run `fn` (a real `mode_brainstorm` call) with `REVIEW_LOG_DIR` and
    `REVIEW_STATS_FILE` redirected to a throwaway temp dir, restored afterward."""
    saved = {k: os.environ.get(k) for k in _ISOLATED_ENV_KEYS}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["REVIEW_LOG_DIR"] = tmp
        os.environ["REVIEW_STATS_FILE"] = str(Path(tmp) / "run-stats.jsonl")
        try:
            return fn()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
