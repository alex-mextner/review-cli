#!/usr/bin/env python3
"""Unit tests for moderator selection + runtime fallback (panel.py).

pick_moderators() builds the ordered candidate list (explicit, then available
MODERATOR_CANDIDATES, then panel[0]); run_moderator() walks it at run time and
falls back to the next candidate when one fails (non-zero exit OR empty output).
So a moderator that passes the cheap availability probe but dies at run time
(e.g. an Anthropic-disabled model) never leaves the panel without a synthesis.

Same harness style as tests/test_streaming.py: plain test_* functions run by the
__main__ block; backends are stubbed by reassigning panel module globals.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import panel as _panel  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402


def _result(model: str, rc: int = 0, out: str = "ok") -> ReviewResult:
    return ReviewResult(model=model, command=f"cmd {model}", returncode=rc, stdout=out, stderr="")


def _with_stubs(*, available=None, runner=None):
    """Swap panel.backend_available / panel.run_single, returning a restore fn."""
    saved_avail = _panel.backend_available
    saved_run = _panel.run_single
    if available is not None:
        _panel.backend_available = available
    if runner is not None:
        _panel.run_single = runner

    def restore():
        _panel.backend_available = saved_avail
        _panel.run_single = saved_run

    return restore


def test_explicit_is_first_then_priority_then_panel():
    restore = _with_stubs(available=lambda m: True)
    try:
        got = _panel.pick_moderators("oc:custom", ["codex", "gemini"])
        # explicit first, then MODERATOR_CANDIDATES (opus, codex, gemini), then panel[0]
        assert got[0] == "oc:custom", got
        assert "claude:claude-opus-4-8" in got
        assert "codex" in got and "gemini" in got
    finally:
        restore()


def test_opus_is_default_first_when_no_explicit():
    restore = _with_stubs(available=lambda m: True)
    try:
        got = _panel.pick_moderators(None, ["codex", "gemini"])
        assert got[0] == "claude:claude-opus-4-8", got
    finally:
        restore()


def test_unavailable_candidates_are_filtered():
    # opus binary missing -> drops out; codex/gemini remain in priority order
    restore = _with_stubs(available=lambda m: m != "claude:claude-opus-4-8")
    try:
        got = _panel.pick_moderators(None, ["oc:x"])
        assert "claude:claude-opus-4-8" not in got, got
        assert got[0] == "codex", got
    finally:
        restore()


def test_dedup_preserves_order():
    restore = _with_stubs(available=lambda m: True)
    try:
        # explicit == panel[0] == a candidate: must appear once, first
        got = _panel.pick_moderators("codex", ["codex"])
        assert got.count("codex") == 1, got
        assert got[0] == "codex", got
    finally:
        restore()


def test_run_moderator_returns_first_success_without_fallback():
    calls = []

    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        calls.append(model)
        return _result(model, rc=0, out="good")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(["claude:claude-opus-4-8", "codex"], "p", Path("."), 5)
        assert res.returncode == 0 and res.model == "claude:claude-opus-4-8"
        assert calls == ["claude:claude-opus-4-8"], calls  # never touched the fallback
    finally:
        restore()


def test_run_moderator_falls_back_on_nonzero_exit():
    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        if model == "claude:claude-opus-4-8":
            return _result(model, rc=124, out="")  # timeout-like failure (dead moderator)
        return _result(model, rc=0, out="recovered")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(["claude:claude-opus-4-8", "codex"], "p", Path("."), 5)
        assert res.returncode == 0 and res.model == "codex", res
        assert res.stdout == "recovered"
    finally:
        restore()


def test_run_moderator_falls_back_on_empty_output():
    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        if model == "codex":
            return _result(model, rc=0, out="   ")  # exit 0 but no real content
        return _result(model, rc=0, out="real answer")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(["codex", "gemini"], "p", Path("."), 5)
        assert res.model == "gemini" and res.stdout == "real answer", res
    finally:
        restore()


def test_run_moderator_returns_last_when_all_fail():
    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        return _result(model, rc=1, out="")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(["a", "b", "c"], "p", Path("."), 5)
        # all failed -> caller still gets a result (the last attempt) to surface
        assert res.model == "c" and res.returncode == 1, res
    finally:
        restore()


def test_run_moderator_all_empty_success_is_reported_as_failure():
    # every candidate exits 0 but with whitespace-only output: must NOT be
    # reported as success, or quorum/brainstorm claim a synthesis that isn't there
    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        return _result(model, rc=0, out="   ")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator(["a", "b"], "p", Path("."), 5)
        assert res.returncode != 0, res
    finally:
        restore()


def test_run_moderator_accepts_single_string():
    # a lone moderator string must be treated as one candidate, not iterated char-by-char
    calls = []

    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        calls.append(model)
        return _result(model, rc=0, out="ok")

    restore = _with_stubs(runner=runner)
    try:
        res = _panel.run_moderator("codex", "p", Path("."), 5)
        assert calls == ["codex"], calls
        assert res.model == "codex" and res.returncode == 0
    finally:
        restore()


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
    sys.exit(1 if failures else 0)
