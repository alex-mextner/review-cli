#!/usr/bin/env python3
"""The diff-review path must fail GRACEFULLY outside a git repository.

Bug (CTO 2026-06-16): a bare `review` (the diff-review default) run outside a
`.git` repo threw a raw `RuntimeError` + Python traceback (`_git_diff` -> `git
diff` failed). A user just trying the tool got a stack trace.

Contract pinned here:
  * git is REQUIRED only by the diff-review mode (`review diff`, incl. `--staged`): its
    diff is mandatory, so outside a repo it prints a clear 3-part message (WHAT / WHY /
    HOW) with NO traceback and a STABLE non-zero exit code (EXIT_NOT_A_REPO), not a
    generic crash. (A bare `review` no longer runs a diff review — it prints HELP — so the
    not-a-repo path is reached via the `diff` subcommand.)
  * For the PANEL modes (`just-ask` / `quorum` / `brainstorm`), the diff is OPTIONAL
    context even with `--diff` / `--staged` (diff_policy "none"/"optional"): outside a
    repo those flags degrade to no-context ("") rather than hard-fail — so panel modes
    and the meta flags (`--list-defaults` / `--show-board` / `--help`) need NO git and
    work ANYWHERE (no git error).
  * a piped diff on stdin still works without a repo (the stdin path must NOT
    require a repo).
  * regression-safe: in a REAL temp git repo the diff path still works as before
    (an empty working tree -> empty diff -> the normal "nothing to review" path,
    NOT the not-a-repo error).

Driven through the REAL `bin/review` as a subprocess (the faithful end-to-end
path: it exercises `main()`'s actual exit + would surface any uncaught
traceback). Offline: every assertion here uses a meta flag or a path that
short-circuits BEFORE any backend call, so no API keys / network are needed.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import cli  # noqa: E402
from reviewlib.cli import EXIT_NOT_A_REPO  # noqa: E402
from reviewlib.modes import review as _review_mod  # noqa: E402

REVIEW = str(REPO_ROOT / "bin" / "review")


def _run(argv, *, cwd, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Invoke the real CLI. stdin=None -> a real TTY-less empty pipe (isatty False,
    read() -> '' -> treated as "no pipe"); a non-empty string -> a piped diff.

    Runs with an ISOLATED HOME (a fresh temp dir) so the subprocess never loads the
    developer's `~/.config/review-cli/config.yaml` — a malformed user `board:` could
    otherwise break `--show-board` / the real-repo path before reaching the behavior under
    test, making these tests environment-dependent (codex review finding)."""
    with tempfile.TemporaryDirectory() as fake_home:
        env = dict(os.environ)
        env["HOME"] = fake_home
        env["XDG_CONFIG_HOME"] = str(Path(fake_home) / ".config")
        env["REVIEW_TASK_CODE"] = "TEST-1"
        return subprocess.run(
            [sys.executable, REVIEW, *argv],
            cwd=str(cwd),
            input="" if stdin is None else stdin,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )


def _no_traceback(proc: subprocess.CompletedProcess) -> None:
    combined = proc.stdout + proc.stderr
    assert "Traceback (most recent call last)" not in combined, combined
    assert "RuntimeError" not in combined, combined


def test_bare_review_outside_repo_prints_help_not_crash():
    """A bare `review` outside a repo no longer runs a diff review — it prints HELP and
    exits 0. The key regression: NO traceback and NO not-a-repo error (it never reaches
    the diff path)."""
    with tempfile.TemporaryDirectory() as d:
        proc = _run([], cwd=d)
        _no_traceback(proc)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        out = proc.stdout + proc.stderr
        assert "subcommands:" in out, out
        assert "review diff" in out, out
        assert "not in a git repository" not in out.lower(), out


def test_diff_subcommand_outside_repo_is_graceful():
    with tempfile.TemporaryDirectory() as d:
        proc = _run(["diff"], cwd=d)
        _no_traceback(proc)
        assert proc.returncode == EXIT_NOT_A_REPO, (proc.returncode, proc.stderr)
        err = (proc.stdout + proc.stderr).lower()
        # 3-part message: WHAT / WHY / HOW
        assert "not in a git repository" in err, err  # WHAT
        assert "diff review needs a repo" in err, err  # WHY
        assert "just-ask" in err and "quorum" in err and "brainstorm" in err, err  # HOW
        assert "cd into a repo" in err, err  # HOW (alternative)


