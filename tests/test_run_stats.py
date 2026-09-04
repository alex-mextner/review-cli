#!/usr/bin/env python3
"""Unit tests for the run-stats store + startup ETA (reviewlib.stats) and its CLI
wiring, plus the no-timeout advertising warning (reviewlib.install).

Same harness style as tests/test_moderator.py: plain test_* functions run by the
__main__ block; backends are stubbed by reassigning module globals so NO live model
call is ever made. The stats store is redirected to a temp file via $REVIEW_STATS_FILE
(and the per-call log dir via $REVIEW_LOG_DIR) so the real ~/.config store is never
touched.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Some tests drive the real board dispatch (mode_review -> panel). Redirect the
# provider-failover last-working cache to a throwaway file (never touch the real
# ~/.cache/review-cli), and neutralise provider-failover to an identity chain: these tests
# exercise stats/ETA + reserve-replace degrade, NOT the seat-level provider switchover
# (which is tested in tests/test_provider_failover.py). Without this a chained seat (glm)
# would fall over to another provider instead of failing, breaking the degrade scenario.
os.environ.setdefault(
    "REVIEW_PROVIDER_CACHE", str(Path(tempfile.mkdtemp()) / "last-provider.json")
)
# SCOPED per-test (autouse fixture / __main__ wrapper), NOT module-level — a module-level
# patch poisons other suites at collection time. See tests/_failover_neutralise.py.
from _failover_neutralise import identity_provider_chain  # noqa: E402

from reviewlib import cli as _cli  # noqa: E402
from reviewlib import panel as _panel  # noqa: E402
from reviewlib import stats as _stats  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.install import SKILL_BLURB, SKILL_MD  # noqa: E402

TASK = "HYP-742"
TASK_ARGS = ["--task", TASK]

try:
    import pytest  # noqa: E402

    @pytest.fixture(autouse=True)
    def _neutralise_provider_failover():
        """Seat-level provider-failover neutralised to an identity chain per-test and
        RESTORED afterwards, so it never leaks to other suites."""
        with identity_provider_chain():
            yield
except ImportError:  # plain-script harness applies it in __main__ instead
    pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _TmpStore:
    """Context manager: point the stats store at a fresh temp file for the test."""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "run-stats.jsonl"
        self._saved = os.environ.get("REVIEW_STATS_FILE")
        os.environ["REVIEW_STATS_FILE"] = str(self.path)
        return self

    def __exit__(self, *exc):
        if self._saved is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = self._saved
        self._dir.cleanup()
        return False

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(ln) for ln in self.path.read_text().splitlines() if ln.strip()
        ]


def _stub_resolve_backend(rc_by_model: dict[str, int] | int = 0):
    """Return a resolve_backend stub that yields a canned ReviewResult per model.

    rc_by_model: an int (same exit code for all) or a {model: rc} map (default 0).
    The returned backend callable matches resolve_backend(model)(model, prompt, diff,
    cwd, timeout) so it drops into panel.run_panel unchanged.
    """

    def resolver(model: str):
        # round_no is the 6th arg panel.run_panel threads through (added with the
        # round-aware board/brainstorm path). Default it so the stub matches BOTH the
        # 5-arg `-m`/quorum callers and the 6-arg panel caller — otherwise a 6-arg call
        # raises TypeError, run_panel catches it, and the success path silently tallies
        # as a failure (codex: success path left under-tested).
        def backend(m, prompt, diff, cwd, timeout, round_no=0, effort=None):
            rc = rc_by_model if isinstance(rc_by_model, int) else rc_by_model.get(m, 0)
            return ReviewResult(
                model=m,
                command=f"stub {m}",
                returncode=rc,
                stdout=f"output from {m}",
                stderr="",
            )

        return backend

    return resolver


def _with_backend_stub(resolver):
    """Swap resolve_backend in BOTH namespaces that dispatch backends, AND force every seat
    available/paid.

    panel.run_panel (just-ask/quorum/brainstorm/board) and the plain `-m` path in
    modes.review each import resolve_backend into their own module namespace, so a
    stub must replace both. It ALSO stubs `backend_available` -> True in all three probe
    namespaces (backends / panel / modes.review): with the dispatch mocked these tests
    exercise stats/ETA wiring, not liveness, and the pre-dispatch pool-selection guard
    (reviewlib.pool_guard) must not bail on a host lacking these backends' keys/CLIs.

    It ALSO stubs `runtime_provider_marked_unpaid` -> False in `backends` + `modes.review`
    (mirrors the `backend_available` stubbing above): the pool guard's liveness probe
    (`provider_failover.any_provider_available`) and the flat `-m` path's provider-failover
    cascade both consult the REAL unpaid state alongside `backend_available`, so a host
    whose `~/.config/review-cli/config.yaml` marks e.g. `gemini` unpaid would otherwise leak
    that into these dispatch-mocked tests and spuriously trip the guard (the same leak class
    tests/conftest.py's autouse fixture exists to contain). Returns a restore fn.
    """
    from reviewlib import backends as _backends
    from reviewlib.modes import review as _review_mode

    saved_panel = _panel.resolve_backend
    saved_review = _review_mode.resolve_backend
    saved_avail_b = _backends.backend_available
    saved_avail_p = _panel.backend_available
    saved_avail_r = _review_mode.backend_available
    saved_unpaid_b = _backends.runtime_provider_marked_unpaid
    saved_unpaid_r = _review_mode.runtime_provider_marked_unpaid
    _panel.resolve_backend = resolver
    _review_mode.resolve_backend = resolver
    _backends.backend_available = lambda _m: True
    _panel.backend_available = lambda _m: True
    _review_mode.backend_available = lambda _m: True
    _backends.runtime_provider_marked_unpaid = lambda _m: False
    _review_mode.runtime_provider_marked_unpaid = lambda _m: False

    def restore():
        _panel.resolve_backend = saved_panel
        _review_mode.resolve_backend = saved_review
        _backends.backend_available = saved_avail_b
        _panel.backend_available = saved_avail_p
        _review_mode.backend_available = saved_avail_r
        _backends.runtime_provider_marked_unpaid = saved_unpaid_b
        _review_mode.runtime_provider_marked_unpaid = saved_unpaid_r

    return restore


# ---------------------------------------------------------------------------
# fmt_duration
# ---------------------------------------------------------------------------
def test_fmt_duration_shapes():
    assert _stats.fmt_duration(0) == "0s"
    assert _stats.fmt_duration(47) == "47s"
    assert _stats.fmt_duration(372) == "6m12s"
    assert _stats.fmt_duration(3700) == "1h01m"
    assert _stats.fmt_duration(-5) == "0s"  # clamped


# ---------------------------------------------------------------------------
# record_run shape
# ---------------------------------------------------------------------------
def test_record_run_writes_correct_shape():
    with _TmpStore() as store:
        ok = _stats.record_run(
            task_code=TASK,
            mode="brainstorm",
            models=["codex", "gemini", "claude"],
            duration_seconds=372.5,
            ok_count=9,
            fail_count=1,
        )
        assert ok is True
        recs = store.records()
        assert len(recs) == 1, recs
        r = recs[0]
        assert r["v"] == _stats.STATS_VERSION
        assert r["task_code"] == TASK
        assert r["mode"] == "brainstorm"
        assert r["pool_size"] == 3
        assert r["models"] == ["codex", "gemini", "claude"]
        assert r["duration_seconds"] == 372.5
        assert r["ok_count"] == 9 and r["fail_count"] == 1
        assert "ts" in r
        # NO secrets/keys/prompts — model names only.
        blob = json.dumps(r)
        assert "prompt" not in blob and "api" not in blob.lower()
        # No `passed=` kwarg given -> verdict UNKNOWN -> the key is omitted entirely
        # (not written as null), so a reader can tell "never told us" from "told us False".
        assert "passed" not in r


def test_record_run_passed_true_and_false_are_persisted():
    with _TmpStore() as store:
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            duration_seconds=1.0,
            ok_count=0,
            fail_count=1,
            passed=False,
        )
        recs = store.records()
        assert recs[0]["passed"] is True
        assert recs[1]["passed"] is False


def test_task_summaries_group_iterations_and_models():
    with _TmpStore():
        _stats.record_run(
            task_code="HYP-742",
            mode="review",
            models=["codex"],
            duration_seconds=10,
            ok_count=1,
            fail_count=0,
        )
        _stats.record_run(
            task_code="HYP-742",
            mode="quorum",
            models=["codex", "gemini"],
            duration_seconds=20,
            ok_count=2,
            fail_count=0,
        )
        _stats.record_run(
            task_code="HYP-999",
            mode="review",
            models=["claude"],
            duration_seconds=30,
            ok_count=1,
            fail_count=0,
        )
        summaries = _stats.task_summaries()
        by_code = {s["task_code"]: s for s in summaries}
        assert by_code["HYP-742"]["iterations"] == 2
        assert by_code["HYP-742"]["models"] == ["codex", "gemini"]
        assert by_code["HYP-742"]["modes"] == ["quorum", "review"]
        iterations = _stats.iterations_for_task("HYP-742")
        assert [it["iteration"] for it in iterations] == [1, 2]
        assert iterations[1]["models"] == ["codex", "gemini"]


def test_cli_task_list_json_includes_multiple_tasks():
    with _TmpStore():
        _stats.record_run(
            task_code="HYP-742",
            mode="review",
            models=["codex"],
            duration_seconds=10,
            ok_count=1,
            fail_count=0,
        )
        _stats.record_run(
            task_code="HYP-999",
            mode="quorum",
            models=["gemini"],
            duration_seconds=20,
            ok_count=1,
            fail_count=0,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", "--json"])
        assert rc == 0, rc
        payload = json.loads(out.getvalue())
        by_code = {item["task_code"]: item for item in payload["tasks"]}
        assert set(by_code) == {"HYP-742", "HYP-999"}
        assert by_code["HYP-742"]["models"] == ["codex"]
        assert by_code["HYP-999"]["modes"] == ["quorum"]


def test_cli_task_subcommand_rejects_global_task_flag():
    err = io.StringIO()
    with redirect_stderr(err), _capture_stdout():
        rc = _cli.main(["task", "--task", "HYP-742"])
    assert rc == 2, rc
    assert "review task CODE" in err.getvalue()
    assert "--task is for recorded review modes" in err.getvalue()


def test_record_run_file_is_0600():
    with _TmpStore() as store:
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
        )
        mode = store.path.stat().st_mode & 0o777
        assert mode == 0o600, oct(mode)


def test_record_run_tightens_preexisting_permissive_file():
    """A run-stats file that already exists world/group-readable must be tightened to
    0600 on the next write — O_CREAT's mode only applies on creation, so without an
    explicit fchmod a pre-existing 0644 file would keep leaking (codex P2)."""
    with _TmpStore() as store:
        store.path.write_text("")  # pre-create
        os.chmod(store.path, 0o644)
        assert store.path.stat().st_mode & 0o777 == 0o644
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
        )
        assert store.path.stat().st_mode & 0o777 == 0o600, oct(
            store.path.stat().st_mode & 0o777
        )
        assert len(store.records()) == 1


def test_record_run_appends_not_truncates():
    with _TmpStore() as store:
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["a"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["a", "b"],
            duration_seconds=2.0,
            ok_count=2,
            fail_count=0,
        )
        assert len(store.records()) == 2


# ---------------------------------------------------------------------------
# estimate_eta + eta_line: (mode,pool) primary, pool-only, no-history
# ---------------------------------------------------------------------------
def test_eta_mode_plus_pool_average():
    with _TmpStore():
        for secs in (360, 380, 400):
            _stats.record_run(
                mode="brainstorm",
                models=["a", "b", "c", "d"],
                duration_seconds=secs,
                ok_count=4,
                fail_count=0,
            )
        eta = _stats.estimate_eta("brainstorm", 4)
        assert eta is not None
        assert eta["basis"] == "mode+pool"
        assert eta["samples"] == 3
        assert abs(eta["avg_seconds"] - 380.0) < 0.01
        line = _stats.eta_line("brainstorm", 4)
        assert "pool=4 (brainstorm)" in line
        assert "~6m20s" in line
        assert "3 past runs of this size" in line
        assert "do NOT timeout" in line


def test_eta_falls_back_to_pool_only():
    with _TmpStore():
        # Only review/4 exists; ask for quorum/4 -> pool-only fallback (any mode).
        for secs in (120, 180):
            _stats.record_run(
                mode="review",
                models=["a", "b", "c", "d"],
                duration_seconds=secs,
                ok_count=4,
                fail_count=0,
            )
        eta = _stats.estimate_eta("quorum", 4)
        assert eta is not None and eta["basis"] == "pool"
        assert eta["samples"] == 2
        line = _stats.eta_line("quorum", 4)
        assert "pool=4 (quorum)" in line
        assert "any mode" in line
        assert "do NOT timeout" in line


def test_eta_no_history_fallback():
    with _TmpStore():
        # Empty store -> no history line, still warns it's multi-round/minutes.
        eta = _stats.estimate_eta("review", 4)
        assert eta is None
        line = _stats.eta_line("review", 4)
        assert "no history yet" in line
        assert "expect MINUTES" in line
        assert "Do NOT timeout" in line


def test_eta_unreadable_store_does_not_raise():
    saved = os.environ.get("REVIEW_STATS_FILE")
    # Point at a path under a non-existent dir tree that read_text() will fail on.
    os.environ["REVIEW_STATS_FILE"] = "/nonexistent-xyz/deeper/run-stats.jsonl"
    try:
        line = _stats.eta_line("brainstorm", 5)
        assert "no history yet" in line  # graceful: behaves like empty
    finally:
        if saved is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = saved


def test_stats_never_raise_on_unexpandable_path():
    """An unexpandable $REVIEW_STATS_FILE (~nosuchuser) makes stats_path() raise
    RuntimeError. record_run is called from a CLI finally, so it (and the ETA helpers)
    must swallow that and behave as best-effort — never crash a finished run (codex P2)."""
    saved = os.environ.get("REVIEW_STATS_FILE")
    os.environ["REVIEW_STATS_FILE"] = "~nosuchuser-zzz/run-stats.jsonl"
    try:
        # record_run must return False, not raise.
        assert (
            _stats.record_run(
                mode="review",
                models=["a"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
            )
            is False
        )
        # estimate_eta / eta_line / announce_eta must degrade to "no history", not raise.
        assert _stats.estimate_eta("review", 1) is None
        assert "no history yet" in _stats.eta_line("review", 1)
        _stats.announce_eta("review", 1, stream=io.StringIO())  # must not raise
    finally:
        if saved is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = saved


def test_load_records_skips_junk_lines():
    with _TmpStore() as store:
        store.path.write_text(
            '{"v":1,"mode":"review","pool_size":2,"duration_seconds":10}\n'
            "not json at all\n"
            "{ broken json\n"
            '{"v":1,"mode":"review","pool_size":2,"duration_seconds":20}\n',
            encoding="utf-8",
        )
        eta = _stats.estimate_eta("review", 2)
        assert eta is not None and eta["samples"] == 2
        assert abs(eta["avg_seconds"] - 15.0) < 0.01


# ---------------------------------------------------------------------------
# CLI wiring: a real run records a stat record + prints an ETA line (mocked backends)
# ---------------------------------------------------------------------------
def test_cli_diff_requires_task_code_before_dispatch():
    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        old_task = os.environ.pop("REVIEW_TASK_CODE", None)
        os.environ["REVIEW_LOG_DIR"] = log
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(["diff", "-C", d.name, "-m", "codex,gemini"])
            assert rc == 2, rc
            assert "--task CODE" in err.getvalue()
            assert store.records() == []
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            if old_task is not None:
                os.environ["REVIEW_TASK_CODE"] = old_task
            d.cleanup()


def test_cli_visual_diff_requires_task_code_before_dispatch():
    with _TmpStore() as store:
        old_task = os.environ.pop("REVIEW_TASK_CODE", None)
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(["visual", "shot.png", "--diff", "-C", str(REPO_ROOT)])
            assert rc == 2, rc
            assert "--task CODE" in err.getvalue()
            assert store.records() == []
        finally:
            if old_task is not None:
                os.environ["REVIEW_TASK_CODE"] = old_task


def test_cli_visual_piped_diff_requires_task_code_before_dispatch():
    with _TmpStore() as store:
        old_task = os.environ.pop("REVIEW_TASK_CODE", None)
        old_stdin = _cli._read_stdin_if_piped
        _cli._read_stdin_if_piped = lambda: "diff --git a/x b/x\n+change\n"
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(["visual", "shot.png", "--no-ai", "-C", str(REPO_ROOT)])
            assert rc == 2, rc
            assert "--task CODE" in err.getvalue()
            assert store.records() == []
        finally:
            _cli._read_stdin_if_piped = old_stdin
            if old_task is not None:
                os.environ["REVIEW_TASK_CODE"] = old_task
            else:
                os.environ.pop("REVIEW_TASK_CODE", None)


def test_cli_invalid_task_code_fails_before_dispatch():
    old_task = os.environ.pop("REVIEW_TASK_CODE", None)
    old_stdin = _cli._read_stdin_if_piped
    _cli._read_stdin_if_piped = lambda: None
    try:
        for bad in ("multi word", "x" * 121, "bad\ncode"):
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(
                    ["diff", "--task", bad, "-C", str(REPO_ROOT), "-m", "codex"]
                )
            assert rc == 2, (bad, rc)
            assert "invalid --task CODE" in err.getvalue(), (bad, err.getvalue())
    finally:
        _cli._read_stdin_if_piped = old_stdin
        if old_task is not None:
            os.environ["REVIEW_TASK_CODE"] = old_task
        else:
            os.environ.pop("REVIEW_TASK_CODE", None)


def _git_init_with_diff() -> tempfile.TemporaryDirectory:
    import subprocess

    d = tempfile.TemporaryDirectory()
    repo = Path(d.name)
    # Neutralize any global review pre-commit gate (core.hooksPath on this machine)
    # AND set REVIEW_SKIP so the test's own commit is never blocked by it.
    env = {**os.environ, "REVIEW_SKIP": "1"}
    common = dict(cwd=str(repo), check=True, env=env)
    subprocess.run(["git", "init", "-q"], **common)
    subprocess.run(["git", "config", "core.hooksPath", "/dev/null"], **common)
    subprocess.run(["git", "config", "user.email", "t@t"], **common)
    subprocess.run(["git", "config", "user.name", "t"], **common)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], **common)
    (repo / "f.txt").write_text("base\n")
    subprocess.run(["git", "add", "-A"], **common)
    subprocess.run(["git", "commit", "-qm", "init", "--no-verify"], **common)
    (repo / "f.txt").write_text("base\nchanged line\n")  # an unstaged diff to review
    return d


def test_cli_review_run_records_stat_and_announces_eta():
    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(["diff", *TASK_ARGS, "-C", d.name, "-m", "codex,gemini"])
            assert rc == 0, rc
            # ETA line went to stderr.
            assert "[review] pool=2 (review)" in err.getvalue()
            assert (
                "do NOT timeout" in err.getvalue() or "Do NOT timeout" in err.getvalue()
            )
            # Exactly one stat record with the real mode + pool + per-call counts.
            recs = store.records()
            assert len(recs) == 1, recs
            r = recs[0]
            assert r["task_code"] == TASK
            assert r["mode"] == "review"
            assert r["pool_size"] == 2
            assert sorted(r["models"]) == ["codex", "gemini"]
            assert r["ok_count"] == 2 and r["fail_count"] == 0
            # A CLI run that exits 0 must record passed=True -- the mode handler's
            # own exit code IS the verdict signal, threaded through end to end.
            assert r["passed"] is True
            # Real wall-clock duration (monotonic-timed), not a fixture proxy: a tiny
            # mocked run is sub-second but must be a real, non-negative number.
            assert isinstance(r["duration_seconds"], (int, float))
            assert r["duration_seconds"] >= 0.0
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_cli_task_flag_overrides_review_task_code_env():
    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        old_task = os.environ.get("REVIEW_TASK_CODE")
        os.environ["REVIEW_LOG_DIR"] = log
        os.environ["REVIEW_TASK_CODE"] = "ENV-999"
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(["diff", "--task", TASK, "-C", d.name, "-m", "codex"])
            assert rc == 0, rc
            recs = store.records()
            assert len(recs) == 1, recs
            assert recs[0]["task_code"] == TASK
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            if old_task is None:
                os.environ.pop("REVIEW_TASK_CODE", None)
            else:
                os.environ["REVIEW_TASK_CODE"] = old_task
            d.cleanup()


def test_read_stdin_if_piped_treats_unreadable_capture_as_no_input():
    class _Unreadable:
        def isatty(self):
            return False

        def read(self):
            raise OSError("pytest capture blocks stdin")

    old_stdin = sys.stdin
    try:
        sys.stdin = _Unreadable()
        assert _cli._read_stdin_if_piped() is None
    finally:
        sys.stdin = old_stdin


def test_cli_standalone_visual_does_not_record_review_task_code_env():
    if not shutil.which("magick"):
        _skip(
            "standalone `review visual` drives the real cvGate, which hard-requires "
            "ImageMagick v7's `magick` binary (absent on this host) — same gate as "
            "test_visual_verification_suite in smoke.py."
        )
    with _TmpStore() as store:
        tests_dir = str(REPO_ROOT / "tests")
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        import visual_fixtures as vf  # noqa: PLC0415

        image = vf.styled_render(Path(tempfile.mkdtemp()) / "styled.png")
        old_task = os.environ.get("REVIEW_TASK_CODE")
        old_stdin = _cli._read_stdin_if_piped
        _cli._read_stdin_if_piped = lambda: None
        os.environ["REVIEW_TASK_CODE"] = "ENV-999"
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(["visual", str(image), "--no-ai", "-C", str(REPO_ROOT)])
            assert rc == 0, rc
            assert store.records() == []
        finally:
            _cli._read_stdin_if_piped = old_stdin
            if old_task is None:
                os.environ.pop("REVIEW_TASK_CODE", None)
            else:
                os.environ["REVIEW_TASK_CODE"] = old_task


def _run_board_review_and_get_record(extra_argv: list[str]) -> dict:
    """Run a default-preset board `review` with `extra_argv` appended,
    returning the single run-stats record + the captured stderr under key "_stderr".
    The board is pinned to the preset boards and config to {} so the test is independent of
    the dev machine's config.yaml; backends are stubbed (no model call)."""
    from reviewlib import backends as _backends
    from reviewlib.config import (
        DEFAULT_BOARD,
        DEFAULT_PRESET_BOARD,
        HEAVY_PRESET_BOARD,
        LIGHT_PRESET_BOARD,
    )
    from reviewlib.modes import review as _review_mode

    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        # Force EVERY seat available in all three namespaces that probe it: the CLI
        # (planned-pool ETA slice), panel.build_board_jobs, and the failover pool's
        # startup split inside modes.review (which imports backend_available into its
        # own namespace, like resolve_backend). All seats available -> the top-4
        # priority pool fills cleanly with no startup/mid-run failover.
        saved_avail_b = _backends.backend_available
        saved_avail_p = _panel.backend_available
        saved_avail_r = _review_mode.backend_available
        _backends.backend_available = lambda _m: True
        _panel.backend_available = lambda _m: True
        _review_mode.backend_available = lambda _m: True
        saved_cfg = _cli.load_config
        saved_lb = _cli.load_board
        _cli.load_config = lambda: {}

        def _load_board(_cfg, **kw):
            preset = kw.get("preset")
            if preset == "default":
                return list(DEFAULT_PRESET_BOARD)
            if preset == "heavy":
                return list(HEAVY_PRESET_BOARD)
            if preset == "light":
                return list(LIGHT_PRESET_BOARD)
            return list(DEFAULT_BOARD)

        _cli.load_board = _load_board
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        # Isolate the cross-invocation seat-cooldown store from this machine's REAL
        # cooldown state -- without this, a seat that's genuinely cooling down from
        # actual concurrent `review` usage elsewhere on the box silently changes which
        # seats this "hermetic" test picks, especially now that the default pool is 2
        # (light preset, Alex 2026-08-28) and there's no reserve slack to absorb it.
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(log) / "seat-cooldown.json")
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(["diff", *TASK_ARGS, "-C", d.name, *extra_argv])
            assert rc == 0, rc
            recs = store.records()
            assert len(recs) == 1, recs
            r = dict(recs[0])
            r["_stderr"] = err.getvalue()
            return r
        finally:
            restore()
            _backends.backend_available = saved_avail_b
            _panel.backend_available = saved_avail_p
            _review_mode.backend_available = saved_avail_r
            _cli.load_config = saved_cfg
            _cli.load_board = saved_lb
            os.environ.pop("REVIEW_LOG_DIR", None)
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file
            d.cleanup()


