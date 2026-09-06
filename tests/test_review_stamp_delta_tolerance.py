#!/usr/bin/env python3
"""Tests for the review-stamp trivial-follow-up delta tolerance (review-cli#208).

Background: the pre-commit gate (`install._PRECOMMIT`, and its byte-identical twin
`agent-tools/git-hooks/global-dispatcher/hooks/review-gate`) used to require the CURRENT
staged diff's sha256 to exactly match the hash written by the last `review diff --staged`
pass. Any restage -- even a one-line follow-up fix -- produced a different hash and forced
a brand-new full multi-model review round. Alex (tg, replying to a report of 9 full review
rounds triggered by trivial fixes on one ticket): "сделать так чтобы только после ребейза
сравнивалось какой дифф был и какой стал, а в процессе не надо" -- small in-progress
follow-ups should not force a fresh review; only a genuinely different diff should.

The fix: `_write_review_stamp` now ALSO writes the raw reviewed diff TEXT to a companion
`review-stamp-diff` file. On a hash MISS, the gate re-diffs the current staged diff against
that stored text and allows the commit when the line-level delta is within
`$REVIEW_TRIVIAL_DELTA_LINES` (default 10) -- without dispatching a fresh review. The
baseline is only ever advanced by a REAL review pass (this test file never calls the gate
in a way that would move it), so drift is always measured from the last genuine review.

These tests exercise the ACTUAL production `_PRECOMMIT` shell script (not a re-implemented
copy of its logic) via subprocess, mirroring agent-tools' tests/test_global_review_gate.py
pattern -- the most faithful way to prove the shipped hook script behaves as designed.

Hermetic: real temp git repos, no network, no backend calls (never invokes `review diff`
itself, only the stamp-writing helper + the gate script directly). Each `test_*` function is
invoked by the __main__ block below, NO pytest required (mirrors tests/test_review_marker.py
so it runs under the documented CI runner, `python3 tests/smoke.py`).

Run from the repo root::

    python3 tests/test_review_stamp_delta_tolerance.py
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make the in-repo package importable without an install (mirrors the bin/review shim).
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import install  # noqa: E402

_BASELINE_LINE_COUNT = 20

# Isolated so a hook invocation never resolves the REAL machine's global core.hooksPath.
_ISOLATION_ENV_VARS = ("HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


@contextlib.contextmanager
def _isolated_repo():
    """A temp git repo with HOME/GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM redirected into the
    temp dir for the duration of the block.

    Without this, `_PRECOMMIT`'s own "A global core.hooksPath shadows each repo's own
    pre-commit, so run it explicitly first" step (`local_hook="$(git rev-parse --git-path
    hooks/pre-commit)"`) resolves against whatever `core.hooksPath` is configured on the
    REAL machine running this test -- on a dev box with review-cli's own commit gate
    installed (as this one is), that silently shadows-in and runs the machine's real
    deployed hook instead of the `_PRECOMMIT` string under test, producing failures whose
    error text belongs to a DIFFERENT script (discovered the hard way: this test file's
    first draft failed with agent-tools' review-gate wording, not review-cli's own)."""
    saved = {name: os.environ.get(name) for name in _ISOLATION_ENV_VARS}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["HOME"] = str(tmp)
        os.environ["GIT_CONFIG_GLOBAL"] = str(tmp / "empty-gitconfig")
        os.environ["GIT_CONFIG_SYSTEM"] = str(tmp / "empty-gitconfig")
        try:
            repo = tmp / "repo"
            repo.mkdir()
            _init_git_repo(repo)
            yield repo
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _install_hook(repo: Path) -> Path:
    """Write the REAL `_PRECOMMIT` script content to a plain file (not `.git/hooks/`, so
    git itself never auto-invokes it) and run it directly via subprocess -- the same
    faithful-to-production approach agent-tools' test_global_review_gate.py uses for its
    byte-identical twin script."""
    hook = repo / "pre-commit-under-test"
    hook.write_text(install._PRECOMMIT, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    return hook


def _run_hook(
    hook: Path, repo: Path, env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Never let the dev's own shell leak an ambient bypass/threshold into a test: REVIEW_SKIP
    # (the --no-verify-adjacent escape hatch) and REVIEW_TRIVIAL_DELTA_LINES (this feature's
    # own opt-out/threshold knob) must be controlled ONLY by `env_extra`, or a machine/CI job
    # that happens to export either would make these tests spuriously pass or fail (glm review
    # finding on this feature's own PR).
    env.pop("REVIEW_SKIP", None)
    env.pop("REVIEW_TRIVIAL_DELTA_LINES", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [str(hook)], cwd=repo, env=env, capture_output=True, text=True
    )


def _staged_diff(repo: Path) -> str:
    """The exact diff text `_write_review_stamp`/the gate hash -- `git diff --no-ext-diff
    --cached`, matching both production call sites verbatim."""
    return subprocess.run(
        ["git", "diff", "--no-ext-diff", "--cached"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _staged_diff_bytes(repo: Path) -> bytes:
    """Raw-bytes twin of `_staged_diff` -- `text=True`'s strict UTF-8 decode raises on
    a genuinely invalid byte sequence (not on a bare NUL, which is valid UTF-8, but
    tests that construct arbitrary byte content should not depend on that)."""
    return subprocess.run(
        ["git", "diff", "--no-ext-diff", "--cached"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout


def _write_and_stage(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)


def _git_path(repo: Path, rel: str) -> Path:
    """Resolve `git rev-parse --git-path <rel>` the same way production code does (absolute
    result used as-is; relative result joined onto `repo`)."""
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", rel],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(out) if os.path.isabs(out) else repo / out


def _stage_baseline(repo: Path) -> str:
    """Stage + review-stamp a 20-line baseline file; returns the reviewed diff text."""
    content = "\n".join(f"line{i}" for i in range(_BASELINE_LINE_COUNT)) + "\n"
    _write_and_stage(repo, "a.py", content)
    diff = _staged_diff(repo)
    install._write_review_stamp(repo, diff)
    return diff


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True)


def test_exact_match_still_passes():
    """Regression guard: an untouched, exactly-matching restage keeps passing via the
    original hash check (this fast path must not have regressed)."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)
        proc = _run_hook(hook, repo)
        assert proc.returncode == 0, proc.stderr


