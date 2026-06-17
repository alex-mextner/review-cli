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
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli as _cli  # noqa: E402
from reviewlib import panel as _panel  # noqa: E402
from reviewlib import stats as _stats  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.install import SKILL_BLURB, SKILL_MD  # noqa: E402


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
        assert r["mode"] == "brainstorm"
        assert r["pool_size"] == 3
        assert r["models"] == ["codex", "gemini", "claude"]
        assert r["duration_seconds"] == 372.5
        assert r["ok_count"] == 9 and r["fail_count"] == 1
        assert "ts" in r
        # NO secrets/keys/prompts — model names only.
        blob = json.dumps(r)
        assert "prompt" not in blob and "api" not in blob.lower()


def test_record_run_file_is_0600():
    with _TmpStore() as store:
        _stats.record_run(mode="review", models=["codex"], duration_seconds=1.0,
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
        _stats.record_run(mode="review", models=["codex"], duration_seconds=1.0,
                          ok_count=1, fail_count=0)
        assert store.path.stat().st_mode & 0o777 == 0o600, oct(store.path.stat().st_mode & 0o777)
        assert len(store.records()) == 1


def test_record_run_appends_not_truncates():
    with _TmpStore() as store:
        _stats.record_run(mode="review", models=["a"], duration_seconds=1.0, ok_count=1, fail_count=0)
        _stats.record_run(mode="review", models=["a", "b"], duration_seconds=2.0, ok_count=2, fail_count=0)
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
                rc = _cli.main(["diff", "-C", d.name, "-m", "codex,gemini"])
            assert rc == 0, rc
            # ETA line went to stderr.
            assert "[review] pool=2 (review)" in err.getvalue()
            assert "do NOT timeout" in err.getvalue() or "Do NOT timeout" in err.getvalue()
            # Exactly one stat record with the real mode + pool + per-call counts.
            recs = store.records()
            assert len(recs) == 1, recs
            r = recs[0]
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


def _run_board_review_and_get_record(extra_argv: list[str]) -> dict:
    """Run a default board `review` (all 8 seats available) with `extra_argv` appended,
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
        # own namespace, like resolve_backend). All 8 seats available -> the top-4
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
                rc = _cli.main(["diff", "-C", d.name, *extra_argv])
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
    pool (4 seats); the run-stats record must report pool_size == 4, NOT the full 8
    (the slice must feed run-stats, not the pre-slice board)."""
    r = _run_board_review_and_get_record([])  # default pool = 4
    assert r["mode"] == "review"
    assert r["pool_size"] == 4, r  # the SLICED board, not the full 8
    assert len(r["models"]) == 4, r
    assert "[review] pool=4 (review)" in r["_stderr"]


def test_cli_board_run_records_explicit_pool_size():
    """`--pool 2` must record pool_size == 2 (not 4, not 8): the slice feeds run-stats
    at arbitrary sizes, not just the default (GLM finding 23)."""
    r = _run_board_review_and_get_record(["--pool", "2"])
    assert r["mode"] == "review"
    assert r["pool_size"] == 2, r
    assert len(r["models"]) == 2, r
    assert "[review] pool=2 (review)" in r["_stderr"]


def _run_board_review_with_resolver(extra_argv: list[str], resolver) -> dict:
    """Like _run_board_review_and_get_record but with a custom resolve_backend stub (so a
    seat can be made to FAIL and trigger failover). All 8 seats are env-available, so the
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
                rc = _cli.main(["diff", "-C", d.name, *extra_argv])
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
    pool. The top priority seat (Fable) fails -> GLM (the 5th, first reserve) backfills,
    so the recorded models include glm-5.2 and EXCLUDE fable, pool_size stays 4, exit 0."""
    # Fable (priority #1, in the default pool of 4) fails; everything else succeeds.
    resolver = _stub_resolve_backend({"claude:claude-fable-5": 1})
    r = _run_board_review_with_resolver([], resolver)
    assert r["_rc"] == 0, r
    assert r["mode"] == "review"
    assert r["pool_size"] == 4, r            # backfilled back up to 4
    assert "claude:claude-fable-5" not in r["models"], r
    assert "oc:zai/glm-5.2" in r["models"], r   # the promoted reserve (agentic GLM), by its real id
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


def test_cli_records_failure_counts_per_call():
    with _TmpStore() as store:
        d = _git_init_with_diff()
        # codex ok, gemini fails -> ok_count=1, fail_count=1, overall exit 1.
        restore = _with_backend_stub(_stub_resolve_backend({"codex": 0, "gemini": 2}))
        log = tempfile.mkdtemp()
        os.environ["REVIEW_LOG_DIR"] = log
        try:
            with redirect_stderr(io.StringIO()), _capture_stdout():
                rc = _cli.main(["diff", "-C", d.name, "-m", "codex,gemini"])
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
                rc = _cli.main(["diff", "-C", d.name, "-m", "codex,gemini"])
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
                _cli.main(["diff", "-C", d.name, "-m", "codex,gemini"])
            assert "past run" in err.getvalue() and "this size" in err.getvalue()
            assert len(store.records()) == 2  # seed + this run
        finally:
            restore()
            os.environ.pop("REVIEW_LOG_DIR", None)
            d.cleanup()


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
                _cli.main(["brainstorm", "topic", "-C", d.name, "-m", "codex,gemini",
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


# ---------------------------------------------------------------------------
# stdout capture helper (keep the mode's printed review output off the test log)
# ---------------------------------------------------------------------------
import contextlib  # noqa: E402


@contextlib.contextmanager
def _capture_stdout():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


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