def test_cli_default_board_run_records_preset_pool_size():
    """A default `review` (no -m, no config models) runs the board sized to the default
    (light, Alex 2026-08-28) preset's pool (2 seats); the run-stats record must report
    pool_size == 2, NOT the full board (the slice must feed run-stats, not the pre-slice
    board)."""
    r = _run_board_review_and_get_record([])  # default preset (light) pool = 2
    assert r["mode"] == "review"
    assert r["pool_size"] == 2, r  # the sliced board, not the full preset
    assert len(r["models"]) == 2, r
    assert "[review] pool=2 (review)" in r["_stderr"]


def test_cli_default_board_run_records_roles_review_cli_221():
    """End-to-end coverage of `_ran_roles`'s `outcome_sink` branch -- the DEFAULT,
    non-exact board dispatch path (Fable round-2 review finding: this closure,
    reading `outcome_sink[0].usable_roles`, had no coverage distinct from the
    `_run_mode_with_stats(roles_after=lambda: ...)` unit-level test).

    Checks role COUNT and VALIDITY, not which specific 2 seats answered: with the
    light preset's zero-slack pool of 2 (Alex, 2026-08-28), a real seat-selection
    signal beyond this test's own stubbing (e.g. live usage-limit-aware reuse) can
    legitimately pick a different pair of seats than raw board order -- see
    tests/test_run_stats.py::test_cli_failover_backfill_records_actual_models_not_planned
    and rig-cli TaskList #80 for the same pre-existing, already-tracked sensitivity."""
    from reviewlib.config import REVIEW_ROLES

    r = _run_board_review_and_get_record([])  # default preset (light) pool = 2
    assert len(r["roles"]) == 2, r
    assert set(r["roles"]) <= set(REVIEW_ROLES), r
    assert len(set(r["roles"])) == 2, r  # 2 DISTINCT roles, not the same role twice


def test_cli_default_board_roles_actually_satisfy_min_roles_review_cli_221():
    """Round-7 review finding (Opus): closes the loop between the real board-
    dispatch record shape (the test above) and `quorum_check`'s counting --
    every OTHER `--min-roles` test hand-writes `record_run(roles=[...])`
    directly, so nothing previously proved the ACTUAL role strings a real
    default-board dispatch produces are valid `REVIEW_ROLES` keys that
    `_distinct_roles` wouldn't silently filter out (a silent no-op on the most
    common path, invisible to the rest of the suite)."""
    dispatched = _run_board_review_and_get_record([])  # default preset (light) pool = 2
    real_roles = dispatched["roles"]
    assert len(real_roles) == 2, dispatched

    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=dispatched["models"],
            roles=real_roles,
            duration_seconds=1.0,
            ok_count=2,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1, min_roles=2)
        assert result["passed"] is True, result
        assert result["distinct_roles_passed"] == 2


def test_cli_board_run_records_explicit_pool_size():
    """`--pool 2` must record pool_size == 2 (not 4, not 9): the slice feeds run-stats
    at arbitrary sizes, not just the default (GLM finding 23)."""
    r = _run_board_review_and_get_record(["--pool", "2"])
    assert r["mode"] == "review"
    assert r["pool_size"] == 2, r
    assert len(r["models"]) == 2, r
    assert "[review] pool=2 (review)" in r["_stderr"]


def _run_board_review_with_resolver(extra_argv: list[str], resolver) -> dict:
    """Like _run_board_review_and_get_record but with a custom resolve_backend stub (so a
    seat can be made to FAIL and trigger failover). All seats are env-available, so the
    startup pool fills cleanly and the mid-run failover does the backfilling. Returns the
    single run-stats record + captured stderr under "_stderr"; tolerates exit 1 (the
    degraded path)."""
    from reviewlib import backends as _backends
    from reviewlib.config import (
        DEFAULT_BOARD,
        DEFAULT_PRESET_BOARD,
        HEAVY_PRESET_BOARD,
        LIGHT_PRESET_BOARD,
    )
    from reviewlib.modes import review as _review_mode

    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(resolver)
        saved_avail_b = _backends.backend_available
        saved_avail_p = _panel.backend_available
        saved_avail_r = _review_mode.backend_available
        _backends.backend_available = lambda _m: True
        _panel.backend_available = lambda _m: True
        _review_mode.backend_available = lambda _m: True
        saved_cfg = _cli.load_config
        saved_lb = _cli.load_board
        _cli.load_config = lambda: {}

        def _load_board(_cfg, **kw):
            preset = kw.get("preset")
            if preset == "default":
                return list(DEFAULT_PRESET_BOARD)
            if preset == "heavy":
                return list(HEAVY_PRESET_BOARD)
            if preset == "light":
                return list(LIGHT_PRESET_BOARD)
            return list(DEFAULT_BOARD)

        _cli.load_board = _load_board
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        # Isolate the cross-invocation seat-cooldown store from this machine's REAL
        # cooldown state -- see the matching comment in _run_board_review_and_get_record.
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(log) / "seat-cooldown.json")
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(["diff", *TASK_ARGS, "-C", d.name, *extra_argv])
            recs = store.records()
            assert len(recs) == 1, recs
            r = dict(recs[0])
            r["_stderr"] = err.getvalue()
            r["_rc"] = rc
            return r
        finally:
            restore()
            _backends.backend_available = saved_avail_b
            _panel.backend_available = saved_avail_p
            _review_mode.backend_available = saved_avail_r
            _cli.load_config = saved_cfg
            _cli.load_board = saved_lb
            os.environ.pop("REVIEW_LOG_DIR", None)
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file
            d.cleanup()