def test_trivial_followup_is_accepted_without_fresh_review():
    """AC(a): a small follow-up (2 of 20 lines changed, well under the default 10-line
    threshold) on top of a reviewed baseline is accepted WITHOUT a hash match and without
    dispatching a new review."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)
        fixed = (
            "\n".join(
                (f"line{i}-fixed" if i in (3, 4) else f"line{i}")
                for i in range(_BASELINE_LINE_COUNT)
            )
            + "\n"
        )
        _write_and_stage(repo, "a.py", fixed)
        proc = _run_hook(hook, repo)
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_substantive_change_after_same_baseline_is_still_blocked():
    """AC(b): a genuinely new change (every line rewritten, far past the threshold) after
    the SAME reviewed baseline must still require a fresh review -- the tolerance is not a
    blanket bypass."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)
        rewritten = (
            "\n".join(f"totally-new-{i}" for i in range(_BASELINE_LINE_COUNT)) + "\n"
        )
        _write_and_stage(repo, "a.py", rewritten)
        proc = _run_hook(hook, repo)
        assert proc.returncode == 1, proc.stdout
        assert "staged changes have not been reviewed" in proc.stderr


def test_no_baseline_diff_falls_back_to_zero_tolerance():
    """AC(c): a stamp with no `review-stamp-diff` companion (e.g. written before this
    feature existed, or the companion write failed) must behave EXACTLY as before -- only
    an exact hash match passes, even for a one-line follow-up."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)
        _git_path(repo, "review-stamp-diff").unlink()  # simulate a pre-#208 stamp
        one_line_fix = (
            "\n".join(
                (f"line{i}-fixed" if i == 3 else f"line{i}")
                for i in range(_BASELINE_LINE_COUNT)
            )
            + "\n"
        )
        _write_and_stage(repo, "a.py", one_line_fix)
        proc = _run_hook(hook, repo)
        assert proc.returncode == 1, proc.stdout


def test_threshold_zero_disables_tolerance():
    """The explicit opt-out (`REVIEW_TRIVIAL_DELTA_LINES=0`) restores today's
    exact-hash-only behavior even with a companion diff present."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)
        one_line_fix = (
            "\n".join(
                (f"line{i}-fixed" if i == 3 else f"line{i}")
                for i in range(_BASELINE_LINE_COUNT)
            )
            + "\n"
        )
        _write_and_stage(repo, "a.py", one_line_fix)
        proc = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "0"})
        assert proc.returncode == 1, proc.stdout


