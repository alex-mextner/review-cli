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
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from dataclasses import replace

from ..backends import (
    ReviewResult,
    backend_available,
    call_backend,
    cap_diff_for_dispatch,
    resolve_backend,
    review_with_images,
    runtime_provider_marked_unpaid,
)
from ..config import (
    DEFAULT_POOL_SIZE,
    BoardReviewer,
    apply_effort_override,
    split_pool_reserve,
)
from ..install import _touch_review_marker, _write_review_stamp
from ..panel import (
    FailoverOutcome,
    _tally_ok,
    _tally_tokens,
    format_result,
    result_is_usable,
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
# codex review finding: the stamp/checkpoint always certify against the UNCAPPED
# canonical diff (by design), while every backend actually saw only the CAPPED,
# possibly-truncated copy — so a truncated staged review used to still pass the
# commit-hook gate / create a checkpoint that CLAIMS the full diff was reviewed when
# only a partial view was. Automation invoking `review diff --staged --commit`
# non-interactively never sees the stderr warning, so a warning alone does not protect
# it. A truncated staged diff now REFUSES to stamp or checkpoint (this exit code for
# `--commit`; the plain `--staged` stamp is skipped the same way, silently, matching how
# it already skips silently on `ok=False`) — the gate exists specifically to certify
# "the full diff was reviewed", and a partial review must never certify as one.
EXIT_COMMIT_DIFF_TRUNCATED = 13

if TYPE_CHECKING:
    from ..config import EffortOverride


def _flat_seat_with_provider_failover(
    model: str, dispatch: Callable[[str], ReviewResult]
) -> ReviewResult:
    """The flat `-m` path's per-seat runner: SAME provider-failover cascade as the board
    path (reviewlib.panel's `_seat`), applied to a single flat seat.

    Each provider in `model`'s chain first gets the in-seat transient retry
    (`run_seat_with_retry`); a still-unusable result FAILS OVER to the chain's next provider,
    continuing the review — the seat only reports failure once every provider is exhausted.
    The provider that produces a usable verdict is cached as last-working (tried first next
    run); a total failure rotates the cache. Returned results always carry `model` (the
    caller-facing name), never the concrete provider spelling, so callers keying results by
    the ORIGINAL requested model (as `mode_review`'s flat path does) still find them.

    `dispatch` is called with the CONCRETE provider spelling for each attempt (not the
    original `model`), so a per-provider `--effort <route>=<level>` override
    (`_seat_effort`/`EffortOverride.effort_for`, keyed on the backend ROUTE) resolves against
    whichever provider is ACTUALLY running at that attempt — e.g. after `zai:glm-5.2` fails
    over to `oc:zai/glm-5.2`, an `--effort opencode=high` override applies (the seat now
    genuinely runs the opencode route) while a `--effort zai=high` override no longer does.
    This is intentional (raised as a 'please confirm' item on review of #157): a route-keyed
    override exists to size whichever backend executes, not the originally-requested route.
    See test_flat_failover_effort_follows_the_actually_dispatched_provider_route.

    `dispatch` is wrapped per-attempt so a RAISE (not just a nonzero `ReviewResult`) from
    one provider still fails over to the next, instead of aborting the whole seat and
    skipping the rest of the chain. The board path gets this for free because `run_panel`'s
    `_run_job` already catches every exception and normalizes it to a failed `ReviewResult`
    before its provider-failover loop (`panel._seat`) ever sees it; the flat path's
    `dispatch` has no such wrapper, so this cascade must do it itself (codex review of
    #157). See test_flat_m_failover_survives_a_provider_that_raises_instead_of_returning.

    The last-working-cache WRITE (`remember_working_provider`/`forget_working_provider`) is
    gated the SAME way `provider_chain`'s cache-reorder READ is
    (`is_default_provider_selection`): an explicit alternate pin (`-m
    commandcode:zai-org/GLM-5.2`) succeeding/failing must not train/clear the shared
    logical-key cache entry a later BARE alias request (`-m glm52`) reads (review of #157:
    'cache write isn't gated the way the read is')."""
    from ..provider_failover import (
        forget_working_provider,
        is_default_provider_selection,
        provider_chain,
        remember_working_provider,
    )

    def _safe_dispatch(provider_model: str) -> ReviewResult:
        try:
            result = dispatch(provider_model)
        except Exception as exc:  # noqa: BLE001 - normalize so failover can continue
            result = ReviewResult(
                model=provider_model,
                command="internal",
                returncode=127,
                stdout="",
                stderr=str(exc),
            )
        # Tally tokens for EVERY real dispatch attempt, not just the seat's final
        # outcome: `run_seat_with_retry` below can invoke this multiple times for
        # ONE provider (in-seat retry) and the chain loop invokes it again per
        # provider on failover -- each call is a real backend round-trip that may
        # have spent real tokens (codex review finding: tallying only the final
        # result undercounted a seat that needed a retry or a provider failover).
        _tally_tokens(result)
        return result

    chain = provider_chain(
        model, available=backend_available, unpaid=runtime_provider_marked_unpaid
    )
    cache_eligible = len(chain) > 1 and is_default_provider_selection(model)
    last: ReviewResult | None = None
    for idx, provider_model in enumerate(chain):
        result = run_seat_with_retry(
            model, lambda pm=provider_model: _safe_dispatch(pm)
        )
        if result_is_usable(result):
            if cache_eligible:
                remember_working_provider(model, provider_model)
            return replace(result, model=model)
        last = result
        if idx + 1 < len(chain):
            print(
                f"[review-cli] seat {model}: provider {provider_model} failed — "
                f"switching to {chain[idx + 1]} (review continues)",
                file=sys.stderr,
                flush=True,
            )
    if cache_eligible:
        forget_working_provider(model)
    final = last if last is not None else _safe_dispatch(model)
    return replace(final, model=model)


def mode_review(
    models: list[str],
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    staged: bool,
    board: list[BoardReviewer] | None = None,
    pool_size: int = DEFAULT_POOL_SIZE,
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
            file=sys.stderr,
            flush=True,
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
            board,
            prompt,
            diff,
            cwd,
            timeout,
            staged,
            pool_size,
            outcome_sink,
            diff_from_stdin,
            visual_images,
            exact_board,
            commit,
        )

    # The flat `-m` / config-`models:` path: each seat runs in parallel AND now gets BOTH
    # in-seat retry on a transient failure (`--retry` / $REVIEW_RETRY_COUNT) AND provider
    # failover (reviewlib.provider_failover) — not just the board path (codex P2 on
    # review-cli#157: "Apply provider failover to flat -m reviews"). Each provider first gets
    # the same-provider retry; a still-unusable result switches this SAME model to its NEXT
    # provider, so the seat only fails once every provider is exhausted.
    results: list[ReviewResult] = []

    def _seat_effort(model: str) -> str | None:
        # The flat `-m` path has no BoardReviewer, so the run-scoped `--effort` is the only
        # effort source. None (no flag) keeps the dispatch byte-identical to before.
        return (
            effort_override.effort_for(model) if effort_override is not None else None
        )

    # Capped copy for backend dispatch ONLY — never for the stamp/checkpoint `diff`
    # below (see cap_diff_for_dispatch's docstring: capping the canonical diff broke
    # the --commit checkpoint's integrity check, codex P1 finding). A PIPED diff
    # (`diff_from_stdin`) is exempt from the cap — the user already explicitly, and
    # deliberately, scoped what they piped in; capping it too would silently truncate
    # an intentional `git diff | review diff ...` (a second codex P1 finding on the
    # same PR: the cap originally ignored this flag even though the stamp/checkpoint
    # calls right below already receive it).
    dispatch_diff = diff if diff_from_stdin else cap_diff_for_dispatch(diff)
    dispatch_diff_truncated = _warn_if_dispatch_diff_truncated(
        diff, dispatch_diff, staged
    )

    def _dispatch(model: str) -> ReviewResult:
        backend = resolve_backend(model)
        effort = _seat_effort(model)
        if visual_images:
            if effort is None:
                return review_with_images(
                    model, prompt, dispatch_diff, cwd, timeout, 0, visual_images
                )
            return review_with_images(
                model,
                prompt,
                dispatch_diff,
                cwd,
                timeout,
                0,
                visual_images,
                effort=effort,
            )
        if effort is None:
            return backend(model, prompt, dispatch_diff, cwd, timeout)
        return call_backend(
            backend, model, prompt, dispatch_diff, cwd, timeout, effort=effort
        )

    def _run_seat_with_failover(model: str) -> ReviewResult:
        return _flat_seat_with_provider_failover(model, _dispatch)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {
            pool.submit(_run_seat_with_failover, model): model for model in models
        }
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - report, never crash the panel
                results.append(
                    ReviewResult(
                        model=model,
                        command="internal",
                        returncode=127,
                        stdout="",
                        stderr=str(exc),
                    )
                )
            # One tally per logical seat (its FINAL outcome after any retry). No-op outside a
            # CLI-driven run; this flat path runs its own executor, so it tallies here.
            # glm-5.2 review finding (2026-08 seat-cooldown feature): this used to tally by
            # bare returncode, so a cached-cooldown sentinel (rc=0, non-empty "unavailable"
            # body) recorded as `ok` in run-stats — the same distortion `run_moderator`'s
            # `_tally_ok(result_is_usable(...))` already fixed. `result_is_usable` is the
            # SAME predicate this function's own `ok` determination uses below, so a
            # cooling-down seat can no longer report differently to run-stats than it does
            # to the review's own pass/fail result.
            _tally_ok(result_is_usable(results[-1]))
            # Token tallying happens PER DISPATCH ATTEMPT inside `_safe_dispatch`
            # above (codex review finding: tallying only the seat's final result
            # here undercounted a seat that needed an in-seat retry or a provider
            # failover -- each such attempt is a separate real backend call). Do
            # NOT also tally `results[-1]` here -- it was already counted once as
            # the last `_safe_dispatch` call that produced it; tallying it again
            # would double-count that one attempt.

    by_model = {result.model: result for result in results}
    print("\n\n---\n\n".join(format_result(by_model[model]) for model in models))
    # Opus review finding: this used to check ONLY `returncode == 0`, not
    # `result_is_usable`. A live rc=0 "is currently unavailable" sentinel (the same
    # shape `_cooldown_skip_result` deliberately mirrors, and the board path already
    # guards against via `result_is_usable` in `run_board_with_failover`) would
    # therefore read as `ok=True` on the FLAT path whenever provider failover exhausts
    # without a working alternate — e.g. a single-seat `-m fable` review with no
    # failover chain left. That is not a hypothetical: `_flat_seat_with_provider_
    # failover` already returns the CHAIN'S FINAL (possibly still-unusable) result
    # unchanged once every provider is exhausted (`final = last if last is not None
    # else ...`); this cooldown feature makes an rc=0 unusable result far more common
    # than the rare live-paywall case that could already trigger this pre-existing gap.
    # A checkpoint/stamp must never certify a diff that produced ZERO real review
    # content — `result_is_usable` is the same predicate the board path already uses
    # for exactly this reason.
    #
    # ACCEPTED LIMITATION (glm review finding, this feature's own PR): `result_is_usable`
    # marks any rc=0 body <= 400 chars containing a marker phrase like "is currently
    # unavailable" as unusable, with no check for WHERE in the body it appears — so a
    # genuine terse verdict that happens to QUOTE that phrase (e.g. reviewing this exact
    # code, "the cooldown check only fires on 'is currently unavailable'; add the other
    # markers") reads as `ok=False` and withholds the stamp/checkpoint. This is not new
    # here — the board path (`run_board_with_failover`) already carried the identical
    # heuristic before this diff; this change only extends the SAME accepted trade-off
    # to the flat path for consistency. Narrowing it (e.g. only matching the marker on
    # its own line) risks a false NEGATIVE — letting a real cache-hit sentinel through
    # as "usable" — which is the more dangerous direction for a commit-gate certifying
    # a review actually happened. Left as-is deliberately; not a regression to fix here.
    ok = all(result_is_usable(result) for result in results)
    _stamp_if_staged_commit_review(
        ok, staged, diff_from_stdin, cwd, diff, dispatch_diff_truncated
    )
    override = _checkpoint_if_requested(
        commit, ok, diff_from_stdin, cwd, diff, dispatch_diff_truncated
    )
    if override is not None:
        return override
    return 0 if ok else 1


def _warn_if_dispatch_diff_truncated(
    diff: str, dispatch_diff: str, staged: bool
) -> bool:
    """Print a visible stderr warning whenever the dispatch cap actually truncated the
    diff, and return whether it did ON A `--staged` RUN specifically (codex review
    finding, round 5, then round 6): the stamp/checkpoint always certify against the
    UNCAPPED canonical `diff` (by design — see cap_diff_for_dispatch's docstring), so
    `review diff --staged` on an oversized staged diff used to still pass the
    commit-hook gate ("the staged index was reviewed") even though every seat actually
    saw only the first $REVIEW_DIFF_MAX_BYTES plus a truncation note. A stderr warning
    alone does NOT protect automation (a non-interactive `review diff --staged --commit`
    never sees it) — the return value here lets the caller REFUSE to stamp/checkpoint on
    a truncated staged diff, not just warn about it (see EXIT_COMMIT_DIFF_TRUNCATED).
    The return value stays STAGED-SCOPED on purpose (an unstaged run has no commit-gate
    certification to refuse), but the WARNING itself now fires either way.

    Opus review finding (this feature's own PR): an unstaged, over-cap `review diff`
    used to truncate completely SILENTLY to the user — the only signal was the marker
    embedded in the payload handed to the backend, which never reaches the user's own
    stdout. Not printing a warning here just because there's no gate to refuse left a
    real, common case (a plain `review diff` on a huge change) reviewing a partial diff
    with zero visible notice. Both branches below share the same byte-count facts; only
    the framing (commit-gate consequence vs none) and the git command hint differ."""
    truncated = dispatch_diff != diff
    if not truncated:
        return False
    full_bytes = len(diff.encode("utf-8"))
    reviewed_bytes = len(dispatch_diff.encode("utf-8"))
    if staged:
        print(
            "[review-cli] WARNING: the staged diff exceeded $REVIEW_DIFF_MAX_BYTES and "
            "was TRUNCATED for every seat — this review covers only a PARTIAL view of "
            "the staged change, not the full diff, so no commit-gate stamp/checkpoint "
            f"will be created. Full diff: {full_bytes} bytes; reviewed: "
            f"{reviewed_bytes} bytes. Scope the review "
            "(`git diff --cached -- <path>`) or raise the cap to review the full change.",
            file=sys.stderr,
            flush=True,
        )
        return True
    print(
        "[review-cli] WARNING: the diff exceeded $REVIEW_DIFF_MAX_BYTES and was "
        "TRUNCATED for every seat — this review covers only a PARTIAL view of the "
        f"change. Full diff: {full_bytes} bytes; reviewed: {reviewed_bytes} bytes. "
        "Scope the review (`git diff -- <path>`) or raise $REVIEW_DIFF_MAX_BYTES to "
        "review the full change.",
        file=sys.stderr,
        flush=True,
    )
    return False


def _stamp_if_staged_commit_review(
    ok: bool,
    staged: bool,
    diff_from_stdin: bool,
    cwd: Path,
    diff: str,
    truncated: bool,
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
      * NOT `truncated` — codex review finding: the stamp certifies the UNCAPPED
        canonical `diff`, but every seat only saw the CAPPED, truncated copy when this
        is True. Skipped silently here (matching the existing `ok=False` silent skip —
        `_warn_if_dispatch_diff_truncated` already printed the loud stderr warning that
        explains WHY, before this function is ever called), so a truncated staged
        review no longer satisfies the pre-commit hook gate it would otherwise pass.

    `truncated` has NO default (Fable review finding): a defaulted `False` would be
    fail-OPEN — a future call site that simply forgets to pass it would silently
    certify a truncated review again, exactly the bug this parameter exists to prevent.
    Every current caller already computes and threads it explicitly.
    """
    if ok and staged and not diff_from_stdin and not truncated:
        _write_review_stamp(cwd, diff)
        _touch_review_marker()


_CHECKPOINT_COMMIT_MESSAGE = "chore: checkpoint via review diff --staged --commit"

# Matches `git commit`'s own stdout summary line — `[branch abc1234] message`, for the
# repo's very first commit `[branch (root-commit) abc1234] message`, or in detached HEAD
# `[detached HEAD abc1234] message` (codex P2 on review-cli#120: "detached HEAD" is TWO
# words where every other ref-description is one token, so it needs its own alternative —
# tried first, or `\S+` would greedily match just "detached" and the rest wouldn't line
# up). Git prints this using the SHA it just created, BEFORE running the post-commit hook
# — see `_parse_new_commit_sha` for why that ordering is exactly what makes this reliable,
# and why `_parse_new_commit_sha` still cross-checks the match rather than trusting it
# blind (a hook can print its OWN stdout into the same captured stream).
_COMMIT_SUMMARY_RE = re.compile(
    r"^\[(?:detached HEAD|\S+)(?:\s+\(root-commit\))?\s+([0-9a-fA-F]+)\]", re.MULTILINE
)


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
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_new_commit_sha(cwd: Path, commit_stdout: str) -> str | None:
    """Extract the SHA `git commit` reports it just created, from ITS OWN stdout summary
    line — never `git rev-parse HEAD` read after the fact (codex P1 on review-cli#120):
    a post-commit hook, or a concurrent session sharing the checkout, can advance HEAD
    past our commit before we get to read it, and a naive `rev-parse HEAD` would then
    verify/undo THAT later commit instead of ours — up to and including resetting away a
    completely unrelated commit that just happened to land in the window. Git prints the
    summary line using the commit it just made, BEFORE invoking the post-commit hook, so
    it always names OUR commit regardless of what happens afterward.

    `commit_stdout` is the WHOLE captured stream, which also carries anything a
    pre-commit/commit-msg hook printed before git's own summary line (codex P2 on
    review-cli#120: a hook that happens to print something bracket-shaped could win a
    naive "first regex match"). So every candidate match is checked against reality: it
    must resolve to a real object (`rev-parse --verify`) whose commit message is EXACTLY
    `_CHECKPOINT_COMMIT_MESSAGE` — a coincidental bracket-shaped hook line practically
    never also names a real object with that exact message. Returns the first candidate
    that checks out, or None if none do — the caller treats that as "can't verify,"
    refusing to touch history rather than guess."""
    for m in _COMMIT_SUMMARY_RE.finditer(commit_stdout):
        try:
            proc = _run(
                ["git", "-C", str(cwd), "rev-parse", "--verify", m.group(1)],
                cwd=cwd,
                env=git_repo_env(cwd),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        sha = proc.stdout.strip()
        try:
            subject_proc = _run(
                ["git", "-C", str(cwd), "log", "-1", "--format=%s", sha],
                cwd=cwd,
                env=git_repo_env(cwd),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if (
            subject_proc.returncode == 0
            and subject_proc.stdout.strip() == _CHECKPOINT_COMMIT_MESSAGE
        ):
            return sha
    return None


def _resolve_parent(cwd: Path, sha: str) -> tuple[str, str | None]:
    """Resolve `sha`'s parent, distinguishing a POSITIVELY CONFIRMED root commit (zero
    parents) from a lookup that merely FAILED (git error, corrupt repo, permissions) —
    conflating the two (codex P2 on review-cli#120) let a transient failure masquerade as
    "root commit", and the caller (`_undo_checkpoint_commit`) would then `update-ref -d
    HEAD` — un-borning the WHOLE branch — for a commit that might not be root at all.

    Returns `("root", None)` only when `git rev-list --parents` positively reports zero
    parents, `("parent", <sha>)` for exactly one, or `("unknown", None)` for anything
    else (lookup failure, a merge commit with 2+ parents — this feature only ever makes
    plain commits, so that shape itself means "not the commit we think it is"). Callers
    MUST refuse to act on "unknown" rather than guess."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "rev-list", "--parents", "-n", "1", sha],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", None
    if proc.returncode != 0:
        return "unknown", None
    parts = proc.stdout.split()
    if len(parts) == 1:
        return "root", None
    if len(parts) == 2:
        return "parent", parts[1]
    return "unknown", None


def _empty_tree_sha(cwd: Path) -> str | None:
    """This repo's empty-tree object hash, used to diff a root commit (no parent)
    against "nothing" — computed via `git mktree` on empty input rather than the
    hardcoded SHA-1 constant `4b825dc642...` (codex P2 on review-cli#120: that constant
    is wrong in a `--object-format=sha256` repo, where it would make every root-commit
    checkpoint mismatch and undo a perfectly good commit). `mktree` writes using
    whatever object format THIS repo is configured for, so it is correct either way."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "mktree"],
            cwd=cwd,
            env=git_repo_env(cwd),
            input_text="",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def _commit_diff(cwd: Path, sha: str) -> str | None:
    """The diff `sha` actually introduces (against its parent, or this repo's empty tree
    for a CONFIRMED root commit) — used to verify AFTER the fact that a just-made commit
    contains exactly the reviewed diff and nothing a hook smuggled in. None on any
    failure OR on an unresolvable parent status (the caller treats "can't verify" as
    "verification failed": don't trust the commit, but also don't guess at an undo)."""
    status, parent = _resolve_parent(cwd, sha)
    if status == "unknown":
        return None
    base = _empty_tree_sha(cwd) if status == "root" else parent
    if base is None:
        return None
    try:
        proc = _run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", base, sha],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _undo_checkpoint_commit(cwd: Path, sha: str) -> bool:
    """Undo a just-made checkpoint commit WITHOUT touching the index/working tree — so
    whatever a hook staged is left exactly as-is for the user to inspect, same spirit as
    `git reset --soft HEAD~1` (documented as the safe undo for this feature). A CONFIRMED
    root commit has no parent to reset to, so it's un-born instead.

    Uses `git update-ref <ref> <new> <old>` — a compare-and-swap, not a blind `git reset
    --soft`/`update-ref -d` (codex P1 on review-cli#120): those act on whatever HEAD
    currently is, and if HEAD has ALREADY moved past `sha` (a post-commit hook, or a
    concurrent session, making its own commit on top), a blind reset would silently drag
    that later, unrelated commit's ref-value back too — discarding it as if it never
    happened. The CAS only succeeds when the ref's CURRENT value is exactly `sha`;
    otherwise it fails and nothing is touched, which is exactly the outcome we want when
    we can no longer be sure `sha` is still what HEAD points to.

    Returns True iff an undo actually ran. Returns False — touching NOTHING — when the
    parent status is "unknown" (refusing to guess whether it's root — codex P2 on
    review-cli#120) OR when the CAS itself was rejected (HEAD moved). The caller surfaces
    either case as a distinct "could not safely undo" message."""
    status, parent = _resolve_parent(cwd, sha)
    if status == "unknown":
        return False
    if status == "root":
        proc = _run(
            ["git", "-C", str(cwd), "update-ref", "-d", "HEAD", sha],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    else:
        proc = _run(
            ["git", "-C", str(cwd), "update-ref", "HEAD", parent, sha],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    return proc.returncode == 0


def _verify_checkpoint_matches_review(cwd: Path, sha: str, reviewed_diff: str) -> str:
    """Post-commit gate (codex review finding on this feature's own PR, review-cli#120):
    a pre-commit hook that auto-formats/lint-`--fix`es and RE-STAGES files, then exits 0,
    lets `git commit` succeed — but the tree it commits is read from the index AS IT
    STANDS when the hook finishes, which can now hold MORE than `reviewed_diff`. The
    TOCTOU guard in `_checkpoint_commit` only catches drift BEFORE `git commit` runs; it
    cannot catch a hook that mutates the index AS A SIDE EFFECT of that same call, since
    the tree snapshot is taken after the hook exits, not before.

    So this re-diffs `sha` (the commit `git commit` JUST created — resolved by the
    caller via `_parse_new_commit_sha`, NOT a fresh `rev-parse HEAD`) against its parent
    and compares it to `reviewed_diff`. A mismatch means the hook smuggled in
    extra/different content — the commit is undone (`_undo_checkpoint_commit`,
    index/worktree left alone so nothing is lost) and a non-empty detail string is
    returned. Empty string means verified clean.
    """
    committed_diff = _commit_diff(cwd, sha)
    if committed_diff is not None and committed_diff == reviewed_diff:
        return ""
    if not _undo_checkpoint_commit(cwd, sha):
        return (
            "the commit at "
            + sha
            + " may hold more than the reviewed diff, but it could "
            "NOT be safely undone — either its parentage couldn't be positively confirmed, "
            "or HEAD has already moved past it (a post-commit hook or a concurrent session "
            "made another commit), so undoing would risk touching history that isn't ours. "
            "Nothing was changed; inspect `git show "
            + sha
            + "` / `git log -1` and clean up "
            "manually if it holds more than the reviewed diff."
        )
    return (
        "a pre-commit hook modified the index during the commit (e.g. auto-format/"
        "lint --fix re-staging files) — the resulting commit would have held more than "
        "the reviewed diff, so it was undone (index/working tree were left untouched). "
        "Inspect `git status`/`git diff --cached`, review the hook's changes separately, "
        "then re-run the checkpoint."
    )


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

    A SECOND guard runs AFTER the commit (`_verify_checkpoint_matches_review`): the first
    guard cannot see a pre-commit hook that mutates the index as a side effect of the
    commit it is itself gating (codex P1 on review-cli#120) — only re-diffing the
    finished commit catches that.

    This is a REAL `git commit` — it runs the repo's own commit-msg/pre-commit hooks
    (lint/typecheck/tests, conventional-commit format) and can genuinely fail on a red
    tree or a malformed message. It never passes `--no-verify`: bypassing that gate is
    exactly the kind of accident this feature exists to prevent, not reproduce under a
    new flag. Returns `(succeeded, detail)` — `detail` is empty on success, else a short
    explanation (drift detected, spawn error, hook mismatch, or git's own stderr/stdout)
    for the caller to report.
    """
    current_diff = _current_staged_diff(cwd)
    if current_diff is None:
        return (
            False,
            "could not re-read the staged diff to verify it is still the reviewed one",
        )
    if current_diff != reviewed_diff:
        return False, (
            "the staged diff changed since the review ran (another process/session may "
            "have touched the index) — refusing to checkpoint a diff that was never reviewed"
        )
    try:
        proc = _run(
            ["git", "-C", str(cwd), "commit", "-m", _CHECKPOINT_COMMIT_MESSAGE],
            cwd=cwd,
            env=git_repo_env(cwd),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git commit could not run: {exc}"
    if proc.returncode != 0:
        return False, proc.stderr.strip() or proc.stdout.strip() or "git commit failed"
    new_sha = _parse_new_commit_sha(cwd, proc.stdout)
    if new_sha is None:
        return False, (
            "the commit succeeded but its SHA could not be parsed from git's own output "
            "to verify it — nothing was undone; inspect `git log -1` manually"
        )
    mismatch_detail = _verify_checkpoint_matches_review(cwd, new_sha, reviewed_diff)
    if mismatch_detail:
        return False, mismatch_detail
    return True, ""


def _checkpoint_if_requested(
    commit: bool,
    ok: bool,
    diff_from_stdin: bool,
    cwd: Path,
    diff: str,
    truncated: bool,
) -> int | None:
    """Create the `--commit` checkpoint commit after a review, if requested.

    Called only after `mode_review` has already validated commit-implies-staged up
    front, so `staged` is always True here by construction — the remaining gate mirrors
    `_stamp_if_staged_commit_review`'s other conditions (`ok`, not `diff_from_stdin`,
    not `truncated`).

    Precisely: this CHECKPOINTS the reviewed staged diff after the review completes —
    it does NOT mean "commits when the review passes". `ok` means the pool produced
    usable verdicts, not that the diff is clean; a review that reports open findings
    still has `ok=True` and still gets checkpointed (findings are informational, the
    same as the existing `--staged` stamp gate). That is intended, not a bug.

    Returns `None` to leave the review's own 0/1 result standing (covers: `--commit`
    not requested; the gate not satisfied — the review failed, the diff was piped, or
    the diff was truncated for dispatch — in which case a note is printed but the
    review's own result is untouched; or the checkpoint commit itself succeeded).
    Returns `EXIT_COMMIT_FAILED` to OVERRIDE an otherwise-successful review's exit code
    when the review passed, the gate was satisfied, but the `git commit` subprocess
    itself failed. Returns `EXIT_COMMIT_DIFF_TRUNCATED` when the staged diff exceeded
    the dispatch cap (codex review finding): a checkpoint is supposed to certify the
    FULL reviewed diff, but every seat only saw the truncated copy — a stderr warning
    alone does not protect non-interactive automation from silently checkpointing a
    partial review, so this REFUSES rather than checkpoints. Either override is a
    visible, distinct failure, never silently swallowed as the review's own 0/1 result.

    `truncated` has NO default (Fable review finding): a defaulted `False` would be
    fail-OPEN — a future call site that simply forgets to pass it would silently
    checkpoint a truncated review again. Every current caller already computes and
    threads it explicitly.
    """
    if not commit:
        return None
    if not ok:
        print(
            "[review-cli] --commit: the review did not succeed — no checkpoint created.",
            file=sys.stderr,
            flush=True,
        )
        return None
    if diff_from_stdin:
        print(
            "[review-cli] --commit: the diff was piped on stdin, not the git index — no "
            "checkpoint created (a pipe reviews arbitrary input; --commit checkpoints the "
            "staged index).",
            file=sys.stderr,
            flush=True,
        )
        return None
    if truncated:
        print(
            "[review-cli] --commit: the staged diff exceeded $REVIEW_DIFF_MAX_BYTES and "
            "was TRUNCATED for dispatch — no checkpoint created (a checkpoint must certify "
            "the FULL reviewed diff, and every seat here only saw a partial view). Scope "
            "the review (`git diff --cached -- <path>`) or raise $REVIEW_DIFF_MAX_BYTES, "
            "then re-run `review diff --staged --commit`.",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_COMMIT_DIFF_TRUNCATED
    succeeded, detail = _checkpoint_commit(cwd, diff)
    if succeeded:
        print(
            "[review-cli] --commit: checkpoint commit created.",
            file=sys.stderr,
            flush=True,
        )
        return None
    print(
        "[review-cli] --commit: the review succeeded but the checkpoint commit itself "
        f"FAILED — no checkpoint was created.\n  git commit failed: {detail}\n"
        "  fix: check the repo's commit-msg/pre-commit hooks (lint/typecheck/tests may be "
        "blocking it), then re-run `review diff --staged --commit`.",
        file=sys.stderr,
        flush=True,
    )
    return EXIT_COMMIT_FAILED


def _mode_review_board(
    board: list[BoardReviewer],
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    staged: bool,
    pool_size: int,
    outcome_sink: list[FailoverOutcome] | None,
    diff_from_stdin: bool = False,
    visual_images: tuple[Path, ...] = (),
    exact_board: bool = False,
    commit: bool = False,
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
        # Chain-aware, matching the pool guard (cli._chain_aware_available) — a raw
        # `backend_available` here would silently shrink the pool the guard just approved:
        # a seat whose head provider is down but has a live failover alternate is REAL live
        # (provider_chain would route around it at dispatch time), so the startup split must
        # agree (codex P1 on review of #157: 'the chain-aware guard approves seats that
        # board dispatch still removes').
        from ..provider_failover import any_provider_available

        def _chain_aware_available(r: BoardReviewer) -> bool:
            return any_provider_available(
                r.model,
                available=backend_available,
                unpaid=runtime_provider_marked_unpaid,
            )

        pool, reserve = split_pool_reserve(board, pool_size, _chain_aware_available)
    if not pool:
        print(
            "[review-cli] board: no reviewers are available — configure at least one "
            "backend key/CLI (e.g. COMMANDCODE_API_KEY, GEMINI_API_KEY, codex/claude on "
            "PATH).",
            file=sys.stderr,
            flush=True,
        )
        return 1

    # Capped copy for backend dispatch ONLY — `diff` itself (used below for the
    # stamp/checkpoint) stays uncapped (see cap_diff_for_dispatch's docstring). Same
    # stdin exemption as the flat path above.
    dispatch_diff = diff if diff_from_stdin else cap_diff_for_dispatch(diff)
    dispatch_diff_truncated = _warn_if_dispatch_diff_truncated(
        diff, dispatch_diff, staged
    )
    outcome = run_board_with_failover(
        pool, reserve, prompt, dispatch_diff, cwd, timeout, visual_images
    )
    if outcome_sink is not None:
        outcome_sink.append(outcome)

    print("\n\n---\n\n".join(format_result(r) for r in outcome.results))

    if outcome.degraded:
        print(
            f"[review-cli] board: degraded — only {len(outcome.usable)} of "
            f"{outcome.target} seats produced a usable verdict (reserve exhausted).",
            file=sys.stderr,
            flush=True,
        )

    # Success gate: the pool refilled to its target of usable verdicts (NOT degraded).
    # A failed-then-replaced seat is handled, so it must not block — only a real
    # shortfall (reserve exhausted) does.
    ok = not outcome.degraded
    _stamp_if_staged_commit_review(
        ok, staged, diff_from_stdin, cwd, diff, dispatch_diff_truncated
    )
    override = _checkpoint_if_requested(
        commit, ok, diff_from_stdin, cwd, diff, dispatch_diff_truncated
    )
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
        "--retry",
        type=int,
        default=None,
        metavar="N",
        help=(
            "in-seat retries on a TRANSIENT failure (429/529/5xx/timeout/overloaded) before "
            "falling to the reserve (default from $REVIEW_RETRY_COUNT, else "
            f"{retry_default()}); applies to the failover board AND the flat -m panel. A "
            "SEAT-FATAL failure (auth/bad-model/501/refusal) is never retried. 0 disables it."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
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
        ctx.models,
        ctx.with_visual(ctx.args.prompt),
        ctx.diff,
        ctx.cwd,
        ctx.timeout,
        ctx.args.staged,
    )
    if board is None:
        return mode_review(
            *base,
            board=None,
            diff_from_stdin=diff_from_stdin,
            visual_images=_visual_images(ctx),
            effort_override=ctx.effort_override,
            commit=commit,
        )
    return mode_review(
        *base,
        board=board,
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