def test_cli_failover_backfill_records_actual_models_not_planned():
    """When a startup-pool seat FAILS mid-run, the CLI must record the models that
    ACTUALLY produced verdicts (a backfilled reserve under its real id), not the planned
    pool. review-cli#286: Fable is demoted to last-resort in DEFAULT_BOARD and excluded
    from HEAVY_PRESET_BOARD entirely, so heavy pool 4 is now [Sol, Opus, GLM-cc, Kimi];
    the top seat (Sol) fails, Codex (the next reserve seat) backfills, and the record
    excludes Sol while keeping pool_size 4."""
    # Sol (priority #1 in the heavy pool of 4, now that Fable is excluded) fails;
    # everything else succeeds.
    resolver = _stub_resolve_backend({"codex:gpt-5.6-sol": 1})
    r = _run_board_review_with_resolver(["--preset", "heavy"], resolver)
    assert r["_rc"] == 0, r
    assert r["mode"] == "review"
    assert r["pool_size"] == 4, r  # backfilled back up to 4
    # Full set, not individual `in` checks: Kimi is now a PLANNED pool seat (heavy pool 4
    # is Sol/Opus/GLM-cc/Kimi post-exclusion), so a regression where Kimi also silently
    # failed and the NEXT reserve backfilled instead would pass a partial `in`-only check.
    assert set(r["models"]) == {
        "claude:claude-opus-4-8",
        "commandcode:zai-org/GLM-5.2",
        "oc:commandcode/moonshotai/Kimi-K2.7-Code",
        "codex",  # the promoted reserve (#5, post-exclusion), recorded by its real id
    }, r
    assert "claude:claude-fable-5" not in r["models"], r
    assert "codex:gpt-5.6-sol" not in r["models"], r  # the failed seat, not backfilled
    assert "[review] pool=4 (review)" in r["_stderr"]  # ETA still keys on the planned 4
    assert "promoting reserve" in r["_stderr"]  # failover actually fired


def test_cli_failover_exhausted_reserve_degrades_exit_1():
    """When the reserve can't refill the pool (every commandcode + everything but one
    fail), the run degrades: exit 1, a degraded message on stderr, and the record holds
    only the seats that produced verdicts. Bare invocation targets the light preset's
    pool of 2 (Alex, 2026-08-28), so only ONE usable seat is needed to fall short."""
    # Fail everything EXCEPT opus -> only 1 usable, reserve can't reach the pool of 2.
    from reviewlib.config import DEFAULT_BOARD

    ok = {"claude:claude-opus-4-8"}
    resolver = _stub_resolve_backend(
        {r.model: (0 if r.model in ok else 1) for r in DEFAULT_BOARD}
    )
    r = _run_board_review_with_resolver([], resolver)
    assert r["_rc"] == 1, r
    assert "degraded" in r["_stderr"], r["_stderr"]
    assert set(r["models"]) == ok, r  # only the seats that produced verdicts
    assert r["pool_size"] == 1, r


def test_cli_exact_board_records_all_explicit_attempted_models_on_partial_failure():
    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend({"codex": 0, "gemini": 1}))
        saved_cfg = _cli.load_config
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        _cli.load_config = lambda: {
            "models": ["codex", "gemini"],
            "board": [
                {"model": "codex", "role": "correctness", "name": "Codex"},
                {"model": "gemini", "role": "contracts", "name": "Gemini"},
            ],
        }
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(
                    [
                        "diff",
                        *TASK_ARGS,
                        "-C",
                        d.name,
                        "-m",
                        "codex",
                        "-m",
                        "gemini",
                    ]
                )
            assert rc == 1, rc
            r = store.records()[0]
            assert r["models"] == ["codex", "gemini"], r
            assert r["pool_size"] == 2, r
            assert r["ok_count"] == 1 and r["fail_count"] == 1, r
        finally:
            restore()
            _cli.load_config = saved_cfg
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_cli_exact_board_failed_seat_roles_never_satisfy_min_roles_review_cli_221():
    """The invariant `_ran_roles`'s `explicit_models` branch relies on (round-2
    review finding, Opus/Fable, independently, both marked 'blocking' pending this
    exact confirmation): an exact board with ANY failed seat can never record
    `passed=True` (there is no reserve to backfill it — `run_board_with_failover`
    degrades), so "return every planned seat's role regardless of pass/fail" (the
    same pre-existing shortcut `_ran_models` already took) can never leak a FAILED
    seat's role into a record that actually counts toward --min-roles."""
    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend({"codex": 0, "gemini": 1}))
        saved_cfg = _cli.load_config
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        _cli.load_config = lambda: {
            "models": ["codex", "gemini"],
            "board": [
                {"model": "codex", "role": "correctness", "name": "Codex"},
                {"model": "gemini", "role": "contracts", "name": "Gemini"},
            ],
        }
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(
                    ["diff", *TASK_ARGS, "-C", d.name, "-m", "codex", "-m", "gemini"]
                )
            assert rc == 1, rc  # gemini failed -> the whole exact-board run degrades
            rec = store.records()[0]
            assert rec.get("passed") is False
            # `_ran_roles` DOES still record both roles onto the iteration (mirrors
            # `_ran_models`'s identical, already-tested choice above) --
            assert rec["roles"] == ["correctness", "contracts"]
            # Round-10 review finding (Opus): pairwise, not just two independently-
            # correct lists -- `_ran_models`/`_ran_roles` build both from the SAME
            # `board` list in the SAME comprehension order, so each model lands
            # with the role IT was actually configured under, not a neighbor's.
            assert list(zip(rec["models"], rec["roles"])) == [
                ("codex", "correctness"),
                ("gemini", "contracts"),
            ]
            # -- but because `passed` is False, `quorum_check`'s `passed is True`
            # filter excludes this iteration entirely: NEITHER role counts, including
            # "correctness" from the seat that actually succeeded.
            result = _stats.quorum_check(TASK, min_iter=1, min_models=1, min_roles=1)
            assert result["passed"] is False
            assert result["roles"] == []
        finally:
            restore()
            _cli.load_config = saved_cfg
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_cli_exact_board_unavailable_sentinel_seat_never_satisfies_min_roles_review_cli_221():
    """Round-4 review finding (Opus, marked 'blocking pending one trace'): the
    failed-seat invariant above must ALSO hold for a seat that returns exit 0
    with an UNAVAILABLE-SENTINEL body (`result_is_usable`'s third failure shape —
    the paywalled-but-keyed case a raw exit-code check can't see), not only a
    plain non-zero exit. Traced: `run_board_with_failover` gates `usable` (hence
    `outcome.degraded`, hence `ok = not degraded`, hence the CLI's exit code) on
    `result_is_usable`, which classifies an rc=0 short body containing an
    unavailable marker as UNUSABLE -- so an exact board (empty reserve) with a
    sentinel-body seat degrades exactly like a plain failure, and `passed` is
    False the same way."""
    with _TmpStore() as store:
        d = _git_init_with_diff()

        def _resolver(_model: str):
            def _backend(m, prompt, diff, cwd, timeout, round_no=0, effort=None):
                if m == "gemini":
                    # rc=0, but a short body matching an _UNAVAILABLE_MARKERS
                    # entry -- result_is_usable's sentinel-body failure shape.
                    return ReviewResult(
                        model=m,
                        command=f"stub {m}",
                        returncode=0,
                        stdout="Gemini is currently unavailable.",
                        stderr="",
                    )
                return ReviewResult(
                    model=m,
                    command=f"stub {m}",
                    returncode=0,
                    stdout=f"output from {m}",
                    stderr="",
                )

            return _backend

        restore = _with_backend_stub(_resolver)
        saved_cfg = _cli.load_config
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        _cli.load_config = lambda: {
            "models": ["codex", "gemini"],
            "board": [
                {"model": "codex", "role": "correctness", "name": "Codex"},
                {"model": "gemini", "role": "contracts", "name": "Gemini"},
            ],
        }
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(
                    ["diff", *TASK_ARGS, "-C", d.name, "-m", "codex", "-m", "gemini"]
                )
            # gemini's sentinel body is unusable -> the exact board (no reserve)
            # degrades -> exit 1, exactly like a plain backend failure.
            assert rc == 1, rc
            rec = store.records()[0]
            assert rec.get("passed") is False
            result = _stats.quorum_check(TASK, min_iter=1, min_models=1, min_roles=1)
            assert result["passed"] is False
            assert result["roles"] == []
        finally:
            restore()
            _cli.load_config = saved_cfg
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_cli_records_failure_counts_per_call():
    with _TmpStore() as store:
        d = _git_init_with_diff()
        # codex ok, gemini fails -> ok_count=1, fail_count=1, overall exit 1.
        restore = _with_backend_stub(_stub_resolve_backend({"codex": 0, "gemini": 2}))
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(["diff", *TASK_ARGS, "-C", d.name, "-m", "codex,gemini"])
            assert rc == 1, rc
            r = store.records()[0]
            assert r["ok_count"] == 1 and r["fail_count"] == 1, r
            # A seat failure -> mode_review's own `ok = all(rc==0 ...)` is False ->
            # exit 1 -> the recorded verdict must be passed=False, not just "ran".
            assert r["passed"] is False, r
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_cli_no_dispatch_run_is_not_recorded_but_eta_still_printed():
    """A clean tree (no diff) dispatches zero backends -> no ~0s record poisoning the
    ETA average, but the ETA line is still printed (costs nothing, warns the agent)."""
    import subprocess

    with _TmpStore() as store:
        d = tempfile.TemporaryDirectory()
        repo = Path(d.name)
        env = {**os.environ, "REVIEW_SKIP": "1"}
        common = dict(cwd=str(repo), check=True, env=env)
        subprocess.run(["git", "init", "-q"], **common)
        subprocess.run(["git", "config", "core.hooksPath", "/dev/null"], **common)
        subprocess.run(["git", "config", "user.email", "t@t"], **common)
        subprocess.run(["git", "config", "user.name", "t"], **common)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], **common)
        (repo / "f.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], **common)
        subprocess.run(["git", "commit", "-qm", "init", "--no-verify"], **common)
        # NO further edit -> clean tree -> empty diff -> mode_review returns early.
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                rc = _cli.main(["diff", *TASK_ARGS, "-C", d.name, "-m", "codex,gemini"])
            assert rc == 1, rc  # "No diff to review."
            assert "[review] pool=2 (review)" in err.getvalue()  # ETA still printed
            assert store.records() == []  # but NOT recorded
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_qa_mode_exit_0_does_not_record_passed_true():
    """qa is deliberately REPORT-ONLY (reviewlib.qa.executor.verdict_to_exit_code): a
    FAIL verdict with real findings still exits 0 unless --strict, so the handler's rc
    is NOT a review verdict for qa the way it is for review/quorum/just-ask/brainstorm.
    A qa run must never be recorded as passed=True purely because dispatch() returned
    0 -- that would let a bug-finding qa run with `VERDICT: FAIL` satisfy the
    self-merge-authority quorum gate (codex review finding on this same change)."""
    from reviewlib import panel as _panel

    def _fake_qa_dispatch_that_found_a_bug() -> int:
        # Mirrors a real non-strict qa run: one call succeeded technically (ok_count
        # goes up) but the run's OWN verdict was FAIL -- exit code is still 0.
        _panel._tally_ok(True)
        return 0

    with _TmpStore() as store:
        rc = _cli._run_mode_with_stats(
            "qa",
            ["codex"],
            _fake_qa_dispatch_that_found_a_bug,
            task_code=TASK,
        )
        assert rc == 0
        recs = store.records()
        assert len(recs) == 1, recs
        # Verdict UNKNOWN -> key omitted, NOT written as True.
        assert "passed" not in recs[0], recs[0]
        # And the quorum gate must never count it: unknown verdict fails closed too.
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1)
        assert result["passed"] is False
        assert result["passed_iterations"] == 0
        assert result["total_iterations"] == 1


def test_cli_second_run_eta_uses_first_runs_history():
    with _TmpStore() as store:
        # Seed one review/2 record, then a fresh review/2 run must announce a
        # mode+pool ETA computed from it (basis "this size", not "no history").
        _stats.record_run(
            mode="review",
            models=["codex", "gemini"],
            duration_seconds=90.0,
            ok_count=2,
            fail_count=0,
        )
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        try:
            err = io.StringIO()
            with redirect_stderr(err), _capture_stdout():
                _cli.main(["diff", *TASK_ARGS, "-C", d.name, "-m", "codex,gemini"])
            assert "past run" in err.getvalue() and "this size" in err.getvalue()
            assert len(store.records()) == 2  # seed + this run
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_cli_task_command_lists_iterations_and_detail_transcript():
    from reviewlib.process import write_sidecar_log

    with _TmpStore():
        log = tempfile.mkdtemp()
        old_log = os.environ.get("REVIEW_LOG_DIR")
        old_task = os.environ.get("REVIEW_TASK_CODE")
        os.environ["REVIEW_LOG_DIR"] = log
        os.environ["REVIEW_TASK_CODE"] = TASK
        try:
            started = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                duration_seconds=1.2,
                ok_count=1,
                fail_count=0,
                started=started,
            )
            write_sidecar_log(
                "codex",
                round_no=0,
                argv0="codex",
                returncode=0,
                stdout="TRANSCRIPT-LINE from codex\n",
                stderr="",
                started=started,
            )
            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                rc = _cli.main(["task", TASK])
            assert rc == 0, rc
            text = out.getvalue()
            assert "HYP-742" in text
            assert "iteration 1" in text
            assert "codex" in text

            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                rc = _cli.main(["task", TASK, "--detail", "1"])
            assert rc == 0, rc
            assert "TRANSCRIPT-LINE from codex" in out.getvalue()

            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                rc = _cli.main(["task", "--json"])
            assert rc == 0, rc
            tasks_payload = json.loads(out.getvalue())
            assert set(tasks_payload) == {"tasks"}
            assert tasks_payload["tasks"][0]["task_code"] == TASK
            assert tasks_payload["tasks"][0]["iterations"] == 1
            assert tasks_payload["tasks"][0]["models"] == ["codex"]
            assert tasks_payload["tasks"][0]["modes"] == ["review"]

            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                rc = _cli.main(["task", TASK, "--json"])
            assert rc == 0, rc
            task_payload = json.loads(out.getvalue())
            assert set(task_payload) == {"task_code", "iterations", "sessions"}
            assert task_payload["task_code"] == TASK
            assert task_payload["iterations"][0]["task_code"] == TASK
            assert task_payload["iterations"][0]["iteration"] == 1
            assert task_payload["iterations"][0]["models"] == ["codex"]
            assert task_payload["sessions"][0]["task_code"] == TASK
            assert task_payload["sessions"][0]["models"] == ["codex"]

            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                rc = _cli.main(["task", TASK, "--detail", "1", "--json"])
            assert rc == 0, rc
            detail_payload = json.loads(out.getvalue())
            assert detail_payload["task_code"] == TASK
            assert detail_payload["session_id"].startswith("sess-")
            assert detail_payload["calls"][0]["task_code"] == TASK
            assert "TRANSCRIPT-LINE from codex" in detail_payload["calls"][0]["body"]
            assert "errors" in detail_payload
            assert "brainstorm" in detail_payload
            assert "roles" in detail_payload
        finally:
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
            if old_task is None:
                os.environ.pop("REVIEW_TASK_CODE", None)
            else:
                os.environ["REVIEW_TASK_CODE"] = old_task


