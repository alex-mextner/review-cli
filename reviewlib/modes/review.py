"""diff review: run the diff across every selected backend in parallel.

`review diff …` is the diff review. A bare `review` prints top-level help, and the
old `review review …` spelling is a usage error that points here. **--diff/diff is
the default** (the mode REQUIRES a diff, as the pre-commit path always has).
Originally the flag-less default of `bin/review:main()` (Stage 0 decomposition);
now a first-class SUBCOMMAND backed by the `MODE` descriptor at the bottom of this
file (see `modes/contract.py`).

The reviewer-board path (HYP-741) is layered on top: when a board is passed, each
reviewer gets its own role-lens prompt + label, but the parallel run, result
formatting, and staged-stamp behaviour are otherwise identical.

The failover pool (priority + availability) is layered on the board path: the board
is a PRIORITY-ordered list, the active pool is the top-N AVAILABLE seats (startup
failover), and a seat that fails DURING the run is replaced by the next-priority
reserve (mid-run failover) so the run still yields N working verdicts when possible.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ..backends import (
    ReviewResult,
    backend_available,
    call_backend,
    resolve_backend,
    review_with_images,
)
from ..config import DEFAULT_POOL_SIZE, BoardReviewer, apply_effort_override, split_pool_reserve
from ..install import _touch_review_marker, _write_review_stamp
from ..panel import (
    FailoverOutcome,
    _tally_result,
    format_result,
    run_board_with_failover,
)
from ..process import _run, git_repo_env
from ..retry import retry_default, run_seat_with_retry
from . import _visual_images
from .contract import ModeContext, ModeSpec

# Stable, per-class exit codes for the `--commit` checkpoint feature (structured-exit-codes),
# continuing the numbering discipline started in cli.py (EXIT_NOT_A_REPO=3 … EXIT_QA_ENV_
# UNHEALTHY=9) and features/visual/policy_engine.py (EXIT_BLOCK_STRICT=10). 11/12 are the next
# free integers — distinct from every class above so a script/hook can tell "you misused
# --commit" apart from "the checkpoint commit itself failed" apart from any other exit class.
#
# --commit REQUIRES --staged: a checkpoint commits the reviewed STAGED diff, and there is no
# such thing as checkpointing an unstaged/piped diff. This is a hard usage ERROR, not a silent
# no-op and not an implicit `--staged` (a smaller surprise is still a surprise) and NEVER a
# fallback to `git commit -a` (which would sweep in unrelated unstaged changes — exactly the
# "sweep everything" accident class this whole feature exists to prevent).
EXIT_COMMIT_REQUIRES_STAGED = 11
# The review itself succeeded (`ok`) and the checkpoint gate was satisfied (staged, not
# piped), but the actual `git commit` subprocess FAILED (a commit-msg/pre-commit hook
# rejected it, "nothing to commit", or any other nonzero). The user explicitly asked for a
# checkpoint guarantee and did not get one — that must be a visible, distinct failure, never
# silently swallowed as if the review itself failed (which already has its own 0/1 result).
EXIT_COMMIT_FAILED = 12

if TYPE_CHECKING:
    from ..config import EffortOverride


def mode_review(
    models: list[str], prompt: str, diff: str, cwd: Path, timeout: int, staged: bool,
    board: list[BoardReviewer] | None = None, pool_size: int = DEFAULT_POOL_SIZE,
    outcome_sink: list[FailoverOutcome] | None = None,
    diff_from_stdin: bool = False,
    visual_images: tuple[Path, ...] = (),
    exact_board: bool = False,
    effort_override: "EffortOverride | None" = None,
    commit: bool = False,
) -> int:
    # --commit REQUIRES --staged (see the exit-code block above). Checked BEFORE the (paid)
    # panel dispatch, not after — a usage mistake should fail fast, not after burning a full
    # multi-model review the checkpoint could never have used anyway.
    if commit and not staged:
        print(
            "[review-cli] --commit requires --staged: a checkpoint commits the reviewed "
            "STAGED diff, and there is no such thing as checkpointing an unstaged/piped "
            "diff.\n  fix: add --staged (`review diff --staged --commit`), or drop --commit.",
            file=sys.stderr, flush=True,
        )
        return EXIT_COMMIT_REQUIRES_STAGED

    if not diff.strip():
        print("No diff to review.", file=sys.stderr)
        return 1

    if board:
        # Resolve the run-scoped effort onto the seats HERE, so a direct lib/MCP caller that
        # passes an un-applied board + effort_override gets the override too — not only the CLI
        # path (which pre-applies). apply_effort_override is idempotent (re-resolving an already
        # resolved seat yields the same effort), so the CLI's earlier apply stays a no-op.
        board = apply_effort_override(board, effort_override)
        return _mode_review_board(
            board, prompt, diff, cwd, timeout, staged, pool_size, outcome_sink,
            diff_from_stdin, visual_images, exact_board, commit,
        )

    # The flat `-m` / config-`models:` path: each seat runs in parallel AND now gets in-seat
    # retry on a transient failure (`--retry` / $REVIEW_RETRY_COUNT) — not just the board path.
    # Each seat's per-attempt dispatch goes through `resolve_backend` (kept as THIS module's
    # name so existing tests that stub `review.resolve_backend` still drive the fakes), wrapped
    # in `run_seat_with_retry` which retries the same seat on a transient class and returns the
    # final outcome. The per-call run-stats tally records that final outcome once per seat.
    results: list[ReviewResult] = []

    def _seat_effort(model: str) -> str | None:
        # The flat `-m` path has no BoardReviewer, so the run-scoped `--effort` is the only
        # effort source. None (no flag) keeps the dispatch byte-identical to before.
        return effort_override.effort_for(model) if effort_override is not None else None

    def _dispatch(model: str) -> ReviewResult:
        backend = resolve_backend(model)
        effort = _seat_effort(model)
        if visual_images:
            if effort is None:
                return review_with_images(model, prompt, diff, cwd, timeout, 0, visual_images)
            return review_with_images(model, prompt, diff, cwd, timeout, 0, visual_images, effort=effort)
        if effort is None:
            return backend(model, prompt, diff, cwd, timeout)
        return call_backend(backend, model, prompt, diff, cwd, timeout, effort=effort)

    def _run_seat_with_retry(model: str) -> ReviewResult:
        return run_seat_with_retry(model, lambda: _dispatch(model))

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {pool.submit(_run_seat_with_retry, model): model for model in models}
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - report, never crash the panel
                results.append(ReviewResult(model=model, command="internal", returncode=127, stdout="", stderr=str(exc)))
            # One tally per logical seat (its FINAL outcome after any retry). No-op outside a
            # CLI-driven run; this flat path runs its own executor, so it tallies here.
            _tally_result(results[-1].returncode)

    by_model = {result.model: result for result in results}
    print("\n\n---\n\n".join(format_result(by_model[model]) for model in models))
    ok = all(result.returncode == 0 for result in results)
    _stamp_if_staged_commit_review(ok, staged, diff_from_stdin, cwd, diff)
    override = _checkpoint_if_requested(commit, ok, diff_from_stdin, cwd, diff)
    if override is not None:
        return override
    return 0 if ok else 1


def _stamp_if_staged_commit_review(
    ok: bool, staged: bool, diff_from_stdin: bool, cwd: Path, diff: str,
) -> None:
    """Write the diff-scoped review-stamp and touch the session marker iff this review
    genuinely satisfies the staged commit gate. Shared by the flat and board paths so the
    gate condition stays in ONE place.

    Conditions (all required):
      * `ok`             — the review actually passed (every seat usable / not degraded).
      * `staged`         — `--staged`; an unstaged/working-tree review is not the gate.
      * NOT `diff_from_stdin` — the diff came from `git diff --cached`, not piped stdin.
        `printf ... | review --staged` reviews ARBITRARY stdin, not the index, so it must
        not satisfy the commit gate. The stamp is diff-scoped (the hook re-derives the
        cached diff and compares), but the marker is mtime-only and would otherwise be
        forgeable this way — so both are gated on real-index provenance.
    """
    if ok and staged and not diff_from_stdin:
        _write_review_stamp(cwd, diff)
        _touch_review_marker()


_CHECKPOINT_COMMIT_MESSAGE = "chore: checkpoint via review diff --staged --commit"


def _current_staged_diff(cwd: Path) -> str | None:
    """Re-derive the CURRENT staged diff, anchored `-C <cwd>` AND pinned via
    `git_repo_env(cwd)` — the exact same invocation shape as `cli._git_diff` (review-
    cli#71/#72), so a foreign GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE can't divert the
    comparison to the wrong repo. Returns None on ANY failure (spawn error, timeout,
    non-zero git diff) — the caller treats "can't verify" the same as "verification
    failed": don't commit."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--cached"],
            cwd=cwd, env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _checkpoint_commit(cwd: Path, reviewed_diff: str) -> tuple[bool, str]:
    """Create the real checkpoint commit of the currently-staged index.

    Anchored `-C <cwd>` AND a pinned `git_repo_env(cwd)` (review-cli#71/#72, the same
    reasoning as `cli._git_diff`): a leaked GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE pointing
    at a FOREIGN repo must not divert this commit to the wrong tree.

    TOCTOU guard (codex review finding on this feature's own PR): a review is
    multi-minute / multi-model, leaving a window in which another process or session
    sharing the same checkout could stage additional changes. Before committing, this
    re-reads the CURRENT staged diff (`_current_staged_diff`) and refuses to checkpoint
    if it no longer matches `reviewed_diff` — otherwise `--commit` could silently commit
    unreviewed/unrelated staged work, which is exactly the "sweep in someone else's
    changes" accident class this whole feature exists to prevent.

    This is a REAL `git commit` — it runs the repo's own commit-msg/pre-commit hooks
    (lint/typecheck/tests, conventional-commit format) and can genuinely fail on a red
    tree or a malformed message. It never passes `--no-verify`: bypassing that gate is
    exactly the kind of accident this feature exists to prevent, not reproduce under a
    new flag. Returns `(succeeded, detail)` — `detail` is empty on success, else a short
    explanation (drift detected, spawn error, or git's own stderr/stdout) for the caller
    to report.
    """
    current_diff = _current_staged_diff(cwd)
    if current_diff is None:
        return False, "could not re-read the staged diff to verify it is still the reviewed one"
    if current_diff != reviewed_diff:
        return False, (
            "the staged diff changed since the review ran (another process/session may "
            "have touched the index) — refusing to checkpoint a diff that was never reviewed"
        )
    try:
        proc = _run(
            ["git", "-C", str(cwd), "commit", "-m", _CHECKPOINT_COMMIT_MESSAGE],
            cwd=cwd, env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git commit could not run: {exc}"
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or "git commit failed"
    return True, ""


def _checkpoint_if_requested(
    commit: bool, ok: bool, diff_from_stdin: bool, cwd: Path, diff: str,
) -> int | None:
    """Create the `--commit` checkpoint commit after a review, if requested.

    Called only after `mode_review` has already validated commit-implies-staged up
    front, so `staged` is always True here by construction — the remaining gate mirrors
    `_stamp_if_staged_commit_review`'s other two conditions (`ok`, not `diff_from_stdin`).

    Precisely: this CHECKPOINTS the reviewed staged diff after the review completes —
    it does NOT mean "commits when the review passes". `ok` means the pool produced
    usable verdicts, not that the diff is clean; a review that reports open findings
    still has `ok=True` and still gets checkpointed (findings are informational, the
    same as the existing `--staged` stamp gate). That is intended, not a bug.

    Returns `None` to leave the review's own 0/1 result standing (covers: `--commit`
    not requested; the gate not satisfied — the review failed, or the diff was piped —
    in which case a note is printed but the review's own result is untouched; or the
    checkpoint commit itself succeeded). Returns `EXIT_COMMIT_FAILED` to OVERRIDE an
    otherwise-successful review's exit code when the review passed, the gate was
    satisfied, but the `git commit` subprocess itself failed — the user asked for a
    checkpoint guarantee and did not get one, so that must be visible, never silently
    swallowed.
    """
    if not commit:
        return None
    if not ok:
        print(
            "[review-cli] --commit: the review did not succeed — no checkpoint created.",
            file=sys.stderr, flush=True,
        )
        return None
    if diff_from_stdin:
        print(
            "[review-cli] --commit: the diff was piped on stdin, not the git index — no "
            "checkpoint created (a pipe reviews arbitrary input; --commit checkpoints the "
            "staged index).",
            file=sys.stderr, flush=True,
        )
        return None
    succeeded, detail = _checkpoint_commit(cwd, diff)
    if succeeded:
        print("[review-cli] --commit: checkpoint commit created.", file=sys.stderr, flush=True)
        return None
    print(
        "[review-cli] --commit: the review succeeded but the checkpoint commit itself "
        f"FAILED — no checkpoint was created.\n  git commit failed: {detail}\n"
        "  fix: check the repo's commit-msg/pre-commit hooks (lint/typecheck/tests may be "
        "blocking it), then re-run `review diff --staged --commit`.",
        file=sys.stderr, flush=True,
    )
    return EXIT_COMMIT_FAILED


def _mode_review_board(
    board: list[BoardReviewer], prompt: str, diff: str, cwd: Path, timeout: int,
    staged: bool, pool_size: int, outcome_sink: list[FailoverOutcome] | None,
    diff_from_stdin: bool = False, visual_images: tuple[Path, ...] = (),
    exact_board: bool = False, commit: bool = False,
) -> int:
    """Board path: a priority-ordered FAILOVER pool of role-lensed reviewers.

    `board` is the FULL priority-ordered board (highest priority first). The active
    pool is the top-`pool_size` AVAILABLE seats by priority (startup failover — a
    higher-priority seat whose backend isn't reachable is skipped and the next-priority
    seat pulled up); the remaining available seats are the RESERVE. The pool runs in
    parallel, and any seat that fails DURING the run (backend error, timeout, empty or
    "unavailable" output) is replaced by the next-priority reserve until `pool_size`
    usable verdicts are produced or the reserve is exhausted (then it degrades, loudly).

    Output lists every seat that ran (successes and the failed seats that triggered a
    backfill, so the whole story is visible). Exit 0 (and the staged stamp) iff the pool
    refilled to `pool_size` usable verdicts — i.e. NOT degraded. A failed-then-replaced
    seat is an expected, HANDLED failover event, not a review failure: the run still
    delivered the target number of working verdicts, so it must succeed (otherwise the
    paywalled-Fable case would make every `review --staged` fail despite a full, healthy
    pool). Only a genuine shortfall (reserve exhausted before the pool refilled) is a
    failure. `outcome_sink`, when given, receives the FailoverOutcome so the CLI can
    report the models that actually ran."""
    if exact_board:
        pool, reserve = list(board), []
    else:
        pool, reserve = split_pool_reserve(board, pool_size, lambda r: backend_available(r.model))
    if not pool:
        print("[review-cli] board: no reviewers are available — configure at least one "
              "backend key/CLI (e.g. COMMANDCODE_API_KEY, GEMINI_API_KEY, codex/claude on "
              "PATH).", file=sys.stderr, flush=True)
        return 1

    outcome = run_board_with_failover(pool, reserve, prompt, diff, cwd, timeout, visual_images)
    if outcome_sink is not None:
        outcome_sink.append(outcome)

    print("\n\n---\n\n".join(format_result(r) for r in outcome.results))

    if outcome.degraded:
        print(f"[review-cli] board: degraded — only {len(outcome.usable)} of "
              f"{outcome.target} seats produced a usable verdict (reserve exhausted).",
              file=sys.stderr, flush=True)

    # Success gate: the pool refilled to its target of usable verdicts (NOT degraded).
    # A failed-then-replaced seat is handled, so it must not block — only a real
    # shortfall (reserve exhausted) does.
    ok = not outcome.degraded
    _stamp_if_staged_commit_review(ok, staged, diff_from_stdin, cwd, diff)
    override = _checkpoint_if_requested(commit, ok, diff_from_stdin, cwd, diff)
    if override is not None:
        return override
    return 0 if ok else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Diff-mode-only options. `--retry` lives HERE, not on the global surface: in-seat retry
    applies only to the diff review path (the failover board + the flat `-m` panel), NOT to
    brainstorm/quorum/just-ask (which call run_panel and never use the retry wrapper). Keeping
    it off the top-level help avoids advertising a no-op flag outside diff review (AGENTS.md:
    the global list is only truly-global options; codex P1 on #46). The other shared options
    (-m / -C / --pool / --prompt / --staged / --visual …) come from the CLI's global surface."""
    parser.add_argument(
        "--retry", type=int, default=None, metavar="N",
        help=(
            "in-seat retries on a TRANSIENT failure (429/529/5xx/timeout/overloaded) before "
            "falling to the reserve (default from $REVIEW_RETRY_COUNT, else "
            f"{retry_default()}); applies to the failover board AND the flat -m panel. A "
            "SEAT-FATAL failure (auth/bad-model/501/refusal) is never retried. 0 disables it."
        ),
    )
    parser.add_argument(
        "--commit", action="store_true",
        help=(
            "checkpoint the reviewed diff with a real `git commit` after the review "
            "completes — REQUIRES --staged (errors otherwise, exit "
            f"{EXIT_COMMIT_REQUIRES_STAGED}). This checkpoints the reviewed staged diff, "
            "it does NOT mean 'commits only when the review passes': a review with open "
            "findings still gets checkpointed (same as the existing --staged stamp gate) "
            "— findings are informational. Runs the repo's own commit-msg/pre-commit "
            "hooks; a hook rejection fails the checkpoint distinctly (exit "
            f"{EXIT_COMMIT_FAILED}), never bypassed with --no-verify. Undo a bad "
            "checkpoint with `git reset --soft HEAD~1` (safe — leaves untracked/foreign "
            "files alone); never `git reset --hard` mid-review-cycle (wipes uncommitted "
            "work, including anyone else's, in a shared checkout)."
        ),
    )


def _handler(ctx: ModeContext) -> int:
    """Thin over `mode_review`: the CLI resolves the board / pool / outcome_sink and
    passes them through `ctx.extra` (only the failover-board path needs them).

    The board path passes `board` + `pool_size` + `outcome_sink`; the flat path
    (explicit -m with no configured board/models) passes board=None and NOTHING else —
    the call shape there is identical to the pre-redesign default review (a board=None
    call with no pool_size/outcome_sink), so consumers/stubs of the flat path stay
    compatible."""
    board = ctx.extra.get("board")
    # A diff piped on stdin (vs read from `git diff --cached`) must not satisfy the staged
    # commit gate even under --staged — see _stamp_if_staged_commit_review.
    diff_from_stdin = bool(ctx.extra.get("diff_from_stdin", False))
    commit = bool(getattr(ctx.args, "commit", False))
    base = (
        ctx.models, ctx.with_visual(ctx.args.prompt), ctx.diff, ctx.cwd, ctx.timeout,
        ctx.args.staged,
    )
    if board is None:
        return mode_review(
            *base, board=None, diff_from_stdin=diff_from_stdin,
            visual_images=_visual_images(ctx),
            effort_override=ctx.effort_override,
        commit=commit,
        )
    return mode_review(
        *base, board=board,
        pool_size=ctx.extra.get("pool_size", DEFAULT_POOL_SIZE),
        outcome_sink=ctx.extra.get("outcome_sink"),
        diff_from_stdin=diff_from_stdin,
        visual_images=_visual_images(ctx),
        exact_board=bool(ctx.extra.get("exact_board", False)),
        commit=commit,
    )


MODE = ModeSpec(
    name="review",
    # The diff-review SUBCOMMAND is `diff` (renamed from the stuttering `review review`).
    # The stable mode `name` / `stats_mode` stay "review" (the run-stats key and the
    # handler dispatch identity are unchanged); only the user-facing verb moved.
    subcommand="diff",
    diff_policy="require",
    stats_mode="review",
    summary="diff review across the reviewer board (requires a diff; --staged --commit checkpoints progress)",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=False,
)