def test_diff_staged_outside_repo_is_graceful():
    with tempfile.TemporaryDirectory() as d:
        proc = _run(["diff", "--staged"], cwd=d)
        _no_traceback(proc)
        assert proc.returncode == EXIT_NOT_A_REPO, (proc.returncode, proc.stderr)
        assert "not in a git repository" in (proc.stdout + proc.stderr).lower()


def test_list_defaults_works_outside_repo():
    with tempfile.TemporaryDirectory() as d:
        proc = _run(["--list-defaults"], cwd=d)
        _no_traceback(proc)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert "codex" in proc.stdout


def test_show_board_works_outside_repo():
    with tempfile.TemporaryDirectory() as d:
        proc = _run(["--show-board"], cwd=d)
        _no_traceback(proc)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert "source: preset:light" in proc.stdout


def test_help_works_outside_repo():
    with tempfile.TemporaryDirectory() as d:
        proc = _run(["--help"], cwd=d)
        _no_traceback(proc)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert "subcommands:" in proc.stdout


def test_just_ask_help_works_outside_repo():
    """just-ask needs no git; its --help must parse + exit 0 anywhere (no git error)."""
    with tempfile.TemporaryDirectory() as d:
        proc = _run(["just-ask", "--help"], cwd=d)
        _no_traceback(proc)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        # Assert the SUBCOMMAND's own usage banner (not the top-level overview, which also
        # contains the word "question") so this can't pass spuriously — `_dispatch` only
        # treats a mode as argv[0], so the subcommand MUST lead.
        assert "usage: review just-ask" in proc.stdout, proc.stdout
        assert "not in a git repository" not in (proc.stdout + proc.stderr).lower()


def test_quorum_and_brainstorm_help_work_outside_repo():
    with tempfile.TemporaryDirectory() as d:
        for mode in ("quorum", "brainstorm"):
            proc = _run([mode, "--help"], cwd=d)
            _no_traceback(proc)
            assert proc.returncode == 0, (mode, proc.returncode, proc.stderr)
            assert f"usage: review {mode}" in proc.stdout, (mode, proc.stdout)
            assert "not in a git repository" not in (proc.stdout + proc.stderr).lower()


def test_nonexistent_cwd_is_graceful_not_a_traceback():
    """A stale `-C /missing/path` must NOT raise FileNotFoundError from the git spawn —
    a non-directory cwd is "not a repo" and degrades to the graceful not-a-repo error.
    (Driven through the real CLI in a non-repo dir; the path simply does not exist.)"""
    with tempfile.TemporaryDirectory() as d:
        missing = str(Path(d) / "no" / "such" / "path")
        proc = _run(["diff", "-C", missing], cwd=d)
        _no_traceback(proc)
        assert "FileNotFoundError" not in (proc.stdout + proc.stderr), proc.stderr
        assert proc.returncode == EXIT_NOT_A_REPO, (proc.returncode, proc.stderr)
        assert "not in a git repository" in (proc.stdout + proc.stderr).lower()


def test_piped_diff_outside_repo_does_not_require_git():
    """A diff piped on stdin must reach the review handler WITHOUT a repo: stdin wins over
    `git diff` (it short-circuits the not-a-repo gate). Driven IN-PROCESS so we faithfully
    exercise the stdin precedence at the real read site (line ~997) without spawning a
    backend: the review handler is stubbed to capture the diff it receives, cwd is a
    non-repo tmpdir, and stdin is forced to a piped diff. Asserts the handler ran with the
    piped diff and the run did NOT take the not-a-repo path."""
    saved_handler = _review_mod.mode_review
    saved_stdin = cli._read_stdin_if_piped
    saved_cfg = cli.load_config
    piped = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n+hi\n"
    captured: dict = {}

    # `_handler` calls mode_review(models, prompt, diff, cwd, timeout, staged, board=...);
    # the diff is the 3rd positional. Match that shape so the stub absorbs the real call.
    def _fake_review(models, prompt, diff, cwd, timeout, staged, *a, **k):
        captured["diff"] = diff
        captured["ran"] = True
        return 0

    _review_mod.mode_review = _fake_review
    cli._read_stdin_if_piped = lambda: piped
    cli.load_config = lambda: {
        "models": ["codex"]
    }  # deterministic one-seat config board
    # ROUTING test (stdin-diff precedence reaches the handler), NOT a pool-assembly test. On a
    # backend-less host the pre-dispatch pool guard would otherwise bail (exit 10) before the
    # stubbed handler runs, so force every seat live via the fake backend — the established
    # hermetic-dispatch seam (see backend_available / test_mode_subcommands).
    saved_fake = os.environ.get("REVIEW_FAKE_BACKEND")
    os.environ["REVIEW_FAKE_BACKEND"] = "1"
    err = io.StringIO()
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
        ):
            rc = cli._dispatch(["diff", "--task", "TEST-1", "-C", d])
    finally:
        _review_mod.mode_review = saved_handler
        cli._read_stdin_if_piped = saved_stdin
        cli.load_config = saved_cfg
        if saved_fake is None:
            os.environ.pop("REVIEW_FAKE_BACKEND", None)
        else:
            os.environ["REVIEW_FAKE_BACKEND"] = saved_fake
    assert captured.get("ran"), "the review handler never ran on a piped diff"
    assert captured.get("diff") == piped, captured.get("diff")
    assert rc == 0, rc
    assert "not in a git repository" not in err.getvalue().lower(), err.getvalue()


