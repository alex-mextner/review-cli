#!/usr/bin/env python3
"""Run-scoped `--effort` is honored by the qa write/exec tester (review-cli#127 harvest).

The run-scoped `--effort` flag (#150) lifts every REVIEW seat's reasoning effort, but the
qa tester — a single write/exec seat that rides `claude-p`/`codex`, NOT the review panel —
was left out: `review qa --effort xhigh` silently ignored the level. #150's own reviewers
flagged exactly this ("don't expose --effort to qa unless it is honored"). This threads the
resolved effort into both qa tester spawns using #150's shared helpers:

  * codex tester -> `-c model_reasoning_effort="<level>"` (same builder as the read-only seat)
  * claude tester -> `--effort <level>` IF the resolved binary advertises it, else nothing
  * both -> the universal `_prompt_with_effort` prompt hint (works even when the CLI has no
    flag), so the level is never a silent no-op.

Argv/prompt is asserted by capturing what the spawn would build (no live backend).
"""

from __future__ import annotations

import subprocess as sp
import sys
from pathlib import Path

# Prepend the repo root so a standalone `python3 tests/test_qa_effort.py` (the smoke.py
# subprocess runner) imports THIS checkout's reviewlib, not a stale editable install.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as backends  # noqa: E402
import reviewlib.qa.executor as ex  # noqa: E402
from reviewlib.config import parse_effort_flag  # noqa: E402


