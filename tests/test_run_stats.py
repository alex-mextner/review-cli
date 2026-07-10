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
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli as _cli  # noqa: E402
from reviewlib import panel as _panel  # noqa: E402
from reviewlib import stats as _stats  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.install import SKILL_BLURB, SKILL_MD  # noqa: E402

TASK = "HYP-742"
TASK_ARGS = ["--task", TASK]


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
        return [json.loads(ln) for ln in self.path.read_text().splitlines() if ln.strip()]


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
        def backend(m, prompt, diff, cwd, timeout, round_no=0):
            rc = rc_by_model if isinstance(rc_by_model, int) else rc_by_model.get(m, 0)
            return ReviewResult(model=m, command=f"stub {m}", returncode=rc,
                                stdout=f"output from {m}", stderr="")
        return backend
    return resolver


def _with_backend_stub(resolver):
    """Swap resolve_backend in BOTH namespaces that dispatch backends.

    panel.run_panel (just-ask/quorum/brainstorm/board) and the plain `-m` path in
    modes.review each import resolve_backend into their own module namespace, so a
    stub must replace both. Returns a restore fn.
    """
    from reviewlib.modes import review as _review_mode
    saved_panel = _panel.resolve_backend
    saved_review = _review_mode.resolve_backend
    _panel.resolve_backend = resolver
    _review_mode.resolve_backend = resolver

    def restore():
        _panel.resolve_backend = saved_panel
        _review_mode.resolve_backend = saved_review
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


def test_task_summaries_group_iterations_and_models():
    with _TmpStore():
        _stats.record_run(task_code="HYP-742", mode="review", models=["codex"],
                          duration_seconds=10, ok_count=1, fail_count=0)
        _stats.record_run(task_code="HYP-742", mode="quorum", models=["codex", "gemini"],
                          duration_seconds=20, ok_count=2, fail_count=0)
        _stats.record_run(task_code="HYP-999", mode="review", models=["claude"],
                          duration_seconds=30, ok_count=1, fail_count=0)
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
        _stats.record_run(task_code="HYP-742", mode="review", models=["codex"],
                          duration_seconds=10, ok_count=1, fail_count=0)
        _stats.record_run(task_code="HYP-999", mode="quorum", models=["gemini"],
                          duration_seconds=20, ok_count=1, fail_count=0)
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
        _stats.record_run(task_code=TASK, mode="review", models=["codex"], duration_seconds=1.0,
                          ok_count=1, fail_count=0)
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
        _stats.record_run(task_code=TASK, mode="review", models=["codex"], duration_seconds=1.0,
                          ok_count=1, fail_count=0)
        assert store.path.stat().st_mode & 0o777 == 0o600, oct(store.path.stat().st_mode & 0o777)
        assert len(store.records()) == 1


def test_record_run_appends_not_truncates():
    with _TmpStore() as store:
        _stats.record_run(task_code=TASK, mode="review", models=["a"], duration_seconds=1.0, ok_count=1, fail_count=0)
        _stats.record_run(task_code=TASK, mode="review", models=["a", "b"], duration_seconds=2.0, ok_count=2, fail_count=0)
        assert len(store.records()) == 2


# ---------------------------------------------------------------------------
# estimate_eta + eta_line: (mode,pool) primary, pool-only, no-history
# ---------------------------------------------------------------------------
def test_eta_mode_plus_pool_average():
    with _TmpStore():
        for secs in (360, 380, 400):
            _stats.record_run(mode="brainstorm", models=["a", "b", "c", "d"],
                              duration_seconds=secs, ok_count=4, fail_count=0)
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
            _stats.record_run(mode="review", models=["a", "b", "c", "d"],
                              duration_seconds=secs, ok_count=4, fail_count=0)
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
        assert _stats.record_run(mode="review", models=["a"], duration_seconds=1.0,
                                 ok_count=1, fail_count=0) is False
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
                rc = _cli.main(["diff", "--task", bad, "-C", str(REPO_ROOT), "-m", "codex"])
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
            assert "do NOT timeout" in err.getvalue() or "Do NOT timeout" in err.getvalue()
            # Exactly one stat record with the real mode + pool + per-call counts.
            recs = store.records()
            assert len(recs) == 1, recs
            r = recs[0]
            assert r["task_code"] == TASK
            assert r["mode"] == "review"
            assert r["pool_size"] == 2
            assert sorted(r["models"]) == ["codex", "gemini"]
            assert r["ok_count"] == 2 and r["fail_count"] == 0
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