def test_panel_staged_outside_repo_degrades_gracefully():
    """A PANEL mode (just-ask / quorum, diff_policy="none") with --staged outside a repo
    must NOT hard-fail with the not-a-repo error (the diff is optional context there): it
    degrades to no-context. Driven in-process (the handler is stubbed) so no backend runs;
    asserts the handler ran with an empty diff and the run did NOT emit the not-a-repo
    message at someone already running just-ask."""
    from reviewlib.modes import just_ask as _ja_mod

    saved_handler = _ja_mod.mode_just_ask
    saved_stdin = cli._read_stdin_if_piped
    saved_cfg = cli.load_config
    captured: dict = {}

    # `_handler` calls mode_just_ask(question, models, diff, cwd, timeout); diff is 3rd.
    def _fake_ja(question, models, diff, cwd, timeout, *a, **k):
        captured["diff"] = diff
        captured["ran"] = True
        return 0

    _ja_mod.mode_just_ask = _fake_ja
    cli._read_stdin_if_piped = lambda: None
    cli.load_config = lambda: {"models": ["codex"]}
    err = io.StringIO()
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
        ):
            rc = cli._dispatch(
                ["just-ask", "what is this", "--task", "TEST-1", "--staged", "-C", d]
            )
    finally:
        _ja_mod.mode_just_ask = saved_handler
        cli._read_stdin_if_piped = saved_stdin
        cli.load_config = saved_cfg
    assert captured.get("ran"), "just-ask never ran with --staged outside a repo"
    assert (captured.get("diff") or "") == "", captured.get(
        "diff"
    )  # degraded to no context
    assert rc != EXIT_NOT_A_REPO, rc
    assert "not in a git repository" not in err.getvalue().lower(), err.getvalue()


def test_real_repo_empty_diff_is_not_treated_as_not_a_repo():
    """Regression-safe: inside a REAL git repo the diff path runs as before. An empty
    working tree yields an EMPTY diff -> the EXISTING 'nothing to review' contract:
    `mode_review` prints "No diff to review." and returns 1 (a gate result, NOT a crash and
    NOT the not-a-repo error). Pins both that it is NOT EXIT_NOT_A_REPO and that the
    pre-existing exit-1 + message contract is unchanged. (No backend: empty diff returns
    before any model.)"""
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        proc = _run(["diff"], cwd=repo)
        _no_traceback(proc)
        assert proc.returncode != EXIT_NOT_A_REPO, (proc.returncode, proc.stderr)
        assert proc.returncode == 1, (
            proc.returncode,
            proc.stderr,
        )  # existing empty-diff contract
        assert "No diff to review." in (proc.stdout + proc.stderr), proc.stderr
        assert "not in a git repository" not in (proc.stdout + proc.stderr).lower()