def test_cli_task_detail_matches_logs_by_timestamp_not_iteration_index():
    from reviewlib.process import write_sidecar_log

    with _TmpStore():
        log_dir = tempfile.TemporaryDirectory()
        old_log = os.environ.get("REVIEW_LOG_DIR")
        old_task = os.environ.get("REVIEW_TASK_CODE")
        os.environ["REVIEW_LOG_DIR"] = log_dir.name
        os.environ["REVIEW_TASK_CODE"] = TASK
        try:
            first = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
            second = datetime(2026, 6, 1, 10, 3, tzinfo=timezone.utc)
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                started=first,
            )
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["gemini"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                started=second,
            )
            write_sidecar_log(
                "gemini",
                round_no=0,
                argv0="gemini",
                returncode=0,
                stdout="SECOND-ITERATION-TRANSCRIPT\n",
                stderr="",
                started=second,
            )

            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                rc = _cli.main(["task", TASK])
            assert rc == 0, rc
            listed = out.getvalue()
            assert "iteration 1" in listed and "logs not found" in listed, listed
            assert "iteration 2" in listed and "session" in listed, listed

            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = _cli.main(["task", TASK, "--detail", "1"])
            assert rc == 1, rc
            assert "No conversation logs found" in err.getvalue()

            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                rc = _cli.main(["task", TASK, "--detail", "2"])
            assert rc == 0, rc
            assert "SECOND-ITERATION-TRANSCRIPT" in out.getvalue()
        finally:
            if old_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = old_log
            if old_task is None:
                os.environ.pop("REVIEW_TASK_CODE", None)
            else:
                os.environ["REVIEW_TASK_CODE"] = old_task
            log_dir.cleanup()


# ---------------------------------------------------------------------------
# advertising: the SKILL self-advertisement carries the no-timeout warning
# ---------------------------------------------------------------------------
def test_cli_brainstorm_records_persona_slot_pool_not_raw_models():
    """A 2-model brainstorm dispatches max(3, len(panel))=3 persona slots per round, so
    the recorded pool_size must be 3, not 2 (codex P2: don't undercount small panels)."""
    with _TmpStore() as store:
        d = tempfile.TemporaryDirectory()  # no diff needed for brainstorm
        restore = _with_backend_stub(_stub_resolve_backend(0))
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        # Keep the run tiny: min rounds is floored at 5 internally, but the moderator is
        # also stubbed (returns ok), so each round is instant. Cap rounds via --rounds.
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                _cli.main(
                    [
                        "brainstorm",
                        "topic",
                        *TASK_ARGS,
                        "-C",
                        d.name,
                        "-m",
                        "codex,gemini",
                        "--rounds",
                        "1",
                        "--max-rounds",
                        "1",
                    ]
                )
            recs = store.records()
            assert len(recs) == 1, recs
            r = recs[0]
            assert r["mode"] == "brainstorm"
            assert r["pool_size"] == 3, r  # 2 models -> 3 persona slots
            # Every stubbed backend (personas + moderator) returns rc=0, so a healthy
            # run must tally ZERO failures and a positive ok_count. This guards the
            # success path: before the stub accepted round_no, the 6-arg panel call
            # raised TypeError and these would have been ok=0 / fail>0 unnoticed.
            assert r["fail_count"] == 0, r
            assert r["ok_count"] >= 1, r
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


def test_moderator_empty_output_tallies_as_fail_not_ok():
    """A moderator candidate that exits 0 with EMPTY output is rejected by run_moderator
    (it falls through / surfaces a failure), so the run-stats tally must count it as a
    fail, not an ok keyed only on the return code (codex P2). And the whole moderator
    turn is ONE tallied call regardless of candidate fall-through."""
    from reviewlib import panel as p

    # Stub run_single so the only candidate returns rc 0 but empty stdout.
    saved = p.run_single
    p.run_single = lambda model, prompt, cwd, timeout, diff="", round_no=0: (
        ReviewResult(model=model, command="stub", returncode=0, stdout="", stderr="")
    )
    p.begin_call_tally()
    try:
        res = p.run_moderator(["codex"], "p", Path("."), 5)
        # run_moderator converts all-empty success into a failure result.
        assert res.returncode != 0, res
        tally = p.end_call_tally()
        # Exactly one moderator call, counted as a FAIL (not ok).
        assert tally == {"ok": 0, "fail": 1}, tally
    finally:
        p.run_single = saved
        # ensure the tally is cleared even if asserts above raised
        if p._call_tally is not None:
            p.end_call_tally()


def test_moderator_success_tallies_one_ok_despite_fallback():
    """A moderator that falls back (first candidate fails, second succeeds) still counts
    as ONE ok call — fall-through attempts are not over-counted."""
    from reviewlib import panel as p

    saved = p.run_single
    calls = {"n": 0}

    def runner(model, prompt, cwd, timeout, diff="", round_no=0):
        calls["n"] += 1
        if model == "bad":
            return ReviewResult(
                model=model, command="stub", returncode=1, stdout="", stderr="boom"
            )
        return ReviewResult(
            model=model, command="stub", returncode=0, stdout="synthesis", stderr=""
        )

    p.run_single = runner
    p.begin_call_tally()
    try:
        res = p.run_moderator(["bad", "good"], "p", Path("."), 5)
        assert res.returncode == 0 and res.model == "good", res
        tally = p.end_call_tally()
        assert tally == {"ok": 1, "fail": 0}, tally  # one logical moderator call, ok
        assert calls["n"] == 2  # but it did try both candidates
    finally:
        p.run_single = saved
        if p._call_tally is not None:
            p.end_call_tally()


def test_skill_md_warns_never_short_timeout():
    md = SKILL_MD
    assert "NEVER wrap" in md or "Never wrap" in md.title()
    low = md.lower()
    assert "timeout" in low
    assert "minutes" in low
    # mentions that the tool prints the expected duration at startup
    assert (
        "eta" in low
        or "prints a one-line eta" in low
        or "expected duration" in low
        or "pool size" in low
    )


def test_skill_blurb_warns_never_short_timeout():
    low = SKILL_BLURB.lower()
    assert "never" in low and "timeout" in low and "minutes" in low
    assert "pool size" in low or "expected duration" in low


def test_skill_docs_show_required_task_code_for_recorded_modes():
    for text in (SKILL_MD, SKILL_BLURB):
        assert "review diff --task CODE" in text
        assert "review brainstorm" in text and "--task CODE" in text
        assert 'review just-ask "Q" --task CODE' in text
        assert 'review quorum "Q" --task CODE' in text


# ---------------------------------------------------------------------------
# quorum_check + `review task CODE --check` (self-merge-authority PR1 gate)
#
# min_iter/min_models are evaluated over PASSED iterations ONLY (CTO decision
# tg#7306 #1) — a record needs record_run(..., passed=True) to count. A record
# written with no `passed` kwarg (the pre-v3 shape) has verdict UNKNOWN and must
# fail closed: it never counts, even though it has real ok_count/fail_count.
# ---------------------------------------------------------------------------
def test_quorum_check_met():
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is True
        assert result["passed_iterations"] == 3
        assert result["total_iterations"] == 3
        assert result["distinct_models_passed"] == 3
        assert result["models"] == ["codex", "fable5", "gemini"]
        assert "error" not in result
        # Contract: the returned dict has a stable shape + value types (Gemini review).
        # review-cli#246: `min_models_advisory` is now always present alongside an
        # explicit `min_models` (see test_quorum_check_min_models_advisory_only_when_
        # explicit_review_cli_246 for the full explicit-vs-default matrix).
        assert set(result) == {
            "task_code",
            "passed_iterations",
            "total_iterations",
            "distinct_models_passed",
            "models",
            "min_iter",
            "min_models",
            "passed",
            "min_models_advisory",
        }
        assert isinstance(result["task_code"], str)
        assert isinstance(result["passed_iterations"], int)
        assert isinstance(result["total_iterations"], int)
        assert isinstance(result["distinct_models_passed"], int)
        assert isinstance(result["models"], list)
        assert all(isinstance(m, str) for m in result["models"])
        assert isinstance(result["min_iter"], int)
        assert isinstance(result["min_models"], int)
        assert isinstance(result["passed"], bool)
        assert isinstance(result["min_models_advisory"], str)


def test_quorum_check_short_on_iterations():
    with _TmpStore():
        # 2 passed runs, but 3 distinct models across them (>= min_models) -- only
        # iterations short.
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex", "gemini"],
            duration_seconds=1.0,
            ok_count=2,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["fable5"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["passed_iterations"] == 2
        assert result["total_iterations"] == 2
        assert result["distinct_models_passed"] == 3
        assert "error" not in result


def test_quorum_check_short_on_models():
    with _TmpStore():
        # 3 passed runs, but the SAME model each time -- only distinct_models short.
        for _ in range(3):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["passed_iterations"] == 3
        assert result["distinct_models_passed"] == 1
        assert "error" not in result


def test_quorum_check_names_currently_stalled_models_review_cli_221():
    """review-cli#221: a bare '2/3 distinct models' short count leaves an operator
    guessing which seat is the problem. When the bar isn't met, quorum_check must name
    any ATTEMPTED model that's currently cooling down (unavailable/timing out), sourced
    from seat_cooldown — the same live signal dispatch itself already checks."""
    from reviewlib import seat_cooldown as _sc

    with tempfile.TemporaryDirectory() as d, _TmpStore():
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        # Also scrub $REVIEW_SEAT_COOLDOWN_SECONDS (the module's own documented un-stick
        # hatch): a developer with it exported would otherwise see this test fail two
        # ways — =0 disables cooldown entirely (no stalled_models key at all), any other
        # value suppresses escalation (consecutive_failures stays 1, not 2). Mirrors
        # test_seat_cooldown.py's `_with_store` fixture, which scrubs it for the exact
        # same reason (codex review finding cited in its own docstring).
        saved_cd_ttl = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        try:
            # codex passed once; oc:zai/glm-5.2 was ATTEMPTED (recorded, even though the
            # overall run failed) and is separately in an active cooldown right now —
            # exactly the HYP-1295 shape (a failed model still shows up in the run's
            # `models` list even though it didn't itself pass).
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex", "oc:zai/glm-5.2"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=1,
                passed=False,
            )
            fixed_now = time.time()
            # No explicit ttl_seconds: two consecutive failures escalate fail_count
            # to 2 (see test_seat_cooldown.py for the escalation schedule itself).
            # "timed out" here is illustrative fixture data for the `reason` field, not
            # a real production value — `record_cooldown`'s `reason` param accepts any
            # string, but `_chronic_unavailable_reason` (the only real caller) only
            # ever produces "unavailable sentinel" or "session limit / usage credits";
            # a plain timeout does not currently start a cooldown (round-4 review
            # finding, k3 — see seat_cooldown.py's own docstring).
            _sc.record_cooldown("oc:zai/glm-5.2", "timed out", now=fixed_now)
            _sc.record_cooldown("oc:zai/glm-5.2", "timed out", now=fixed_now)
            # Round-4 review finding (k3): a model that's genuinely cooling down but was
            # NEVER attempted for THIS task code must still be excluded — the scoping
            # claim ("never lists an unrelated seat that simply isn't part of this
            # task's history") had no regression coverage; a change that dropped the
            # attempted_models filter and listed every cooling seat machine-wide would
            # otherwise pass the suite unnoticed.
            _sc.record_cooldown("claude:claude-fable-5", "session limit", now=fixed_now)

            result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
            assert result["passed"] is False
            assert "stalled_models" in result
            stalled = {s["model"]: s for s in result["stalled_models"]}
            assert "oc:zai/glm-5.2" in stalled
            assert stalled["oc:zai/glm-5.2"]["reason"] == "timed out"
            assert stalled["oc:zai/glm-5.2"]["consecutive_failures"] == 2
            assert stalled["oc:zai/glm-5.2"]["remaining_seconds"] > 0
            # codex isn't cooling down at all — must not be listed as stalled.
            assert "codex" not in stalled
            # claude:claude-fable-5 IS cooling down but was never part of this task's
            # history — must not leak into this task's stalled_models either.
            assert "claude:claude-fable-5" not in stalled
        finally:
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file
            if saved_cd_ttl is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = saved_cd_ttl


def test_quorum_check_met_has_no_stalled_models_key():
    """A satisfied gate never needs the diagnostic key — keeps the passing-path JSON
    shape exactly as documented (test_quorum_check_met's exact-shape assertion)."""
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is True
        assert "stalled_models" not in result