def test_cli_standalone_visual_does_not_record_review_task_code_env():
    if not shutil.which("magick"):
        _skip("standalone `review visual` drives the real cvGate, which hard-requires "
              "ImageMagick v7's `magick` binary (absent on this host) — same gate as "
              "test_visual_verification_suite in smoke.py.")
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
    """Run a default board `review` (all 9 seats available) with `extra_argv` appended,
    returning the single run-stats record + the captured stderr under key "_stderr".
    The board is pinned to DEFAULT_BOARD and config to {} so the test is independent of
    the dev machine's config.yaml; backends are stubbed (no model call)."""
    from reviewlib import backends as _backends
    from reviewlib.config import DEFAULT_BOARD
    from reviewlib.modes import review as _review_mode

    with _TmpStore() as store:
        d = _git_init_with_diff()
        restore = _with_backend_stub(_stub_resolve_backend(0))
        # Force EVERY seat available in all three namespaces that probe it: the CLI
        # (planned-pool ETA slice), panel.build_board_jobs, and the failover pool's
        # startup split inside modes.review (which imports backend_available into its
        # own namespace, like resolve_backend). All 9 seats available -> the top-4
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
        _cli.load_board = lambda _cfg: list(DEFAULT_BOARD)
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
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
            d.cleanup()