def test_custom_threshold_is_honored():
    """A caller-supplied threshold is honored in BOTH directions: a delta within a raised
    threshold passes; the same delta exceeds a lowered threshold and blocks."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)
        # 4 changed lines.
        fixed = (
            "\n".join(
                (f"line{i}-fixed" if i in (1, 2, 3, 4) else f"line{i}")
                for i in range(_BASELINE_LINE_COUNT)
            )
            + "\n"
        )
        _write_and_stage(repo, "a.py", fixed)
        blocked = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "2"})
        assert blocked.returncode == 1, blocked.stdout
        allowed = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "20"})
        assert allowed.returncode == 0, allowed.stderr


def test_write_review_stamp_writes_companion_diff_file():
    """`_write_review_stamp` writes both the hash stamp AND the new `review-stamp-diff`
    companion: a first line recording the reviewed HEAD (Opus round-3 finding -- binds
    the tolerance anchor so it can't outlive a commit, see `_write_review_stamp_diff`'s
    docstring), then the reviewed diff text verbatim. `_stage_baseline` reviews before
    any commit exists, so the recorded HEAD line is empty here -- the real, matchable
    "no commits yet" state, not a sentinel."""
    with _isolated_repo() as repo:
        diff = _stage_baseline(repo)
        stamp_diff = _git_path(repo, "review-stamp-diff")
        assert stamp_diff.is_file()
        assert stamp_diff.read_text(encoding="utf-8") == f"\n{diff}"


def test_write_review_stamp_diff_failure_does_not_break_hash_stamp():
    """A failure while writing the companion file (e.g. an unwritable path) must not take
    down the primary hash stamp -- mirrors `_write_review_stamp`'s own best-effort
    contract. Simulated by making `review-stamp-diff`'s parent read-only isn't portable
    across CI filesystems, so this instead points REVIEW's git-path resolution at a
    directory (a git-path that already exists as a dir can't be written as a file) and
    checks the hash stamp still lands."""
    with _isolated_repo() as repo:
        _write_and_stage(repo, "a.py", "line1\n")
        diff = _staged_diff(repo)
        stamp_diff_path = _git_path(repo, "review-stamp-diff")
        stamp_diff_path.mkdir(parents=True, exist_ok=True)  # occupy the path with a dir
        install._write_review_stamp(repo, diff)
        stamp_path = _git_path(repo, "review-stamp")
        assert stamp_path.is_file(), "the hash stamp must still be written"
        import hashlib

        assert (
            stamp_path.read_text(encoding="utf-8").strip()
            == hashlib.sha256(diff.encode("utf-8")).hexdigest()
        )


def test_length_changing_followup_on_multi_hunk_committed_file_is_still_trivial():
    """GLM review finding on this feature's own PR: a naive `diff -U0` line count over the
    two raw diff TEXTS includes diff-generation METADATA -- the `index <old>..<new>` line
    (always changes when a file's content changes) and every `@@ -a,b +c,d @@` hunk header
    downstream of a length-changing edit (each shifts, so its outer line also "changes").
    A 30-line COMMITTED file (so `git diff --cached` shows real hunks with context, not a
    single new-file hunk) is reviewed with 3 separate one-line edits (3 hunks); the
    follow-up ALSO inserts one line near the top, shifting all 3 hunks' line numbers. The
    genuine content delta is tiny (one inserted line), but the metadata delta alone
    (`index` +2, 3 shifted headers +6) exceeded the default 10-line threshold under the
    ORIGINAL (metadata-counting) implementation -- verified empirically before the fix
    (real count: 11, just over the default threshold), which is exactly the failure this
    test pins: the gate must still accept it as trivial."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        lines = [f"line{i}" for i in range(30)]
        (repo / "f.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "f.py"], check=True)
        _commit_all(repo, "initial")

        edited = list(lines)
        edited[5] = "line5-r"
        edited[15] = "line15-r"
        edited[25] = "line25-r"
        _write_and_stage(repo, "f.py", "\n".join(edited) + "\n")
        baseline_diff = _staged_diff(repo)
        assert baseline_diff.count("\n@@ ") + baseline_diff.startswith("@@ ") >= 1
        install._write_review_stamp(repo, baseline_diff)

        followup = list(edited)
        followup.insert(
            1, "inserted-line"
        )  # shifts every downstream hunk's line numbers
        _write_and_stage(repo, "f.py", "\n".join(followup) + "\n")
        proc = _run_hook(hook, repo)
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_binary_file_swap_after_baseline_is_still_blocked():
    """k3 review finding (Security) on this feature's own PR: binary content is invisible
    to a line-count ruler -- `git diff --cached` on a binary file emits only an `index`
    line + `Binary files ... differ`, so swapping an ARBITRARY, arbitrarily large binary
    payload after a reviewed baseline could otherwise measure as ~0 delta and sail through
    unreviewed. The gate must fail CLOSED on unmeasurable (binary) content, not silently
    treat it as trivial."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        (repo / "img.bin").write_bytes(b"\x00\x01\x02\x03binary-payload-v1" * 50)
        subprocess.run(["git", "-C", str(repo), "add", "img.bin"], check=True)
        baseline_diff = _staged_diff(repo)
        assert "Binary files" in baseline_diff, baseline_diff
        install._write_review_stamp(repo, baseline_diff)

        # Swap the binary payload for something completely different -- an arbitrary,
        # unreviewed content change that a line-count heuristic cannot see.
        (repo / "img.bin").write_bytes(
            b"\xff\xfe\xfd\xfcTOTALLY-DIFFERENT-PAYLOAD" * 200
        )
        subprocess.run(["git", "-C", str(repo), "add", "img.bin"], check=True)
        proc = _run_hook(hook, repo)
        assert proc.returncode == 1, proc.stdout
        assert "staged changes have not been reviewed" in proc.stderr


def test_gitlink_bump_after_baseline_is_still_blocked():
    """k3 review finding (Security), the submodule-pointer variant: a gitlink bump shows
    only two `Subproject commit` lines regardless of how much the submodule's actual
    content changed between the two commits it points at. Constructed via
    `update-index --cacheinfo` (a real gitlink entry, mode 160000) without needing an
    actual submodule checkout -- `git diff --cached` renders the same `Subproject commit`
    lines either way."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        sha_a = "a" * 40
        sha_b = "b" * 40
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "update-index",
                "--add",
                "--cacheinfo",
                f"160000,{sha_a},vendor/sub",
            ],
            check=True,
        )
        _commit_all(repo, "add gitlink")
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "update-index",
                "--cacheinfo",
                f"160000,{sha_b},vendor/sub",
            ],
            check=True,
        )
        baseline_diff = _staged_diff(repo)
        assert "Subproject commit" in baseline_diff, baseline_diff
        install._write_review_stamp(repo, baseline_diff)

        sha_c = "c" * 40
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "update-index",
                "--cacheinfo",
                f"160000,{sha_c},vendor/sub",
            ],
            check=True,
        )
        proc = _run_hook(hook, repo)
        assert proc.returncode == 1, proc.stdout


def test_invalid_threshold_value_falls_back_to_default():
    """A non-numeric or empty `REVIEW_TRIVIAL_DELTA_LINES` must fall back to the default
    (10), not silently disable the tolerance (empty/non-numeric could otherwise pass `-gt 0`
    unpredictably depending on the shell) nor crash the hook."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)
        # 2 changed lines -- within the default-10 fallback, would be BLOCKED under a
        # literal (unsanitized) "abc" threshold if `[ "$changed" -le "$threshold" ]` ran a
        # string compare or errored out instead of falling back.
        fixed = (
            "\n".join(
                (f"line{i}-fixed" if i in (3, 4) else f"line{i}")
                for i in range(_BASELINE_LINE_COUNT)
            )
            + "\n"
        )
        _write_and_stage(repo, "a.py", fixed)
        proc = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "abc"})
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_delta_exactly_at_threshold_boundary_passes_one_line_over_blocks():
    """Boundary check for the `[ "$changed" -le "$threshold" ]` comparison: a delta that
    lands EXACTLY on the configured threshold must pass (`-le` is inclusive by design --
    the threshold is the maximum tolerated size, not an exclusive ceiling), and a delta of
    threshold+1 must block. Both invocations re-diff against the SAME stamped baseline
    (never re-stamped in between), isolating the boundary itself as the only variable.

    Also pins the `changed = max(added, removed)` fix (review-cli#208 round-2): each
    modified line here appears in the outer diff-of-diffs as a `-old`/`+new` PAIR, so
    under the old raw-line-count formula 3 edited lines would have raw-counted as 6 --
    this test's threshold of 3 would then have wrongly BLOCKED a delta the design intends
    to accept. `max(added, removed)` reports the real edited-line count (3), matching the
    threshold's documented meaning ("N changed lines")."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)  # 20-line new-file baseline, "line{i}" content, stamped
        at_limit = (
            "\n".join(
                (f"line{i}-fixed" if i in (1, 2, 3) else f"line{i}")
                for i in range(_BASELINE_LINE_COUNT)
            )
            + "\n"
        )
        _write_and_stage(repo, "a.py", at_limit)
        proc = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "3"})
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"

        one_over = (
            "\n".join(
                (f"line{i}-fixed" if i in (1, 2, 3, 4) else f"line{i}")
                for i in range(_BASELINE_LINE_COUNT)
            )
            + "\n"
        )
        _write_and_stage(repo, "a.py", one_over)
        proc = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "3"})
        assert proc.returncode == 1, proc.stdout