def test_quorum_check_min_roles_met_has_no_stalled_models_key_review_cli_221():
    """Round-10 review finding (Opus): `stalled_models` must key on the ACTUAL
    role-mode verdict (`result["passed"]`), not a re-derived model-count
    shortfall -- a run that PASSES via --min-roles (with a cooling-down model
    that has nothing to do with the passed roles) must not carry
    `stalled_models` at all.

    `min_models` is deliberately OMITTED here (review-cli#246: an explicitly
    given `min_models` is now always enforced, AND'd with `min_roles` -- see
    test_quorum_check_explicit_min_models_always_enforced_review_cli_246 for
    that scenario) so this test isolates the pure "--min-roles alone governs"
    path the original review-cli#221 feature documents."""
    from reviewlib import seat_cooldown as _sc

    with tempfile.TemporaryDirectory() as d, _TmpStore():
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        try:
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                roles=["architect"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                roles=["security"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
            # An unrelated model is cooling down -- must not surface, since the
            # gate PASSED via roles (2 >= min_roles=2) regardless of models.
            _sc.record_cooldown("gemini", "timed out", now=time.time())
            result = _stats.quorum_check(TASK, min_iter=2, min_roles=2)
            assert result["passed"] is True, result  # 2 distinct roles >= 2
            assert "stalled_models" not in result
        finally:
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file


# ---------------------------------------------------------------------------
# review-cli#246: an explicit floor is ALWAYS enforced (fixes a codex review
# finding on PR #246 -- a direct caller passing BOTH min_models and min_roles
# explicitly used to let min_roles silently outvote an explicit min_models).
# ---------------------------------------------------------------------------
def test_quorum_check_explicit_min_models_always_enforced_review_cli_246():
    """The exact bug: min_models=5, min_roles=1, only 1 distinct model reviewing.
    Before this fix, min_roles governed alone and this returned passed=True from
    a single-model review -- exactly what a self-merge-authority gate must never
    allow when the caller explicitly asked for 5 distinct models."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check(TASK, min_iter=1, min_models=5, min_roles=1)
        assert result["passed"] is False, result
        assert result["distinct_models_passed"] == 1
        assert result["min_models"] == 5
        assert result["distinct_roles_passed"] == 1
        assert result["min_roles"] == 1


def test_quorum_check_explicit_min_models_only_preserves_old_behavior_review_cli_246():
    """Only --min-models explicitly given (no --min-roles at all): reproduces the
    exact pre-#221 behavior -- a strict distinct-model-name count decides
    `passed`, no role keys in the result."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1)
        assert result["passed"] is True, result
        assert "roles" not in result
        assert "distinct_roles_passed" not in result
        assert "min_roles" not in result

        short = _stats.quorum_check(TASK, min_iter=1, min_models=2)
        assert short["passed"] is False, short


def test_quorum_check_explicit_min_roles_only_preserves_pr221_behavior_review_cli_246():
    """Only --min-roles explicitly given (no --min-models at all): reproduces the
    exact pre-#246 --min-roles behavior -- distinct board roles decide
    `passed`; `min_models`/`distinct_models_passed` are still reported for
    visibility but never gate."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check(TASK, min_iter=1, min_roles=1)
        assert result["passed"] is True, result
        assert "min_models" not in result
        assert result["distinct_models_passed"] == 1  # reported, just not enforced


def test_quorum_check_both_explicit_and_logic_review_cli_246():
    """BOTH floors explicitly given: `passed` requires min_iter AND the model
    floor AND the role floor to all be met -- neither can silently outvote the
    other anymore."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        # 2 distinct models, 2 distinct roles -- both floors of 2 met.
        both_met = _stats.quorum_check(TASK, min_iter=2, min_models=2, min_roles=2)
        assert both_met["passed"] is True, both_met

        # Role floor raised past what's recorded -- roles fail, models still fine.
        role_short = _stats.quorum_check(TASK, min_iter=2, min_models=2, min_roles=3)
        assert role_short["passed"] is False, role_short

        # Model floor raised past what's recorded -- models fail, roles still fine.
        model_short = _stats.quorum_check(TASK, min_iter=2, min_models=3, min_roles=2)
        assert model_short["passed"] is False, model_short


def test_quorum_check_neither_explicit_defaults_to_role_based_review_cli_246():
    """The true default (no flags at all): switches to a ROLE-based check at
    the same numeric floor --min-models used to default to (3) -- Alex's
    direction that role-based counting is the new default everywhere, with no
    default model-count floor."""
    with _TmpStore():
        for model, role in (
            ("codex", "architect"),
            ("gemini", "security"),
            ("codex", "performance"),  # duplicated-model role-fill (PR #207)
        ):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                roles=[role],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=3)
        assert result["passed"] is True, result
        assert result["min_roles"] == 3
        assert result["distinct_roles_passed"] == 3
        assert "min_models" not in result
        assert result["distinct_models_passed"] == 2  # reported, just not enforced


def test_quorum_check_min_models_advisory_only_when_explicit_review_cli_246():
    """The non-blocking `min_models_advisory` key appears whenever `min_models`
    is explicitly given (regardless of pass/fail, regardless of whether
    `min_roles` is also given) and is absent otherwise -- it never affects
    `passed`."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        with_models = _stats.quorum_check(TASK, min_iter=1, min_models=1)
        assert "min_models_advisory" in with_models

        with_both = _stats.quorum_check(TASK, min_iter=1, min_models=1, min_roles=1)
        assert "min_models_advisory" in with_both

        roles_only = _stats.quorum_check(TASK, min_iter=1, min_roles=1)
        assert "min_models_advisory" not in roles_only

        neither = _stats.quorum_check(TASK, min_iter=1)
        assert "min_models_advisory" not in neither


def test_quorum_check_min_models_advisory_absent_on_unreadable_store_review_cli_246():
    """k3/Opus review finding: `min_models_advisory` must NOT leak onto the
    unreadable-store fail-closed shape -- unlike invalid-task-code/floor-
    validation (which short-circuit via `_rejected()` before the advisory is
    ever added), an unreadable store does NOT short-circuit early; `store_error`
    is only folded into the result as an `"error"` key at the very end, by
    `_finalize_quorum_result`. The advisory is gated on `store_error is None`
    (matching the pre-existing `min_roles_suggestion` guard) specifically so
    this denial never carries a nudge that contradicts its own `"error"`."""
    saved = os.environ.get("REVIEW_STATS_FILE")
    os.environ["REVIEW_STATS_FILE"] = "/nonexistent-xyz/deeper/run-stats.jsonl"
    try:
        result = _stats.quorum_check(TASK, min_iter=1, min_models=3)
        assert result["passed"] is False
        assert "error" in result
        assert "min_models_advisory" not in result
    finally:
        if saved is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = saved


def test_quorum_check_min_models_advisory_absent_on_zero_recorded_iterations_review_cli_246():
    """k3/Opus round-2 review finding: the SAME leak as the unreadable-store
    case above, but for a READABLE store with ZERO recorded iterations for this
    task code -- that denial also does not short-circuit via `_rejected()` (it
    goes through the main result path, and only gets its `"error"` key from
    `_finalize_quorum_result` at the very end), so the advisory guard must
    exclude it too via `and iterations`, exactly like the sibling
    `min_roles_suggestion` guard -- otherwise a caller sees a nudge that "this
    explicit floor may not be needed" on a task with NO review history
    whatsoever, contradicting its own fail-closed error."""
    with _TmpStore():
        result = _stats.quorum_check("HYP-999-review-cli-246", min_iter=1, min_models=3)
        assert result["passed"] is False
        assert "error" in result
        assert "min_models_advisory" not in result


def test_quorum_check_min_models_advisory_absent_on_rejected_shapes_review_cli_246():
    """k3 round-7 review finding: the two `_rejected()` early-return shapes
    (invalid task code, floor-validation failure) are trivially excluded from
    the advisory today, since `_rejected()` returns before the advisory
    assignment is ever reached -- but the documented contract covers them
    explicitly, and until now only the two shapes that DON'T short-circuit
    (unreadable store, zero recorded iterations) had a pinning test. Closes
    that gap for both shapes that DO short-circuit."""
    invalid_code = _stats.quorum_check("bad code", min_iter=1, min_models=3)
    assert invalid_code["passed"] is False
    assert "error" in invalid_code
    assert "min_models_advisory" not in invalid_code

    with _TmpStore():
        floor_violation = _stats.quorum_check(TASK, min_iter=1, min_models=0)
        assert floor_violation["passed"] is False
        assert "error" in floor_violation
        assert "min_models_advisory" not in floor_violation


def test_quorum_check_bare_default_aggregates_roles_from_one_multi_seat_record_review_cli_246():
    """Opus review finding: real board dispatch records a SINGLE run-stats
    record whose `roles` list carries multiple seats' roles at once
    (`panel.FailoverOutcome.usable_roles`), not one record per role -- pin
    that shape specifically against the new bare-default role-based check,
    not just the one-role-per-record shape every other test in this file
    hand-writes."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex", "gemini", "fable5"],
            roles=["architect", "security", "performance"],
            duration_seconds=1.0,
            ok_count=3,
            fail_count=0,
            passed=True,
        )
        # Only 1 passed ITERATION (one multi-seat record), so min_iter=3 (the
        # bare default) is NOT met even though all 3 default roles are covered
        # by that single record -- iterations and roles are counted separately.
        short_on_iter = _stats.quorum_check(TASK, min_iter=3)
        assert short_on_iter["passed"] is False, short_on_iter
        assert short_on_iter["distinct_roles_passed"] == 3
        assert short_on_iter["passed_iterations"] == 1

        # Lowering min_iter to what this one record actually satisfies passes,
        # proving the 3 roles genuinely aggregated from the single record's
        # `roles` list (not silently zero/one).
        result = _stats.quorum_check(TASK, min_iter=1)
        assert result["passed"] is True, result
        assert result["distinct_roles_passed"] == 3
        assert set(result["roles"]) == {"architect", "security", "performance"}


def test_quorum_check_min_roles_not_met_shows_stalled_models_review_cli_221():
    """Round-10 review finding (Opus), the inverse case: a run that would
    SATISFY a raw distinct-model-count floor but FAILS via --min-roles must
    still surface `stalled_models` -- proving the guard doesn't independently
    re-derive a model-count shortfall that disagrees with the actual role-mode
    verdict."""
    from reviewlib import seat_cooldown as _sc

    with tempfile.TemporaryDirectory() as d, _TmpStore():
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        try:
            # 2 distinct models (would satisfy a --min-models=2 floor), but BOTH
            # under the SAME role -- only 1 distinct role, short of min_roles=2.
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                roles=["architect"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["gemini"],
                roles=["architect"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
            fixed_now = time.time()
            _sc.record_cooldown("gemini", "timed out", now=fixed_now)
            _sc.record_cooldown("gemini", "timed out", now=fixed_now)
            result = _stats.quorum_check(TASK, min_iter=2, min_models=2, min_roles=2)
            assert result["passed"] is False, result  # only 1 distinct role
            assert "stalled_models" in result
            stalled = {s["model"]: s for s in result["stalled_models"]}
            assert "gemini" in stalled
        finally:
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file


def test_quorum_check_only_counts_passed_iterations():
    """The bug being fixed: 3 DISPATCHED iterations (enough to satisfy the old
    count-everything gate) but only 1 of them PASSED -- must fail, not pass, and
    the failed ones must not even count toward total for the purpose of the bar."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            duration_seconds=1.0,
            ok_count=0,
            fail_count=1,
            passed=False,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["fable5"],
            duration_seconds=1.0,
            ok_count=0,
            fail_count=1,
            passed=False,
        )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=1)
        assert result["passed"] is False
        assert result["passed_iterations"] == 1
        assert result["total_iterations"] == 3  # all 3 ran, only 1 passed
        assert result["distinct_models_passed"] == 1
        assert result["models"] == ["codex"]  # the failed seats' models don't count


def test_quorum_check_min_models_only_counts_models_that_passed():
    """3 distinct models dispatched, but only ONE of them ever came back passed --
    --min-models 2 must fail even though 3 distinct models technically ran (a model
    that only ever failed doesn't count toward diversity)."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=False,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["fable5"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=False,
        )
        result = _stats.quorum_check(TASK, min_iter=1, min_models=2)
        assert result["passed"] is False
        assert result["passed_iterations"] == 1
        assert result["distinct_models_passed"] == 1
        assert result["models"] == ["codex"]


def test_quorum_check_legacy_records_without_verdict_fail_closed():
    """Records with no `passed` key at all -- the shape every run written before
    STATS_VERSION 3 has -- must NOT satisfy the gate: unknown verdict is treated as
    not-passed, even though the old count-everything gate would have happily
    counted them."""
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            # No passed= kwarg -> pre-migration shape, verdict unknown.
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
            )
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1)
        assert result["passed"] is False
        assert result["passed_iterations"] == 0
        assert result["total_iterations"] == 3
        assert result["distinct_models_passed"] == 0
        assert result["models"] == []


def test_quorum_check_current_verdictless_mode_also_fails_closed():
    """A missing `passed` key is not EXCLUSIVELY a legacy (pre-v3) signature -- a
    CURRENT record from a mode with no verdict to thread (qa, recorded via
    `_run_mode_with_stats`'s `mode == "qa"` branch) has the identical shape and must
    fail closed the same way, not be mistaken for "written before v3"."""
    with _TmpStore() as store:
        from reviewlib import panel as _panel

        def _fake_qa_dispatch() -> int:
            _panel._tally_ok(True)
            return 0

        _cli._run_mode_with_stats("qa", ["codex"], _fake_qa_dispatch, task_code=TASK)
        rec = store.records()[0]
        assert rec["v"] == _stats.STATS_VERSION  # a CURRENT record, not legacy
        assert "passed" not in rec
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1)
        assert result["passed"] is False
        assert result["passed_iterations"] == 0


def test_run_mode_with_stats_roles_after_wiring_review_cli_221():
    """The `roles_after` -> `record_run(roles=)` glue, exercised directly through
    `_run_mode_with_stats` (Fable round-1 review finding: distinct from both the
    panel-level `usable_roles` tests and the stats-level `record_run` tests, this
    exact wire had no coverage of its own). A successful `roles_after` lands its
    list in the record -- alongside a `models_after`, since round-6 review
    (Opus) tightened the contract to require BOTH present, so `recorded_roles`
    is only ever paired against the ACTUAL roster, never the merely-planned one
    (see `test_run_mode_with_stats_roles_omitted_when_no_models_after_given_review_cli_221`
    for the "models_after absent" case this excludes)."""
    with _TmpStore() as store:
        from reviewlib import panel as _panel

        def _dispatch() -> int:
            _panel._tally_ok(True)
            return 0

        _cli._run_mode_with_stats(
            "review",
            ["codex", "gemini"],
            _dispatch,
            models_after=lambda: ["codex", "gemini"],
            roles_after=lambda: ["architect", "security"],
            task_code=TASK,
        )
        rec = store.records()[0]
        assert rec["roles"] == ["architect", "security"]