def test_empty_diff_bypasses_pool_guard_even_with_no_live_backend():
    """Regression: the review-mode pool guard is gated on a NON-EMPTY diff. An empty diff runs
    NO panel, so even on a host with ZERO live backends `review diff` must fall through to
    mode_review's "No diff to review." (exit 1) — NOT bail with EXIT_UNSATISFIED (10). Forces
    every backend DOWN and dispatches an empty-diff review in a real repo; pins that the guard
    is bypassed (not merely "not reached because a backend happened to be live")."""
    import reviewlib.backends as backends
    from reviewlib.pool_guard import EXIT_UNSATISFIED

    saved_avail = backends.backend_available
    saved_reason = backends.backend_unavailable_reason
    saved_cfg = cli.load_config
    saved_stdin = cli._read_stdin_if_piped
    backends.backend_available = lambda _m: False  # NO live backend at all
    backends.backend_unavailable_reason = lambda _m: "no backend (test)"
    cli.load_config = lambda: {"models": ["codex", "gemini"]}
    cli._read_stdin_if_piped = lambda: None
    err = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                rc = cli._dispatch(["diff", "--task", "TEST-1", "-C", str(repo)])
    finally:
        backends.backend_available = saved_avail
        backends.backend_unavailable_reason = saved_reason
        cli.load_config = saved_cfg
        cli._read_stdin_if_piped = saved_stdin
    assert rc != EXIT_UNSATISFIED, (rc, err.getvalue())  # the guard did NOT fire
    assert rc == 1, (rc, err.getvalue())  # existing empty-diff contract
    assert "No diff to review." in err.getvalue(), err.getvalue()


def test_git_diff_normalizes_spawn_failures_to_runtimeerror():
    """`_git_diff` is the single source of truth for the diff probes: EVERY failure becomes
    a RuntimeError so the OPTIONAL callers (--visual / brainstorm / `just-ask --diff`),
    which all `except RuntimeError`, degrade to "" instead of leaking a raw traceback. A
    missing/non-dir cwd (FileNotFoundError) and a wedged git (TimeoutExpired) are the two
    spawn failures that previously slipped past those catches."""
    with tempfile.TemporaryDirectory() as d:
        missing = Path(d) / "no" / "such" / "dir"
        try:
            cli._git_diff(missing, staged=False)
        except RuntimeError:
            pass  # expected — normalized
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"missing cwd leaked {type(exc).__name__}, not RuntimeError"
            ) from exc
        else:
            raise AssertionError("missing cwd did not raise at all")

    saved = cli._run

    def _boom_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else ["git"], timeout=120)

    cli._run = _boom_timeout
    try:
        with tempfile.TemporaryDirectory() as d:
            try:
                cli._git_diff(Path(d), staged=False)
            except RuntimeError:
                pass  # expected — TimeoutExpired normalized to RuntimeError
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"timeout leaked {type(exc).__name__}, not RuntimeError"
                ) from exc
            else:
                raise AssertionError("timeout did not raise at all")
    finally:
        cli._run = saved


def test_brainstorm_missing_cwd_degrades_without_traceback():
    """brainstorm's diff is OPTIONAL grounding: a stale/missing `-C` must degrade to pure
    ideation (empty diff), NOT traceback. In-process (handler stubbed) so no backend runs;
    asserts brainstorm ran with an empty diff and never hit the not-a-repo error."""
    from reviewlib.modes import brainstorm as _bs_mod

    saved_handler = _bs_mod.mode_brainstorm
    saved_stdin = cli._read_stdin_if_piped
    saved_cfg = cli.load_config
    captured: dict = {}

    def _fake_bs(*a, **k):
        # `_handler` passes diff as the `diff=` KEYWORD (mode_brainstorm's diff is keyword,
        # not positional — topic/models/cwd/timeout/... come first).
        captured["diff"] = k.get("diff")
        captured["ran"] = True
        return 0

    _bs_mod.mode_brainstorm = _fake_bs
    cli._read_stdin_if_piped = lambda: None
    cli.load_config = lambda: {"models": ["codex"], "brainstorm_models": ["codex"]}
    err = io.StringIO()
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
        ):
            missing = str(Path(d) / "gone")
            rc = cli._dispatch(
                ["brainstorm", "topic", "--task", "TEST-1", "--diff", "-C", missing]
            )
    finally:
        _bs_mod.mode_brainstorm = saved_handler
        cli._read_stdin_if_piped = saved_stdin
        cli.load_config = saved_cfg
    assert captured.get("ran"), (
        "brainstorm never ran (the missing-cwd diff probe was not caught)"
    )
    assert (captured.get("diff") or "") == "", captured.get("diff")
    assert rc != EXIT_NOT_A_REPO, rc