def test_multi_file_diff_excludes_index_metadata_across_both_files():
    """AC: the `index <old>..<new>` metadata-line exclusion (the GLM-cc-last finding
    documented above `_TRIVIAL_DELTA_BLOCK`) must hold across MULTIPLE touched files in one
    diff, not just the single-file case every other test in this suite exercises -- each
    touched file gets its OWN `index` line (a blob hash that changes whenever that file's
    content changes), so a count that fails to exclude it scales with the NUMBER OF FILES
    touched, not the number of lines actually edited.

    Two committed files are each edited by one real line in the baseline (stamped), then
    each edited by one MORE real line in the follow-up -- 2 genuine edited lines total,
    spread across 2 files, each contributing its own changed `index` line. The follow-up
    edit lands ADJACENT to the baseline edit (within the same hunk's context window) so the
    only structural change is the `index` line and the new content pair, not a relocated or
    newly-split hunk -- isolating exactly the metadata this test targets. Empirically
    verified real count: 6 (well under the default 10-line threshold; a naive count that
    fails to exclude the 2 `index` lines would still pass here too, so this test's real
    value is pinning the BEHAVIOR -- both files' `index` lines demonstrably differ between
    baseline and follow-up, and the gate still accepts the follow-up as trivial)."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        nlines = 20
        b_lines = [f"b-line{i}" for i in range(nlines)]
        c_lines = [f"c-line{i}" for i in range(nlines)]
        (repo / "b.py").write_text("\n".join(b_lines) + "\n", encoding="utf-8")
        (repo / "c.py").write_text("\n".join(c_lines) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "b.py", "c.py"], check=True)
        _commit_all(repo, "initial: two files")

        b_edit1 = list(b_lines)
        b_edit1[10] = "b-line10-r1"
        c_edit1 = list(c_lines)
        c_edit1[10] = "c-line10-r1"
        _write_and_stage(repo, "b.py", "\n".join(b_edit1) + "\n")
        _write_and_stage(repo, "c.py", "\n".join(c_edit1) + "\n")
        baseline_diff = _staged_diff(repo)
        assert baseline_diff.count("\nindex ") == 2, baseline_diff
        install._write_review_stamp(repo, baseline_diff)

        b_edit2 = list(b_edit1)
        b_edit2[11] = "b-line11-r2"
        c_edit2 = list(c_edit1)
        c_edit2[11] = "c-line11-r2"
        _write_and_stage(repo, "b.py", "\n".join(b_edit2) + "\n")
        _write_and_stage(repo, "c.py", "\n".join(c_edit2) + "\n")
        cur_diff = _staged_diff(repo)
        assert cur_diff.count("\nindex ") == 2, cur_diff
        # Both files' index lines genuinely differ from the stamped baseline -- the
        # exclusion is actually exercised here, not vacuously true.
        for line in cur_diff.splitlines():
            if line.startswith("index "):
                assert line not in baseline_diff, (line, baseline_diff)

        proc = _run_hook(hook, repo)
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_new_file_followup_excludes_file_header_metadata():
    """review-cli#208 round-3 finding (Opus + Fable, independently, same review round): the
    `index `/`@@ ` exclusion covers a length-changing edit inside an ALREADY-TOUCHED file,
    but not a follow-up that adds a WHOLE NEW file -- that pulls in per-file diff-generation
    lines the original exclusion never listed: `diff --git a/x b/x`, `--- /dev/null`,
    `+++ b/x`, `new file mode NNNNNN`. Left unexcluded, a genuinely tiny 3-line new file
    raw-counts as 7 (verified empirically before this fix), which can tip a small, honestly
    trivial follow-up past the default 10-line threshold and force a full review anyway --
    exactly the failure this feature exists to prevent. Direction was always safe (it
    over-counts, never under -- no unreviewed change could slip through undercounted), so
    this was a correctness-vs-intent gap, not a security hole.

    A reviewed 20-line baseline (one file) is followed by staging a BRAND NEW 3-line file
    with nothing else changed -- the real, honest delta is exactly 3 lines."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)  # 20-line new-file baseline "a.py", stamped

        (repo / "new.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "new.py"], check=True)
        cur_diff = _staged_diff(repo)
        assert "new file mode" in cur_diff, cur_diff

        proc = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "5"})
        assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"