def test_run_mode_with_stats_roles_after_raising_omits_roles_key_review_cli_221():
    """A `roles_after` that raises must never break the run -- stats are best-
    effort -- and must omit the `roles` key (not write a misleading `[]`). Paired
    with a succeeding `models_after` so the round-6 `models_after is not None`
    guard doesn't short-circuit before `roles_after` is ever even called --
    this test is specifically about `roles_after`'s OWN exception handling."""
    with _TmpStore() as store:
        from reviewlib import panel as _panel

        def _dispatch() -> int:
            _panel._tally_ok(True)
            return 0

        def _boom():
            raise RuntimeError("boom")

        rc = _cli._run_mode_with_stats(
            "review",
            ["codex"],
            _dispatch,
            models_after=lambda: ["codex"],
            roles_after=_boom,
            task_code=TASK,
        )
        assert rc == 0
        rec = store.records()[0]
        assert "roles" not in rec


def test_run_mode_with_stats_roles_after_empty_omits_roles_key_review_cli_221():
    """An empty `roles_after()` result (e.g. a degraded run with zero usable
    seats) omits the `roles` key, same as `models_after` falling back to the
    planned pool -- an empty role LIST is never persisted as `roles: []`. Paired
    with a succeeding `models_after` for the same reason as the raising case
    above -- this test is about `roles_after`'s empty-result handling
    specifically, not the round-6 `models_after is not None` guard."""
    with _TmpStore() as store:
        from reviewlib import panel as _panel

        def _dispatch() -> int:
            _panel._tally_ok(True)
            return 0

        _cli._run_mode_with_stats(
            "review",
            ["codex"],
            _dispatch,
            models_after=lambda: ["codex"],
            roles_after=lambda: [],
            task_code=TASK,
        )
        rec = store.records()[0]
        assert "roles" not in rec


def test_run_mode_with_stats_roles_omitted_when_models_after_falls_back_review_cli_221():
    """Round-5 review finding (Opus): `quorum_check`'s monoculture guard zips
    `models`/`roles` BY INDEX, so the two must always describe the SAME seats in
    the SAME order or omit `roles` entirely -- never a real `roles_after()` result
    paired against the PLANNED `pool_models` fallback. When `models_after` falls
    back (raises, or returns empty), `roles_after`'s result must be discarded too,
    even if `roles_after` itself succeeded."""
    with _TmpStore() as store:
        from reviewlib import panel as _panel

        def _dispatch() -> int:
            _panel._tally_ok(True)
            return 0

        def _models_boom():
            raise RuntimeError("boom")

        _cli._run_mode_with_stats(
            "review",
            ["codex", "gemini"],
            _dispatch,
            models_after=_models_boom,
            roles_after=lambda: ["architect", "security"],
            task_code=TASK,
        )
        rec = store.records()[0]
        # models fell back to the planned pool ...
        assert rec["models"] == ["codex", "gemini"]
        # ... so roles must be OMITTED, not paired against a roster that isn't the
        # one those roles were actually observed on.
        assert "roles" not in rec


def test_run_mode_with_stats_roles_omitted_when_no_models_after_given_review_cli_221():
    """Round-6 review finding (Opus): the round-5 guard only checked
    `models_after_fell_back` (which stays False when `models_after` is None
    entirely, since it's never set) -- a caller supplying `roles_after` WITHOUT
    `models_after` would then pair real roles against the merely-PLANNED
    `pool_models`, the exact drift the round-5 fix exists to prevent. No CURRENT
    caller does this (the board dispatch always supplies both), but the guard
    itself must not depend on that being true forever."""
    with _TmpStore() as store:
        from reviewlib import panel as _panel

        def _dispatch() -> int:
            _panel._tally_ok(True)
            return 0

        _cli._run_mode_with_stats(
            "review",
            ["codex", "gemini"],
            _dispatch,
            roles_after=lambda: ["architect", "security"],
            task_code=TASK,
        )
        rec = store.records()[0]
        assert rec["models"] == ["codex", "gemini"]  # the planned pool, unchanged
        assert "roles" not in rec  # no ACTUAL-roster callable -> no roles either


def test_quorum_check_zero_records_fails_closed():
    with _TmpStore():
        # Write a record for a DIFFERENT task first so the store exists and is
        # readable -- isolates "store readable, zero records for THIS code" from
        # "store missing/unreadable" (covered separately below).
        _stats.record_run(
            task_code="HYP-000",
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check("HYP-999", min_iter=1, min_models=1)
        assert result["passed"] is False
        assert result["passed_iterations"] == 0
        assert "error" in result
        assert "HYP-999" in result["error"]


def test_quorum_check_missing_store_fails_closed():
    saved = os.environ.get("REVIEW_STATS_FILE")
    os.environ["REVIEW_STATS_FILE"] = "/nonexistent-xyz/deeper/run-stats.jsonl"
    try:
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1)
        assert result["passed"] is False
        assert "error" in result
    finally:
        if saved is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = saved


def test_quorum_check_unexpandable_store_fails_closed():
    """An unexpandable $REVIEW_STATS_FILE makes stats_path() raise RuntimeError --
    quorum_check must catch it and fail closed, never raise (mirrors
    test_stats_never_raise_on_unexpandable_path for record_run/eta_line)."""
    saved = os.environ.get("REVIEW_STATS_FILE")
    os.environ["REVIEW_STATS_FILE"] = "~nosuchuser-zzz/run-stats.jsonl"
    try:
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1)
        assert result["passed"] is False
        assert "error" in result
    finally:
        if saved is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = saved


def test_quorum_check_invalid_task_code_fails_closed():
    result = _stats.quorum_check("bad code", min_iter=1, min_models=1)
    assert result["passed"] is False
    assert "error" in result
    assert result["passed_iterations"] == 0 and result["distinct_models_passed"] == 0


def test_quorum_check_rejects_zero_or_negative_thresholds_directly():
    """The floor validation lives in quorum_check() itself, not only the CLI
    wrapper -- a direct library caller (bypassing the CLI's own --min-iter/
    --min-models argparse validation) must not be able to get passed=True via
    0 >= 0 for a task with only failed/unverdicted iterations (codex review
    finding: the CLI-only check let a lib caller bypass the fail-closed floor)."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=0,
            fail_count=1,
            passed=False,
        )
        for min_iter, min_models in ((0, 1), (1, 0), (-1, 1), (1, -1)):
            result = _stats.quorum_check(TASK, min_iter=min_iter, min_models=min_models)
            assert result["passed"] is False, (min_iter, min_models, result)
            assert "error" in result, (min_iter, min_models, result)


def test_quorum_check_floor_validation_message_never_names_a_defaulted_min_roles_review_cli_246():
    """Opus review finding: a direct library caller passing NEITHER --min-models
    nor --min-roles, but an invalid --min-iter, must not see a validation
    message that falsely claims they typed `min_roles=3` -- the default-
    substitution (which sets min_roles=3 when neither is given) runs BEFORE
    this validation, so the message builder must check the PRE-substitution
    `min_roles_explicit` flag, not `min_roles is not None`, to decide whether
    to name it (the CLI layer's own copy of this validation was never affected,
    since the CLI never substitutes internally -- only a direct library caller
    bypassing the CLI sees this)."""
    result = _stats.quorum_check(TASK, min_iter=0)
    assert result["passed"] is False
    assert "error" in result
    assert "(got min_iter=0)" in result["error"]
    # The static prose legitimately says "min_models/min_roles" as a concept --
    # what must NOT appear is a per-caller VALUE clause for either, since
    # neither was actually part of the input.
    assert "min_roles=" not in result["error"]
    assert "min_models=" not in result["error"]


# ---------------------------------------------------------------------------
# --min-roles quorum-counting mode (review-cli#221)
# ---------------------------------------------------------------------------
def test_record_run_roles_omitted_when_not_given():
    """A mode with no per-seat role concept (quorum/just-ask/brainstorm/qa) never
    passes `roles=` -- the key must be OMITTED from the persisted record, not
    written as an empty list (same 'unknown, not written as null' convention as
    `passed`/`repo_id`), so `_distinct_roles` can tell "never recorded" apart from
    "recorded zero roles"."""
    with _TmpStore() as store:
        _stats.record_run(
            task_code=TASK,
            mode="quorum",
            models=["codex", "gemini"],
            duration_seconds=1.0,
            ok_count=2,
            fail_count=0,
            passed=True,
        )
        rec = store.records()[0]
        assert "roles" not in rec


def test_record_run_roles_persisted_when_given():
    with _TmpStore() as store:
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex", "codex"],
            roles=["architect", "security"],
            duration_seconds=1.0,
            ok_count=2,
            fail_count=0,
            passed=True,
        )
        rec = store.records()[0]
        assert rec["roles"] == ["architect", "security"]


def test_record_run_roles_omitted_on_length_mismatch_review_cli_221():
    """Round-7 review finding (Fable): `_distinct_roles` (the ENFORCING counter
    behind --min-roles) has no reference to `models` -- unlike the advisory
    `_models_behind_role_coverage`, which zips and fails closed, a record with
    MORE roles than models would inflate role coverage from fewer real seats
    than it claims. No current producer can write mismatched lengths, but
    `record_run` (the one write chokepoint) defends against it anyway: a
    length mismatch omits `roles` entirely rather than persisting a shape that
    lies about which seats it describes."""
    with _TmpStore() as store:
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect", "security"],  # more roles than models
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        rec = store.records()[0]
        assert "roles" not in rec


def test_quorum_check_min_roles_counts_distinct_roles_not_models_review_cli_221():
    """The core review-cli#221 scenario: 2 passed iterations each on a genuinely
    distinct model, plus a 3rd whose model REPEATS the first one but was reviewing
    under a DIFFERENT board role (the board's #207 shortage-resilience duplicate-
    model role-fill). `--min-roles 3` must count that 3rd pass (3 distinct roles
    covered); `--min-models 3` must NOT (only 2 distinct model-name strings).

    `by_roles` deliberately omits `min_models` (review-cli#246: an explicitly
    given `min_models` is now always enforced, AND'd with `min_roles` -- see
    test_quorum_check_both_explicit_and_logic_review_cli_246 for that case) so
    this isolates the pure "--min-roles alone governs" path."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        # Duplicated-model role-fill: same model ("codex") as the first iteration,
        # but a DIFFERENT role.
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["performance"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )

        by_models = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert by_models["passed"] is False
        assert by_models["distinct_models_passed"] == 2

        by_roles = _stats.quorum_check(TASK, min_iter=3, min_roles=3)
        assert by_roles["passed"] is True, by_roles
        assert by_roles["distinct_roles_passed"] == 3
        assert by_roles["roles"] == ["architect", "performance", "security"]
        assert by_roles["min_roles"] == 3
        # distinct_models_passed is still reported for visibility even without
        # an explicit --min-models -- it just never gates.
        assert by_roles["distinct_models_passed"] == 2


def test_quorum_check_min_models_regression_unchanged_review_cli_221():
    """Regression guard: omitting --min-roles must reproduce the EXACT pre-#221
    result shape (no roles/distinct_roles_passed/min_roles keys) -- --min-models
    keeps strictly counting distinct model-name strings."""
    with _TmpStore():
        for model in ("codex", "gemini"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=2, min_models=3)
        assert result["passed"] is False
        assert result["distinct_models_passed"] == 2
        assert "roles" not in result
        assert "distinct_roles_passed" not in result
        assert "min_roles" not in result


def test_quorum_check_min_roles_suggestion_when_switching_would_actually_pass_review_cli_221():
    """The suggestion must fire ONLY when re-running with --min-roles at the SAME
    number would actually satisfy the gate -- round-1 review finding (Opus/Codex/
    Fable, independently): a bare 'models fell short' check suggests a flag that
    is guaranteed to fail worse when the history has no recorded roles at all.
    This scenario is the genuine PR #207 shape: 2 real distinct models plus a
    role-fill pass that reused one of them under a third role."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],  # duplicated model, distinct role
            roles=["performance"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["distinct_models_passed"] == 2
        assert "min_roles_suggestion" in result
        msg = result["min_roles_suggestion"]
        assert "3 distinct models required, only 2 found" in msg
        assert "--min-roles 3" in msg
        assert "PR #207" in msg
        # And the suggestion is honest: literally re-running with --min-roles 3
        # in PLACE of --min-models (as the hint's own wording says -- "try
        # --min-roles 3 instead") passes. review-cli#246: min_models is
        # deliberately OMITTED here -- explicitly keeping BOTH would now AND
        # them together and this exact history (2 distinct models) would fail
        # the still-explicit min_models=3 floor, which is not what the hint
        # is suggesting.
        by_roles = _stats.quorum_check(TASK, min_iter=3, min_roles=3)
        assert by_roles["passed"] is True, by_roles


def test_quorum_check_no_min_roles_suggestion_when_no_roles_recorded_at_all_review_cli_221():
    """The exact bug all three reviewers flagged: a task whose history has NO
    recorded roles (predates the field, or every iteration came from a mode with
    no role concept) must never be told to try --min-roles -- it is guaranteed to
    fail there too, just with less diagnostic detail."""
    with _TmpStore():
        for _ in range(3):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["distinct_models_passed"] == 1
        assert "min_roles_suggestion" not in result
        # Confirms the premise: --min-roles 3 on this exact history really does fail.
        by_roles = _stats.quorum_check(TASK, min_iter=3, min_models=3, min_roles=3)
        assert by_roles["passed"] is False


def test_quorum_check_no_min_roles_suggestion_when_iterations_also_short_review_cli_221():
    """Even with 3 distinct roles recorded, the suggestion must not fire if the
    ITERATION floor is also unmet -- switching counting mode can't add iterations,
    so --min-roles would fail on the iteration check too."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        # Only 2 passed iterations, but min_iter=3 -- the roles ARE distinct (2)
        # yet still short of min_models=3 too, so this also covers the "models
        # short" precondition; min_iter is the binding constraint under test.
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["passed_iterations"] == 2
        assert "min_roles_suggestion" not in result


def test_quorum_check_no_min_roles_suggestion_when_min_roles_already_used():
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1, min_roles=5)
        assert result["passed"] is False  # only 1 distinct role, need 5
        assert "min_roles_suggestion" not in result


def test_quorum_check_no_min_roles_suggestion_when_never_reviewed():
    """The suggestion must not fire for the unrelated 'no history at all' fail-
    closed case -- switching counting mode wouldn't help there either."""
    with _TmpStore():
        result = _stats.quorum_check("HYP-999-review-cli-221", min_iter=1, min_models=1)
        assert result["passed"] is False
        assert "error" in result
        assert "min_roles_suggestion" not in result


def test_quorum_check_min_roles_rejects_zero_or_negative():
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=0,
            fail_count=1,
            passed=False,
        )
        for min_roles in (0, -1):
            result = _stats.quorum_check(
                TASK, min_iter=1, min_models=1, min_roles=min_roles
            )
            assert result["passed"] is False, (min_roles, result)
            assert "error" in result, (min_roles, result)