def test_is_git_repo_tolerates_spawn_failures():
    """`_is_git_repo` is the no-traceback gate, so EVERY way the git spawn can blow up must
    return False, not propagate. A missing cwd (OSError -> FileNotFoundError) and a wedged
    git (subprocess.TimeoutExpired, NOT an OSError) both count as "not a repo"."""
    with tempfile.TemporaryDirectory() as d:
        assert cli._is_git_repo(Path(d) / "does-not-exist") is False

    saved = cli._run

    def _boom_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else ["git"], timeout=10)

    cli._run = _boom_timeout
    try:
        # A real existing dir, but the (stubbed) git spawn times out -> still False, no raise.
        with tempfile.TemporaryDirectory() as d:
            assert cli._is_git_repo(Path(d)) is False
    finally:
        cli._run = saved


def test_no_git_mode_runs_when_git_binary_is_missing_or_wedged():
    """`_effective_cwd` git-toplevel probe runs on EVERY invocation BEFORE mode dispatch, so a
    missing git binary (OSError -> FileNotFoundError) or a wedged `git rev-parse`
    (TimeoutExpired) must NOT leak a traceback through a no-git mode. Stub `cli._run` to raise
    each failure and dispatch a real `just-ask`: it must still run (degrading to no diff),
    never crash. This is the regression for the `_effective_cwd` spawn-failure gap."""
    from reviewlib.modes import just_ask as _ja_mod

    saved_run = cli._run
    saved_handler = _ja_mod.mode_just_ask
    saved_stdin = cli._read_stdin_if_piped
    saved_cfg = cli.load_config

    def _fake_ja(question, models, diff, cwd, timeout, *a, **k):
        return 0

    _ja_mod.mode_just_ask = _fake_ja
    cli._read_stdin_if_piped = lambda: None
    cli.load_config = lambda: {"models": ["codex"]}

    def _boom_missing(*a, **k):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    def _boom_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else ["git"], timeout=10)

    try:
        for boom in (_boom_missing, _boom_timeout):
            cli._run = boom
            err = io.StringIO()
            with (
                tempfile.TemporaryDirectory() as d,
                redirect_stderr(err),
                redirect_stdout(io.StringIO()),
            ):
                # Must not raise: the toplevel probe in `_effective_cwd` swallows the spawn
                # failure and degrades to "review the dir as-is", then just-ask runs.
                rc = cli._dispatch(
                    ["just-ask", "what is this", "--task", "TEST-1", "-C", d]
                )
            assert rc == 0, (boom.__name__, rc)
            assert "Traceback" not in err.getvalue(), (boom.__name__, err.getvalue())
            assert "FileNotFoundError" not in err.getvalue(), (
                boom.__name__,
                err.getvalue(),
            )
    finally:
        cli._run = saved_run
        _ja_mod.mode_just_ask = saved_handler
        cli._read_stdin_if_piped = saved_stdin
        cli.load_config = saved_cfg


def test_show_board_end_to_end_in_non_repo_dir():
    """End-to-end companion to `test_opencode_runs_in_repo_tolerates_spawn_failures` (which
    unit-tests the spawn-failure guard directly): `--show-board` is a meta flag that calls
    `backends._opencode_runs_in_repo` for the opencode scope label, so it must exit 0 with no
    traceback from a NON-repo dir — the real CLI, not the function in isolation."""
    with tempfile.TemporaryDirectory() as d:
        proc = _run(["--show-board"], cwd=d)
        _no_traceback(proc)
        assert proc.returncode == 0, (proc.returncode, proc.stderr)