def test_deleted_file_of_comment_lines_is_not_excluded_as_diff_header():
    """review-cli#208 round-4 finding (Opus, on round-3's own fix, same PR) [Security --
    undercount]: round-3's exclusion pattern (bare `--- `/`+++ `, see the comment above
    `_TRIVIAL_DELTA_BLOCK`) collides with a REMOVED source line whose own text starts with
    `-- ` (SQL/Lua/Haskell line-comment syntax) -- git's diff renders that removal as its own
    `-` marker directly followed by the source text `-- comment one`, i.e. literally
    `--- comment one`, which the bare round-3 pattern wrongly matched as "just a file header"
    and excluded from the count, indistinguishable from a genuine `--- a/path` header line.
    Enough such collisions make a genuine unreviewed deletion read as `changed == 0` and slip
    through the gate unreviewed -- the one direction this mechanism must never take. Fixed by
    anchoring the pattern to git's actual header shapes (`--- a/`, `--- /dev/null`, `+++ b/`,
    `+++ /dev/null`), which ordinary comment content cannot spell.

    A pre-existing committed file `q.sql` whose ENTIRE content is three `-- comment N` lines
    is left untouched by the reviewed baseline (which only stages an unrelated new file,
    `a.py`) -- so `q.sql` appears nowhere in the stamped baseline diff. The follow-up then
    stages `git rm q.sql`, deleting the whole file with nothing else changed: a genuine
    3-line unreviewed deletion, entirely new to the outer diff-of-diffs, where every single
    content line collides with the vulnerable pattern.

    This is the design that actually discriminates the bug from the fix (verified
    empirically): because the whole q.sql deletion block is new-to-cur-diff, its genuine
    header lines (`diff --git`, `deleted file mode`, `index`, `--- a/q.sql`, `+++ /dev/null`,
    `@@ `) are correctly excluded either way, leaving ONLY the 3 collision-prone content
    lines as the observable delta -- so `changed` reads exactly 3 under the fixed (tightened)
    pattern but exactly 0 under the reverted bare round-3 pattern, a direct 0-vs-3 flip. With
    the threshold lowered to 2 (below the real 3-line delta), the gate must BLOCK: under the
    bare pattern this would incorrectly read `changed == 0` and PASS."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        comment_lines = ["-- comment one", "-- comment two", "-- comment three"]
        (repo / "q.sql").write_text("\n".join(comment_lines) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "q.sql"], check=True)
        _commit_all(repo, "initial: q.sql, comment-only content")

        _stage_baseline(repo)  # unrelated new file a.py, stamped; never touches q.sql
        baseline_diff = _staged_diff(repo)
        assert "q.sql" not in baseline_diff, baseline_diff

        subprocess.run(["git", "-C", str(repo), "rm", "-q", "q.sql"], check=True)
        cur_diff = _staged_diff(repo)
        # Sanity: git really does render each deleted comment line with the collision shape
        # ("-" removal marker + the source's own leading "-- " == "--- comment N"), and the
        # deletion is a genuine whole-file removal (exercises `deleted file mode`/`+++
        # /dev/null` too, not just the plain `--- `/`+++ ` case).
        assert "deleted file mode" in cur_diff, cur_diff
        for comment in comment_lines:
            assert f"-{comment}" in cur_diff, cur_diff

        proc = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "2"})
        assert proc.returncode == 1, (
            "a genuine 3-line unreviewed deletion must still block at threshold=2 -- "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def test_mode_only_followup_is_not_unbounded_trivial():
    """review-cli#208 round-5 finding (Opus, reviewing rounds 1-4 together on this same PR)
    [Security -- unbounded undercount]: `old mode `/`new mode ` are pure diff-generation
    metadata (same class as `index `/`@@ `, round-1's own exclusion) -- but unlike a metadata
    line attached to a real content edit, a PURE mode change has NO content hunk at all: `git
    diff` for a `chmod +x` emits ONLY `diff --git ` + `old mode `/`new mode `, nothing else.
    So `changed` reads exactly 0 no matter how many files are touched this way -- this is
    UNBOUNDED, unlike every other trivial follow-up in this suite (which is only accepted
    because it's genuinely small): `chmod +x` on an arbitrary number of scripts always sails
    through unreviewed, defeating the block's own core premise (a SIZE heuristic bounded by
    `threshold`). Fixed the same way as the k3 binary/gitlink case: the fail-closed pre-check
    now also fires on an `old mode `/`new mode ` line, forcing a full review instead of a
    silent pass.

    Five scripts are committed, then a reviewed baseline touches an unrelated file, then the
    follow-up `chmod +x`'s all five scripts and nothing else -- an arbitrarily large
    (5-file) unreviewed permission change that a naive line count reads as 0."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        script_names = [f"s{i}.sh" for i in range(5)]
        for name in script_names:
            (repo / name).write_text("echo hi\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", *script_names], check=True)
        _commit_all(repo, "initial: five scripts, mode 644")

        _stage_baseline(
            repo
        )  # unrelated new file a.py, stamped; never touches the scripts

        for name in script_names:
            path = repo / name
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        subprocess.run(["git", "-C", str(repo), "add", *script_names], check=True)
        cur_diff = _staged_diff(repo)
        assert cur_diff.count("\nold mode ") == 5, cur_diff
        # Sanity: each script's OWN diff block is pure mode-change metadata, no content hunk
        # (the "@@ " hunk header that DOES appear in cur_diff belongs to a.py's unrelated,
        # already-reviewed new-file addition, not to any of the five mode changes).
        for name in script_names:
            block_start = cur_diff.index(f"diff --git a/{name} b/{name}")
            block_end = cur_diff.find("\ndiff --git ", block_start + 1)
            block = cur_diff[block_start : block_end if block_end != -1 else None]
            assert "@@ " not in block, block

        proc = _run_hook(hook, repo, env_extra={"REVIEW_TRIVIAL_DELTA_LINES": "2"})
        assert proc.returncode == 1, (
            "an arbitrarily large unreviewed chmod-only follow-up must still block -- "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def test_subproject_commit_anchor_requires_a_real_sha():
    """review-cli#208 round-5 finding (Opus), the companion fix to the mode-change gap above:
    the k3 fail-closed pre-check originally matched a bare `[-+]Subproject commit` prefix,
    which collides with an ordinary ADDED/REMOVED content line (e.g. documentation) that
    merely starts with that same English phrase -- forcing a spurious full-review block on a
    file that was never a gitlink. Safe direction (over-blocks, no bypass), but it defeats
    the trivial-follow-up feature for such files. Fixed by anchoring the pattern to a real
    40-hex-char sha (`[-+]Subproject commit [0-9a-f]{40}`), which prose cannot spell.

    A reviewed baseline is followed by a genuinely trivial one-line docs edit whose new line
    happens to start with the words "Subproject commit" (no hex sha) -- the gate must still
    accept it as trivial, not fail closed on the false-positive collision."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)  # 20-line new-file baseline "a.py", stamped

        followup = (
            "\n".join(
                (
                    "Subproject commit is a git feature we don't use here."
                    if i == 3
                    else f"line{i}"
                )
                for i in range(_BASELINE_LINE_COUNT)
            )
            + "\n"
        )
        _write_and_stage(repo, "a.py", followup)
        cur_diff = _staged_diff(repo)
        assert "+Subproject commit is a git feature" in cur_diff, cur_diff

        proc = _run_hook(hook, repo)
        assert proc.returncode == 0, (
            "a one-line docs edit must not fail closed just because its text starts with "
            f"'Subproject commit' -- stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def test_fresh_region_single_line_edit_does_not_count_context_lines():
    """Opus round-review finding: a follow-up edit landing in a FRESH region of an
    already-committed file (a new hunk, not adjacent to the reviewed baseline's own edit)
    produces a diff-of-diffs where the entire new hunk -- including its surrounding
    unchanged CONTEXT lines (`diff -U3`'s default 3-before/3-after window) -- appears as
    outer `+` lines. Before the exclusion pattern also stripped `^[+-] ` (outer marker,
    inner space = an unchanged source line quoted as context, never a real edit), each of
    those context lines was miscounted as a genuine change: one single-line edit in a
    fresh region inflated to ~8 "changed" lines, 8x the real number, defeating this
    feature's own purpose for exactly the common case (editing a different part of an
    already-reviewed file) it exists to help.

    30-line baseline, reviewed edit at line 5, follow-up edit at line 25 -- far enough
    apart (with `-U3` context) that the two hunks never overlap, so line 25's hunk is
    entirely ABSENT from the reviewed baseline diff and entirely NEW in the diff-of-diffs.
    One genuine edited line must count as 2 (the accepted `max()` semantics for an edit to
    a line that was CONTEXT, not itself a change, in the baseline -- see the module
    docstring above `_TRIVIAL_DELTA_BLOCK`), never the ~8 the context-line bug produced,
    and must pass at a LOW threshold (2) that the pre-fix count would fail."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        nlines = 30
        lines = [f"line{i}" for i in range(nlines)]
        (repo / "a.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
        _commit_all(repo, "initial: 30-line file")

        reviewed = list(lines)
        reviewed[5] = "line5-reviewed"
        _write_and_stage(repo, "a.py", "\n".join(reviewed) + "\n")
        baseline_diff = _staged_diff(repo)
        install._write_review_stamp(repo, baseline_diff)

        followup = list(reviewed)
        followup[25] = "line25-followup"
        _write_and_stage(repo, "a.py", "\n".join(followup) + "\n")
        cur_diff = _staged_diff(repo)
        # Sanity: the two edits really do land in non-overlapping hunks (line 25's hunk
        # is genuinely absent from the reviewed baseline), or this test isn't exercising
        # the fresh-region shape it claims to.
        assert "line25-followup" not in baseline_diff, baseline_diff
        assert "line25" in cur_diff, cur_diff

        proc = _run_hook(hook, repo, {"REVIEW_TRIVIAL_DELTA_LINES": "2"})
        assert proc.returncode == 0, (
            "a single-line fresh-region follow-up must pass at threshold=2 -- "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def test_non_utf8_content_byte_does_not_defeat_the_line_count():
    """Opus round-review finding: on Linux, GNU grep in a UTF-8 locale treats a byte
    that's invalid for the current encoding as a "binary" signal and stops emitting
    matches from that point on — NOT just a NUL byte, unlike the fail-closed pre-check's
    own binary detection (`git diff`'s own `Binary files ... differ`, which is NUL-only
    and never fires for a text file that merely contains a non-UTF-8 byte, e.g. a
    Latin-1/CP1251 source comment). Before `LC_ALL=C` was pinned on every grep in this
    block, the `content=$(... | grep -Ev ...)` pipeline could truncate at exactly such a
    byte on Linux CI, undercounting (or zeroing) `changed` and letting a large unreviewed
    follow-up pass the gate — invisible on this suite's dev platform (BSD grep has no
    such locale-dependent behavior), which is exactly why this test pins `LC_ALL`/`LANG`
    explicitly in `env_extra` rather than relying on the ambient shell's locale.

    This test cannot reproduce the bug's SYMPTOM on a non-GNU-grep platform (BSD grep
    counts correctly regardless of `LC_ALL`, so it would have passed even before the
    fix here) — its value is pinning that the fix (`LC_ALL=C` everywhere in the block)
    is actually present and doesn't itself break correct counting on a byte sequence a
    locale-aware grep might otherwise stumble on, which IS platform-independent and
    exercised by this run."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        nlines = 20
        lines = [f"line{i}" for i in range(nlines)]
        (repo / "a.py").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
        _commit_all(repo, "initial: 20-line file")

        reviewed = list(lines)
        reviewed[3] = "line3-reviewed"
        _write_and_stage(repo, "a.py", "\n".join(reviewed) + "\n")
        baseline_diff = _staged_diff(repo)
        install._write_review_stamp(repo, baseline_diff)

        # A genuinely large, UNREVIEWED follow-up (well over any reasonable threshold)
        # whose FIRST changed line carries a raw non-UTF-8 byte (`\xe9`, Latin-1 "é") --
        # positioned early so a truncating grep would swallow everything after it,
        # undercounting nearly the whole delta.
        followup_lines = list(reviewed)
        followup_lines[0] = "caf\xe9-line0-followup"
        for i in range(6, 18):
            followup_lines[i] = f"line{i}-followup-unreviewed"
        (repo / "a.py").write_bytes(
            ("\n".join(followup_lines) + "\n").encode("latin-1")
        )
        subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)
        # Not `_staged_diff` (strict UTF-8 `text=True` decode, which the raw Latin-1
        # byte this test deliberately stages would make raise `UnicodeDecodeError` --
        # exactly the byte shape under test, so decode leniently here instead).
        cur_diff_bytes = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--cached"],
            cwd=repo,
            capture_output=True,
            check=True,
        ).stdout
        cur_diff = cur_diff_bytes.decode("utf-8", errors="replace")
        # Sanity: the follow-up really did stage, non-UTF-8 byte included.
        assert "line6-followup-unreviewed" in cur_diff, cur_diff

        proc = _run_hook(
            hook,
            repo,
            {
                "REVIEW_TRIVIAL_DELTA_LINES": "3",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
            },
        )
        assert proc.returncode != 0, (
            "a genuinely large unreviewed follow-up containing a non-UTF-8 byte must "
            f"still BLOCK at a low threshold -- stdout={proc.stdout!r} "
            f"stderr={proc.stderr!r}"
        )


def test_stale_stamp_diff_does_not_anchor_an_unrelated_change_after_commit():
    """Opus round-3 finding (gate bypass): a `review-stamp-diff` left on disk after the
    reviewed change is COMMITTED must never anchor the comparison for what comes after --
    without a binding to the reviewed HEAD, it becomes a fixed, small, permanent baseline
    that a completely unrelated small change to a DIFFERENT file, against a DIFFERENT
    HEAD, measures favorably against and passes -- repeatably, once per small unreviewed
    commit, unboundedly. `_stage_baseline` + `_commit_all` (review, then commit -- no
    prior test in this file exercises that sequence) followed by staging an unrelated new
    file must fall through to the pre-existing exact-hash gate and BLOCK, exactly as it
    would have before this whole feature existed."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        _stage_baseline(repo)
        _commit_all(repo, "initial: a.py")

        # An UNRELATED, UNREVIEWED small change to a DIFFERENT file after the commit.
        (repo / "y.py").write_text("y1\ny2\ny3\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "y.py"], check=True)

        proc = _run_hook(hook, repo, {"REVIEW_TRIVIAL_DELTA_LINES": "10"})
        assert proc.returncode != 0, (
            "an unrelated unreviewed change after the reviewed baseline was committed "
            f"must BLOCK, not ride the stale stamp-diff's small anchor -- "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        assert "have not been reviewed" in proc.stderr, proc.stderr


def test_diff_binary_detection_mismatch_does_not_defeat_the_line_count():
    """Opus round-4 finding (security / undercount -> gate bypass): git's OWN binary
    check (the fail-closed pre-check above) scans the first ~8000 bytes of the BLOB and
    decides "text" or "binary" by its own rule; the outer `diff -U0` (comparing the two
    STORED diff texts) makes an INDEPENDENT binary determination on the diff text it was
    handed. A NUL byte placed past git's own scan window can produce a diff `git diff
    --cached` itself calls TEXT (no `Binary files` line -- the pre-check grep sees
    nothing to reject) but that `diff -U0` still calls BINARY once it reads the resulting
    diff TEXT containing that embedded NUL -- collapsing to a single "Binary files ...
    differ" line instead of the normal two-header unified-diff shape a blind `tail -n +3`
    assumed. Verified against the real `diff`/`git diff` on this machine before writing
    this test (both binaries' actual behavior, not assumed): a >8000-byte text file with
    a NUL placed only in the FOLLOW-UP edit (past git's own scan window) reproduces
    exactly this split."""
    with _isolated_repo() as repo:
        hook = _install_hook(repo)
        # >8000 bytes of plain ASCII text so a NUL added later, past that window, is
        # invisible to git's own (blob-scanning) binary check.
        base_content = "line" + ("x" * 8100) + "\n" + "\n".join(f"a{i}" for i in range(20))
        (repo / "d.txt").write_text(base_content + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "d.txt"], check=True)
        _commit_all(repo, "initial: >8000-byte text file")

        _stage_baseline(repo)  # an unrelated reviewed baseline (a.py)

        # Follow-up: a NUL embedded in a NEW line (past git's scan window) plus ~30
        # more genuinely unreviewed lines -- git still calls this TEXT.
        followup_lines = base_content.split("\n")
        followup_lines.append("unrev\x00iewed")
        followup_lines.extend(f"unrev{i}" for i in range(30))
        (repo / "d.txt").write_text("\n".join(followup_lines) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "d.txt"], check=True)
        cur_diff_bytes = _staged_diff_bytes(repo)
        assert b"Binary files" not in cur_diff_bytes, (
            "test setup invalid: git itself must call this a TEXT diff"
        )
        assert b"\x00" in cur_diff_bytes, (
            "test setup invalid: the NUL byte must reach the diff text"
        )

        proc = _run_hook(hook, repo, {"REVIEW_TRIVIAL_DELTA_LINES": "10"})
        assert proc.returncode != 0, (
            "a real ~30-line unreviewed change must BLOCK even when the inner `diff "
            "-U0` binary-detects on an embedded NUL git itself calls text -- "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def _commit_window_straddling_file(repo: Path) -> str:
    """Commit `d.txt` with a ~16000-byte first line -- past git's (~8000 byte) and
    `diff`'s (~1KB/few KB) binary-scan windows but within `grep`'s (~32KB) -- so a
    NUL inserted right after it lands in the gap between those windows. Returns
    the committed content (without its trailing newline)."""
    base_content = "line" + ("x" * 16000) + "\n" + "\n".join(f"a{i}" for i in range(20))
    (repo / "d.txt").write_text(base_content + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "d.txt"], check=True)
    _commit_all(repo, "initial: >16000-byte text file")
    return base_content


def _current_side_nul_scenario(repo: Path, followup_line: str) -> subprocess.CompletedProcess:
    """Shared setup for the pair of tests below: an unrelated reviewed baseline
    (`a.py`, via `_stage_baseline`), then a SINGLE inserted line `followup_line`
    (with or without an embedded NUL) into the committed window-straddling `d.txt`,
    staged as the live follow-up -- 1 line, well under the threshold -- and the
    hook run against it."""
    hook = _install_hook(repo)
    base_content = _commit_window_straddling_file(repo)
    _stage_baseline(repo)  # an unrelated reviewed baseline (a.py)

    followup_lines = base_content.split("\n")
    followup_lines.insert(1, followup_line)
    (repo / "d.txt").write_text("\n".join(followup_lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "d.txt"], check=True)
    cur_diff_bytes = _staged_diff_bytes(repo)
    assert b"Binary files" not in cur_diff_bytes, (
        "test setup invalid: git itself must call this a TEXT diff"
    )
    assert (b"\x00" in cur_diff_bytes) == ("\x00" in followup_line), (
        "test setup invalid: the NUL byte must reach the diff text iff it was inserted"
    )
    return _run_hook(hook, repo, {"REVIEW_TRIVIAL_DELTA_LINES": "10"})


def test_nul_past_diffs_window_but_within_greps_still_fails_closed():
    """A NUL positioned past git's (~8000 byte) and `diff`'s (~1KB/few KB) own
    binary-detection windows, but still within `grep`'s (~32KB), must still block:
    the gate scans the whole file for a NUL up front rather than trusting either
    tool's own partial-window heuristic. The follow-up is a SINGLE inserted line
    (well under the threshold) so that only the current-diff-side NUL check --
    not an incidentally large line-count delta -- can be responsible for the
    block (Codex review finding: an earlier version used a 30-line follow-up
    against a threshold of 10, which would have blocked from the line count alone
    even with the NUL check removed). Paired with the positive control below."""
    with _isolated_repo() as repo:
        proc = _current_side_nul_scenario(repo, "unrev\x00iewed")
        assert proc.returncode != 0, (
            "a trivially small (1-line) unreviewed change must still BLOCK when "
            "the NUL falls in the gap between git's, diff's, and grep's differing "
            f"binary-scan windows -- stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def test_current_side_without_a_nul_positive_control_takes_the_fast_path():
    """Positive control for the test above (Opus review finding): the IDENTICAL
    scenario, except the inserted follow-up line has no NUL, must be accepted via
    the trivial-delta fast path (exit 0). Without it, `returncode != 0` alone
    could not distinguish a real current-side NUL detection from the fast path
    never having been reached at all."""
    with _isolated_repo() as repo:
        proc = _current_side_nul_scenario(repo, "unreviewed")
        assert proc.returncode == 0, (
            "the identical 1-line scenario WITHOUT a NUL must pass via the fast "
            f"path -- stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def _baseline_nul_scenario(repo: Path, reviewed_middle_line: str) -> subprocess.CompletedProcess:
    """Shared setup for the pair of tests below: review-stamp a change to `d.txt`
    whose inserted middle line is `reviewed_middle_line` (with or without an
    embedded NUL, past git's and diff's binary-scan windows but within grep's),
    via `install._write_review_stamp` -- the SAME production entry point a real
    `review diff --staged` uses, confirmed (reviewlib/install.py) to also write
    the `review-stamp-diff` companion via `_write_review_stamp_diff`, so this
    exercises the real baseline path, not a hand-rolled stand-in for it. Then
    stages a trivially small (2-line), always-clean-text live follow-up on top,
    reverting the inserted middle line so ONLY the recorded baseline can differ
    between the two calling tests -- and runs the hook."""
    base_content = _commit_window_straddling_file(repo)

    reviewed_lines = base_content.split("\n")
    reviewed_lines.insert(1, reviewed_middle_line)
    (repo / "d.txt").write_text("\n".join(reviewed_lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "d.txt"], check=True)
    reviewed_diff = _staged_diff_bytes(repo)
    assert b"Binary files" not in reviewed_diff, (
        "test setup invalid: git itself must call the reviewed diff TEXT"
    )
    install._write_review_stamp(repo, reviewed_diff.decode("utf-8", errors="replace"))

    # Live follow-up: revert the reviewed middle line and add two clean, trivial
    # lines instead. Net staged diff from HEAD is always NUL-free and only 2 lines
    # -- comfortably under the default threshold -- regardless of what the
    # recorded baseline above contained.
    final_lines = base_content.split("\n") + ["unrev0", "unrev1"]
    (repo / "d.txt").write_text("\n".join(final_lines) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "d.txt"], check=True)
    cur_diff_bytes = _staged_diff_bytes(repo)
    assert b"\x00" not in cur_diff_bytes, "test setup invalid: the live staged diff must be clean text"

    hook = _install_hook(repo)
    return _run_hook(hook, repo, {"REVIEW_TRIVIAL_DELTA_LINES": "10"})


def test_nul_in_the_reviewed_baseline_itself_still_fails_closed():
    """The gate checks BOTH `baseline_tmp` (the on-disk recorded review) and
    `cur_tmp` (the current staged diff) for a NUL -- this test puts the NUL on the
    baseline side only, with the live follow-up a TRIVIALLY small, genuinely
    clean-text change (well under the threshold). Without the baseline-side
    check, this would wrongly pass via the fast path on line count alone; the
    recorded review having ever contained a NUL must still force a fall-through
    to the full review requirement. Paired with the positive control below (Opus
    review finding): without it, "blocked" alone can't distinguish a real
    baseline-NUL detection from the trivial-delta block never having run at all
    (e.g. a broken `review-stamp-diff` write) -- the positive control proves the
    exact same setup, minus the NUL, exits 0, so the NUL is what flips it."""
    with _isolated_repo() as repo:
        proc = _baseline_nul_scenario(repo, "review\x00ed")
        assert proc.returncode != 0, (
            "a trivially small, clean-text live follow-up must still BLOCK when "
            "the RECORDED review baseline itself once contained a NUL -- "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


def test_baseline_without_a_nul_positive_control_takes_the_fast_path():
    """Positive control for the test above: the IDENTICAL scenario, except the
    recorded baseline's inserted line has no NUL -- so the live 2-line follow-up
    must be accepted via the trivial-delta fast path (exit 0). This is what
    proves the previous test's block is actually caused by the NUL (not by the
    trivial-delta block failing to run at all for some unrelated setup reason)."""
    with _isolated_repo() as repo:
        proc = _baseline_nul_scenario(repo, "reviewed")
        assert proc.returncode == 0, (
            "the identical scenario WITHOUT a NUL in the recorded baseline must "
            f"pass via the fast path -- stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )


if __name__ == "__main__":
    # Standalone runner (mirrors tests/test_review_marker.py / tests/smoke.py's expectations):
    # `python <file>` must exit 0 on success, non-zero on any failure.
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except BaseException as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
