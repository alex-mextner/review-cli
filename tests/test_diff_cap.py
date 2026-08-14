#!/usr/bin/env python3
"""The diff handed to backend DISPATCH is capped at a configurable byte ceiling — but
the canonical diff used for the staged-commit stamp / `--commit` checkpoint's integrity
check is NEVER capped.

Bug this fixes (2026-08 token-burn investigation): an uncapped `git diff` sent a real
6.5MB / 583-file diff (a debug harness's screenshot/video capture scripts touching
hundreds of files) whole to every seat in the board — the single largest token-burn
outlier found in the real logs. `cap_diff_for_dispatch` truncates an over-cap diff to
`$REVIEW_DIFF_MAX_BYTES` (default 300,000) with a visible marker naming the real total.

Architecture note (codex P1 finding on the FIRST version of this fix): the cap must
live at the DISPATCH boundary (`reviewlib.backends.cap_diff_for_dispatch`, applied
inside `mode_review`'s two dispatch paths), NOT inside `cli._git_diff()` — capping the
diff at its SOURCE broke `review diff --staged --commit`'s checkpoint integrity check,
because `_current_staged_diff` independently re-derives an UNCAPPED `git diff --cached`
and compares it byte-for-byte against the diff the stamp/checkpoint was given. Two
things are pinned here: the pure function (`cap_diff_for_dispatch`), and the end-to-end
guarantee that an oversized STAGED diff still checkpoints correctly.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.modes import review as review_mode  # noqa: E402
from reviewlib.modes.review import EXIT_COMMIT_DIFF_TRUNCATED, mode_review  # noqa: E402


def _with_env(key, value, fn):
    saved = os.environ.get(key)
    try:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
        return fn()
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def _with_default_cap(fn):
    """Clear $REVIEW_DIFF_MAX_BYTES for the duration of `fn` — every test that assumes
    `DIFF_MAX_BYTES_DEFAULT` must not depend on the host's ambient env (codex review
    finding: a developer with this var exported would otherwise see these fail)."""
    return _with_env("REVIEW_DIFF_MAX_BYTES", None, fn)


# ---- pure function: cap_diff_for_dispatch -------------------------------------------
def test_small_diff_passes_through_unchanged():
    small = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@\n+hi\n"
    assert _with_default_cap(lambda: backends.cap_diff_for_dispatch(small)) == small


def test_oversized_diff_is_truncated_with_visible_marker():
    big = "diff --git a/x b/x\n" + ("+line\n" * 100_000)
    assert len(big.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT
    capped = _with_default_cap(lambda: backends.cap_diff_for_dispatch(big))
    # codex review finding: the marker used to be appended ON TOP OF the cap, so the
    # dispatched payload always exceeded it. The marker is now reserved OUT OF the cap —
    # the result must actually respect the configured ceiling, not just be "smaller than
    # the original".
    assert len(capped.encode("utf-8")) <= backends.DIFF_MAX_BYTES_DEFAULT
    assert len(capped.encode("utf-8")) < len(big.encode("utf-8"))
    assert "[review-cli] diff truncated at" in capped
    assert str(backends.DIFF_MAX_BYTES_DEFAULT) in capped
    assert (
        str(len(big.encode("utf-8"))) in capped
    )  # the REAL total is named, not hidden
    assert "REVIEW_DIFF_MAX_BYTES" in capped


def test_capped_result_never_exceeds_the_configured_cap():
    """codex review finding: `cap_diff_for_dispatch` truncated the diff TO `cap` bytes
    and then appended the marker AFTER — so the actual payload handed to every backend
    always exceeded the configured cap by the marker's length. For the ~300-byte
    default marker against the 300,000-byte default cap that's a ~0.1% overshoot, but a
    small custom cap could see a payload many times the requested size. Pins the fix for
    any cap comfortably above the marker's own size (the realistic case — the default is
    1,000x the marker)."""
    big = "diff --git a/x b/x\n" + ("+line\n" * 10_000)
    for cap in (2_000, 5_000, 50_000):
        capped = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            str(cap),
            lambda: backends.cap_diff_for_dispatch(big),
        )
        assert len(capped.encode("utf-8")) <= cap, (cap, len(capped.encode("utf-8")))


def test_cap_smaller_than_the_marker_floors_at_the_marker_alone():
    """The documented floor case: a cap smaller than the marker itself (a pathological
    config, not a realistic one — the default is 1,000x the marker) cannot truncate the
    diff text below empty, so the result is the marker alone and DOES exceed the
    configured cap. Still never silent (the marker names the real total) and still far
    smaller than the original — just not <= cap. Pins that this floor is a deliberate,
    bounded exception, not a crash or an unbounded payload."""
    big = "diff --git a/x b/x\n" + ("+line\n" * 10_000)
    tiny_cap = 10
    capped = _with_env(
        "REVIEW_DIFF_MAX_BYTES",
        str(tiny_cap),
        lambda: backends.cap_diff_for_dispatch(big),
    )
    assert "[review-cli] diff truncated at" in capped
    assert len(capped.encode("utf-8")) < len(big.encode("utf-8"))
    assert len(capped.encode("utf-8")) > tiny_cap  # the documented floor, not a bug


def test_binary_stub_lines_do_not_defeat_the_cap():
    """A diff dominated by many cheap 'Binary files ... differ' stubs (the real 583-file /
    351-binary-stub outlier shape) is still capped by total bytes, not by file count."""
    big = "diff --git a/x b/x\n" + ("Binary files a/f0 and b/f0 differ\n" * 20_000)
    assert len(big.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT
    assert "[review-cli] diff truncated at" in _with_default_cap(
        lambda: backends.cap_diff_for_dispatch(big)
    )


def test_env_override_raises_the_cap():
    big = "x" * (backends.DIFF_MAX_BYTES_DEFAULT + 1000)
    assert "truncated" in _with_default_cap(lambda: backends.cap_diff_for_dispatch(big))
    result = _with_env(
        "REVIEW_DIFF_MAX_BYTES",
        str(backends.DIFF_MAX_BYTES_DEFAULT + 10_000),
        lambda: backends.cap_diff_for_dispatch(big),
    )
    assert result == big


def test_env_disable_value_turns_off_the_cap():
    big = "x" * (backends.DIFF_MAX_BYTES_DEFAULT * 3)
    assert (
        _with_env(
            "REVIEW_DIFF_MAX_BYTES", "0", lambda: backends.cap_diff_for_dispatch(big)
        )
        == big
    )
    assert (
        _with_env(
            "REVIEW_DIFF_MAX_BYTES", "-1", lambda: backends.cap_diff_for_dispatch(big)
        )
        == big
    )


def test_invalid_env_value_falls_back_to_default():
    big = "x" * (backends.DIFF_MAX_BYTES_DEFAULT + 1000)
    result = _with_env(
        "REVIEW_DIFF_MAX_BYTES",
        "not-a-number",
        lambda: backends.cap_diff_for_dispatch(big),
    )
    assert "truncated" in result


# ---- cli._git_diff must stay UNCAPPED (the integrity fix) -----------------------------
def test_git_diff_no_longer_caps_the_canonical_diff():
    """Regression guard for the codex P1 finding: `cli._git_diff` must return the FULL
    diff, unmodified, no matter how large — capping happens only at the dispatch
    boundary now. If someone re-adds a cap inside `_git_diff`, this must fail."""
    from reviewlib import cli

    big_stdout = "diff --git a/x b/x\n" + ("+line\n" * 100_000)
    assert len(big_stdout.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT
    saved_run = cli._run

    def _fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=big_stdout, stderr="")

    cli._run = _fake_run
    try:
        result = cli._git_diff(REPO_ROOT, staged=False)
    finally:
        cli._run = saved_run
    assert result == big_stdout  # byte-identical — no truncation, no marker
    assert "diff truncated at" not in result


# ---- end-to-end: an oversized STAGED diff still checkpoints correctly -----------------
_ENV_VARS = ("REVIEW_FAKE_BACKEND", "REVIEW_MARKER", "REVIEW_DIFF_MAX_BYTES", "HOME")


class _EnvSandbox:
    def __enter__(self):
        self._saved = {name: os.environ.get(name) for name in _ENV_VARS}
        # Save/restore alone is not enough (codex review finding): the e2e checkpoint
        # test below assumes the DEFAULT cap, so a host with $REVIEW_DIFF_MAX_BYTES
        # exported would otherwise change its outcome. Clear it explicitly.
        os.environ.pop("REVIEW_DIFF_MAX_BYTES", None)
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _head_count(repo: Path) -> int:
    proc = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return int(proc.stdout.strip()) if proc.returncode == 0 else 0


def test_oversized_staged_diff_dispatch_capped_and_checkpoint_refused():
    """The headline regression, TWICE fixed: a staged diff bigger than the cap must (a)
    reach the backend TRUNCATED (the token-burn fix), and (b) REFUSE the `--commit`
    checkpoint rather than certify it (codex review finding, round 6 — a follow-up to
    (a)): the first version of this fix let the stamp/checkpoint verify against the
    UNCAPPED canonical diff independent of what the backend saw, which meant a
    non-interactive `review diff --staged --commit` (automation never sees the stderr
    warning) would silently checkpoint a commit CLAIMING the full diff was reviewed
    when every seat only saw a truncated copy. A checkpoint is supposed to certify the
    diff it commits was actually reviewed — a partial review must never satisfy that
    gate, so this now returns EXIT_COMMIT_DIFF_TRUNCATED and creates NO commit."""
    captured: dict = {}

    def _capturing_backend(model, prompt, diff, cwd, timeout, round_no=0):
        captured["diff"] = diff
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ.pop(
            "REVIEW_FAKE_BACKEND", None
        )  # use the capturing fake, not the built-in one
        os.environ["REVIEW_MARKER"] = str(tmp / "last-review")
        repo = tmp / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        big_content = "x" * (backends.DIFF_MAX_BYTES_DEFAULT + 100_000) + "\n"
        (repo / "big.txt").write_text(big_content, encoding="utf-8")
        subprocess.run(["git", "add", "big.txt"], cwd=repo, check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert len(diff.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT  # sanity

        saved_resolve = review_mode.resolve_backend
        review_mode.resolve_backend = lambda model: _capturing_backend
        err = io.StringIO()
        try:
            before = _head_count(repo)
            with redirect_stderr(err):
                rc = mode_review(
                    ["fakemodel"],
                    prompt="p",
                    diff=diff,
                    cwd=repo,
                    timeout=30,
                    staged=True,
                    commit=True,
                )
        finally:
            review_mode.resolve_backend = saved_resolve

        # The checkpoint must be REFUSED, not silently certified — no commit created.
        assert rc == EXIT_COMMIT_DIFF_TRUNCATED, rc
        assert _head_count(repo) == before, "no checkpoint commit should be created"

        # The backend still received the CAPPED copy — the underlying token-burn fix
        # itself is untouched by this refusal.
        assert "diff" in captured
        assert "[review-cli] diff truncated at" in captured["diff"]
        assert len(captured["diff"].encode("utf-8")) < len(diff.encode("utf-8"))

        # The operator must SEE both why the review was partial AND why no checkpoint
        # was made.
        assert "WARNING: the staged diff exceeded" in err.getvalue()
        assert "TRUNCATED" in err.getvalue()
        assert "--commit:" in err.getvalue()
        assert "no checkpoint created" in err.getvalue()


def test_oversized_staged_diff_without_commit_skips_the_stamp_too():
    """The PLAIN `--staged` stamp (no `--commit`) must be skipped the same way — the
    pre-commit hook gate must not accept a truncated review as "the staged index was
    reviewed" either. `mode_review`'s own 0/1 exit code is untouched (the review itself
    still succeeded); only the STAMP that would let `git commit` bypass the hook is
    withheld."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ.pop("REVIEW_FAKE_BACKEND", None)
        marker_path = tmp / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker_path)
        repo = tmp / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        big_content = "x" * (backends.DIFF_MAX_BYTES_DEFAULT + 100_000) + "\n"
        (repo / "big.txt").write_text(big_content, encoding="utf-8")
        subprocess.run(["git", "add", "big.txt"], cwd=repo, check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        def _ok_backend(model, prompt, diff, cwd, timeout, round_no=0):
            return ReviewResult(
                model=model, command="fake", returncode=0, stdout="ok", stderr=""
            )

        saved_resolve = review_mode.resolve_backend
        review_mode.resolve_backend = lambda model: _ok_backend
        stamp_path = repo / ".git" / "review-stamp"
        try:
            rc = mode_review(
                ["fakemodel"],
                prompt="p",
                diff=diff,
                cwd=repo,
                timeout=30,
                staged=True,
                commit=False,
            )
        finally:
            review_mode.resolve_backend = saved_resolve
        assert rc == 0, rc  # the review itself still succeeded
        assert not stamp_path.exists(), (
            "no stamp should be written for a truncated staged review"
        )
        assert not marker_path.exists(), "no session marker should be touched either"


def test_small_staged_diff_prints_no_truncation_warning():
    """No false positives: a staged diff WITHIN the cap must never print the
    partial-coverage warning — only a REAL truncation does."""

    def _capturing_backend(model, prompt, diff, cwd, timeout, round_no=0):
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ.pop("REVIEW_FAKE_BACKEND", None)
        os.environ["REVIEW_MARKER"] = str(tmp / "last-review")
        repo = tmp / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / "small.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "small.txt"], cwd=repo, check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        saved_resolve = review_mode.resolve_backend
        review_mode.resolve_backend = lambda model: _capturing_backend
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                rc = mode_review(
                    ["fakemodel"],
                    prompt="p",
                    diff=diff,
                    cwd=repo,
                    timeout=30,
                    staged=True,
                    commit=True,
                )
        finally:
            review_mode.resolve_backend = saved_resolve
        assert rc == 0, rc
        assert "WARNING: the staged diff exceeded" not in err.getvalue()


def test_unstaged_oversized_diff_still_prints_a_truncation_warning():
    """Opus review finding: a plain, UNSTAGED `review diff` on an over-cap diff used to
    truncate completely SILENTLY — the only signal was the marker embedded in the
    payload handed to the backend, never surfaced to the user's own stderr, because the
    warning function only fired for `staged=True` (there being no commit-gate to refuse
    for an unstaged run). Pins that the warning now fires either way (worded without
    the staged-only "no commit-gate stamp" claim), while the STAGED-scoped return value
    that gates the checkpoint refusal (tested elsewhere) is untouched — this test uses
    `staged=False` specifically to isolate the new unstaged-warning behavior."""

    def _capturing_backend(model, prompt, diff, cwd, timeout, round_no=0):
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    big = "diff --git a/x b/x\n" + ("+line\n" * 100_000)
    assert len(big.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT

    saved_resolve = review_mode.resolve_backend
    review_mode.resolve_backend = lambda model: _capturing_backend
    err = io.StringIO()
    try:
        with redirect_stderr(err):
            rc = _with_default_diff_cap(
                lambda: mode_review(
                    ["fakemodel"],
                    prompt="p",
                    diff=big,
                    cwd=REPO_ROOT,
                    timeout=30,
                    staged=False,
                )
            )
    finally:
        review_mode.resolve_backend = saved_resolve
    assert rc == 0, rc
    output = err.getvalue()
    assert "WARNING: the diff exceeded $REVIEW_DIFF_MAX_BYTES" in output
    # The staged-only framing (commit-gate consequence) must NOT appear here — this is
    # the unstaged branch's own, differently-worded message.
    assert "no commit-gate stamp/checkpoint" not in output
    assert "the staged diff exceeded" not in output


# ---- board path: the DEFAULT `review diff` dispatch, independent cap coverage --------
# kimi review finding: the two tests above exercise the FLAT `-m` path only (they patch
# `review_mode.resolve_backend`, which the board path never consults). The board path
# (`_mode_review_board` -> `run_board_with_failover` -> `run_panel`) has its OWN
# independent `dispatch_diff = cap_diff_for_dispatch(diff)` line
# (reviewlib/modes/review.py) — a regression there (passing the uncapped `diff` instead,
# or dropping the `diff_from_stdin` exemption) would pass every test above and still
# send an uncapped diff to every seat on the path most users actually run by default.
def test_board_path_oversized_diff_is_capped_for_dispatch():
    from reviewlib.config import BoardReviewer
    import reviewlib.panel as panel

    big = "diff --git a/x b/x\n" + ("+line\n" * 100_000)
    assert len(big.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT
    captured: dict = {}

    def _capturing_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        captured["diff"] = diff
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    board = [BoardReviewer("codex", "correctness", "Codex")]
    saved_available = backends.backend_available
    saved_panel_available = panel.backend_available
    saved_review_mod_available = review_mode.backend_available
    saved_panel_resolve = panel.resolve_backend
    backends.backend_available = lambda model: True
    panel.backend_available = lambda model: True
    review_mode.backend_available = lambda model: True
    panel.resolve_backend = lambda model: _capturing_backend
    try:
        rc = _with_default_diff_cap(
            lambda: mode_review(
                [],
                prompt="p",
                diff=big,
                cwd=REPO_ROOT,
                timeout=5,
                staged=False,
                board=board,
            )
        )
    finally:
        backends.backend_available = saved_available
        panel.backend_available = saved_panel_available
        review_mode.backend_available = saved_review_mod_available
        panel.resolve_backend = saved_panel_resolve
    assert rc == 0, rc
    assert "diff" in captured
    assert "[review-cli] diff truncated at" in captured["diff"]
    assert len(captured["diff"].encode("utf-8")) < len(big.encode("utf-8"))


# glm review finding: `test_oversized_staged_diff_dispatch_capped_and_checkpoint_refused`
# above pins the truncation-refuses-the-checkpoint guarantee on the FLAT `-m` path only
# (it patches `review_mode.resolve_backend`, which `_mode_review_board` never consults —
# same gap `test_board_path_oversized_diff_is_capped_for_dispatch` closed for the cap
# itself, but not yet for the staged+commit refusal). `_mode_review_board` threads
# `dispatch_diff_truncated` through the SAME `_stamp_if_staged_commit_review` /
# `_checkpoint_if_requested` helpers as the flat path, but nothing exercised that on the
# board path specifically — a regression there (dropping the thread-through, or passing
# the wrong diff) would pass every other test here and still let the DEFAULT `review
# diff --staged --commit` (board is the default pool shape) silently checkpoint a
# partial review.
def test_board_path_oversized_staged_diff_refuses_the_checkpoint():
    from reviewlib.config import BoardReviewer
    import reviewlib.panel as panel

    def _capturing_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return ReviewResult(
            model=model, command="fake", returncode=0, stdout="ok", stderr=""
        )

    board = [BoardReviewer("codex", "correctness", "Codex")]
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["REVIEW_MARKER"] = str(tmp / "last-review")
        repo = tmp / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        big_content = "x" * (backends.DIFF_MAX_BYTES_DEFAULT + 100_000) + "\n"
        (repo / "big.txt").write_text(big_content, encoding="utf-8")
        subprocess.run(["git", "add", "big.txt"], cwd=repo, check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert len(diff.encode("utf-8")) > backends.DIFF_MAX_BYTES_DEFAULT  # sanity

        saved_panel_available = panel.backend_available
        saved_review_mod_available = review_mode.backend_available
        saved_panel_resolve = panel.resolve_backend
        panel.backend_available = lambda model: True
        review_mode.backend_available = lambda model: True
        panel.resolve_backend = lambda model: _capturing_backend
        try:
            before = _head_count(repo)
            rc = mode_review(
                [],
                prompt="p",
                diff=diff,
                cwd=repo,
                timeout=30,
                staged=True,
                board=board,
                commit=True,
            )
        finally:
            panel.backend_available = saved_panel_available
            review_mode.backend_available = saved_review_mod_available
            panel.resolve_backend = saved_panel_resolve

        assert rc == EXIT_COMMIT_DIFF_TRUNCATED, rc
        assert _head_count(repo) == before, "no checkpoint commit should be created"


def _with_default_diff_cap(fn):
    """Clear $REVIEW_DIFF_MAX_BYTES for the duration of `fn` — matches
    test_brainstorm_diff.py's identical helper (a host with this exported would
    otherwise flake the default-cap assumption above)."""
    saved = os.environ.get("REVIEW_DIFF_MAX_BYTES")
    try:
        os.environ.pop("REVIEW_DIFF_MAX_BYTES", None)
        return fn()
    finally:
        if saved is None:
            os.environ.pop("REVIEW_DIFF_MAX_BYTES", None)
        else:
            os.environ["REVIEW_DIFF_MAX_BYTES"] = saved


# ---- diff_already_capped: brainstorm/quorum/just-ask skip a redundant re-cap ----------
# codex review finding, round 2 (had ZERO test coverage before this): cli.py's
# `_dispatch` caps the diff once for these three flat-panel modes and threads
# `diff_already_capped=True` into `ModeContext.extra` so each mode's OWN
# dispatch-boundary `cap_diff_for_dispatch` call (which exists so a direct library
# caller bypassing the CLI is still protected) becomes a no-op instead of a second real
# application. Silently harmless at the DEFAULT cap (a second call on an already-<=cap
# diff is itself a no-op), but NOT idempotent when $REVIEW_DIFF_MAX_BYTES is set below
# the truncation marker's own length: a second `cap_diff_for_dispatch` call would
# re-truncate the FIRST call's marker text and report ITS byte count as "the full
# diff". A tiny cap (10 bytes, well below any marker) makes a stray second application
# observable: the oversized `big` diff below would arrive at the backend already
# marker-truncated once, and a second application would silently mangle that marker
# rather than pass it through. Each mode gets two tests: `diff_already_capped=True`
# must reach the backend byte-identical (proving the mode's own cap was skipped), and
# the default (False) must still cap it (proving a direct library caller stays
# protected).
_DIFF_ALREADY_CAPPED_BIG = "diff --git a/x b/x\n" + ("+line\n" * 1000)


def test_just_ask_diff_already_capped_skips_redundant_recap():
    from reviewlib.modes import just_ask as ja_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured["prompt"] = jobs[0].prompt
        return [
            ReviewResult(
                model=j.model, command="fake", returncode=0, stdout="ok", stderr=""
            )
            for j in jobs
        ]

    saved = ja_mod.run_panel
    ja_mod.run_panel = _fake_run_panel
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: ja_mod.mode_just_ask(
                "question",
                ["m1"],
                _DIFF_ALREADY_CAPPED_BIG,
                REPO_ROOT,
                5,
                diff_already_capped=True,
            ),
        )
    finally:
        ja_mod.run_panel = saved
    assert rc == 0, rc
    assert _DIFF_ALREADY_CAPPED_BIG in captured["prompt"]
    assert "diff truncated at" not in captured["prompt"]


def test_just_ask_diff_not_already_capped_still_caps_for_a_direct_caller():
    """The flip side: a direct library caller that does NOT set `diff_already_capped`
    (the default) is still protected — the mode's own dispatch-boundary cap fires."""
    from reviewlib.modes import just_ask as ja_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured["prompt"] = jobs[0].prompt
        return [
            ReviewResult(
                model=j.model, command="fake", returncode=0, stdout="ok", stderr=""
            )
            for j in jobs
        ]

    saved = ja_mod.run_panel
    ja_mod.run_panel = _fake_run_panel
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: ja_mod.mode_just_ask(
                "question", ["m1"], _DIFF_ALREADY_CAPPED_BIG, REPO_ROOT, 5
            ),
        )
    finally:
        ja_mod.run_panel = saved
    assert rc == 0, rc
    assert "diff truncated at" in captured["prompt"]
    assert _DIFF_ALREADY_CAPPED_BIG not in captured["prompt"]


def test_quorum_diff_already_capped_skips_redundant_recap():
    from reviewlib.modes import quorum as q_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured["prompt"] = jobs[0].prompt
        return [
            ReviewResult(
                model=j.model, command="fake", returncode=0, stdout="ok", stderr=""
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary",
            stderr="",
        )

    saved_panel = q_mod.run_panel
    saved_mod = q_mod.run_moderator
    q_mod.run_panel = _fake_run_panel
    q_mod.run_moderator = _fake_run_moderator
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: q_mod.mode_quorum(
                "question",
                ["m1"],
                _DIFF_ALREADY_CAPPED_BIG,
                REPO_ROOT,
                5,
                ["mod"],
                diff_already_capped=True,
            ),
        )
    finally:
        q_mod.run_panel = saved_panel
        q_mod.run_moderator = saved_mod
    assert rc == 0, rc
    assert _DIFF_ALREADY_CAPPED_BIG in captured["prompt"]
    assert "diff truncated at" not in captured["prompt"]


def test_quorum_diff_not_already_capped_still_caps_for_a_direct_caller():
    from reviewlib.modes import quorum as q_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured["prompt"] = jobs[0].prompt
        return [
            ReviewResult(
                model=j.model, command="fake", returncode=0, stdout="ok", stderr=""
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary",
            stderr="",
        )

    saved_panel = q_mod.run_panel
    saved_mod = q_mod.run_moderator
    q_mod.run_panel = _fake_run_panel
    q_mod.run_moderator = _fake_run_moderator
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: q_mod.mode_quorum(
                "question", ["m1"], _DIFF_ALREADY_CAPPED_BIG, REPO_ROOT, 5, ["mod"]
            ),
        )
    finally:
        q_mod.run_panel = saved_panel
        q_mod.run_moderator = saved_mod
    assert rc == 0, rc
    assert "diff truncated at" in captured["prompt"]
    assert _DIFF_ALREADY_CAPPED_BIG not in captured["prompt"]


def test_brainstorm_diff_already_capped_skips_redundant_recap():
    from reviewlib.modes import brainstorm as bs_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured.setdefault("diffs", []).append(jobs[0].diff)
        return [
            ReviewResult(
                model=j.label or j.model,
                command="fake",
                returncode=0,
                stdout="idea",
                stderr="",
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary\nDECISION: STOP",
            stderr="",
        )

    saved_panel = bs_mod.run_panel
    saved_mod = bs_mod.run_moderator
    bs_mod.run_panel = _fake_run_panel
    bs_mod.run_moderator = _fake_run_moderator
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: bs_mod.mode_brainstorm(
                "topic",
                ["m1", "m2"],
                REPO_ROOT,
                5,
                ["mod"],
                rounds=1,
                max_rounds=1,
                diff=_DIFF_ALREADY_CAPPED_BIG,
                diff_already_capped=True,
            ),
        )
    finally:
        bs_mod.run_panel = saved_panel
        bs_mod.run_moderator = saved_mod
    assert rc == 0, rc
    assert captured["diffs"], "expected at least one persona round"
    assert captured["diffs"][0] == _DIFF_ALREADY_CAPPED_BIG


def test_brainstorm_diff_not_already_capped_still_caps_for_a_direct_caller():
    from reviewlib.modes import brainstorm as bs_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured.setdefault("diffs", []).append(jobs[0].diff)
        return [
            ReviewResult(
                model=j.label or j.model,
                command="fake",
                returncode=0,
                stdout="idea",
                stderr="",
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary\nDECISION: STOP",
            stderr="",
        )

    saved_panel = bs_mod.run_panel
    saved_mod = bs_mod.run_moderator
    bs_mod.run_panel = _fake_run_panel
    bs_mod.run_moderator = _fake_run_moderator
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: bs_mod.mode_brainstorm(
                "topic",
                ["m1", "m2"],
                REPO_ROOT,
                5,
                ["mod"],
                rounds=1,
                max_rounds=1,
                diff=_DIFF_ALREADY_CAPPED_BIG,
            ),
        )
    finally:
        bs_mod.run_panel = saved_panel
        bs_mod.run_moderator = saved_mod
    assert rc == 0, rc
    assert captured["diffs"]
    assert "diff truncated at" in captured["diffs"][0]
    assert captured["diffs"][0] != _DIFF_ALREADY_CAPPED_BIG


# ---- diff_from_stdin: the mode-level pipe exemption, for all three flat panel modes ---
# glm review finding, round 2: the README promises a piped diff "is NEVER capped, in any
# mode" — enforced by THREE separately-duplicated `diff if diff_from_stdin or
# diff_already_capped else cap_diff_for_dispatch(diff)` lines (one per mode), plus the
# shared CLI-level condition in `cli.py`. Coverage before this: the CLI-level pipe
# exemption was pinned for brainstorm only (`test_cli_brainstorm_piped_diff_is_not_
# capped`); the MODE-level exemption (what actually protects a direct library caller
# bypassing the CLI, same reasoning as the `diff_already_capped` tests above) had ZERO
# tests for any of the three modes. A regression inverting/dropping `diff_from_stdin` in
# any one of the three duplicated lines would silently truncate piped input with no
# failing test — exactly the class of edit the `diff_already_capped` round-2 fix shows
# this file's authors already made once.
def test_just_ask_diff_from_stdin_is_never_capped():
    from reviewlib.modes import just_ask as ja_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured["prompt"] = jobs[0].prompt
        return [
            ReviewResult(
                model=j.model, command="fake", returncode=0, stdout="ok", stderr=""
            )
            for j in jobs
        ]

    saved = ja_mod.run_panel
    ja_mod.run_panel = _fake_run_panel
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: ja_mod.mode_just_ask(
                "question",
                ["m1"],
                _DIFF_ALREADY_CAPPED_BIG,
                REPO_ROOT,
                5,
                diff_from_stdin=True,
            ),
        )
    finally:
        ja_mod.run_panel = saved
    assert rc == 0, rc
    assert _DIFF_ALREADY_CAPPED_BIG in captured["prompt"]
    assert "diff truncated at" not in captured["prompt"]


def test_quorum_diff_from_stdin_is_never_capped():
    from reviewlib.modes import quorum as q_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured["prompt"] = jobs[0].prompt
        return [
            ReviewResult(
                model=j.model, command="fake", returncode=0, stdout="ok", stderr=""
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary",
            stderr="",
        )

    saved_panel = q_mod.run_panel
    saved_mod = q_mod.run_moderator
    q_mod.run_panel = _fake_run_panel
    q_mod.run_moderator = _fake_run_moderator
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: q_mod.mode_quorum(
                "question",
                ["m1"],
                _DIFF_ALREADY_CAPPED_BIG,
                REPO_ROOT,
                5,
                ["mod"],
                diff_from_stdin=True,
            ),
        )
    finally:
        q_mod.run_panel = saved_panel
        q_mod.run_moderator = saved_mod
    assert rc == 0, rc
    assert _DIFF_ALREADY_CAPPED_BIG in captured["prompt"]
    assert "diff truncated at" not in captured["prompt"]


def test_brainstorm_diff_from_stdin_is_never_capped():
    from reviewlib.modes import brainstorm as bs_mod

    captured: dict = {}

    def _fake_run_panel(jobs, cwd, timeout):
        captured.setdefault("diffs", []).append(jobs[0].diff)
        return [
            ReviewResult(
                model=j.label or j.model,
                command="fake",
                returncode=0,
                stdout="idea",
                stderr="",
            )
            for j in jobs
        ]

    def _fake_run_moderator(candidates, prompt, cwd, timeout, diff="", round_no=0):
        return ReviewResult(
            model=candidates[0],
            command="fake",
            returncode=0,
            stdout="summary\nDECISION: STOP",
            stderr="",
        )

    saved_panel = bs_mod.run_panel
    saved_mod = bs_mod.run_moderator
    bs_mod.run_panel = _fake_run_panel
    bs_mod.run_moderator = _fake_run_moderator
    try:
        rc = _with_env(
            "REVIEW_DIFF_MAX_BYTES",
            "10",
            lambda: bs_mod.mode_brainstorm(
                "topic",
                ["m1", "m2"],
                REPO_ROOT,
                5,
                ["mod"],
                rounds=1,
                max_rounds=1,
                diff=_DIFF_ALREADY_CAPPED_BIG,
                diff_from_stdin=True,
            ),
        )
    finally:
        bs_mod.run_panel = saved_panel
        bs_mod.run_moderator = saved_mod
    assert rc == 0, rc
    assert captured["diffs"]
    assert captured["diffs"][0] == _DIFF_ALREADY_CAPPED_BIG


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