def test_quorum_check_explicit_invalid_min_roles_is_named_in_message_review_cli_246():
    """k3 review finding: the symmetric counterpart of
    test_quorum_check_floor_validation_message_never_names_a_defaulted_min_roles
    -- when --min-roles WAS explicitly given (and invalid), the message MUST
    name it, since the whole point of `min_roles_explicit` gating the message
    builder is "only name what was actually part of the input", not "never
    name min_roles at all"."""
    result = _stats.quorum_check(TASK, min_iter=1, min_roles=0)
    assert result["passed"] is False
    assert "error" in result
    assert "min_roles=0" in result["error"]


def test_quorum_check_unknown_role_strings_never_satisfy_min_roles_review_cli_221():
    """Codex round-2 review finding: a config `board:` entry may carry an UNKNOWN
    role string (kept by `config._normalize_board_reviewer`, but degraded to the
    generic prompt -- no distinct lens -- with a warning logged). N seats that all
    reviewed under that SAME generic prompt must not satisfy --min-roles N as if
    they were N genuinely distinct facets -- only a real REVIEW_ROLES key counts."""
    with _TmpStore():
        for typo_role in ("architeckt", "Correctness", "made-up-role"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                roles=[typo_role],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=1, min_roles=3)
        assert result["passed"] is False
        assert result["roles"] == []
        assert result["distinct_roles_passed"] == 0


def test_quorum_check_min_roles_ignores_roles_on_failed_iterations_review_cli_221():
    """Fable round-2 review finding: a role recorded on a `passed=False` iteration
    must never count toward --min-roles -- the fail-closed core of the gate
    (mirrors the pre-existing `_distinct_models`/passed-only contract exactly)."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=0,
            fail_count=1,
            passed=False,  # this seat's role must NOT count
        )
        result = _stats.quorum_check(TASK, min_iter=1, min_models=1, min_roles=2)
        assert result["passed"] is False
        assert result["roles"] == ["architect"]
        assert result["distinct_roles_passed"] == 1


def test_quorum_check_min_roles_mixed_role_less_and_role_bearing_history_review_cli_221():
    """README's documented mixed-history contract: quorum/just-ask/brainstorm
    iterations (no role concept) count toward --min-iter but never toward
    --min-roles -- a task whose passed iterations combine both kinds meets the
    iteration floor from all of them, but the role floor only from the ones that
    actually carry roles."""
    with _TmpStore():
        # A role-less iteration (e.g. a `quorum` run) -- counts toward min_iter.
        _stats.record_run(
            task_code=TASK,
            mode="quorum",
            models=["codex", "gemini"],
            duration_seconds=1.0,
            ok_count=2,
            fail_count=0,
            passed=True,
        )
        # Two role-bearing `review` iterations.
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        # min_iter=3 is met by all three passed iterations combined.
        result = _stats.quorum_check(TASK, min_iter=3, min_models=1, min_roles=3)
        assert result["passed_iterations"] == 3
        # min_roles=3 is NOT met: only the 2 role-bearing iterations contribute.
        assert result["distinct_roles_passed"] == 2
        assert result["passed"] is False
        # A lower --min-roles that only the role-bearing iterations need to clear
        # DOES pass, proving the role-less iteration didn't silently help OR hurt.
        result2 = _stats.quorum_check(TASK, min_iter=3, min_models=1, min_roles=2)
        assert result2["passed"] is True, result2


def test_quorum_check_min_roles_excludes_diff_mismatched_iterations_review_cli_221():
    """Fable round-2 review finding: a passed iteration EXCLUDED by diff-identity
    verification (recorded repo/diff doesn't match the current check context) must
    not contribute its role either -- role counting sits downstream of the SAME
    verified/mismatched/unverifiable split `--min-models` already respects."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
            repo_id="repo-a",
            diff_files=["a.py"],
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
            repo_id="repo-b",  # a DIFFERENT repo -- excluded as mismatched
            diff_files=["b.py"],
        )
        result = _stats.quorum_check(
            TASK,
            min_iter=1,
            min_models=1,
            min_roles=2,
            repo_id="repo-a",
            diff_files=["a.py"],
        )
        assert result["passed"] is False
        assert result["roles"] == ["architect"]
        assert result["excluded_mismatched_iterations"] == 1


def test_quorum_check_no_min_roles_suggestion_for_single_model_under_distinct_roles_review_cli_221():
    """Opus round-3 review finding: the `len(models) >= 2`-style monoculture guard
    is load-bearing -- prove it directly. 3 passed iterations, all on the SAME
    single model, each under a genuinely distinct role: `--min-roles 3` on this
    history would pass with ZERO real model diversity (one model self-authorizing
    under different lenses) -- the suggestion must never steer a caller there."""
    with _TmpStore():
        for role in ("architect", "security", "performance"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                roles=[role],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["distinct_models_passed"] == 1
        assert "min_roles_suggestion" not in result
        # Confirms the premise: --min-roles 3 ALONE on this exact history WOULD
        # pass -- which is exactly why the hint must never point there. (min_models
        # is deliberately omitted here -- review-cli#246: an explicitly given
        # min_models is now always enforced too, which would fail this on the
        # model floor and defeat the point of this specific proof.)
        by_roles = _stats.quorum_check(TASK, min_iter=3, min_roles=3)
        assert by_roles["passed"] is True, by_roles


def test_quorum_check_no_min_roles_suggestion_when_role_coverage_traces_one_model_review_cli_221():
    """Fable round-3 review finding: the ORIGINAL `len(models) >= 2` guard checked
    model diversity across ALL passed iterations, not just the role-BEARING ones --
    a role-less iteration on a SECOND model let the guard pass even though 100% of
    the counted role coverage still traced back to a single other model. Exact
    scenario: one board run where codex alone covers 3 distinct roles, plus two
    role-less quorum runs on gemini (a different model, but contributing ZERO
    roles). `models` = {codex, gemini} (2, passes the OLD guard); the roles that
    would satisfy --min-roles all come from codex alone."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex", "codex", "codex"],
            roles=["architect", "security", "performance"],
            duration_seconds=1.0,
            ok_count=3,
            fail_count=0,
            passed=True,
        )
        for _ in range(2):
            _stats.record_run(
                task_code=TASK,
                mode="quorum",
                models=["gemini"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["distinct_models_passed"] == 2  # codex, gemini
        assert "min_roles_suggestion" not in result
        # Confirms the premise: --min-roles 3 ALONE on this history passes with
        # 100% of the role coverage traced to codex ALONE -- exactly what the
        # guard must never recommend. (min_models omitted -- see the sibling
        # test above for why keeping it would defeat this specific proof.)
        by_roles = _stats.quorum_check(TASK, min_iter=3, min_roles=3)
        assert by_roles["passed"] is True, by_roles


def test_quorum_check_no_min_roles_suggestion_when_unknown_role_shares_record_review_cli_221():
    """Round-4 review finding (Codex), a sharper version of the round-3 fix above:
    scoping the monoculture guard to "this ITERATION contributed >=1 valid role"
    is not enough either — a SINGLE multi-seat record can mix a valid-role seat
    with an unknown-role seat, and crediting every model in that record (not just
    the one that earned the valid role) lets the unknown-role seat's model launder
    in as "diverse". `models`/`roles` are index-aligned per seat, so the guard must
    pair them and credit only a model whose OWN role is valid.

    Record 1: codex earns "architect" (valid); gemini gets a typo'd/unknown role in
    the SAME record — gemini must NOT be credited as a role-earning model here.
    Records 2-3: codex alone earns "security"/"performance". `models` overall =
    {codex, gemini} (2, would pass a record-level-only guard); every valid role
    traces to codex alone."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex", "gemini"],
            roles=["architect", "not-a-real-role"],
            duration_seconds=1.0,
            ok_count=2,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["performance"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["distinct_models_passed"] == 2  # codex, gemini
        assert "min_roles_suggestion" not in result
        # Confirms the premise: --min-roles 3 ALONE on this history passes with
        # every valid role traced back to codex alone -- gemini earned nothing.
        # (min_models omitted -- see the sibling tests above for why.)
        by_roles = _stats.quorum_check(TASK, min_iter=3, min_roles=3)
        assert by_roles["passed"] is True, by_roles


def test_quorum_check_rejected_json_shape_includes_role_keys_when_min_roles_set_review_cli_221():
    """Fable round-3 review finding: `_rejected()` (the fail-closed early-return
    for an invalid task code / unreadable store / zero-iterations) was extended to
    carry `roles`/`distinct_roles_passed`/`min_roles` when `min_roles` is given --
    but nothing asserted that shape directly."""
    result = _stats.quorum_check("bad code", min_iter=1, min_models=1, min_roles=2)
    assert result["passed"] is False
    assert "error" in result
    assert result["roles"] == []
    assert result["distinct_roles_passed"] == 0
    assert result["min_roles"] == 2


def test_cli_check_met_prints_verdict_and_exits_0():
    """review-cli#246: this exercises the EXPLICIT --min-models path (the bare
    default switched to role-based counting -- see
    test_cli_check_bare_default_is_role_based_review_cli_246 for that case)."""
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--min-models", "3"])
        assert rc == 0, rc
        text = out.getvalue()
        assert "review bar met" in text
        assert TASK in text
        assert "3 passed iteration" in text and "3 distinct model" in text


def test_cli_check_bare_default_is_role_based_review_cli_246():
    """review-cli#246: with NO flags at all, --check now defaults to a ROLE-based
    check at the same floor (3) --min-models used to default to -- 3 distinct
    models with NO recorded roles must still fail, since role coverage (not
    model diversity) now governs by default."""
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check"])
        assert rc != 0, rc
        text = out.getvalue()
        assert "review bar NOT met" in text
        assert "0/3 distinct roles" in text
        # The model audit line still names which models actually reviewed.
        assert "models: 3 distinct model" in text


def test_cli_check_bare_default_passes_on_distinct_roles_review_cli_246():
    """The inverse of the above: 3 distinct board roles covered (even by fewer
    distinct models, PR #207's duplicated-model role-fill shape) satisfies the
    new bare default."""
    with _TmpStore():
        for model, role in (
            ("codex", "architect"),
            ("gemini", "security"),
            ("codex", "performance"),
        ):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                roles=[role],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check"])
        assert rc == 0, rc
        text = out.getvalue()
        assert "review bar met" in text
        assert "3 distinct role" in text


def test_cli_check_short_prints_ratio_and_exits_nonzero():
    """review-cli#246: explicit --min-models path -- see the bare-default tests
    above for the new role-based default."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex", "gemini"],
            duration_seconds=1.0,
            ok_count=2,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--min-models", "3"])
        assert rc != 0, rc
        text = out.getvalue()
        assert "review bar NOT met" in text
        assert "1/3 passed iterations" in text
        assert "2/3 distinct models" in text


def test_cli_check_short_prints_stalled_model_line_review_cli_221():
    """review-cli#221: the TEXT-mode `--check` output (not just --json) must name the
    specific attempted model that's currently cooling down — this is the path a human
    running `review task X --check` directly actually sees, distinct from ship.sh's
    own --json-driven refusal message (covered separately in agent-tools' test_ship.py)."""
    from reviewlib import seat_cooldown as _sc

    with tempfile.TemporaryDirectory() as d, _TmpStore():
        saved_cd_file = os.environ.get("REVIEW_SEAT_COOLDOWN_FILE")
        saved_cd_ttl = os.environ.get("REVIEW_SEAT_COOLDOWN_SECONDS")
        os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = str(Path(d) / "seat-cooldown.json")
        os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
        try:
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex", "oc:zai/glm-5.2"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=1,
                passed=False,
            )
            _sc.record_cooldown(
                "oc:zai/glm-5.2", "timed out", now=time.time(), ttl_seconds=1800.0
            )
            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                rc = _cli.main(["task", TASK, "--check"])
            assert rc != 0, rc
            text = out.getvalue()
            assert "review bar NOT met" in text
            assert "stalled: oc:zai/glm-5.2" in text
            assert "timed out" in text
        finally:
            if saved_cd_file is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_FILE", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_FILE"] = saved_cd_file
            if saved_cd_ttl is None:
                os.environ.pop("REVIEW_SEAT_COOLDOWN_SECONDS", None)
            else:
                os.environ["REVIEW_SEAT_COOLDOWN_SECONDS"] = saved_cd_ttl


def test_cli_check_ran_but_not_passed_does_not_satisfy_bar():
    """The exact scenario the fix targets: enough iterations RAN, none of them
    PASSED -- the old gate would have said "met", the new one must say "NOT met"."""
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=0,
                fail_count=1,
                passed=False,
            )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check"])
        assert rc != 0, rc
        text = out.getvalue()
        assert "review bar NOT met" in text
        assert "0/3 passed iterations" in text


def test_cli_check_custom_thresholds():
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex", "gemini"],
            duration_seconds=1.0,
            ok_count=2,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(
                ["task", TASK, "--check", "--min-iter", "1", "--min-models", "2"]
            )
        assert rc == 0, rc
        assert "review bar met" in out.getvalue()


def test_cli_check_missing_store_fails_closed_nonzero():
    saved = os.environ.get("REVIEW_STATS_FILE")
    os.environ["REVIEW_STATS_FILE"] = "/nonexistent-xyz/deeper/run-stats.jsonl"
    try:
        err = io.StringIO()
        with redirect_stderr(err), _capture_stdout():
            rc = _cli.main(["task", TASK, "--check"])
        assert rc != 0, rc
        assert "review bar NOT met" in err.getvalue()
    finally:
        if saved is None:
            os.environ.pop("REVIEW_STATS_FILE", None)
        else:
            os.environ["REVIEW_STATS_FILE"] = saved


def test_cli_check_zero_records_fails_closed_nonzero():
    with _TmpStore():
        err = io.StringIO()
        with redirect_stderr(err), _capture_stdout():
            rc = _cli.main(["task", "HYP-999", "--check"])
        assert rc != 0, rc
        assert "review bar NOT met" in err.getvalue()


def test_cli_check_invalid_task_code_text_mode_does_not_crash_review_cli_221():
    """review-cli#221 round-6 review finding (Opus AND Fable, independently, both
    flagging the same concern a 3rd/4th time despite it already being verified false
    by direct code reading and by test_quorum_check_invalid_task_code_fails_closed's
    own assertion that passed_iterations/distinct_models_passed are BOTH present on
    this exact _rejected() shape): drives the invalid-task-code early-return through
    the REAL CLI in TEXT mode specifically (not --json, which returns before the
    reordered count-key access; not quorum_check() directly, which the stats-level
    test above already covers) -- the one remaining `_rejected()` branch (alongside
    missing-store and zero-records, both already covered by the two sibling tests
    above) that hadn't been driven through _quorum_check_subcommand's actual
    print-then-return-1 path. If the reorder ever DID crash on a minimal error dict,
    this is the test that would catch it -- a KeyError, not an AssertionError."""
    with _TmpStore():
        err = io.StringIO()
        with redirect_stderr(err), _capture_stdout():
            rc = _cli.main(["task", "bad code", "--check"])
        assert rc != 0, rc
        assert "review bar NOT met" in err.getvalue()
        assert "invalid task code" in err.getvalue()


