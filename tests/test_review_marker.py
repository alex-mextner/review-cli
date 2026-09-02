#!/usr/bin/env python3
"""Tests for the agent-tools session review marker (`~/.cache/agent-tools/last-review`).

A successful `review --staged` run must touch the marker file that the separate
agent-tools `require-review-before-commit` agent-hook checks (mtime-windowed), so a
GENUINE review run satisfies that gate without the agent forging the marker. An
unstaged/piped review must NOT touch it (it does not satisfy the staged commit gate),
and a FAILED staged review must NOT touch it either.

Hermetic: routes every backend to the in-process fake (REVIEW_FAKE_BACKEND) — no
network, no CLI — and redirects the marker to a temp path via REVIEW_MARKER. Runs as a
plain script (mirroring tests/test_provider_keys.py / tests/test_streaming.py): each
`test_*` function is invoked by the __main__ block below, NO pytest required (so it runs
under the documented CI runner, `python3 tests/...` via tests/smoke.sh).

Run from the repo root::

    python3 tests/test_review_marker.py
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import install  # noqa: E402
from reviewlib.backends import ReviewResult  # noqa: E402
from reviewlib.config import BoardReviewer  # noqa: E402
from reviewlib.modes import review as review_mode  # noqa: E402
from reviewlib.modes.review import mode_review  # noqa: E402

_MARKER_ENV = (
    "REVIEW_MARKER",
    "REVIEW_FAKE_BACKEND",
    "REVIEW_DIFF_MAX_BYTES",
    "HOME",
    "LC_ALL",
    "LANG",
)


class _EnvSandbox:
    """Snapshot + restore the marker-related env vars so tests don't leak into one
    another (or touch the real ~/.cache/agent-tools/last-review on the dev machine)."""

    def __enter__(self):
        self._saved = {name: os.environ.get(name) for name in _MARKER_ENV}
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        return False


def _init_git_repo(path: Path) -> None:
    """A real git repo so `git rev-parse --git-path review-stamp` resolves (the stamp
    write shares mode_review's success path with the marker touch)."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _make_repo(tmp: Path) -> Path:
    d = tmp / "repo"
    d.mkdir()
    _init_git_repo(d)
    return d


_DIFF = "diff --git a b\n+x\n"


def test_touch_helper_creates_marker_and_parents():
    """The helper makes the parent dir and an empty marker file (idempotent)."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        marker = Path(d) / "deep" / "nested" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        assert not marker.exists()
        install._touch_review_marker()
        assert marker.exists()
        # Re-touch must not raise and keeps the file present (idempotent).
        install._touch_review_marker()
        assert marker.exists()


def test_touch_helper_advances_mtime():
    """The hook is mtime-WINDOWED, so a re-touch must refresh mtime (not just keep the
    file). A regression to write-once-only would pass an exists() check but break the
    gate — pin the mtime advance explicitly."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        marker = Path(d) / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        install._touch_review_marker()
        # Backdate so the second touch must move mtime forward on any fs granularity.
        old = marker.stat().st_mtime
        os.utime(marker, (old - 100, old - 100))
        backdated = marker.stat().st_mtime
        install._touch_review_marker()
        assert marker.stat().st_mtime > backdated, "re-touch must refresh the marker mtime"


def test_touch_helper_expands_default_home_path_when_env_unset():
    """With REVIEW_MARKER unset, the helper must expand the `~/...` DEFAULT_REVIEW_MARKER
    via os.path.expanduser — NOT write a literal `~`-prefixed path. Pin this so a refactor
    that drops expanduser (or a non-`~` default) is caught. HOME is redirected to a temp
    dir so the real ~/.cache is never touched."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        os.environ.pop("REVIEW_MARKER", None)
        os.environ["HOME"] = d  # expanduser resolves ~ against this
        install._touch_review_marker()
        expected = Path(d) / ".cache" / "agent-tools" / "last-review"
        assert expected.exists(), f"default marker should expand to {expected}"
        # And NOT a literal '~' path under the cwd.
        assert not (Path("~") / ".cache").exists()


def test_touch_helper_never_raises_on_bad_path():
    """A marker path whose parent can't be created must be swallowed (best-effort)."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        # Point the marker UNDER a regular file so mkdir(parents=True) fails.
        blocker = Path(d) / "iamafile"
        blocker.write_text("x", encoding="utf-8")
        os.environ["REVIEW_MARKER"] = str(blocker / "child" / "last-review")
        install._touch_review_marker()  # must not raise


def test_staged_review_touches_marker():
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"  # deterministic in-process backend
        rc = mode_review(["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=True)
        assert rc == 0, rc
        assert marker.exists(), "a successful staged review must touch the session marker"


def test_unstaged_review_does_not_touch_marker():
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc = mode_review(["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=False)
        assert rc == 0, rc
        assert not marker.exists(), "an unstaged/piped review must NOT touch the gate marker"


def test_failed_staged_review_does_not_touch_marker():
    """A staged review whose backend FAILS (rc != 0) must NOT satisfy the gate. The
    marker touch is gated on `ok and staged`, so a failed review leaves it absent."""
    def _failing_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return ReviewResult(model=model, command="fake-fail", returncode=1, stdout="", stderr="boom")

    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        # Route the resolved backend to a failing stand-in (the single chokepoint
        # mode_review funnels through). Restore it after, regardless of outcome.
        original = review_mode.resolve_backend
        review_mode.resolve_backend = lambda model: _failing_backend
        try:
            rc = mode_review(["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=True)
        finally:
            review_mode.resolve_backend = original
        assert rc == 1, rc
        assert not marker.exists(), "a FAILED staged review must NOT touch the gate marker"


def test_piped_staged_review_does_not_touch_marker():
    """A diff piped on stdin (`printf ... | review --staged`) is NOT the git index, so it
    must NOT satisfy the commit gate even under --staged — otherwise the mtime-only marker
    would be forgeable for a commit whose staged changes were never reviewed (codex P2).
    The handler passes diff_from_stdin=True, which suppresses the stamp/marker."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc = mode_review(
            ["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=True,
            diff_from_stdin=True,
        )
        assert rc == 0, rc
        assert not marker.exists(), "a piped --staged review must NOT touch the gate marker"


def test_staged_review_returns_zero_even_if_marker_unwritable():
    """Best-effort contract at the INTEGRATION level: if the marker can't be written
    (its parent is a regular file), a successful staged review still returns 0 — the
    marker is a discipline reminder, never a correctness gate that can fail the review."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        blocker = tmp / "iamafile"
        blocker.write_text("x", encoding="utf-8")
        os.environ["REVIEW_MARKER"] = str(blocker / "child" / "last-review")
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc = mode_review(["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=True)
        assert rc == 0, "an unwritable marker must never fail an otherwise-successful review"


# The stderr line an OK-but-not-gate-eligible review must print. Matched on a stable
# fragment, not the full sentence, so wording tweaks don't break the test while the
# CONTRACT (the caller is told the gate is unsatisfied) stays pinned.
_GATE_NOTICE = "not written —"
# Each artifact also has its OWN frame, deliberately: the marker feeds agent-tools' hook
# and the stamp feeds the local git pre-commit hook, so a line naming the wrong one sends
# the reader to fix the wrong file. These pin WHICH artifact a given run blamed.
_MARKER_BLAMED = "commit-gate marker not written"
_STAMP_BLAMED = "diff-scoped review-stamp not written"


def _review_capturing_stderr(**kwargs) -> tuple[int, str]:
    """Run mode_review with stderr captured. `print(..., file=sys.stderr)` resolves
    sys.stderr at call time, so redirect_stderr catches the gate notice."""
    buf = io.StringIO()
    diff = kwargs.pop("diff", _DIFF)
    with contextlib.redirect_stderr(buf):
        rc = mode_review(["codex"], prompt="p", diff=diff, cwd=kwargs.pop("cwd"),
                         timeout=30, **kwargs)
    return rc, buf.getvalue()


def test_unstaged_review_says_why_the_gate_is_unsatisfied():
    """The silent skip is the trap that killed a detached agent: a passing review plus a
    stale marker reads as "the gate is broken", and the agent forges the marker by hand.
    An OK review that does not satisfy the gate must NAME the failed condition."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        os.environ["REVIEW_MARKER"] = str(tmp / "cache" / "last-review")
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc, err = _review_capturing_stderr(cwd=repo, staged=False)
        assert rc == 0, rc
        assert _GATE_NOTICE in err, err
        assert "--staged" in err, "the notice must name the condition that failed"
        assert "commit-gate marker and diff-scoped review-stamp" in err, (
            "an ineligible run writes NEITHER artifact, and naming only one lets a "
            "reader with the other gate installed conclude the line is not about them"
        )


def test_piped_staged_review_says_why_the_gate_is_unsatisfied():
    """Same for the piped-stdin shape: `--staged` was passed, so the reason has to be the
    stdin provenance, not the missing flag."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        os.environ["REVIEW_MARKER"] = str(tmp / "cache" / "last-review")
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc, err = _review_capturing_stderr(cwd=repo, staged=True, diff_from_stdin=True)
        assert rc == 0, rc
        assert _GATE_NOTICE in err, err
        assert "stdin" in err, err


def test_gate_eligible_staged_review_prints_no_notice():
    """The notice is for the UNSATISFIED gate only — a review that writes the marker must
    not also claim it didn't."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc, err = _review_capturing_stderr(cwd=repo, staged=True)
        assert rc == 0, rc
        assert marker.exists()
        assert _GATE_NOTICE not in err, err


def test_failed_staged_review_prints_no_gate_notice():
    """A FAILED review skips silently: the failure output is the reason, and a gate line
    on top of it would be noise pointing at the wrong problem."""
    def _failing_backend(model, prompt, diff, cwd, timeout, round_no=0, effort=None):
        return ReviewResult(model=model, command="fake-fail", returncode=1, stdout="", stderr="boom")

    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        os.environ["REVIEW_MARKER"] = str(tmp / "cache" / "last-review")
        original = review_mode.resolve_backend
        review_mode.resolve_backend = lambda model: _failing_backend
        try:
            rc, err = _review_capturing_stderr(cwd=repo, staged=True)
        finally:
            review_mode.resolve_backend = original
        assert rc == 1, rc
        assert _GATE_NOTICE not in err, err


def test_truncated_staged_review_gets_its_own_remediation():
    """The truncated branch must NOT be told to "re-run --staged": re-running the same
    oversized diff truncates again, so that advice loops — the exact "agent follows the
    tool's own instructions into a wall" failure this notice exists to prevent. Driven at
    the helper (the dispatch cap that produces `truncated=True` is `_warn_if_dispatch_diff_
    truncated`'s business, not this gate's), which is also the only place `truncated` can
    be pinned without a multi-megabyte fixture diff."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            review_mode._stamp_if_staged_commit_review(
                True, True, False, repo, _DIFF, True,
            )
        err = buf.getvalue()
        assert not marker.exists(), "a truncated staged review must not satisfy the gate"
        assert _GATE_NOTICE in err, err
        assert "truncated" in err, err
        assert "split the change" in err, "the truncated branch needs its own remediation"
        assert "stage what you are about to commit" not in err, (
            "looping advice: re-running the same oversized diff truncates again"
        )


def test_unstaged_oversized_review_is_told_to_split_not_to_re_run():
    """END-TO-END counterpart to `test_truncated_reason_wins_over_not_staged`, which only
    drives the helper with a hand-supplied `truncated=True`. That left the CALLER free to
    hand the helper a wrong value, and it did: `_warn_if_dispatch_diff_truncated` used to
    return the truncation fact only for `--staged` runs, so a real unstaged oversized diff
    reached the helper as `truncated=False`, took the not-staged branch, and was told to
    "stage and re-run `--staged`" — which truncates again (codex finding, iteration 4).
    Only a run through `mode_review` with a diff that genuinely exceeds the cap pins the
    caller/helper contract, so this drives the cap for real."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        # A diff comfortably larger than the cap, so `cap_diff_for_dispatch` really cuts
        # it (the cap also has to exceed the truncation marker's own length, or the
        # dispatched text degenerates to marker-only — a shape that proves nothing here).
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "1000"
        big_diff = "diff --git a b\n" + "+x\n" * 2000
        rc, err = _review_capturing_stderr(cwd=repo, staged=False, diff=big_diff)
        assert rc == 0, rc
        assert not marker.exists()
        assert _GATE_NOTICE in err, err
        assert "split the change" in err, (
            "an unstaged OVERSIZED review must get the truncation remediation: staging it "
            "and re-running truncates the same diff again"
        )
        assert "stage what you are about to commit" not in err, err


def test_staged_oversized_review_writes_nothing_and_says_to_split():
    """The STAGED twin of the unstaged oversized run. `test_truncated_staged_review_gets_
    its_own_remediation` drives the helper with a hand-supplied `truncated=True`, which
    proves the helper but not the WIRING: a refactor that hardcoded `truncated=False` on
    the staged path (or crossed the staged/unstaged wiring) would silently stamp an
    oversized staged review again — the exact fail-open the "no default for `truncated`"
    rule exists to prevent — with every existing test still green (Fable finding,
    iteration 8). Only a real cap overflow through `mode_review` pins it."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "1000"
        big_diff = "diff --git a b\n" + "+x\n" * 2000
        rc, err = _review_capturing_stderr(cwd=repo, staged=True, diff=big_diff)
        assert rc == 0, rc
        assert not marker.exists(), (
            "a truncated STAGED review must not satisfy the gate — no seat saw it all"
        )
        assert not (repo / ".git" / "review-stamp").exists(), (
            "and it must not leave a diff-scoped stamp either"
        )
        assert _GATE_NOTICE in err, err
        assert "split the change" in err, err


def test_gate_reason_truth_table():
    """The helper's own truth table: None exactly for the one gate-eligible shape, and a
    non-empty (reason, remediation) pair for every other."""
    assert review_mode._commit_gate_skip_reason(True, False, False) is None
    for args in ((False, False, False), (True, True, False), (True, False, True)):
        skipped = review_mode._commit_gate_skip_reason(*args)
        assert skipped is not None, args
        reason, remediation = skipped
        assert reason and remediation, args


def test_truncated_reason_wins_over_not_staged():
    """Compound failure ordering: an UNSTAGED and TRUNCATED run must hear "split the
    change", not "stage it and re-run `--staged`". Staging fixes the staged condition but
    not the truncation, so the staged-first advice costs a whole extra review round and
    then lands on the truncation anyway — advice that loops is the failure mode this
    notice exists to remove, so the ordering is part of the contract, not an accident."""
    skipped = review_mode._commit_gate_skip_reason(False, False, True)
    assert skipped is not None
    reason, remediation = skipped
    assert "truncated" in reason, reason
    assert "split the change" in remediation, remediation


def test_stamp_asks_the_helper_instead_of_re_spelling_the_predicate():
    """The contract `test_gate_reason_truth_table` canNOT pin: that
    `_stamp_if_staged_commit_review` DELEGATES to `_commit_gate_skip_reason` rather than
    re-spelling the same conditions inline. A previous version of this test only walked
    the helper's truth table, so the regression it claimed to guard — someone adding a
    fourth condition to the caller's `if` alone, where it would skip the warning silently
    — passed it untouched (iteration-2 review finding).

    Driving the caller with the helper PATCHED pins the delegation in both directions:
    a forced None must stamp a shape that is otherwise ineligible, and a forced reason
    must block a shape that is otherwise eligible and surface that exact reason."""
    original = review_mode._commit_gate_skip_reason
    sentinel = ("SENTINEL-REASON", "SENTINEL-REMEDIATION")
    try:
        with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            repo = _make_repo(tmp)
            marker = tmp / "cache" / "eligible"
            os.environ["REVIEW_MARKER"] = str(marker)
            review_mode._commit_gate_skip_reason = lambda *a, **k: None
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                # Unstaged + piped + truncated: ineligible by every real condition.
                review_mode._stamp_if_staged_commit_review(
                    True, False, True, repo, _DIFF, True,
                )
            assert marker.exists(), (
                "the caller re-spelled the predicate instead of asking the helper"
            )
            assert _GATE_NOTICE not in buf.getvalue(), buf.getvalue()

        with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            repo = _make_repo(tmp)
            marker = tmp / "cache" / "ineligible"
            os.environ["REVIEW_MARKER"] = str(marker)
            review_mode._commit_gate_skip_reason = lambda *a, **k: sentinel
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                # Staged, from the index, untruncated: eligible by every real condition.
                review_mode._stamp_if_staged_commit_review(
                    True, True, False, repo, _DIFF, False,
                )
            err = buf.getvalue()
            assert not marker.exists(), "the caller ignored the helper's skip reason"
            assert sentinel[0] in err and sentinel[1] in err, err
    finally:
        review_mode._commit_gate_skip_reason = original


def test_gate_eligible_review_reports_a_failed_marker_write():
    """The one branch that actually ATTEMPTS the write must report losing it. The write
    is best-effort (an unwritable marker never fails a review — pinned separately by
    `test_staged_review_returns_zero_even_if_marker_unwritable`), but a silent loss
    rebuilds the original trap one level down: review green, marker stale, no reason
    anywhere, caller forges the marker by hand. Three review seats raised this branch
    independently on iteration 2."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        blocker = tmp / "iamafile"
        blocker.write_text("x", encoding="utf-8")
        marker = blocker / "child" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc, err = _review_capturing_stderr(cwd=repo, staged=True)
        assert rc == 0, "a failed marker write must not fail the review"
        assert not marker.exists()
        assert _MARKER_BLAMED in err, err
        assert _STAMP_BLAMED not in err, "only the marker write failed here"
        assert "could not be written" in err, err
        assert "REVIEW_MARKER" in err, "the notice must name the knob that fixes it"


def test_marker_pointing_at_a_directory_is_reported_as_a_failure():
    """`Path.touch()` on an existing DIRECTORY succeeds — it bumps the directory mtime —
    so without an explicit regular-file check a `REVIEW_MARKER` pointing at a directory
    reads as "marker written" while no marker file exists. The gate then blocks the
    commit right after review-cli claimed success: the silent-failure shape this whole
    change exists to remove (codex finding, iteration 5)."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        as_dir = tmp / "marker-is-a-dir"
        as_dir.mkdir()
        os.environ["REVIEW_MARKER"] = str(as_dir)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        rc, err = _review_capturing_stderr(cwd=repo, staged=True)
        assert rc == 0, "a bad marker target must not fail an otherwise-successful review"
        assert as_dir.is_dir(), "the directory must be left alone, not replaced"
        assert _GATE_NOTICE in err, err
        assert "not a regular file" in err, err


def test_unstaged_oversized_commit_run_still_fails_on_the_staged_requirement():
    """`_warn_if_dispatch_diff_truncated` now returns the plain truncation fact instead of
    a `--staged`-scoped one, so the staged-scoping lives in its callers. This pins the
    caller that could quietly change meaning: `--commit` without `--staged` must still be
    rejected as a usage error BEFORE dispatch, not reinterpreted as a truncation refusal
    (Fable finding, iteration 5 — the widened boolean must not leak into the checkpoint
    path)."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        os.environ["REVIEW_DIFF_MAX_BYTES"] = "1000"
        big_diff = "diff --git a b\n" + "+x\n" * 2000
        rc, err = _review_capturing_stderr(
            cwd=repo, staged=False, diff=big_diff, commit=True
        )
        assert rc == review_mode.EXIT_COMMIT_REQUIRES_STAGED, rc
        assert not marker.exists()
        assert "requires --staged" in err, err


def test_unwritable_review_stamp_is_reported_even_when_the_marker_writes():
    """Two gates, two files, two ways to be silently blocked. agent-tools' hook stats the
    session MARKER; the local git pre-commit hook verifies the diff-scoped REVIEW-STAMP.
    With a healthy marker and an unwritable `.git/review-stamp`, the old code wrote the
    marker, swallowed the stamp failure, and returned green — and the commit was then
    rejected by the git hook with nothing anywhere explaining why (codex finding,
    iteration 6). The stamp failure must get its own line naming its own file."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        # A DIRECTORY at the stamp path: the write raises, the marker is untouched by it.
        stamp = repo / ".git" / "review-stamp"
        stamp.mkdir(parents=True, exist_ok=True)
        rc, err = _review_capturing_stderr(cwd=repo, staged=True)
        assert rc == 0, "a failed stamp write must not fail the review"
        assert marker.exists(), "the marker write is independent and must still happen"
        assert _STAMP_BLAMED in err, err
        assert _MARKER_BLAMED not in err, (
            "the marker WAS written here — claiming otherwise sends the reader to "
            "diagnose $REVIEW_MARKER while the broken file is the stamp"
        )


def test_empty_review_marker_env_falls_back_to_the_default_path():
    """An exported-but-EMPTY `REVIEW_MARKER=` must mean the same as unset. Read naively it
    becomes `Path("")` — the current directory — so review-cli would "write" the marker by
    bumping the cwd's mtime while the gate (which normalizes the same way, agent-tools#506)
    watches its default path, or vice versa. Both sides must normalize identically."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        home = Path(d) / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["REVIEW_MARKER"] = ""
        # Run from a scratch cwd so the "did it touch the current directory instead"
        # half of the contract can actually be observed. `Path.cwd() / ""` is just the
        # cwd — asserting `.is_file()` on it is a tautology that stays green through a
        # revert of the normalization (Fable finding), so watch the MTIME instead.
        scratch = Path(d) / "cwd"
        scratch.mkdir()
        before = os.stat(scratch).st_mtime_ns
        original_cwd = os.getcwd()
        os.chdir(scratch)
        try:
            assert install._touch_review_marker() is None
        finally:
            os.chdir(original_cwd)
        default = Path(os.path.expanduser(install.DEFAULT_REVIEW_MARKER))
        assert default.is_file(), "an empty value must resolve to the default marker path"
        assert os.stat(scratch).st_mtime_ns == before, (
            "an empty REVIEW_MARKER degenerated to the current directory and 'wrote' the "
            "marker by bumping its mtime"
        )


def test_unresolvable_stamp_path_is_reported_but_a_non_repo_is_not():
    """`git rev-parse --git-path review-stamp` failing has two very different meanings and
    used to share one silent return (codex P1 + Fable, iteration 9). Outside a repo there
    is no stamp-keyed gate, so silence is correct. A failure INSIDE one — dubious
    ownership in a container, metadata briefly unreadable in a shared worktree — leaves
    the gate installed and the stamp missing, which must be said out loud."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        not_a_repo = tmp / "plain"
        not_a_repo.mkdir()
        assert install._write_review_stamp(not_a_repo, _DIFF) is None, (
            "outside a git repo there is no stamp gate to satisfy — stay quiet"
        )
        # Same, under a localized shell: the probe pins LC_ALL=C, so git's message stays
        # matchable and a non-repo does not turn into a spurious stamp warning.
        os.environ["LC_ALL"] = "de_DE.UTF-8"
        os.environ["LANG"] = "de_DE.UTF-8"
        assert install._write_review_stamp(not_a_repo, _DIFF) is None, (
            "a localized git must not make a non-repo look like a failed stamp write"
        )

    original = subprocess.run

    def _dubious_ownership(cmd, *args, **kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(
                cmd, 128, stdout="", stderr="fatal: detected dubious ownership in repository\n"
            )
        return original(cmd, *args, **kwargs)

    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        install.subprocess.run = _dubious_ownership
        try:
            error = install._write_review_stamp(repo, _DIFF)
        finally:
            install.subprocess.run = original
        assert error and "dubious ownership" in error, error


def test_marker_writer_reports_the_error_it_swallows():
    """Unit-level counterpart: `_touch_review_marker` returns None on success and the OS
    error text on failure, and still never raises."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["REVIEW_MARKER"] = str(tmp / "cache" / "last-review")
        assert install._touch_review_marker() is None
        blocker = tmp / "iamafile"
        blocker.write_text("x", encoding="utf-8")
        os.environ["REVIEW_MARKER"] = str(blocker / "child" / "last-review")
        error = install._touch_review_marker()
        assert error, "a swallowed OSError must still be reported to the caller"


def test_unstaged_board_review_says_why_the_gate_is_unsatisfied():
    """The board path routes through the same helper as the flat path, so it must also
    explain itself — otherwise the incident recurs on every board (multi-role) run, which
    is the DEFAULT shape of `review diff`."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        board = [
            BoardReviewer(model="codex", role="correctness", display="codex"),
            BoardReviewer(model="claude", role="security", display="claude"),
        ]
        rc, err = _review_capturing_stderr(cwd=repo, staged=False, board=board, pool_size=2)
        assert rc == 0, rc
        assert not marker.exists()
        assert _GATE_NOTICE in err, err


def test_staged_board_review_touches_marker():
    """The board path (run_board_with_failover) shares the same `ok and staged` gate, so
    a successful, non-degraded staged board review must also touch the marker. With
    REVIEW_FAKE_BACKEND every seat is usable, so the pool is not degraded."""
    with _EnvSandbox(), tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo = _make_repo(tmp)
        marker = tmp / "cache" / "agent-tools" / "last-review"
        os.environ["REVIEW_MARKER"] = str(marker)
        os.environ["REVIEW_FAKE_BACKEND"] = "1"
        board = [
            BoardReviewer(model="codex", role="correctness", display="codex"),
            BoardReviewer(model="claude", role="security", display="claude"),
        ]
        rc = mode_review(
            ["codex"], prompt="p", diff=_DIFF, cwd=repo, timeout=30, staged=True,
            board=board, pool_size=2,
        )
        assert rc == 0, rc
        assert marker.exists(), "a successful staged board review must touch the session marker"


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