def _capture_streamed():
    captured: dict = {}

    def _fake_streamed(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return sp.CompletedProcess(args=argv, returncode=0, stdout="{}", stderr="")

    return captured, _fake_streamed


def test_codex_spawn_applies_effort_flag_and_prompt_hint():
    captured, fake = _capture_streamed()
    old_streamed, old_which = ex._run_streamed, ex._which
    ex._run_streamed = fake  # type: ignore[assignment]
    ex._which = lambda name: f"/usr/bin/{name}"  # type: ignore[assignment]
    try:
        ex._spawn_codex_writeexec("prompt", Path("/tmp/sut"), 60, effort="xhigh")
        argv = captured["argv"]
        assert "-c" in argv, argv
        assert 'model_reasoning_effort="xhigh"' in argv, argv
        # The prompt-level hint is a universal belt-and-suspenders for the level.
        assert "reasoning effort" in captured["kw"]["input_text"].lower(), captured[
            "kw"
        ]
        # No effort -> no `-c model_reasoning_effort`, prompt unchanged.
        ex._spawn_codex_writeexec("prompt", Path("/tmp/sut"), 60)
        argv = captured["argv"]
        assert not any("model_reasoning_effort" in tok for tok in argv), argv
        assert captured["kw"]["input_text"] == "prompt", captured["kw"]
    finally:
        ex._run_streamed, ex._which = old_streamed, old_which  # type: ignore[assignment]


def test_codex_spawn_effort_max_maps_to_xhigh():
    """codex has no `max` tier; #150's `_codex_reasoning_effort` maps max -> xhigh."""
    captured, fake = _capture_streamed()
    old_streamed, old_which = ex._run_streamed, ex._which
    ex._run_streamed = fake  # type: ignore[assignment]
    ex._which = lambda name: f"/usr/bin/{name}"  # type: ignore[assignment]
    try:
        ex._spawn_codex_writeexec("prompt", Path("/tmp/sut"), 60, effort="max")
        assert 'model_reasoning_effort="xhigh"' in captured["argv"], captured["argv"]
    finally:
        ex._run_streamed, ex._which = old_streamed, old_which  # type: ignore[assignment]


def _stub_claude_env():
    old_streamed, old_which = ex._run_streamed, ex._which
    old_seed, old_reap = (
        backends._ensure_workspace_trusted,
        backends._remove_workspace_trust,
    )
    backends._ensure_workspace_trusted = lambda _cwd: None  # type: ignore[assignment]
    backends._remove_workspace_trust = lambda _cwd: None  # type: ignore[assignment]
    return (old_streamed, old_which, old_seed, old_reap)


def _restore_claude_env(saved):
    old_streamed, old_which, old_seed, old_reap = saved
    ex._run_streamed, ex._which = old_streamed, old_which  # type: ignore[assignment]
    backends._ensure_workspace_trusted, backends._remove_workspace_trust = (
        old_seed,
        old_reap,
    )  # type: ignore[assignment]


def test_claude_spawn_applies_effort_flag_only_when_supported():
    captured, fake = _capture_streamed()
    saved = _stub_claude_env()
    ex._run_streamed = fake  # type: ignore[assignment]
    ex._which = lambda name: f"/usr/bin/{name}"  # type: ignore[assignment]
    old_supports = backends._claude_cli_supports_effort
    try:
        # Binary advertises --effort -> the flag is passed.
        backends._claude_cli_supports_effort = lambda _b: True  # type: ignore[assignment]
        ex._spawn_claude_writeexec(
            "prompt", Path("/tmp/review-qa-wt-x"), 60, effort="high"
        )
        argv = captured["argv"]
        assert "--effort" in argv and argv[argv.index("--effort") + 1] == "high", argv
        assert "reasoning effort" in captured["kw"]["input_text"].lower()

        # Binary does NOT advertise --effort -> no flag, but the prompt hint still carries it.
        backends._claude_cli_supports_effort = lambda _b: False  # type: ignore[assignment]
        ex._spawn_claude_writeexec(
            "prompt", Path("/tmp/review-qa-wt-x"), 60, effort="high"
        )
        argv = captured["argv"]
        assert "--effort" not in argv, argv
        assert "reasoning effort" in captured["kw"]["input_text"].lower()
    finally:
        backends._claude_cli_supports_effort = old_supports  # type: ignore[assignment]
        _restore_claude_env(saved)


def test_dispatch_tester_forwards_effort_and_fake_path_ignores_it():
    captured: dict = {}

    def _fake_codex(prompt, cwd, timeout, model=None, effort=None):
        captured["codex"] = {"prompt": prompt, "effort": effort}
        return sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    old_codex = ex._spawn_codex_writeexec
    old_fake = ex._fake_tester_enabled
    ex._spawn_codex_writeexec = _fake_codex  # type: ignore[assignment]
    ex._fake_tester_enabled = lambda: False  # type: ignore[assignment]
    try:
        ex._dispatch_tester(
            "codex", "prompt", Path("/tmp/sut"), 60, None, effort="xhigh"
        )
        # The dispatcher forwards the level to the real spawn (which owns the prompt hint +
        # CLI flag — see the spawn-level tests above).
        assert captured["codex"]["effort"] == "xhigh"
    finally:
        ex._spawn_codex_writeexec = old_codex  # type: ignore[assignment]
        ex._fake_tester_enabled = old_fake  # type: ignore[assignment]

    # The fake in-process tester takes the prompt verbatim (no effort mutation) so the
    # deterministic CI plumbing stays byte-stable.
    old_fake = ex._fake_tester_enabled
    old_fakerun = ex._fake_tester_run
    seen: dict = {}

    def _fake_run(prompt, cwd):
        seen["prompt"] = prompt
        return sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    ex._fake_tester_enabled = lambda: True  # type: ignore[assignment]
    ex._fake_tester_run = _fake_run  # type: ignore[assignment]
    try:
        ex._dispatch_tester(
            "codex", "prompt", Path("/tmp/sut"), 60, None, effort="xhigh"
        )
        assert seen["prompt"] == "prompt"
    finally:
        ex._fake_tester_enabled = old_fake  # type: ignore[assignment]
        ex._fake_tester_run = old_fakerun  # type: ignore[assignment]


def test_resolve_tester_effort_scopes_by_backend_not_pinned_model():
    """qa is single-seat: the run-scoped effort resolves against the tester BACKEND, so a
    `-m codex:gpt-5.5` pin does NOT misroute a `codex=high` override to the opencode catch-all
    (review-cli#127 review P1). None override -> None."""
    assert ex.resolve_tester_effort(None, "codex") is None

    # A bare global level lifts either backend.
    glob = parse_effort_flag(["xhigh"])
    assert ex.resolve_tester_effort(glob, "codex") == "xhigh"
    assert ex.resolve_tester_effort(glob, "claude") == "xhigh"

    # A provider-scoped level hits ONLY its backend — and is NOT dropped when a model is pinned
    # (the resolution keys on backend, never the bare `gpt-5.5` suffix that would route to oc).
    scoped = parse_effort_flag(["codex=high"])
    assert ex.resolve_tester_effort(scoped, "codex") == "high"
    assert ex.resolve_tester_effort(scoped, "claude") is None


def test_run_tester_threads_effort_into_dispatch():
    """The public `run_tester(effort=...)` seam forwards the level into `_dispatch_tester`."""
    captured: dict = {}

    def _fake_dispatch(backend, prompt, cwd, timeout, model=None, effort=None):
        captured["effort"] = effort
        return sp.CompletedProcess(
            args=[], returncode=0, stdout="## QA RESULTS\nVERDICT: PASS\n", stderr=""
        )

    old_dispatch = ex._dispatch_tester
    old_guard = ex._guard_in_place
    ex._dispatch_tester = _fake_dispatch  # type: ignore[assignment]
    ex._guard_in_place = lambda **_kw: None  # type: ignore[assignment]
    try:
        ex.run_tester(
            prompt_builder=lambda _cwd: "prompt",
            sut_path=Path("/tmp/sut"),
            timeout=60,
            in_place=True,
            backend="codex",
            effort="xhigh",
        )
        assert captured["effort"] == "xhigh"
    finally:
        ex._dispatch_tester = old_dispatch  # type: ignore[assignment]
        ex._guard_in_place = old_guard  # type: ignore[assignment]


if __name__ == "__main__":
    import sys

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