def test_cli_default_board_run_records_pool_size_four():
    """A default `review` (no -m, no config models) runs the board sized to the default
    pool (4 seats); the run-stats record must report pool_size == 4, NOT the full 9
    (the slice must feed run-stats, not the pre-slice board)."""
    r = _run_board_review_and_get_record([])  # default pool = 4
    assert r["mode"] == "review"
    assert r["pool_size"] == 4, r  # the SLICED board, not the full 9
    assert len(r["models"]) == 4, r
    assert "[review] pool=4 (review)" in r["_stderr"]


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
    seat can be made to FAIL and trigger failover). All 9 seats are env-available, so the
    startup pool fills cleanly and the mid-run failover does the backfilling. Returns the
    single run-stats record + captured stderr under "_stderr"; tolerates exit 1 (the
    degraded path)."""
    from reviewlib import backends as _backends
    from reviewlib.config import DEFAULT_BOARD
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
        _cli.load_board = lambda _cfg: list(DEFAULT_BOARD)
        os.environ["REVIEW_LOG_DIR"] = tempfile.mkdtemp()
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
            d.cleanup()


def test_cli_failover_backfill_records_actual_models_not_planned():
    """When a startup-pool seat FAILS mid-run, the CLI must record the models that
    ACTUALLY produced verdicts (a backfilled reserve under its real id), not the planned
    pool. The default pool of 4 is now [Fable, Opus, GLM-cc, Codex]; the top seat (Fable)
    fails -> the first reserve, Kimi (#5, `oc:commandcode/...`), backfills, so the recorded
    models include the agentic Kimi id and EXCLUDE fable, pool_size stays 4, exit 0."""
    # Fable (priority #1, in the default pool of 4) fails; everything else succeeds.
    resolver = _stub_resolve_backend({"claude:claude-fable-5": 1})
    r = _run_board_review_with_resolver([], resolver)
    assert r["_rc"] == 0, r
    assert r["mode"] == "review"
    assert r["pool_size"] == 4, r            # backfilled back up to 4
    assert "claude:claude-fable-5" not in r["models"], r
    # The promoted reserve is the first reserve seat (Kimi, #5), recorded by its real id.
    assert "oc:commandcode/moonshotai/Kimi-K2.7-Code" in r["models"], r
    # The priority-3 GLM-cc seat is in the planned pool itself (it didn't fail), so it is
    # recorded directly — proving the new seat participates in a default run.
    assert "commandcode:zai-org/GLM-5.2" in r["models"], r
    assert "[review] pool=4 (review)" in r["_stderr"]  # ETA still keys on the planned 4
    assert "promoting reserve" in r["_stderr"]          # failover actually fired


def test_cli_failover_exhausted_reserve_degrades_exit_1():
    """When the reserve can't refill the pool (every commandcode + everything but a few
    fail), the run degrades: exit 1, a degraded message on stderr, and the record holds
    only the seats that produced verdicts."""
    # Fail everything EXCEPT opus + gemini -> only 2 usable, reserve can't reach 4.
    from reviewlib.config import DEFAULT_BOARD

    ok = {"claude:claude-opus-4-8", "gemini"}
    resolver = _stub_resolve_backend(
        {r.model: (0 if r.model in ok else 1) for r in DEFAULT_BOARD}
    )
    r = _run_board_review_with_resolver([], resolver)
    assert r["_rc"] == 1, r
    assert "degraded" in r["_stderr"], r["_stderr"]
    assert set(r["models"]) == ok, r        # only the seats that produced verdicts
    assert r["pool_size"] == 2, r


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
                rc = _cli.main([
                    "diff", *TASK_ARGS,
                    "-C", d.name,
                    "-m", "codex",
                    "-m", "gemini",
                ])
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


def test_cli_second_run_eta_uses_first_runs_history():
    with _TmpStore() as store:
        # Seed one review/2 record, then a fresh review/2 run must announce a
        # mode+pool ETA computed from it (basis "this size", not "no history").
        _stats.record_run(mode="review", models=["codex", "gemini"],
                          duration_seconds=90.0, ok_count=2, fail_count=0)
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
            _stats.record_run(task_code=TASK, mode="review", models=["codex"],
                              duration_seconds=1.2, ok_count=1, fail_count=0,
                              started=started)
            write_sidecar_log(
                "codex", round_no=0, argv0="codex", returncode=0,
                stdout="TRANSCRIPT-LINE from codex\n", stderr="", started=started,
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
            _stats.record_run(task_code=TASK, mode="review", models=["codex"],
                              duration_seconds=1.0, ok_count=1, fail_count=0,
                              started=first)
            _stats.record_run(task_code=TASK, mode="review", models=["gemini"],
                              duration_seconds=1.0, ok_count=1, fail_count=0,
                              started=second)
            write_sidecar_log(
                "gemini", round_no=0, argv0="gemini", returncode=0,
                stdout="SECOND-ITERATION-TRANSCRIPT\n", stderr="", started=second,
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
                _cli.main(["brainstorm", "topic", *TASK_ARGS, "-C", d.name, "-m", "codex,gemini",
                           "--rounds", "1", "--max-rounds", "1"])
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
    p.run_single = lambda model, prompt, cwd, timeout, diff="", round_no=0: ReviewResult(
        model=model, command="stub", returncode=0, stdout="", stderr="")
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
            return ReviewResult(model=model, command="stub", returncode=1, stdout="", stderr="boom")
        return ReviewResult(model=model, command="stub", returncode=0, stdout="synthesis", stderr="")

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
    assert "eta" in low or "prints a one-line eta" in low or "expected duration" in low or "pool size" in low


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
# ---------------------------------------------------------------------------
def test_quorum_check_met():
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(task_code=TASK, mode="review", models=[model],
                              duration_seconds=1.0, ok_count=1, fail_count=0)
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is True
        assert result["iterations"] == 3
        assert result["distinct_models"] == 3
        assert result["models"] == ["codex", "fable5", "gemini"]
        assert "error" not in result
        # Contract: the returned dict has a stable shape + value types (Gemini review).
        assert set(result) == {"task_code", "iterations", "distinct_models", "models",
                               "min_iter", "min_models", "passed"}
        assert isinstance(result["task_code"], str)
        assert isinstance(result["iterations"], int)
        assert isinstance(result["distinct_models"], int)
        assert isinstance(result["models"], list)
        assert all(isinstance(m, str) for m in result["models"])
        assert isinstance(result["min_iter"], int)
        assert isinstance(result["min_models"], int)
        assert isinstance(result["passed"], bool)


def test_quorum_check_short_on_iterations():
    with _TmpStore():
        # 2 runs, but 3 distinct models across them (>= min_models) -- only iterations short.
        _stats.record_run(task_code=TASK, mode="review", models=["codex", "gemini"],
                          duration_seconds=1.0, ok_count=2, fail_count=0)
        _stats.record_run(task_code=TASK, mode="review", models=["fable5"],
                          duration_seconds=1.0, ok_count=1, fail_count=0)
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["iterations"] == 2
        assert result["distinct_models"] == 3
        assert "error" not in result


def test_quorum_check_short_on_models():
    with _TmpStore():
        # 3 runs, but the SAME model each time -- only distinct_models short.
        for _ in range(3):
            _stats.record_run(task_code=TASK, mode="review", models=["codex"],
                              duration_seconds=1.0, ok_count=1, fail_count=0)
        result = _stats.quorum_check(TASK, min_iter=3, min_models=3)
        assert result["passed"] is False
        assert result["iterations"] == 3
        assert result["distinct_models"] == 1
        assert "error" not in result


def test_quorum_check_zero_records_fails_closed():
    with _TmpStore():
        # Write a record for a DIFFERENT task first so the store exists and is
        # readable -- isolates "store readable, zero records for THIS code" from
        # "store missing/unreadable" (covered separately below).
        _stats.record_run(task_code="HYP-000", mode="review", models=["codex"],
                          duration_seconds=1.0, ok_count=1, fail_count=0)
        result = _stats.quorum_check("HYP-999", min_iter=1, min_models=1)
        assert result["passed"] is False
        assert result["iterations"] == 0
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
    assert result["iterations"] == 0 and result["distinct_models"] == 0


def test_cli_check_met_prints_verdict_and_exits_0():
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(task_code=TASK, mode="review", models=[model],
                              duration_seconds=1.0, ok_count=1, fail_count=0)
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check"])
        assert rc == 0, rc
        text = out.getvalue()
        assert "review bar met" in text
        assert TASK in text
        assert "3 iteration" in text and "3 distinct model" in text


def test_cli_check_short_prints_ratio_and_exits_nonzero():
    with _TmpStore():
        _stats.record_run(task_code=TASK, mode="review", models=["codex", "gemini"],
                          duration_seconds=1.0, ok_count=2, fail_count=0)
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check"])
        assert rc != 0, rc
        text = out.getvalue()
        assert "review bar NOT met" in text
        assert "1/3 iterations" in text
        assert "2/3 distinct models" in text


def test_cli_check_custom_thresholds():
    with _TmpStore():
        _stats.record_run(task_code=TASK, mode="review", models=["codex", "gemini"],
                          duration_seconds=1.0, ok_count=2, fail_count=0)
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--min-iter", "1", "--min-models", "2"])
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


def test_cli_check_json_shape():
    with _TmpStore():
        _stats.record_run(task_code=TASK, mode="review", models=["codex"],
                          duration_seconds=1.0, ok_count=1, fail_count=0)
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--json"])
        assert rc != 0, rc  # short of default 3/3
        payload = json.loads(out.getvalue())
        required = {"task_code", "iterations", "distinct_models", "models", "min_iter",
                    "min_models", "passed"}
        assert required.issubset(payload.keys()), payload
        assert payload["task_code"] == TASK
        assert payload["iterations"] == 1
        assert payload["distinct_models"] == 1
        assert payload["models"] == ["codex"]
        assert payload["min_iter"] == 3
        assert payload["min_models"] == 3
        assert payload["passed"] is False


def test_cli_check_json_exits_0_when_passed():
    with _TmpStore():
        for model in ("codex", "gemini", "fable5"):
            _stats.record_run(task_code=TASK, mode="review", models=[model],
                              duration_seconds=1.0, ok_count=1, fail_count=0)
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            rc = _cli.main(["task", TASK, "--check", "--json"])
        assert rc == 0, rc
        assert json.loads(out.getvalue())["passed"] is True


def test_cli_check_requires_code():
    err = io.StringIO()
    with redirect_stderr(err), _capture_stdout():
        rc = _cli.main(["task", "--check"])
    assert rc == 2, rc
    assert "requires a task CODE" in err.getvalue()


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
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s), {skipped} skipped")
    sys.exit(1 if failures else 0)