def test_panel_staged_in_repo_degrades_when_git_diff_raises():
    """A PANEL mode --staged INSIDE a repo where `git diff` fails (e.g. unborn HEAD) must
    degrade to no-context, not hard-fail — the diff is optional context (diff_policy="none"),
    matching the `--diff` sibling. In-process: cwd is a real repo (so `_is_git_repo` is True),
    but `_git_diff` is stubbed to raise; assert just-ask still runs with an empty diff."""
    from reviewlib.modes import just_ask as _ja_mod

    saved_handler = _ja_mod.mode_just_ask
    saved_stdin = cli._read_stdin_if_piped
    saved_cfg = cli.load_config
    saved_gitdiff = cli._git_diff
    captured: dict = {}

    def _fake_ja(question, models, diff, cwd, timeout, *a, **k):
        captured["diff"] = diff
        captured["ran"] = True
        return 0

    def _raise_diff(cwd, staged):
        raise RuntimeError("fatal: ambiguous argument 'HEAD': unknown revision")

    _ja_mod.mode_just_ask = _fake_ja
    cli._read_stdin_if_piped = lambda: None
    cli.load_config = lambda: {"models": ["codex"]}
    cli._git_diff = _raise_diff
    err = io.StringIO()
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
        ):
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            rc = cli._dispatch(
                [
                    "just-ask",
                    "what is this",
                    "--task",
                    "TEST-1",
                    "--staged",
                    "-C",
                    str(repo),
                ]
            )
    finally:
        _ja_mod.mode_just_ask = saved_handler
        cli._read_stdin_if_piped = saved_stdin
        cli.load_config = saved_cfg
        cli._git_diff = saved_gitdiff
    assert captured.get("ran"), (
        "just-ask never ran (the git-diff failure was not caught)"
    )
    assert (captured.get("diff") or "") == "", captured.get("diff")
    assert rc != EXIT_NOT_A_REPO, rc


def test_required_review_in_repo_fails_gracefully_when_git_diff_raises():
    """The REQUIRED review path INSIDE a real repo where `git diff` itself fails (`_is_git_repo`
    True but `_git_diff` raises — e.g. a wedged git, a corrupt index) must NOT traceback: the
    diff is required so it can't degrade to "", but it must fail GRACEFULLY with a structured
    error + the stable EXIT_GIT_DIFF_FAILED (distinct from EXIT_NOT_A_REPO — you ARE in a repo).
    In-process: cwd is a real repo so `_is_git_repo` is True, but `_git_diff` is stubbed to
    raise; assert the structured exit + message and NO traceback / NO not-a-repo confusion."""
    from reviewlib.cli import EXIT_GIT_DIFF_FAILED

    saved_stdin = cli._read_stdin_if_piped
    saved_cfg = cli.load_config
    saved_gitdiff = cli._git_diff

    def _raise_diff(cwd, staged):
        raise RuntimeError("fatal: index file corrupt")

    cli._read_stdin_if_piped = lambda: None
    cli.load_config = lambda: {"models": ["codex"]}
    cli._git_diff = _raise_diff
    err = io.StringIO()
    try:
        with (
            tempfile.TemporaryDirectory() as d,
            redirect_stderr(err),
            redirect_stdout(io.StringIO()),
        ):
            repo = Path(d) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            # `review diff` (the REQUIRED diff path) in a real repo where git diff blows up.
            rc = cli._dispatch(["diff", "--task", "TEST-1", "-C", str(repo)])
    finally:
        cli._read_stdin_if_piped = saved_stdin
        cli.load_config = saved_cfg
        cli._git_diff = saved_gitdiff
    assert rc == EXIT_GIT_DIFF_FAILED, rc
    assert rc != EXIT_NOT_A_REPO, (
        "a git-diff failure must NOT be reported as 'not a repo'"
    )
    msg = err.getvalue()
    assert "could not read the git diff" in msg, msg
    assert "index file corrupt" in msg, msg  # the underlying cause is surfaced
    assert "Traceback" not in msg, msg  # no raw traceback
    assert "not in a git repository" not in msg.lower(), msg  # not the wrong message
    # The stdin fix hint must point at the CURRENT verb + required task code, not the
    # bare `review` (which now exits 2 on a piped diff) — codex review finding.
    assert "git diff | review diff --task CODE" in msg, msg


def test_opencode_runs_in_repo_tolerates_spawn_failures():
    """`--show-board` reaches backends._opencode_runs_in_repo, which probes `git rev-parse`.
    A missing git binary (OSError) or a wedged git (TimeoutExpired) must degrade to False
    (not a repo we can run opencode in), never a raw traceback through the meta-flag path."""
    from reviewlib import backends

    saved = backends._run

    def _boom_oserror(*a, **k):
        raise FileNotFoundError(2, "No such file or directory: 'git'")

    def _boom_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else ["git"], timeout=10)

    try:
        for boom in (_boom_oserror, _boom_timeout):
            backends._run = boom
            with tempfile.TemporaryDirectory() as d:
                # an existing dir (passes the is_dir gate) but the git spawn blows up
                assert backends._opencode_runs_in_repo(Path(d)) is False, boom.__name__
    finally:
        backends._run = saved


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