def test_cli_check_json_shape():
    """review-cli#246: explicit --min-models path -- see
    test_cli_check_json_shape_bare_default_review_cli_246 for the new default
    (role-based) JSON shape."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--json", "--min-models", "3"])
        assert rc != 0, rc  # short of 3/3
        payload = json.loads(out.getvalue())
        required = {
            "task_code",
            "passed_iterations",
            "total_iterations",
            "distinct_models_passed",
            "models",
            "min_iter",
            "min_models",
            "passed",
            "min_models_advisory",
        }
        assert required.issubset(payload.keys()), payload
        assert payload["task_code"] == TASK
        assert payload["passed_iterations"] == 1
        assert payload["total_iterations"] == 1
        assert payload["distinct_models_passed"] == 1
        assert payload["models"] == ["codex"]
        assert payload["min_iter"] == 3
        assert payload["min_models"] == 3
        assert payload["passed"] is False
        assert "roles" not in payload


def test_cli_check_json_shape_bare_default_review_cli_246():
    """review-cli#246: with NO flags, the JSON shape carries role keys (the new
    default) and NO `min_models`/`min_models_advisory` keys at all -- the model
    floor is not enforced, and the advisory only appears when min_models was
    explicitly given."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--json"])
        assert rc != 0, rc  # short of 3/3 roles
        payload = json.loads(out.getvalue())
        assert payload["min_iter"] == 3
        assert payload["min_roles"] == 3
        assert payload["distinct_roles_passed"] == 1
        assert payload["roles"] == ["architect"]
        assert "min_models" not in payload
        assert "min_models_advisory" not in payload


def test_cli_check_json_exits_0_when_passed():
    """review-cli#246: explicit --min-models path."""
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--json", "--min-models", "3"])
        assert rc == 0, rc
        assert json.loads(out.getvalue())["passed"] is True


def test_cli_check_requires_code():
    err = io.StringIO()
    with redirect_stderr(err), _capture_stdout():
        rc = _cli.main(["task", "--check"])
    assert rc == 2, rc
    assert "requires a task CODE" in err.getvalue()


def test_cli_check_rejects_zero_min_iter():
    """--min-iter 0 would trivially satisfy the bar (0 >= 0) for a task whose every
    recorded iteration failed or predates the verdict field -- defeating the whole
    fail-closed point of this gate. Both floors must be >= 1 (codex review finding
    on this same change)."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=0,
            fail_count=1,
            passed=False,
        )
        err = io.StringIO()
        with redirect_stderr(err), _capture_stdout():
            rc = _cli.main(["task", TASK, "--check", "--min-iter", "0"])
        assert rc == 2, rc
        assert "--min-iter" in err.getvalue() and ">= 1" in err.getvalue()


def test_cli_check_rejects_zero_min_models():
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        err = io.StringIO()
        with redirect_stderr(err), _capture_stdout():
            rc = _cli.main(["task", TASK, "--check", "--min-models", "0"])
        assert rc == 2, rc
        assert "--min-models" in err.getvalue() and ">= 1" in err.getvalue()


# ---------------------------------------------------------------------------
# `review task CODE --check --min-roles N` (review-cli#221)
# ---------------------------------------------------------------------------
def test_cli_check_min_roles_governs_gate_when_given_review_cli_221():
    """The core review-cli#221 scenario, driven through the real CLI: 2 real
    distinct models plus one duplicated-model role-fill pass (the board's #207
    shortage-resilience behavior) -- --min-roles 3 passes even though the default
    --min-models 3 does not, reading the SAME recorded history."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        # Duplicated-model role-fill: repeats "codex" from iteration 1, under a
        # DIFFERENT role -- exactly what config.select_pool_with_reuse (PR #207)
        # produces when too few distinct models are available.
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["performance"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )

        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--min-roles", "3"])
        assert rc == 0, out.getvalue()
        text = out.getvalue()
        assert "review bar met" in text
        assert "3 distinct role" in text
        # Fable review finding: role mode's headline drops model names -- a
        # secondary audit line must still name which models actually reviewed.
        assert "models: 2 distinct model" in text
        assert "codex" in text and "gemini" in text

        # Same history, default --min-models 3: only 2 distinct model strings.
        out2 = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out2):
            rc2 = _cli.main(["task", TASK, "--check", "--min-models", "3"])
        assert rc2 != 0, out2.getvalue()
        assert "2/3 distinct models" in out2.getvalue()


def test_cli_check_min_roles_not_met_shows_model_audit_line_review_cli_221():
    """The model-audit line (Fable review finding) must also appear on the
    NOT-met path, not just the met path -- an operator debugging a --min-roles
    denial still needs to see which models actually ran."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(
                ["task", TASK, "--check", "--min-iter", "2", "--min-roles", "3"]
            )
        assert rc != 0, out.getvalue()
        text = out.getvalue()
        assert "review bar NOT met" in text
        assert "2/3 distinct roles" in text
        assert "models: 2 distinct model" in text
        assert "codex" in text and "gemini" in text


def test_cli_check_min_models_default_behavior_unchanged_review_cli_221():
    """Regression guard: review-cli#221 must not change --min-models' own
    text-mode output/exit code. review-cli#246 changed the BARE-CHECK default
    to role-based, so --min-models must now be passed explicitly to exercise
    this path (see test_cli_check_bare_default_is_role_based_review_cli_246
    for the new default's own text-mode shape)."""
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=[model],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--min-models", "3"])
        assert rc == 0, rc
        text = out.getvalue()
        assert "review bar met" in text
        assert "3 distinct model" in text
        # review-cli#246: an explicit --min-models now always prints the
        # non-blocking advisory, which itself mentions "role-based coverage" --
        # so the regression guard here is "role-mode never GOVERNED" (no
        # "distinct role" counting line), not "the word role never appears".
        assert "distinct role" not in text


def test_cli_check_min_models_short_shows_min_roles_suggestion_review_cli_221():
    """When --min-models fails because model diversity is short AND switching to
    --min-roles at the same number would actually pass, the text-mode denial must
    suggest it, with concrete counts. --min-models is now passed EXPLICITLY
    (review-cli#246: the bare-check default switched to role-based, which would
    never reach this model-only code path at all)."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],  # duplicated model, distinct role
            roles=["performance"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--min-models", "3"])
        assert rc != 0, rc
        text = out.getvalue()
        assert "review bar NOT met" in text
        assert "hint:" in text
        assert "3 distinct models required, only 2 found" in text
        assert "--min-roles 3" in text
        assert "PR #207" in text


def test_cli_check_min_models_short_no_suggestion_when_roles_wouldnt_help_review_cli_221():
    """Regression for the round-1 review finding: a role-less history must NOT
    get the --min-roles hint -- it would only fail again with less detail.
    --min-models is explicit (review-cli#246: see the sibling test above)."""
    with _TmpStore():
        for _ in range(3):
            _stats.record_run(
                task_code=TASK,
                mode="review",
                models=["codex"],
                duration_seconds=1.0,
                ok_count=1,
                fail_count=0,
                passed=True,
            )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--min-models", "3"])
        assert rc != 0, rc
        text = out.getvalue()
        assert "review bar NOT met" in text
        assert "hint:" not in text
        assert "--min-roles" not in text


def test_cli_check_rejects_zero_min_roles():
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        err = io.StringIO()
        with redirect_stderr(err), _capture_stdout():
            rc = _cli.main(["task", TASK, "--check", "--min-roles", "0"])
        assert rc == 2, rc
        assert "--min-roles" in err.getvalue() and ">= 1" in err.getvalue()


def test_cli_check_min_roles_json_shape_review_cli_221():
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(
                [
                    "task",
                    TASK,
                    "--check",
                    "--json",
                    "--min-iter",
                    "1",
                    "--min-roles",
                    "1",
                ]
            )
        assert rc == 0, out.getvalue()
        payload = json.loads(out.getvalue())
        assert payload["passed"] is True
        assert payload["roles"] == ["architect"]
        assert payload["distinct_roles_passed"] == 1
        assert payload["min_roles"] == 1


def test_cli_check_min_roles_error_path_does_not_crash_review_cli_221():
    """--min-roles against a fail-closed ERROR path (invalid task code) must not
    crash and must route to stderr, exactly like the --min-models error path --
    `_print_quorum_bar_not_met`'s error branch never touches the role-only result
    keys (`roles`/`distinct_roles_passed`/`min_roles`), which the `_rejected()`
    shape omits entirely when they're absent from a `KeyError` risk (Fable/Opus
    review finding: this combination had no coverage)."""
    with _TmpStore():
        err = io.StringIO()
        with redirect_stderr(err), _capture_stdout():
            rc = _cli.main(["task", "bad code", "--check", "--min-roles", "2"])
        assert rc != 0, rc
        assert "review bar NOT met" in err.getvalue()
        assert "invalid task code" in err.getvalue()


def test_print_quorum_bar_not_met_mismatch_error_shows_model_audit_line_review_cli_221():
    """Fable round-6 review finding: the `distinct_models_passed > 0` gating in
    `_print_quorum_bar_not_met` (added for the Codex round-5 finding) exists
    precisely so a diff-identity MISMATCH denial -- which still carries real
    counts from the non-mismatched iterations -- keeps its audit line. The only
    prior error-path test used an invalid task code (`distinct_models_passed`
    == 0 there, where the line IS correctly suppressed), so a regression back to
    gating on bare `"error" in result` would still pass the rest of the suite."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
            repo_id="repo-a",
            diff_files=["a.py"],
        )
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["gemini"],
            roles=["security"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
            repo_id="repo-b",  # a DIFFERENT repo -- excluded as mismatched
            diff_files=["b.py"],
        )
        result = _stats.quorum_check(
            TASK,
            min_iter=1,
            min_models=1,
            min_roles=2,  # only "architect" survives verification -- not met
            repo_id="repo-a",
            diff_files=["a.py"],
        )
        assert result["passed"] is False
        assert "error" in result
        assert result["distinct_models_passed"] == 1  # real data survives exclusion

        err = io.StringIO()
        with redirect_stderr(err):
            _cli._print_quorum_bar_not_met(result, True, True)
        text = err.getvalue()
        assert "review bar NOT met" in text
        assert "models: 1 distinct model (codex)" in text


def test_cli_check_min_roles_json_shape_not_met_review_cli_221():
    """Opus round-2 review finding: the existing JSON-shape test only covers the
    PASSING role-mode path -- `roles`/`distinct_roles_passed`/`min_roles` must
    also be present (and correct) alongside `passed: false`."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(
                [
                    "task",
                    TASK,
                    "--check",
                    "--json",
                    "--min-iter",
                    "1",
                    "--min-roles",
                    "3",
                ]
            )
        assert rc != 0, out.getvalue()
        payload = json.loads(out.getvalue())
        assert payload["passed"] is False
        assert payload["roles"] == ["architect"]
        assert payload["distinct_roles_passed"] == 1
        assert payload["min_roles"] == 3


def test_cli_check_both_explicit_min_models_always_enforced_review_cli_246():
    """review-cli#246 (the PR #246 codex review finding, fixed here): when BOTH
    floors are given explicitly, an explicit --min-models is now ALWAYS
    enforced, AND'd with --min-roles -- it can no longer be silently outvoted.
    Before this fix, --min-roles governed alone and this exact input (5 model
    floor, only 1 distinct model reviewing) returned passed=True."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        err = io.StringIO()
        out = io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = _cli.main(
                [
                    "task",
                    TASK,
                    "--check",
                    "--min-iter",
                    "1",
                    "--min-models",
                    "5",  # the bug: only 1 distinct model reviewing
                    "--min-roles",
                    "1",  # satisfied on its own, but must NOT outvote min_models
                ]
            )
        assert rc != 0, (out.getvalue(), err.getvalue())
        assert "review bar NOT met" in out.getvalue()
        text = out.getvalue()
        assert "1/5 distinct models" in text
        assert "1/1 distinct roles" in text
        # The non-blocking advisory still appears (it never affects `passed`).
        assert "note:" in text
        assert "role-based coverage" in text


def test_cli_check_both_explicit_and_both_satisfied_passes_review_cli_246():
    """The positive case of the same AND logic: BOTH explicit floors met."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(
                [
                    "task",
                    TASK,
                    "--check",
                    "--min-iter",
                    "1",
                    "--min-models",
                    "1",
                    "--min-roles",
                    "1",
                ]
            )
        assert rc == 0, out.getvalue()
        text = out.getvalue()
        assert "review bar met" in text
        assert "1 distinct role" in text
        assert "1 distinct model" in text


def test_cli_check_min_models_advisory_absent_when_only_min_roles_given_review_cli_246():
    """review-cli#246: the `min_models_advisory` note must NOT fire when
    --min-models was never typed (only --min-roles was given) -- the advisory
    is specifically about an EXPLICIT model floor, not about role mode itself."""
    with _TmpStore():
        _stats.record_run(
            task_code=TASK,
            mode="review",
            models=["codex"],
            roles=["architect"],
            duration_seconds=1.0,
            ok_count=1,
            fail_count=0,
            passed=True,
        )
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(
                ["task", TASK, "--check", "--min-iter", "1", "--min-roles", "1"]
            )
        assert rc == 0, out.getvalue()
        assert "note:" not in out.getvalue()


# ---------------------------------------------------------------------------
# stdout capture helper (keep the mode's printed review output off the test log)
# ---------------------------------------------------------------------------
import contextlib  # noqa: E402


@contextlib.contextmanager
def _capture_stdout():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


# A missing ImageMagick `magick` v7 binary is a fatal CV error by design (cv_gate.py hard-
# requires it), so the one test below that drives the real `review visual` CLI path must SKIP
# rather than fail on a host without it — same convention as test_qa_env.py's _Skip/_skip and
# the sibling test_visual_verification_suite gate in smoke.py.
class _Skip(Exception):
    pass


def _skip(reason: str):
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest  # noqa: PLC0415

        pytest.skip(reason)
    raise _Skip(reason)


if __name__ == "__main__":
    failures = 0
    skipped = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with identity_provider_chain():
                    fn()
                print(f"PASS {name}")
            except _Skip as exc:
                skipped += 1
                print(f"SKIP {name}: {exc}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(
        f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s), {skipped} skipped"
    )
    sys.exit(1 if failures else 0)
