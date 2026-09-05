"""review CLI entry: argparse + dispatch only.

This is the package entry point (`[project.scripts] review = "reviewlib.cli:main"`
and the target of the thin `bin/review` shim). It owns argument parsing, diff
acquisition, model selection, and dispatch to the mode functions. All behaviour
lives in the sibling modules — this file is the thin entry the Stage 0
decomposition was about.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from . import backends
from . import usage_limits
from .backends import _which  # re-export for tests/compat  # noqa: F401
from .backstop import run_backstop
from .config import (
    CONFIG_PATH,
    DEFAULT_PRESET,
    DEFAULT_MODELS,
    DEFAULT_POOL_SIZE,
    DEFAULT_PROMPT,
    MODERATOR_CANDIDATES,
    PANEL_TIMEOUT_DEFAULT,
    QA_TIMEOUT_DEFAULT,
    VISUAL_MODELS,
    BoardConfigError,
    EffortValueError,
    _expand_alias,
    _effective_pool_size,
    _split_models,
    apply_effort_override,
    board_from_models,
    expand_flat_models_with_reuse,
    load_board,
    load_config,
    parse_effort_flag,
    preset_names,
    preset_pool_size,
    split_pool_reserve,
)
from .usage_limits import usage_percent_for_model
from .pool_guard import (
    PROCEED,
    Candidate,
    default_distinct_key,
    evaluate_selection,
)
from .install import install_commit_hook, install_hook_tg, install_skill
from .modes.brainstorm import mode_brainstorm
from .modes.contract import ModeContext, ModeSpec
from .modes.just_ask import mode_just_ask
from .modes.quorum import mode_quorum
from .modes.registry import (
    REMOVED_FLAGS,
    REMOVED_MODE_FLAGS,
    REMOVED_SUBCOMMANDS,
    brainstorm_pool,
    diff_mode,
    get_mode,
    iter_modes,
    known_subcommands,
)
from .modes.review import mode_review
from .panel import begin_call_tally, end_call_tally, pick_moderators
from .process import _run, git_repo_env, strip_control_sequences
from .retry import max_retry_count
from .stats import (
    announce_eta,
    diff_content_hash,
    extract_diff_files,
    fmt_duration,
    iterations_for_task,
    normalize_repo_remote,
    normalize_task_code,
    quorum_check,
    record_run,
    task_summaries,
)

if TYPE_CHECKING:
    from agenttools_service import ServiceManager

# Keep the mode-handler names imported off `cli` for legacy import compatibility ONLY
# (some external/legacy callers `from reviewlib.cli import mode_review`). Dispatch goes
# through `modes/registry`, so rebinding `cli.mode_*` has NO effect on the running mode.
# NEW tests/code must patch the handler in its OWN module (e.g.
# `reviewlib.modes.review.mode_review`) or configure the `ModeSpec`, never via `cli`.
# This tuple just keeps the names referenced so the imports aren't flagged unused.
__mode_fns__ = (mode_brainstorm, mode_just_ask, mode_quorum, mode_review)

# Stable, per-class exit codes (structured-exit-codes). The diff-review path REQUIRES a
# git repo; run it outside one and it must fail GRACEFULLY with this distinct code (a
# "wrong place to run this" usage class), NOT a raw traceback / generic crash. Scripts can
# branch on it; it stays stable. 0=success, 2=argparse/usage (argparse's own), 124=backstop
# (reviewlib.backstop). 3 is the not-a-repo class — distinct from argparse-2 so a caller can
# tell "you ran the diff review outside a repo" apart from "you mistyped a flag".
EXIT_NOT_A_REPO = 3
# 4 is the "in a repo, but `git diff` itself failed" class (e.g. a wedged/timed-out git, a
# corrupt index) — distinct from EXIT_NOT_A_REPO (you ARE in a repo) and argparse-2. The
# REQUIRED review path catches the RuntimeError `_git_diff` raises so this never tracebacks.
EXIT_GIT_DIFF_FAILED = 4
# qa mode (review-qa.md §4/§6): "qa ran but no test-case suites/cases are authored for the
# target" — a CONTRACT failure (a green qa run with zero authored cases is a lie), distinct
# from a real finding, so CI can tell "you didn't author any suites" apart from "a test
# failed". The qa handler's no-suites gate returns this BEFORE any agent/docker/browser.
#
# Value is 6, NOT the 5 the spec's §6 first proposed: code 5 is already taken at the
# PROCESS-exit level by brainstorm's EXIT_DEAD_PANEL (modes/brainstorm.py), and structured
# exit codes must stay per-class distinct, so a script seeing 5 can't tell "brainstorm dead
# panel" from "qa no suites". qa's own codes therefore start at the next free integer (6);
# the later qa env classes (NO_ENV / ENV_UNHEALTHY / SUT_BOOT_FAILED) continue from 7 in
# Phase 2/3 (the spec's 5/6/7/8 block shifts up by one for the brainstorm collision).
EXIT_QA_NO_SUITES = 6
# qa env classes (review-qa.md §6, shifted +1 from the spec's 5/6/7/8 because code 5 is
# brainstorm's EXIT_DEAD_PANEL — qa's own block starts at EXIT_QA_NO_SUITES=6). Phase 2
# lands the executor; the only env class it needs is "could not bring the SUT up at all"
# (the agent emitted VERDICT: BLOCKED) — distinct from a real finding (which is report-only)
# and from no-suites (6), so CI tells "infra broke" apart from "a bug was found". The
# NO_ENV / ENV_UNHEALTHY classes belong to the (later) compose env harness — reserved here
# as 7/9 so the executor's BLOCKED code (8) sits between them in the same coherent block.
EXIT_QA_NO_ENV = (
    7  # no stage AND no bring-up config for a backend/bot SUT (env harness, later)
)
EXIT_QA_SUT_BOOT_FAILED = (
    8  # the tester could not bring the SUT up at all (VERDICT: BLOCKED)
)
EXIT_QA_ENV_UNHEALTHY = (
    9  # bring-up succeeded but the health gate timed out (env harness, later)
)
# 10 is the pool-selection FOOLPROOFING class (reviewlib.pool_guard.EXIT_UNSATISFIED): the
# resolved review selection could not converge, so the CLI printed a proposal / targeted
# per-provider error instead of dispatching a degenerate panel. Distinct from every class
# above so a script can tell "the review pool couldn't be assembled" apart from a finding,
# a usage error, or a not-a-repo run. The value is owned by pool_guard (single source).

# RETIRED Phase-1 scaffold code. Phase 1 returned 70 from the suites-resolved-but-no-executor
# branch ("not implemented yet"). Phase 2 lands the executor, so that branch is GONE — the
# constant is kept (and pointed at the real executor path) only so a stale caller/import does
# not break; nothing in the live code returns it anymore.
EXIT_QA_NOT_IMPLEMENTED = 70

# Codes 11/12 are CLAIMED by `modes/review.py` (EXIT_COMMIT_REQUIRES_STAGED / EXIT_COMMIT_
# FAILED, the `--commit` checkpoint feature) — defined there, not here, because that mode
# module can't import from cli.py without a circular import. Recorded here too so the next
# person picking a new top-level exit code doesn't re-use them.


def _is_git_repo(cwd: Path) -> bool:
    """Cheap, correct "is `cwd` inside a git work tree?" probe.

    `git rev-parse --is-inside-work-tree` is the canonical, fast check (exit 0 + `true`
    inside a work tree, non-zero outside). The whole point of this probe is to AVOID a raw
    traceback, so every way the spawn itself can blow up is caught and treated as "not a
    repo": OSError (a non-existent / non-directory `cwd` -> FileNotFoundError /
    NotADirectoryError, e.g. a stale `-C /missing/path`) and TimeoutExpired (a wedged `git
    rev-parse` -> `_run` forwards `timeout=` straight to subprocess.run, which raises). `_run`
    is `text=True, stdout=PIPE`, so `proc.stdout` is always a str (never None)."""
    try:
        proc = _run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and proc.stdout.strip().lower() == "true"


def _fail_not_a_repo(cwd: Path) -> int:
    """Print the 3-part WHAT/WHY/HOW message for "ran the diff review outside a repo" and
    return the stable EXIT_NOT_A_REPO code. Verb-named because it has a side effect (prints)
    AND returns the code. No traceback — this is an expected user error, not a crash."""
    print(
        f"[review-cli] not in a git repository ({cwd}).\n"
        "  the diff review needs a repo to diff (it reviews your working-tree / staged changes).\n"
        '  fix: run a mode that needs no git — `review just-ask "..." --task CODE` / '
        '`review quorum "..." --task CODE` / `review brainstorm "..." --task CODE` — '
        "or cd into a repo and re-run.",
        file=sys.stderr,
        flush=True,
    )
    return EXIT_NOT_A_REPO


def _fail_git_diff(cwd: Path, exc: Exception) -> int:
    """Print a structured error for "in a git repo, but `git diff` failed" (the REQUIRED
    review path) and return the stable EXIT_GIT_DIFF_FAILED code. `_is_git_repo` passing does
    NOT guarantee `git diff` succeeds (a wedged/timed-out git, a corrupt index), so this is
    the no-traceback floor for that path — an expected runtime failure, not a crash."""
    print(
        f"[review-cli] could not read the git diff in {cwd}.\n"
        f"  git diff failed: {exc}\n"
        "  fix: check the repo is healthy (`git status`), or pipe a diff on stdin "
        "(`git diff | review diff --task CODE`).",
        file=sys.stderr,
        flush=True,
    )
    return EXIT_GIT_DIFF_FAILED


def _git_diff(cwd: Path, staged: bool) -> str:
    """Return the working-tree (or --staged) diff. Raises RuntimeError on ANY failure —
    a non-zero `git diff`, a spawn failure (missing/non-dir `cwd` -> OSError, or a missing
    git binary -> FileNotFoundError), or a wedged git (TimeoutExpired). Normalizing every
    failure to the single RuntimeError type is what lets each OPTIONAL caller (--visual /
    brainstorm / panel --diff|--staged) catch it and degrade to "". The REQUIRED review path
    is gated by `_is_git_repo` first, so the common non-repo case is handled gracefully
    there; a RARE in-repo `git diff` failure (a wedge, a corrupt repo) on that path still
    surfaces as the RuntimeError above — a clean one-line error, not a silent wrong result.

    The diff is anchored to `cwd` TWICE — `git -C <cwd>` AND `env=git_repo_env(cwd)` (FOREIGN
    repo-pinning git vars dropped) — so a `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` leaked from
    a parent pointing at an UNRELATED repo can't silently divert the diff. Without it, `git -C
    /repoB diff --cached` reads the env's repo, not repoB — the review-gate then reviews the
    wrong (or empty) diff (review-cli#71). `git_repo_env` KEEPS the target repo's own hook env
    (a legit pre-commit's GIT_INDEX_FILE/temp `next-index` that scopes `--cached` to the partial
    commit), dropping only env vars that resolve outside `cwd`'s git dir (codex P2 on PR #72).

    `--src-prefix=a/ --dst-prefix=b/` pins the header format regardless of the invoking
    machine's `diff.noprefix`/custom-prefix git config (some machines set
    `diff.noprefix=true` globally, which emits headers with NO a/b prefix at all —
    `diff --git f.txt f.txt` — silently breaking `stats.extract_diff_files`'s
    `diff --git a/<path> b/<path>` parse, found live on this repo's own dev machine).
    Content is unaffected — only the path labels in the header/`---`/`+++` lines."""
    args = [
        "git",
        "-C",
        str(cwd),
        "diff",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
    ]
    if staged:
        args.append("--cached")
    try:
        proc = _run(args, cwd=cwd, env=git_repo_env(cwd), timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git diff could not run: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff failed")
    return proc.stdout


def _stamp_hash_for_staged_diff(cwd: Path) -> str | None:
    """Sha256 of `git diff --no-ext-diff --cached` with NO `--src-prefix`/
    `--dst-prefix` — i.e. exactly what the pre-commit hook's own independent
    verification computes, whatever the machine's ambient `diff.noprefix`/prefix
    git config happens to produce. None on any failure (best-effort; the review
    still proceeds without the tightened stamp, falling back to
    `_write_review_stamp`'s own re-derive at write time).

    Called ONCE, immediately adjacent to the `_git_diff(cwd, staged=True)` call
    that captures the diff actually sent to the models (reviewlib.install
    "_write_review_stamp" docstring has the full story of why this exists —
    round-5 review finding, k3+Opus: hashing at stamp-WRITE time instead of
    dispatch-CAPTURE time reopened a multi-minute TOCTOU window where a
    concurrent index mutation during the panel run could get silently
    certified as reviewed)."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--cached"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    import hashlib

    return hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest()


def _git_remote_origin_url(cwd: Path) -> str | None:
    """`git -C cwd remote get-url origin`, or None on ANY failure (no remote, not a
    repo, no git binary, a wedge) — best-effort, this only feeds an identity label,
    never a required path."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


def _git_toplevel(cwd: Path) -> Path | None:
    """`git -C cwd rev-parse --show-toplevel`, or None on any failure (not a repo,
    no git binary, a wedge)."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip())


def _compute_repo_id(cwd: Path) -> str | None:
    """Best-effort stable identity for the repo at `cwd`, for run-stats diff-identity
    binding (reviewlib.stats "Diff-identity binding") — see that module's docstring for
    WHY this exists (closes 3 real task-code quorum-pollution incidents).

    Prefers the normalized `origin` remote URL (stable across worktrees/clones of the
    SAME repo, unlike a local path). Falls back to the resolved absolute repo root
    for a remote-less repo, prefixed `path:` so a path-based id can never collide with
    a remote-based one. The path fallback SELF-NORMALIZES via `_git_toplevel` rather
    than trusting `cwd` as-is (review finding on this feature's own PR, round 2: every
    CURRENT caller already passes an `_effective_cwd`-resolved toplevel, so this was
    not a live bug today, but the id would silently diverge — `path:/repo/subdir` at
    record time vs `path:/repo` at check time, a spurious `repo_mismatch` — the moment
    any future caller passed a subdirectory, and nothing enforced the invariant this
    function's OWN docstring asserted). Falls back to `cwd` itself only when `cwd` is
    a real directory but git can't resolve its toplevel (a non-repo directory, kept
    for the same "reviewing it as-is" posture `_effective_cwd` already has elsewhere).
    None only when `cwd` isn't even a real directory.
    """
    url = _git_remote_origin_url(cwd)
    if url:
        normalized = normalize_repo_remote(url)
        if normalized:
            return normalized
    toplevel = _git_toplevel(cwd)
    if toplevel is not None:
        return f"path:{toplevel}"
    try:
        if cwd.is_dir():
            return f"path:{cwd}"
    except OSError:
        pass
    return None


def _default_branch_ref(cwd: Path) -> str | None:
    """Best-effort `origin/<default-branch>` ref for `cwd`, or None.

    Tries the recorded `origin/HEAD` symref first (what `git clone` sets up), then
    falls back to probing the two common default-branch names directly — a shallow
    CI checkout or a repo cloned with `--single-branch` may lack `origin/HEAD`
    entirely. Feeds `--check`'s diff-identity file-set comparison for the common
    post-push case (clean working tree, nothing to diff locally); never raises.
    """
    try:
        proc = _run(
            ["git", "-C", str(cwd), "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().replace("refs/remotes/", "")
    for candidate in ("origin/main", "origin/master"):
        try:
            proc = _run(
                ["git", "-C", str(cwd), "rev-parse", "--verify", candidate],
                cwd=cwd,
                env=git_repo_env(cwd),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return candidate
    return None


def _diff_name_only(cwd: Path, ref_args: list[str]) -> list[str] | None:
    """`git diff --name-only <ref_args>` -> sorted touched-file list, or None on ANY
    failure (non-repo, unresolvable ref, a wedge). `--name-only` sidesteps the
    `diff.noprefix` footgun `_git_diff` otherwise has to pin `--src-prefix`/
    `--dst-prefix` against (`extract_diff_files`'s `diff --git a/... b/...` regex
    doesn't even apply here — there IS no `diff --git` header, just bare file
    paths) — a real bug this exact fallback shipped with once already (codex/
    GLM/opus/fable review finding on this feature's own PR: the first cut only
    pinned the prefix on `_git_diff`, not on this fallback's own separate `git
    diff` call, silently disabling file-level matching on any `diff.noprefix=true`
    machine — precisely the class this feature targets). Also far cheaper than a
    full patch body for an identity check: file NAMES only, not hunks."""
    try:
        proc = _run(
            ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--name-only", *ref_args],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _current_diff_files_for_check(cwd: Path) -> list[str] | None:
    """Best-effort file list for the diff currently under review at `cwd`, for
    `--check`'s repo/diff mismatch detection (reviewlib.stats "Diff-identity
    binding").

    Returns the UNION of `git diff --name-only HEAD` (local uncommitted changes —
    staged AND unstaged together, the dev-loop case) and the branch's diff against
    its own default branch when one resolves (the post-push `gh ship` case).
    Deliberately a UNION, not first-non-empty-wins: two independent review
    findings (Opus + Fable) on this feature's own PR caught that "HEAD probe wins
    whenever the tree has ANY uncommitted change" lets a single unrelated dirty
    file at check time (a stray edit, a version bump in progress) SHADOW the
    branch's real PR files entirely — the post-push case is exactly when the
    branch diff is what matters, and it's exactly when a stray local edit is most
    likely to be sitting around. Unioning means a legitimate iteration whose files
    overlap the REAL branch diff still verifies even with local dirt present.
    Returns None (skip the file-level check; `repo_id` alone still gates) only
    when NEITHER source resolves to anything — never raises.
    """
    local = set(_diff_name_only(cwd, ["HEAD"]) or [])
    default_ref = _default_branch_ref(cwd)
    branch = (
        set(_diff_name_only(cwd, [f"{default_ref}...HEAD"]) or [])
        if default_ref
        else set()
    )
    union = sorted(local | branch)
    return union or None


def _read_stdin_if_piped() -> str | None:
    if sys.stdin.isatty():
        return None
    try:
        data = sys.stdin.read()
    except OSError:
        return None
    return data if data else None


def _effective_cwd(raw: str, *, warn: bool = True) -> Path:
    """Resolve the review cwd, preferring the enclosing git repository root.

    Agents commonly invoke `review` from a scratch / temp directory and forget
    -C, so the diff and the claude-p workspace silently point at the wrong place
    (often /tmp) and the review is empty or about the wrong code. Resolve to the
    git toplevel when inside a repo (also robust to being run from a subdir), and
    warn loudly when the cwd is not a git repo at all so the mistake is visible
    instead of producing a misleading review. Pass -C <project-root> to be exact.

    `warn=False` suppresses the non-repo "reviewing it as-is" warning for a caller that
    will itself print a clearer message: the review-mode diff path hard-fails via
    `_fail_not_a_repo`, so the "as-is" promise would contradict that hard-fail.
    """
    resolved = Path(raw).expanduser().resolve()
    if resolved.is_dir():
        # This runs on EVERY invocation, BEFORE mode dispatch — including the no-git modes
        # (just-ask / quorum / brainstorm) that must "work anywhere". So the git spawn here
        # must NEVER leak a raw traceback: a missing git binary (OSError -> FileNotFoundError)
        # or a wedged `git rev-parse` (TimeoutExpired) degrades to "review the dir as-is",
        # exactly like a non-repo dir — same defensive catch as `_is_git_repo`. The rev-parse
        # is anchored to `resolved` AND runs with the repo-pinning git env stripped
        # (git_repo_env) so a leaked GIT_DIR/GIT_WORK_TREE can't resolve the toplevel to an
        # UNRELATED repo — the same #71 footgun the diff probe guards against.
        try:
            proc = _run(
                ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
                cwd=resolved,
                env=git_repo_env(resolved),
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
    if warn:
        print(
            f"[review-cli] warning: {resolved} is not inside a git repository; "
            "reviewing it as-is — pass -C <project-root> to point review at your repo.",
            file=sys.stderr,
            flush=True,
        )
    return resolved


# Shared between the `__serve` parser and the managed-service parser so the two surfaces never
# drift on what `--host` means (loopback-only by default; 0.0.0.0 exposes over Tailscale).
_DASHBOARD_HOST_HELP = "interface to bind (default: 127.0.0.1 loopback-only; 0.0.0.0 exposes over Tailscale)"

# Printed (exit 4) when a genuine lifecycle action is requested but the shared service lib is
# absent. Kept as a module constant so the smoke test can grep it and it stays aligned with the
# `pyproject.toml [dashboard]` extra comment that explains the same in-ecosystem dependency.
_AGENTTOOLS_SERVICE_MISSING_MSG = (
    "[review dashboard] the managed-service subcommands need the shared "
    "'agenttools_service' lib, which isn't installed.\n"
    "  why:  run/start/status/stop/enable/disable come from one shared service-manager "
    "(agent-tools/lib/agenttools_service), not a per-tool copy.\n"
    "  fix:  pip install -e <agent-tools>/lib/agenttools_daemon "
    "-e <agent-tools>/lib/agenttools_service\n"
    "  note: `review dashboard run` still works without it for an ad-hoc foreground server."
)


def _dashboard_help_no_lib() -> int:
    """Help for a BARE ``review dashboard`` when the service lib is ABSENT (still launches nothing).

    Mirrors the lib-present help surface — it advertises every lifecycle action so the bare-HELP
    contract (and the smoke test that greps for ``status`` / the action names) holds regardless of
    whether ``agenttools_service`` is installed — while making clear those actions need the lib."""
    print(
        "usage: review dashboard [--host H] [--port N] "
        "{run,start,stop,status,enable,disable}\n\n"
        "review-cli dashboard as a managed service. A bare `review dashboard` prints this help "
        "and launches nothing.\n\n"
        "actions:\n"
        "  run      run in the FOREGROUND (this shell), blocking — ad-hoc / when disabled\n"
        "  start    start in the BACKGROUND (detached daemon); return immediately\n"
        "  status   is it running? pid / port / url / autostart-enabled\n"
        "  stop     stop the background instance\n"
        "  enable   install OS autostart (launchd / systemd --user / fallback) AND start now\n"
        "  disable  remove OS autostart AND stop\n\n"
        "note: start/status/stop/enable/disable need the shared 'agenttools_service' lib "
        "(pip install -e <agent-tools>/lib/agenttools_daemon "
        "-e <agent-tools>/lib/agenttools_service); `run` works without it."
    )
    return 0


def _dashboard_serve(rest: list[str]) -> int:
    """Hidden entry: ``review dashboard __serve`` — run the blocking web server in THIS shell.

    This is the FOREGROUND server the managed service runs (``run``) and detaches (``start``):
    it calls ``run_dashboard`` directly and so never re-enters the service dispatcher (a bare
    ``run`` action whose argv pointed back at ``review dashboard run`` would fork-bomb). Not
    advertised in help — operators use ``review dashboard run`` (ad-hoc) or ``start`` (managed).

    Binds 127.0.0.1 by default; ``--host 0.0.0.0`` exposes it over Tailscale (mirrors
    ``review spec-web``). Imported lazily so the dashboard's stdlib HTTP stack never loads on
    the hot review path (and a stray import error in dashboard code can't break `review`)."""
    sub = argparse.ArgumentParser(
        prog="review dashboard __serve",
        description="Run the review-cli dashboard web server (blocking).",
    )
    sub.add_argument("--host", default="127.0.0.1", help=_DASHBOARD_HOST_HELP)
    sub.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to bind (default: a free ephemeral port)",
    )
    sub.add_argument(
        "--no-open", action="store_true", help="do not open a browser window"
    )
    sub.add_argument(
        "--verbose", action="store_true", help="log every HTTP request to stderr"
    )
    ns = sub.parse_args(rest)
    from .dashboard import run_dashboard

    return run_dashboard(
        port=ns.port, host=ns.host, open_browser=not ns.no_open, verbose=ns.verbose
    )


def _dashboard_subcommand(rest: list[str]) -> int:
    """`review dashboard [run|start|status|stop|enable|disable]` — the dashboard as a MANAGED
    service, plus the legacy ad-hoc ``run``.

    The lifecycle subcommands (run/start/status/stop/enable/disable) come from the shared
    ``agenttools_service`` lib (one service-manager for every long-running server in the
    ecosystem — review dashboard, config-web, tg-ctl, future daemons), NOT hand-rolled here:

      run      run in the FOREGROUND (this shell), blocking — ad-hoc / when disabled.
      start    start in the BACKGROUND (detached daemon); return immediately.
      status   is it running? pid / port / url / autostart-enabled.
      stop     stop the background instance.
      enable   install OS autostart (launchd / systemd --user / no-systemd fallback) AND start now.
      disable  remove OS autostart AND stop.

    A BARE ``review dashboard`` (no action) prints HELP and launches NOTHING. ``--host`` /
    ``--port`` select the managed bind (a stable default port, unlike ad-hoc ``run``'s ephemeral
    one). Everything (the lib, the service descriptor) is imported lazily so the hot ``review``
    path never pays for the dashboard/service stack."""
    # Hidden foreground entry that the service argv points at (see dashboard.service._serve_argv).
    # Kept BEFORE the agenttools_service import so the blocking server still runs even on a host
    # where the shared service lib isn't installed (a detached `start` from a host that HAS the
    # lib can run `__serve` here).
    if rest and rest[0] == "__serve":
        return _dashboard_serve(rest[1:])

    try:
        from agenttools_service import add_service_subcommands, dispatch
    except ImportError:
        # No shared service lib on this host. The bare-HELP and ad-hoc `run` contracts must
        # still hold (they don't depend on the lib): a BARE `review dashboard` (or `--help`)
        # prints help and launches nothing, and `run` still brings up an ad-hoc foreground
        # server so a lib-less operator isn't fully blocked. Only the genuine lifecycle actions
        # (start/status/stop/enable/disable) need the lib — for those, emit an actionable error
        # (structured-exit-codes) instead of a raw ImportError traceback.
        action, action_idx = _dashboard_action_with_index(rest)
        # A BARE `review dashboard` OR a help-only `review dashboard [--help|-h]` (no lifecycle
        # action) prints help and launches nothing — exit 0. The help surface is the SAME
        # whether or not the lib is installed (the smoke test greps it for the action names),
        # so a lib-less operator still sees what the dashboard can do. `--help`/`-h` with no
        # action was previously mis-routed to the missing-lib error (exit 4), breaking the
        # bare-HELP contract — a `--help`/`-h` is NOT itself a lifecycle action.
        if action is None:
            return _dashboard_help_no_lib()
        if action == "run":
            # Ad-hoc foreground server still works without the lib. Drop ONLY the `run` action
            # token (by index, not by value — a value that happens to equal "run" must survive);
            # `_dashboard_serve` parses the rest as server flags (and prints its own --help/exit 0
            # for `run --help`, so the help contract holds there too).
            return _dashboard_serve(rest[:action_idx] + rest[action_idx + 1 :])
        # A genuine lifecycle action (start/status/stop/enable/disable) needs the lib — emit the
        # actionable missing-lib error (structured-exit-codes) instead of a raw ImportError.
        print(_AGENTTOOLS_SERVICE_MISSING_MSG, file=sys.stderr)
        return 4

    from .dashboard.service import DEFAULT_DASHBOARD_HOST, DEFAULT_DASHBOARD_PORT

    parser = argparse.ArgumentParser(
        prog="review dashboard",
        description="review-cli dashboard as a managed service (run/start/status/stop/enable/disable).",
    )
    parser.add_argument(
        "--host", default=DEFAULT_DASHBOARD_HOST, help=_DASHBOARD_HOST_HELP
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DASHBOARD_PORT,
        help=f"port to bind the managed dashboard (default: {DEFAULT_DASHBOARD_PORT})",
    )
    subs = parser.add_subparsers(dest="action")
    # Parse FIRST, then build the manager factory over the parsed namespace. The factory closes
    # over `ns`, so `ns` must exist before any action can invoke it; registering the subcommands
    # does not call the factory (it is lazy, per the lib contract), so registration order vs parse
    # is free — but binding `ns` explicitly avoids a fragile forward-reference closure.
    add_service_subcommands(
        subs,
        manager_factory=lambda: _dashboard_manager(ns.host, ns.port),
        service_name="dashboard",
    )
    ns = parser.parse_args(rest)

    def _print_help_ok() -> int:
        parser.print_help()
        return 0

    return dispatch(ns, on_no_subcommand=_print_help_ok)


def _dashboard_manager(host: str, port: int) -> "ServiceManager":
    """Build a ``ServiceManager`` for the dashboard at ``host:port`` (lazy, per the lib's
    ``manager_factory`` contract — constructed only when an action actually runs)."""
    from agenttools_service import ServiceManager

    from .dashboard.service import dashboard_service

    return ServiceManager(dashboard_service(port=port, host=host))


def _sessions_subcommand(rest: list[str]) -> int:
    """`review sessions [-a/--all] [-s/--resume <id>] [--force] [-m … --moderator …]`.

    List or RESUME brainstorm sessions parsed from the on-disk discussion logs. Kept as a
    bare subcommand (like `dashboard`) — it is a MANAGEMENT command over the logs, not a
    fan-out review mode, so it does not go through the mode registry. All session logic
    lives in `reviewlib.sessions` (lib); this handler is thin.

    Default listing (no `-a`) shows recent COMPLETED sessions (a sensible recent subset);
    `-a/--all` adds the dead/interrupted ones (crashed / killed / timed out — no synthesis)
    and lifts the cap. `-s <id>` RESUMES: it reloads the saved transcript and continues the
    brainstorm from `completed_round + 1`, reusing the saved topic / panel / moderator,
    then synthesizes — it does NOT start from scratch.
    """
    from . import sessions as _sessions

    sub = argparse.ArgumentParser(
        prog="review sessions",
        description="List or resume brainstorm sessions (parsed from the discussion logs).",
    )
    sub.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="list ALL sessions incl. dead/interrupted (no synthesis); default lists recent completed",
    )
    sub.add_argument(
        "-s",
        "--resume",
        metavar="ID",
        default=None,
        help="resume the session with this id (short id or unambiguous prefix): continue the round loop and synthesize",
    )
    sub.add_argument(
        "--force",
        action="store_true",
        help="with --resume on an already-completed session, re-synthesize anyway",
    )
    # Resume reuses the saved panel/moderator by default; -m / --moderator override.
    sub.add_argument(
        "-m",
        "--model",
        action="append",
        default=[],
        help="override the resume panel (repeat or comma-separate); default = the saved session's panel",
    )
    sub.add_argument(
        "-C",
        "--cwd",
        default=".",
        help="repository directory (resume diff/agentic cwd)",
    )
    sub.add_argument(
        "--moderator",
        default=None,
        help="override the resume moderator; default = the saved session's moderator",
    )
    # Grounding diff on resume: the original `--diff`/`--staged` grounding is NOT persisted
    # in the discussion log, so a resumed grounded brainstorm would otherwise continue
    # UNgrounded. These flags re-attach the current working-tree (--diff) or staged
    # (--staged) diff as grounding for the resumed rounds + synthesis (opt-in, like the
    # brainstorm mode's own grounding). Absent -> the resume runs ungrounded.
    sub.add_argument(
        "--diff",
        action="store_true",
        help="re-attach the working-tree diff as grounding for the resumed rounds",
    )
    sub.add_argument(
        "--staged",
        action="store_true",
        help="re-attach the staged diff (git diff --cached) as grounding for the resumed rounds",
    )
    sub.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=f"per-call timeout seconds for the resumed rounds (default {PANEL_TIMEOUT_DEFAULT})",
    )
    ns = sub.parse_args(rest)

    if ns.resume:
        return _resume_session_cli(ns)

    sessions = _sessions.list_sessions(include_dead=ns.all)
    if not sessions:
        scope = "" if ns.all else " completed"
        print(f"No{scope} brainstorm sessions found in {_sessions.log_dir()}.")
        if not ns.all:
            print("(pass -a/--all to include dead/interrupted sessions.)")
        return 0
    header = (
        "all sessions (incl. interrupted)" if ns.all else "recent completed sessions"
    )
    print(
        f"Brainstorm {header} — newest first; resume with `review sessions -s <id>`:\n"
    )
    for s in sessions:
        ts = s.timestamp.strftime("%Y-%m-%d %H:%M UTC") if s.timestamp else "?"
        topic = (s.topic[:60] + "…") if len(s.topic) > 61 else (s.topic or "(no topic)")
        print(
            f"  {s.session_id}  [{s.status:<11}]  r{s.completed_rounds}  {ts}  {topic}"
        )
    return 0


def _resume_session_cli(ns: argparse.Namespace) -> int:
    """Resolve the saved session by id and continue its brainstorm. Thin over
    `reviewlib.sessions.resume_session`; resolves the panel/moderator (saved unless
    overridden) and reports the clean errors (unknown id / ambiguous prefix / already
    complete) with actionable messages + meaningful exit codes."""
    from . import backends, sessions as _sessions

    backends.configure_unpaid_providers(load_config().get("unpaid_providers"))

    try:
        sess = _sessions.find_session(ns.resume)
    except _sessions.AmbiguousSessionError as exc:
        print(f"[review sessions] {exc}", file=sys.stderr, flush=True)
        return 2
    if sess is None:
        print(
            f"[review sessions] no session with id '{ns.resume}'. "
            "Run `review sessions -a` to list available ids.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    cwd = _effective_cwd(ns.cwd)
    # Panel: explicit -m override > the saved session panel (dropping unreachable
    # backends so a vanished key never aborts) > whatever the saved panel was.
    explicit_models = _split_models(ns.model)
    if explicit_models:
        models = explicit_models
    else:
        models = (
            [m for m in sess.panel if backends.backend_available(m)]
            or sess.panel
            or list(DEFAULT_MODELS)
        )
    # Moderator: explicit --moderator override > the saved session moderator > picked.
    # The log records the moderator FALLBACK CHAIN joined with `>` (e.g. `claude:..>codex`),
    # so the saved value must be SPLIT back into candidates — passing the whole `a>b>c`
    # string as one explicit seed would make `pick_moderators` try an invalid single
    # backend id before falling back. Take the FIRST (highest-priority) saved candidate as
    # the explicit seed; pick_moderators rebuilds the rest of the priority order.
    saved_mod = sess.moderator.split(">")[0].strip() if sess.moderator else ""
    mod_seed = ns.moderator or (saved_mod or None)
    moderators = pick_moderators(mod_seed, models)
    timeout = ns.timeout if ns.timeout is not None else PANEL_TIMEOUT_DEFAULT

    # Optional grounding diff for the resumed rounds: --diff / --staged re-attach the
    # current diff (the original grounding is not persisted in the log). Degrades to
    # ungrounded on a non-repo / git failure, exactly like the brainstorm mode.
    diff = ""
    if getattr(ns, "diff", False) or getattr(ns, "staged", False):
        try:
            diff = _git_diff(cwd, ns.staged)
        except RuntimeError:
            diff = ""

    print(
        f"[review sessions] resuming '{sess.session_id}' ({sess.status}, "
        f"{sess.completed_rounds} round(s) done): {sess.topic}",
        file=sys.stderr,
        flush=True,
    )

    # Panel modes announce their live-log paths (the resumed rounds stream to the log).
    backends._ANNOUNCE_LOGS = True
    try:
        return _sessions.resume_session(
            sess,
            models=models,
            cwd=cwd,
            timeout=timeout,
            moderators=moderators,
            diff=diff,
            force=ns.force,
        )
    except _sessions.SessionAlreadyCompleteError as exc:
        # A refused resume (already-complete, no --force) did NO requested work. Return the
        # same non-zero code the unknown/ambiguous-id paths use so scripts and hooks can
        # tell a refusal from a real resume — exit 0 here was indistinguishable from success
        # (codex P2: CTO sided with the bot over the prior "intentional" exit-0 choice).
        print(f"[review sessions] {exc}", file=sys.stderr, flush=True)
        return 2


def _task_sessions(task_code: str):
    """Dashboard sessions for a task, oldest first. Best-effort over existing logs."""
    from .dashboard import parser as dparser
    from .process import log_dir

    sessions = [s for s in dparser.load_sessions(log_dir()) if s.task_code == task_code]
    return sorted(sessions, key=lambda s: s.started)


_TASK_SESSION_MATCH_WINDOW_SECONDS = 10 * 60


def _parse_task_record_started(record: dict) -> datetime | None:
    ts = record.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _session_started_utc(session) -> datetime:
    dt = session.started
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _task_record_iteration(record: dict) -> int | None:
    try:
        return int(record["iteration"])
    except (KeyError, TypeError, ValueError):
        return None


def _closest_iteration_for_session(session, iterations: list[dict]) -> int | None:
    session_started = _session_started_utc(session)
    best_iteration: int | None = None
    best_delta: float | None = None
    for record in iterations:
        started = _parse_task_record_started(record)
        iteration = _task_record_iteration(record)
        if started is None or iteration is None:
            continue
        delta = abs((session_started - started).total_seconds())
        if delta > _TASK_SESSION_MATCH_WINDOW_SECONDS:
            continue
        if best_delta is None or delta < best_delta:
            best_iteration = iteration
            best_delta = delta
    return best_iteration


def _session_for_task_iteration(
    record: dict,
    sessions: list,
    all_iterations: list[dict] | None = None,
    used_session_ids: set[str] | None = None,
):
    """Match a stats iteration to the session whose start time is closest to it."""
    started = _parse_task_record_started(record)
    iteration = _task_record_iteration(record)
    if started is None or iteration is None:
        return None
    best = None
    best_delta: float | None = None
    used = used_session_ids or set()
    for session in sessions:
        if session.session_id in used:
            continue
        delta = abs((_session_started_utc(session) - started).total_seconds())
        if delta > _TASK_SESSION_MATCH_WINDOW_SECONDS:
            continue
        if (
            all_iterations
            and _closest_iteration_for_session(session, all_iterations) != iteration
        ):
            continue
        if best_delta is None or delta < best_delta:
            best = session
            best_delta = delta
    return best


def _iteration_for_session(session, iterations: list[dict]) -> int | None:
    return _closest_iteration_for_session(session, iterations)


def _task_subcommand(rest: list[str]) -> int:
    """`review task CODE [--detail N|SESSION] [--json]` — task-scoped review history.

    The run-stats JSONL is the authoritative iteration list; dashboard logs provide
    transcript detail when they are still present. Both stores are private local files.
    """
    if "--task" in rest or any(arg.startswith("--task=") for arg in rest):
        print(
            "[review task] use positional CODE for history lookup: review task CODE. "
            "--task is for recorded review modes.",
            file=sys.stderr,
        )
        return 2
    parser = argparse.ArgumentParser(
        prog="review task",
        description="Show review iterations, models, and transcripts for one task code.",
    )
    parser.add_argument("code", nargs="?", help="task/issue code, e.g. HYP-742")
    parser.add_argument(
        "--detail",
        metavar="N|SESSION",
        help="print the full transcript for an iteration number or session id",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 iff the task has enough PASSED recorded iterations across enough "
        "distinct models (self-merge-authority gate); see --min-iter/--min-models. "
        "Counts only iterations whose run came back clean — a review that ran but "
        "failed/degraded does not count toward the bar, and pre-verdict-field "
        "history never satisfies it either (fail-closed)",
    )
    parser.add_argument(
        "--min-iter",
        type=int,
        default=3,
        help="review-bar floor: PASSED recorded iterations (default 3)",
    )
    parser.add_argument(
        "--min-models",
        type=int,
        default=3,
        help="review-bar floor: distinct models among the PASSED iterations (default 3)",
    )
    parser.add_argument(
        "-C",
        "--cwd",
        default=".",
        help="--check only: repo to verify recorded iterations against "
        "(diff-identity binding — see reviewlib.stats module docstring)",
    )
    parser.add_argument(
        "--no-verify-identity",
        action="store_true",
        help="--check only: skip repo/diff mismatch detection entirely (legacy "
        "behavior — every PASSED iteration counts, exactly as before this gate "
        "existed). Escape hatch for a cwd that can't resolve a repo; NOT a way "
        "around a genuine mismatch finding.",
    )
    ns = parser.parse_args(rest)

    if ns.check:
        if not ns.code:
            print("[review task] --check requires a task CODE", file=sys.stderr)
            return 2
        # A floor of 0 would trivially satisfy the bar for a task with ZERO passed
        # iterations (0 >= 0), even one whose every recorded run failed or predates
        # the verdict field -- defeating the fail-closed contract this gate exists
        # for. Both floors must be at least 1.
        if ns.min_iter < 1 or ns.min_models < 1:
            print(
                "[review task] --min-iter and --min-models must both be >= 1 "
                f"(got --min-iter {ns.min_iter} --min-models {ns.min_models})",
                file=sys.stderr,
            )
            return 2
        return _quorum_check_subcommand(
            ns.code,
            ns.min_iter,
            ns.min_models,
            as_json=ns.json,
            cwd_raw=ns.cwd,
            verify_identity=not ns.no_verify_identity,
        )

    if not ns.code:
        summaries = task_summaries()
        if ns.json:
            import json as _json

            print(_json.dumps({"tasks": summaries}, indent=2))
            return 0
        if not summaries:
            print("No task-coded review iterations found.")
            return 0
        print("Review tasks:")
        for item in summaries:
            models = ", ".join(item["models"]) or "-"
            print(
                f"  {item['task_code']}: {item['iterations']} iteration"
                f"{'' if item['iterations'] == 1 else 's'} · models: {models}"
            )
        return 0

    try:
        code = normalize_task_code(ns.code)
    except ValueError as exc:
        print(f"[review task] invalid task code: {exc}", file=sys.stderr)
        return 2
    assert code is not None

    iterations = iterations_for_task(code)
    sessions = _task_sessions(code)

    if ns.detail:
        return _task_detail(code, ns.detail, iterations, sessions, as_json=ns.json)

    if ns.json:
        import json as _json

        payload = {
            "task_code": code,
            "iterations": iterations,
            "sessions": [s.to_summary() for s in sessions],
        }
        print(_json.dumps(payload, indent=2))
        return 0

    if not iterations and not sessions:
        print(f"No review iterations found for task {code}.")
        return 1

    count = len(iterations) if iterations else len(sessions)
    print(f"Task {code}: {count} iteration{'' if count == 1 else 's'}")
    if iterations:
        used_session_ids: set[str] = set()
        for item in iterations:
            idx = int(item["iteration"])
            session = _session_for_task_iteration(
                item, sessions, iterations, used_session_ids
            )
            if session is not None:
                used_session_ids.add(session.session_id)
            models = ", ".join(item.get("models") or []) or "-"
            duration = fmt_duration(float(item.get("duration_seconds") or 0))
            suffix = (
                f" · session {session.session_id}" if session else " · logs not found"
            )
            print(
                f"  iteration {idx}: {item.get('ts', '?')} · {item.get('mode', '?')} "
                f"· pool={item.get('pool_size', 0)} · models: {models} "
                f"· ok={item.get('ok_count', 0)} fail={item.get('fail_count', 0)} "
                f"· {duration}{suffix}"
            )
    else:
        for idx, session in enumerate(sessions, start=1):
            print(
                f"  iteration {idx}: {session.started.isoformat()} · {session.mode} "
                f"· models: {', '.join(session.models) or '-'} · session {session.session_id}"
            )
    print(f"\nDetail: review task {code} --detail <iteration|session_id>")
    return 0


def _resolve_stat_since(since_arg: str | None, days: int) -> datetime | None | bool:
    """Resolve `--since`/`--days` into a UTC datetime floor (None = all history).
    Returns `False` as a sentinel for an unparseable `--since` — the caller reports it
    as a usage error rather than silently falling back to "all history".

    Opus/kimi review finding: `datetime.fromisoformat` only accepts a trailing `Z`
    (Zulu/UTC) shorthand from Python 3.11 onward, but this project declares
    `requires-python = ">=3.9"` — and every call-log filename this command's own output
    is built from uses exactly that `...Z` stamp format. A user copying a timestamp
    straight out of `review stat`'s own report (or a filename) got a real usage error
    on 3.9/3.10 while the identical value worked on 3.11+. Normalize the shorthand
    ourselves before parsing so the accepted syntax doesn't depend on the interpreter."""
    if since_arg:
        normalized = since_arg[:-1] + "+00:00" if since_arg.endswith("Z") else since_arg
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return False
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if days <= 0:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


# glm review finding, round 2: `--harness` filtered on an EXACT match against
# `report["harnesses"]`'s keys, which are the raw `CallLog.backend` string every OTHER
# call-log writer uses (`opencode`, `z.ai`, `commandcode`, ...) — not the short aliases
# `-m`/config actually teach (`-m glm`, `-m cc`, board ids `zai:...`/`oc:...`). So
# `--harness glm`/`zai`/`oc`/`cc` all printed "no calls recorded" while the data sat in
# the report under a different spelling. Sourced ONLY from aliases genuinely resolved
# elsewhere in this codebase — `resolve_backend`'s own alt-spelling matching
# (`zai`/`zhipu`/`glm` -> the z.ai backend, `oc` -> opencode) and `config.MODEL_ALIASES`
# (the `glm*` family, `cc`/`commoncode` -> commandcode) — NOT invented here. `cmd` is
# deliberately absent: despite a stale comment elsewhere once claiming it as a
# commandcode alias, it resolves NOWHERE in `_match_named_backend` or `MODEL_ALIASES` —
# it is not a real alias, so it is not silently accepted here either.
_HARNESS_ARG_ALIASES = {
    "zai": "z.ai",
    "zhipu": "z.ai",
    "glm": "z.ai",
    "glm52": "z.ai",
    "glm51": "z.ai",
    "glm47": "z.ai",
    "glm46": "z.ai",
    "glm45": "z.ai",
    "oc": "opencode",
    "cc": "commandcode",
    "commoncode": "commandcode",
    "command-code": "commandcode",
    "command_code": "commandcode",
    "common-code": "commandcode",
    "common_code": "commandcode",
}


def _normalize_harness_arg(raw: str) -> str:
    """The `--harness` value, normalized to the exact `CallLog.backend` spelling the
    report's `harnesses` dict is keyed by — see `_HARNESS_ARG_ALIASES` above.

    Opus review finding, round 4: the fallback for a token NOT in the alias dict used
    to return `raw` completely UNCHANGED — but every real backend key is lowercase with
    no surrounding whitespace, so `--harness Codex` (different casing) or `--harness
    "codex "` (trailing space) fell through as literally `"Codex"`/`"codex "`, which
    then never equals the report's `"codex"` key — a false "no calls recorded" for data
    that genuinely exists. The fallback now returns the SAME normalized (stripped,
    lowercased) key the alias lookup itself used, so an exact name matches regardless
    of input casing/whitespace, while a genuinely unrecognized token still produces the
    honest "no calls recorded for harness ..." message, not a silent misroute."""
    key = raw.strip().lower()
    return _HARNESS_ARG_ALIASES.get(key, key)


def _stat_subcommand(rest: list[str]) -> int:
    """`review stat [--days N] [--since ISO] [--top N] [--harness NAME] [--json]` —
    detailed per-harness/per-model usage + health report parsed from the real per-call
    logs (see `reviewlib.dashboard.tokenstats` for the data model and the 2026-08
    token-burn investigation this answers). Default window is the last 7 days — a full
    scan of a long-lived install's log dir (tens of thousands of files) is slow, and
    most token-burn questions are about recent behaviour; `--days 0` scans everything."""
    parser = argparse.ArgumentParser(
        prog="review stat",
        description="Per-harness/per-model usage + health report from the real call logs.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="only calls from the last N days (default 7; <= 0 = all recorded history)",
    )
    parser.add_argument(
        "--since",
        metavar="ISO",
        default=None,
        help="only calls at/after this ISO-8601 timestamp (overrides --days)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="how many largest calls to list (default 10; clamped to >= 1)",
    )
    parser.add_argument(
        "--harness",
        default=None,
        help="narrow the per-harness BREAKDOWN TABLE to this backend (e.g. codex, "
        "opencode, commandcode, omp, claude, z.ai — also accepts common aliases: "
        "glm/zai/oc/cc) — every other DATA section (models, Fable, retry events, "
        "call count, top oversized calls) stays whole-window in --json; the text "
        "report's Fable/retry/oversized sections show it too, but per-model detail is "
        "--json-only (see `review stat --json`)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    ns = parser.parse_args(rest)
    # A non-positive --top has no sensible "top N" meaning, and a negative value would
    # silently slice as "all but the last |N|" (sorted(...)[:-3]) — a confusing result
    # under the "Top N oversized calls" heading (kimi review finding). Clamp instead of
    # erroring: a typo'd --top still gets a useful (if minimal) report.
    if ns.top < 1:
        ns.top = 1

    since = _resolve_stat_since(ns.since, ns.days)
    if since is False:
        print(
            f"[review stat] invalid --since value: {ns.since!r} "
            "(expected ISO-8601, e.g. 2026-08-01T00:00:00+00:00)",
            file=sys.stderr,
        )
        return 2

    from .dashboard.tokenstats import compute_stat_report

    report = compute_stat_report(since=since, top=ns.top)
    # glm review finding, round 2: normalize BEFORE filtering — see
    # `_normalize_harness_arg` for why a raw alias (`glm`, `zai`, `oc`, `cc`) used to
    # always miss even though the report's own footnote teaches it. Displaying the
    # normalized name (not the raw one the user typed) in the empty-result message is
    # deliberate: it shows exactly what was actually searched for, so a mistaken alias
    # mapping is visible, not hidden.
    harness = _normalize_harness_arg(ns.harness) if ns.harness else None
    if harness:
        # Table-only filter (codex review finding): this narrows ONLY the per-harness
        # breakdown row set, not the whole report — models/fable/retry-events/
        # call_count/top_oversized_calls are cross-harness context that stays
        # whole-window on purpose (e.g. seeing where codex's calls rank among the
        # largest overall). See the --harness help text above.
        report["harnesses"] = {
            name: hs for name, hs in report["harnesses"].items() if name == harness
        }
        # Opus review finding, round 3: this stderr note used to print UNCONDITIONALLY,
        # so the text-report path showed the "no calls recorded for harness ..." message
        # TWICE — once here, once again via `_render_stat_harness_table`'s own empty-
        # table message in the printed report body. Harmless but redundant. The `--json`
        # path has no equivalent in-payload message (an empty `harnesses` dict alone
        # doesn't explain WHY), so it's the only path that still needs this stderr note.
        if not report["harnesses"] and ns.json:
            print(
                f"[review stat] no calls recorded for harness {harness!r} in this window.",
                file=sys.stderr,
            )

    if ns.json:
        import json as _json

        print(_json.dumps(report, indent=2))
        return 0
    print(_render_stat_report_text(report, requested_harness=harness))
    return 0


def _render_stat_report_text(
    report: dict, *, requested_harness: str | None = None
) -> str:
    """Assemble the full human-readable `review stat` report from its sections.
    `requested_harness` (the raw `--harness` value, if any) only affects the empty-table
    message — see `_render_stat_harness_table`."""
    sections = [
        _render_stat_header(report),
        _render_stat_harness_table(
            report["harnesses"], requested_harness=requested_harness
        ),
        _render_stat_fable_section(report["fable"]),
        _render_stat_retry_section(report["retry_events_by_kind"]),
        _render_stat_oversized_section(report["top_oversized_calls"]),
        "Note: real token counts exist ONLY for REST-backed calls "
        f"({', '.join(report['tokens_recorded_backends'])}); the agentic CLI harnesses "
        "(oc/opencode, omp, codex, claude in CLI mode) show tokens: not captured (see "
        "below) — bytes are the best available cross-harness proxy today. Each of these "
        "CLIs DOES expose exact usage/cost via its own --json/--format json mode "
        "(verified live); review-cli doesn't invoke them that way yet because it would "
        "replace their readable stdout wholesale, breaking the paywall/auth detection "
        "this report itself relies on — tracked separately, see review-cli"
        "#186. `cc` is not a separate harness — it resolves to `commandcode`, same as "
        "`--harness cc` (see `--harness`'s help text for the full alias list).",
    ]
    return "\n\n".join(sections)


def _render_stat_header(report: dict) -> str:
    window = f"since {report['since']}" if report["since"] else "all recorded history"
    return (
        f"review stat — {report['log_dir']} ({window})\n"
        f"calls: {report['call_count']}   retry/promotion events: {report['retry_event_count']}"
    )


def _render_stat_harness_table(
    harnesses: dict, *, requested_harness: str | None = None
) -> str:
    """One row per backend: call/health counts, byte-proxy distribution, real tokens
    (REST backends only), and the SKILL.md/MEMORY.md context-pollution rate.

    `requested_harness` distinguishes "genuinely nothing in this window" from "calls
    exist, just none for the requested --harness" (kimi review finding: the generic
    message was false/misleading in the latter case — this table can be legitimately
    empty here while `report['call_count']` in the header above is nonzero)."""
    if not harnesses:
        if requested_harness:
            return (
                f"No calls recorded for harness {requested_harness!r} in this window "
                "(other harnesses may still have activity — see the header above)."
            )
        return "No calls recorded in this window."
    from .dashboard.tokenstats import format_bytes

    header = (
        f"{'HARNESS':<14}{'CALLS':>7}{'OK':>6}{'FAIL':>6}{'RUN':>5}  "
        f"{'BYTES':>9}{'AVG':>9}{'P90':>9}{'MAX':>9}  {'TOK(real)':>10}  SKILL.md  MEMORY.md"
    )
    rows = [header, "-" * len(header)]
    for name, hs in harnesses.items():
        tok = (
            f"{hs['tokens_prompt']}/{hs['tokens_output']}" if hs["tokens_real"] else "-"
        )
        calls = hs["calls"] or 1
        skill_pct = f"{100 * hs['skill_md_calls'] / calls:.0f}%"
        mem_pct = f"{100 * hs['memory_md_calls'] / calls:.0f}%"
        rows.append(
            f"{name:<14}{hs['calls']:>7}{hs['ok']:>6}{hs['fail']:>6}{hs['running']:>5}  "
            f"{format_bytes(hs['bytes_total']):>9}{format_bytes(hs['bytes_avg']):>9}"
            f"{format_bytes(hs['bytes_p90']):>9}{format_bytes(hs['bytes_max']):>9}  "
            f"{tok:>10}  {skill_pct:>8}  {mem_pct:>9}"
        )
    title = (
        "Per-harness breakdown (bytes = call-log size, a token PROXY for every "
        "harness; TOK(real) = exact prompt/output tokens, REST backends only):"
    )
    return title + "\n" + "\n".join(rows)


def _render_stat_fable_section(fable: dict) -> str:
    """Surfaces the investigation's headline finding: the priority-1 Fable seat's
    dispatch/failure rate and WHY it failed (session-limit vs paywall vs auth vs other)."""
    if not fable["dispatch_attempts"] and not fable["retry_events"]:
        return "Fable (priority-1 board seat): no dispatch attempts recorded in this window."
    rate = (
        f"{fable['failure_rate']:.0%}" if fable["failure_rate"] is not None else "n/a"
    )
    reasons = fable["retry_event_reasons"]
    return (
        "Fable (priority-1 board seat) pattern:\n"
        f"  dispatch attempts: {fable['dispatch_attempts']}   "
        f"cached-skips: {fable['cached_skips']}   failure rate: {rate}\n"
        f"  retry/promotion events: {fable['retry_events']} "
        f"(session_limit={reasons['session_limit']} paywall={reasons['paywall']} "
        f"auth={reasons['auth']} other={reasons['other']})\n"
        "  note: dispatch_attempts/failure_rate are a LOWER BOUND — a claude CLI-mode "
        "call is only attributable to Fable when its body carries the paywall sentinel; "
        "a successful Fable dispatch or a session-limit-shaped failure is attributed to "
        "Opus instead (see reviewlib.dashboard.tokenstats.compute_fable_report)."
    )


def _render_stat_retry_section(by_kind: dict) -> str:
    if not by_kind:
        return "Retry/promotion events: none recorded in this window."
    parts = " ".join(f"{kind}={count}" for kind, count in sorted(by_kind.items()))
    return f"Retry/promotion events by kind: {parts}"


def _render_stat_oversized_section(top: list[dict]) -> str:
    """The largest calls by log size — the investigation's own outlier-hunting method,
    now a standing report instead of a one-off manual pass."""
    if not top:
        return "Top oversized calls: none recorded in this window."
    from .dashboard.tokenstats import format_bytes

    lines = [f"Top {len(top)} oversized calls:"]
    for i, call in enumerate(top, start=1):
        task = call["task_code"] or "-"
        flags = []
        if call["diff_git_files"]:
            flags.append(f"diff_git_files={call['diff_git_files']}")
        if call["binary_stub_files"]:
            flags.append(f"binary_stub_files={call['binary_stub_files']}")
        if call["skill_md"]:
            flags.append("SKILL.md")
        if call["memory_md"]:
            flags.append("MEMORY.md")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"  {i}. {call['backend']:<12} {format_bytes(call['size_bytes']):>9}  "
            f"task={task}{flag_str}"
        )
    return "\n".join(lines)


def _resolve_quorum_check_context(
    cwd_raw: str, verify_identity: bool
) -> tuple[str | None, list[str] | None, str]:
    """Resolve the (repo_id, diff_files, status) check context for `--check`. See
    `_quorum_check_subcommand`.

    `status` is one of ``"ran"`` (repo_id/diff_files are usable), ``"disabled"``
    (`--no-verify-identity`), or ``"skipped_unresolvable"`` (`-C`/`cwd_raw` isn't
    even a real directory — `_compute_repo_id` otherwise always has a `path:`
    fallback). This is a MACHINE-READABLE version of the same distinction the
    stderr warnings below already make for a human — review finding (Fable) on
    this feature's own PR: the original cut only had the stderr text, so a
    machine caller like `gh ship` had no JSON field to assert "verification
    actually ran" against; it could only infer it from the ABSENCE of the
    diagnostic keys, which is indistinguishable from "verification ran and this
    task genuinely has zero passed iterations". `_quorum_check_subcommand` writes
    `status` into the result as `"identity_verification"`.
    """
    if not verify_identity:
        print(
            "[review task] warning: diff-identity verification disabled "
            "(--no-verify-identity) — every PASSED iteration counts regardless of "
            "recorded repo/diff, exactly as before this gate existed",
            file=sys.stderr,
        )
        return None, None, "disabled"
    cwd = _effective_cwd(cwd_raw, warn=False)
    repo_id = _compute_repo_id(cwd)
    if repo_id is None:
        print(
            f"[review task] warning: could not resolve a repo at -C {cwd_raw!r} — "
            "diff-identity verification skipped, falling back to legacy counting "
            "(every PASSED iteration counts regardless of recorded repo/diff)",
            file=sys.stderr,
        )
        return None, None, "skipped_unresolvable"
    return repo_id, _current_diff_files_for_check(cwd), "ran"


def _quorum_check_subcommand(
    code: str,
    min_iter: int,
    min_models: int,
    *,
    as_json: bool,
    cwd_raw: str = ".",
    verify_identity: bool = True,
) -> int:
    """`review task CODE --check [--min-iter N] [--min-models M] [-C DIR]`.

    Exit 0 iff CODE has >= min_iter PASSED recorded iterations across >= min_models
    distinct models among those passed iterations; exit 1 otherwise, including the
    fail-closed cases (invalid code, unreadable/missing stats store, zero records,
    or a task whose only history predates the verdict field) where reviewlib.stats.
    quorum_check sets an "error" key or simply comes up short. See quorum_check's
    docstring for the "passed, not just dispatched" semantics this check enforces.

    `verify_identity` (default True) resolves `cwd_raw` (`-C`) to a repo id + the
    current diff's touched-file set and hands both to `quorum_check`, so a PASSED
    iteration recorded against a DIFFERENT repo, or a diff sharing no touched file,
    is EXCLUDED from the count rather than silently trusted — see reviewlib.stats
    "Diff-identity binding" for why (closes 3 real quorum-pollution incidents).
    `--no-verify-identity` restores the pre-binding behavior (every passed iteration
    counts, no repo/diff cross-check) — an escape hatch for a cwd that can't resolve
    a repo, not a way around a genuine mismatch finding.
    """
    repo_id, diff_files, verification_status = _resolve_quorum_check_context(
        cwd_raw, verify_identity
    )
    result = quorum_check(
        code,
        min_iter=min_iter,
        min_models=min_models,
        repo_id=repo_id,
        diff_files=diff_files,
    )
    # Machine-readable counterpart to the stderr warnings below: a caller like
    # `gh ship` parsing --json alone (never sees stderr) can assert on this
    # directly instead of inferring "did verification run" from key absence.
    result["identity_verification"] = verification_status
    # This warning goes to stderr in BOTH --json and text mode: --json's structured
    # payload already carries excluded_mismatched_iterations/mismatch_details for a
    # machine reader, but a human watching stderr (e.g. `gh ship`'s own console
    # output) must see the mismatch surfaced too, not just a smaller-than-expected
    # number with no explanation.
    excluded = result.get("excluded_mismatched_iterations", 0)
    if excluded:
        print(
            f"[review task] warning: excluded {excluded} recorded iteration(s) for "
            f"{result['task_code']} — recorded repo/diff did not match the code "
            "currently being checked (--json for detail; --no-verify-identity disables "
            "this check)",
            file=sys.stderr,
        )

    if as_json:
        import json as _json

        print(_json.dumps(result, indent=2))
        return 0 if result["passed"] else 1

    passed_iter = result["passed_iterations"]
    distinct = result["distinct_models_passed"]
    if result["passed"]:
        models = ", ".join(result["models"]) or "-"
        plural_i = "" if passed_iter == 1 else "s"
        plural_m = "" if distinct == 1 else "s"
        print(
            f"review bar met for {result['task_code']}: {passed_iter} passed "
            f"iteration{plural_i} across {distinct} distinct model{plural_m} ({models})"
        )
        return 0

    # review-cli#221 round-4 review finding (k3/Fable): the header and the `stalled:`
    # detail lines must land on the SAME stream — a caller capturing only one of
    # stdout/stderr must never see a bare header with no detail, or bare detail lines
    # with no task-code/NOT-met context. The ratio branch already prints both to
    # stdout; mirror that choice for the error branch's stream too.
    detail_stream = sys.stderr if "error" in result else None
    if "error" in result:
        print(
            f"review bar NOT met for {result['task_code']}: {result['error']}",
            file=sys.stderr,
        )
    else:
        print(
            f"review bar NOT met for {result['task_code']}: "
            f"{passed_iter}/{min_iter} passed iterations, "
            f"{distinct}/{min_models} distinct models"
        )
    # review-cli#221: a bare N/M count (or a bare mismatch-error line) leaves a human
    # no better off than before — name the SPECIFIC attempted model(s) currently
    # cooling down (an unavailable sentinel or a session-limit/usage-credits notice —
    # a plain timeout doesn't record a cooldown, see seat_cooldown.py's docstring) and
    # why, same signal --json already carries in `stalled_models`. Reached on EITHER
    # not-met path (round-3 review
    # finding: an earlier version of this diff returned early on the `error` branch,
    # before ever reaching this loop, even though `stalled_models` can genuinely be
    # populated alongside a mismatch error). Text-mode-only (the --json branch above
    # already returned this data structured) so a plain `review task X --check` run
    # directly by a developer sees it too, not just a `gh ship` caller parsing --json.
    for stalled in result.get("stalled_models", []):
        minutes = max(1, round(stalled["remaining_seconds"] / 60))
        times = stalled["consecutive_failures"]
        plural = "" if times == 1 else "s"
        print(
            f"  stalled: {stalled['model']} ({stalled['reason']}, "
            f"{times} consecutive failure{plural}, ~{minutes}m until retry-eligible)",
            file=detail_stream,
        )
    return 1


def _task_detail(
    code: str, selector: str, iterations: list[dict], sessions: list, *, as_json: bool
) -> int:
    session = None
    iteration_no: int | None = None
    if selector.isdigit():
        iteration_no = int(selector)
        if iteration_no <= 0:
            print("[review task] --detail iteration must be >= 1", file=sys.stderr)
            return 2
        if iterations:
            record = next(
                (item for item in iterations if item.get("iteration") == iteration_no),
                None,
            )
            if record is None:
                print(
                    f"No review iteration {iteration_no} found for task {code}.",
                    file=sys.stderr,
                )
                return 1
            session = _session_for_task_iteration(record, sessions, iterations)
        elif iteration_no - 1 < len(sessions):
            session = sessions[iteration_no - 1]
    else:
        session = next((s for s in sessions if s.session_id == selector), None)
        if session is not None:
            iteration_no = (
                _iteration_for_session(session, iterations)
                or sessions.index(session) + 1
            )
    if session is None:
        print(
            f"No conversation logs found for task {code} detail {selector}.",
            file=sys.stderr,
        )
        return 1
    detail = session.to_detail()
    if as_json:
        import json as _json

        print(_json.dumps(detail, indent=2))
        return 0

    label = f"iteration {iteration_no}" if iteration_no is not None else selector
    print(f"# Task {code} {label}")
    print(f"Session: {session.session_id}")
    print(f"Mode: {session.mode}")
    print(f"Started: {session.started.isoformat()}")
    print(f"Models: {', '.join(session.models) or '-'}")
    if session.brainstorm is not None:
        print("\n## Brainstorm transcript")
        print(session.brainstorm.body.rstrip())
    for idx, call in enumerate(session.calls, start=1):
        status = (
            "error" if call.has_error else ("running" if not call.completed else "ok")
        )
        print(
            f"\n## Call {idx}: {call.backend} r{call.round} [{status}] {call.filename}"
        )
        if call.body:
            print(call.body)
        if call.stderr_lines:
            print("\nstderr:")
            print("\n".join(call.stderr_lines))
    return 0


_SPECWEB_HELP = """review spec-web — multi-spec markdown reviewer (a persistent daemon)

DAEMON LIFECYCLE (shared agenttools_service lib, like `review dashboard`):
  review spec-web start --agent A [--host H] [--port N]   start the daemon in the background
  review spec-web status                        is it running? + registered specs + URLs
  review spec-web stop                          stop the daemon
  review spec-web run --agent A                 run the daemon in the foreground (this shell)
  review spec-web enable --agent A | disable    install / remove OS autostart

SPECS (spec-web-specific):
  review spec-web add <spec.md> [--agent A]     register a spec, print its name-based URL
  review spec-web serve <spec.md> --agent A     add + wait for the review (submit -> stdout)
  review spec-web <spec.md> --agent A            same as `serve` (backward-compatible)
  review spec-web list                          list registered specs
  review spec-web remove <name>                 unregister a spec
  review spec-web watch <name|path>             wait for a submit on an already-registered spec
  review spec-web reply <id> <answer> --spec P  the agent answers a reviewer's note

add/serve options: --seed FILE (import a review thread before serving), --open (open the
browser), --exit-on-submit (serve/watch return after the first submit), --no-watch (add:
register into the daemon and return without waiting).

--agent <name> is the tmux window/session that OWNS the served specs (e.g. --agent ext):
every submitted review batch is INJECTED into that live session (tg-ctl-style tmux
injection), so reviews reach the agent even when nothing is watching stdout. The
daemon-launching actions require it; a spec's own --agent (add/serve) overrides the
daemon default for that spec.

The daemon serves EVERY registered spec by name at /spec/<name> on ONE port (navigator at /),
so one port / Tailscale mapping covers all specs instead of a server per spec."""

# The lifecycle actions handled by the shared agenttools_service ServiceManager.
_SPECWEB_LIFECYCLE = frozenset(
    {"start", "status", "stop", "run", "enable", "disable", "__serve"}
)


def _spec_web(argv: list[str]) -> int:
    """Dispatch ``review spec-web`` — daemon lifecycle + spec registration + the reply/watch glue.

    A bare ``review spec-web`` prints HELP and launches nothing. The daemon lifecycle
    (start/status/stop/run/enable/disable) is delegated to the shared ``agenttools_service`` lib
    (see ``reviewlib.specweb.service`` — same pattern as ``review dashboard``); only the
    spec-web-unique surface (add/serve/list/remove/watch/reply and the legacy positional) lives
    here. See ``reviewlib.specweb`` for the full design.
    """
    if not argv or argv[0] in ("-h", "--help"):
        print(_SPECWEB_HELP)
        return 0
    sub = argv[0]
    if sub == "reply":
        return _spec_web_reply(argv[1:])
    if sub == "__serve":
        return _spec_web_serve_daemon(argv[1:])
    if sub in _SPECWEB_LIFECYCLE:
        return _spec_web_lifecycle(sub, argv[1:])
    if sub == "list":
        return _spec_web_list(argv[1:])
    if sub == "remove":
        return _spec_web_remove(argv[1:])
    if sub == "watch":
        return _spec_web_watch(argv[1:])
    if sub == "add":
        return _spec_web_add(argv[1:], watch=False)
    if sub == "serve":
        return _spec_web_add(argv[1:], watch=True)
    # A legacy positional `review spec-web <spec.md>`: register + serve-in-daemon + watch for the
    # submit (backward-compatible with the old blocking single-spec command).
    return _spec_web_add(argv, watch=True)


# ---- spec-web: shared agenttools_service message + host/port parsing ---------- #
_SPECWEB_MISSING_LIB = (
    "[review spec-web] the daemon lifecycle needs the shared 'agenttools_service' lib, which "
    "isn't installed.\n"
    "  why:  start/status/stop/run/enable/disable come from one shared service-manager "
    "(agent-tools/lib/agenttools_service), not a per-tool copy — same as `review dashboard`.\n"
    "  fix:  pip install -e <agent-tools>/lib/agenttools_daemon -e <agent-tools>/lib/agenttools_service\n"
)


def _spec_web_host_port(rest: list[str], *, defaults: bool = True):
    """Parse the optional ``--host``/``--port`` (+ ``--agent``) shared by the spec-web daemon
    commands.

    Returns ``(ns, remaining_positionals)``. With ``defaults=True`` the daemon defaults
    (0.0.0.0 / the stable port) are applied; the raw values (possibly None) are used otherwise."""
    from .specweb.service import DEFAULT_SPECWEB_HOST, DEFAULT_SPECWEB_PORT

    parser = argparse.ArgumentParser(prog="review spec-web", add_help=False)
    parser.add_argument("--host", default=DEFAULT_SPECWEB_HOST if defaults else None)
    parser.add_argument(
        "--port", type=int, default=DEFAULT_SPECWEB_PORT if defaults else None
    )
    parser.add_argument("--agent", default=None)
    return parser.parse_known_args(rest)


# The daemon-launching actions REQUIRE an owning agent (submitted reviews are DELIVERED into
# that agent's live session — a daemon nobody owns re-creates the "comments reach nobody" bug).
_SPECWEB_AGENT_REQUIRED = frozenset({"start", "run", "enable", "__serve"})


def _spec_web_require_agent(action: str, agent: str | None) -> int | None:
    """Exit-2 usage error when a daemon-LAUNCHING action lacks ``--agent``, else None.

    The agent names the tmux window/session (e.g. ``--agent ext``) that owns the served
    specs: every submitted review batch is injected into that session (see specweb.deliver),
    so an agentless daemon would silently strand reviews in the store again."""
    if agent:
        return None
    print(
        f"[review spec-web] `{action}` requires --agent <name> — the tmux window/session "
        f"that owns the served specs (submitted reviews are delivered INTO that session).\n"
        f"  use:  review spec-web {action} --agent <name>   (e.g. --agent ext)",
        file=sys.stderr,
    )
    return 2


def _spec_web_manager(host: str, port: int | None, agent: str | None = None):
    """Build the agenttools_service ServiceManager for the spec-web daemon, or None if the lib
    is absent (the caller prints the shared missing-lib guidance and returns exit 4)."""
    try:
        from agenttools_service import ServiceManager
    except ImportError:
        return None
    from .specweb.service import spec_web_service

    return ServiceManager(spec_web_service(host=host, port=port, agent=agent))


def _spec_web_serve_daemon(rest: list[str]) -> int:
    """Hidden entry: ``review spec-web __serve`` — the blocking multi-spec daemon (this shell).

    This is the FOREGROUND server the managed service runs (``run``) and detaches (``start``);
    it calls ``run_daemon`` directly so it never re-enters the service dispatcher. Not advertised
    — operators use ``run`` (ad-hoc) or ``start`` (managed)."""
    ns, _ = _spec_web_host_port(rest)
    rc = _spec_web_require_agent("__serve", ns.agent)
    if rc is not None:
        return rc
    verbose = "--verbose" in rest
    from .specweb.server import run_daemon

    return run_daemon(host=ns.host, port=ns.port, verbose=verbose, agent=ns.agent)


def _spec_web_lifecycle(action: str, rest: list[str]) -> int:
    """Run a daemon lifecycle action (start/status/stop/run/enable/disable) via the shared lib.

    ``status``/``start`` additionally print the registered specs + their name-based URLs (the
    spec-web-specific bit), so the operator immediately sees what the daemon is serving.

    ``start`` is IDEMPOTENT: an already-running daemon is reported (pid + what it serves) and
    exits 0 — a repeated ``start`` in a setup script must not read as a failure. (The shared
    ``run_action`` alone would exit 3 there; that convention is for ``status``/``stop``'s
    "was anything up?" branch, not for a start that got what it asked for.)

    The daemon-LAUNCHING actions (start/run/enable) REQUIRE ``--agent`` — see
    ``_spec_web_require_agent``; status/stop/disable manage the existing instance and don't."""
    ns, _ = _spec_web_host_port(rest)
    if action in _SPECWEB_AGENT_REQUIRED:
        rc = _spec_web_require_agent(action, ns.agent)
        if rc is not None:
            return rc
    mgr = _spec_web_manager(ns.host, ns.port, ns.agent)
    if mgr is None:
        print(_SPECWEB_MISSING_LIB, file=sys.stderr)
        return 4
    if action == "start":
        st = mgr.status()
        if st.running:
            suffix = f" (pid {st.pid})" if st.pid is not None else ""
            print(f"[review spec-web] already running{suffix} — nothing to start.")
            _spec_web_print_registry(ns.host, ns.port)
            return 0
    from agenttools_service import run_action

    rc = run_action(mgr, action)
    if action in ("start", "status"):
        _spec_web_print_registry(ns.host, ns.port)
    return rc


def _spec_web_print_registry(host: str, port: int) -> None:
    """Print the navigator URL(s) + every registered spec's name-based URL (loopback + Tailscale)."""
    from .specweb import registry
    from .specweb.server import _reachable_urls, daemon_spec_urls

    for url in _reachable_urls(host, port):
        print(f"[review spec-web] navigator {url}")
    specs = registry.list_specs()
    if not specs:
        print(
            "[review spec-web] no specs registered yet — add one with `review spec-web add <spec.md>`"
        )
        return
    for rec in specs:
        urls = " ".join(daemon_spec_urls(rec["name"], host, port))
        missing = "" if rec["exists"] else "  (file missing)"
        agent = f"  (agent: {rec['agent']})" if rec.get("agent") else ""
        print(f"[review spec-web]   {rec['name']}: {urls}{agent}{missing}")


def _spec_web_add(rest: list[str], *, watch: bool) -> int:
    """``review spec-web add|serve <spec.md>`` (and the legacy positional): ensure the daemon is
    running, register the spec, print its name-based URL. ``serve`` (and the legacy form) then
    BLOCK watching for the submit -> stdout handoff; ``add`` returns immediately.

    Backward-compat: on a host WITHOUT the shared service lib, the legacy positional falls back
    to the classic single-spec foreground server so `review spec-web <spec>` still works."""
    parser = argparse.ArgumentParser(prog="review spec-web", add_help=False)
    parser.add_argument("spec")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--agent", default=None)
    parser.add_argument("--seed", metavar="FILE", default=None)
    parser.add_argument("--open", dest="open_browser", action="store_true")
    parser.add_argument("--exit-on-submit", dest="exit_on_submit", action="store_true")
    parser.add_argument("--no-watch", dest="no_watch", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    ns, _ = parser.parse_known_args(rest)

    # `serve` / the legacy positional own a review round-trip — they REQUIRE the owning agent
    # (submitted batches are DELIVERED into that session; see _spec_web_require_agent). A bare
    # `add` may omit it (the spec then inherits the daemon's own --agent at delivery time).
    if watch:
        rc = _spec_web_require_agent("serve", ns.agent)
        if rc is not None:
            return rc

    spec = Path(ns.spec).expanduser()
    if not spec.is_file():
        print(f"[review spec-web] spec not found: {spec}", file=sys.stderr)
        return 1
    # Canonicalize once, up front: registry + SpecStore key by the RESOLVED path internally,
    # so this keeps every printed path/URL consistent with what the daemon actually serves.
    spec = spec.resolve()

    from .specweb.service import DEFAULT_SPECWEB_HOST, DEFAULT_SPECWEB_PORT

    host = ns.host if ns.host is not None else DEFAULT_SPECWEB_HOST
    # NOT `ns.port or DEFAULT`: `--port 0` is falsy and would silently become the default.
    port = ns.port if ns.port is not None else DEFAULT_SPECWEB_PORT

    mgr = _spec_web_manager(host, port, ns.agent)
    if mgr is None:
        if ns.no_watch or not watch:
            # `add` / `--no-watch` mean "register into the daemon + return fast"; without the
            # service lib there is no daemon to register into — the only lib-less mode is the
            # BLOCKING single-spec server, the opposite of what was asked (and what the
            # backstop classifier assumes). Refuse loudly rather than silently blocking.
            print(_SPECWEB_MISSING_LIB, file=sys.stderr)
            print(
                "[review spec-web] add/--no-watch need the daemon; without the lib the only "
                "mode is the blocking single-spec server (`review spec-web <spec.md>`).",
                file=sys.stderr,
            )
            return 4
        # Lib-less host: preserve the classic single-spec foreground server (full backward-compat).
        print(
            "[review spec-web] shared service lib absent — falling back to a single-spec server.",
            file=sys.stderr,
        )
        return _spec_web_legacy_foreground(spec, ns)

    if ns.seed is not None and _spec_web_seed(spec, ns.seed) != 0:
        return 1

    from .specweb import registry
    from .specweb.server import daemon_spec_urls, watch_submits
    from .specweb.store import SpecStore

    # Baseline the store's last_submit BEFORE the daemon is (maybe) started / the spec is
    # registered: a submit that lands in the ensure-running/register window must count as
    # FRESH for the watch below, not be folded into the baseline and lost (the caller would
    # then wait forever on a review that already happened).
    baseline = SpecStore(spec).last_submit()

    rc = _spec_web_ensure_running(mgr, agent=ns.agent)
    if rc is not None:
        return rc

    name = registry.register(spec, agent=ns.agent)
    urls = daemon_spec_urls(name, host, port)
    print(f"[review spec-web] registered '{name}' -> {spec}")
    for url in urls:
        print(f"[review spec-web] {url}")
    if ns.open_browser and urls:
        _spec_web_open_browser(urls[0])
    if watch and not ns.no_watch:
        print(
            "[review spec-web] waiting for a submit (Ctrl-C to stop; the daemon keeps running)."
        )
        return watch_submits(spec, exit_on_submit=ns.exit_on_submit, baseline=baseline)
    return 0


def _spec_web_ensure_running(mgr, *, agent: str | None = None) -> int | None:
    """Start the daemon if it isn't already up (idempotent); None on success, an exit code on
    refusal. A concurrent start losing the race (AlreadyRunningError) is fine — the daemon is
    up either way. LAUNCHING an agentless daemon is refused (nothing may start without an
    owner — see _spec_web_require_agent); an already-running daemon needs no agent here."""
    st = mgr.status()
    if st.running:
        return None
    rc = _spec_web_require_agent("start", agent)
    if rc is not None:
        print(
            "[review spec-web] the daemon is not running and cannot be auto-started "
            "without --agent.",
            file=sys.stderr,
        )
        return rc
    # AlreadyRunningError lives in agenttools_daemon (the pidfile layer agenttools_service
    # builds on) — it is NOT re-exported by agenttools_service. Imported only on the
    # actually-starting path so a fake manager in tests never needs the lib installed.
    from agenttools_daemon import AlreadyRunningError

    try:
        st = mgr.start()
    except AlreadyRunningError:
        return None
    suffix = f" (pid {st.pid})" if st.pid is not None else ""
    print(f"[review spec-web] daemon started{suffix}.")
    return None


def _spec_web_seed(spec: Path, seed: str) -> int:
    """Import an initial review thread into ``spec``'s store before serving (``--seed``)."""
    import json as _json

    from .specweb.store import SpecStore

    seed_path = Path(seed).expanduser()
    try:
        payload = _json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[review spec-web] cannot read seed {seed_path}: {exc}", file=sys.stderr)
        return 1
    replace = bool(payload.get("replace")) if isinstance(payload, dict) else False
    try:
        result = SpecStore(spec).import_thread(payload, replace=replace)
    except ValueError as exc:
        print(f"[review spec-web] bad seed: {exc}", file=sys.stderr)
        return 1
    print(f"[review spec-web] seeded {result['imported']} comment(s) from {seed_path}")
    return 0


def _spec_web_open_browser(url: str) -> None:
    import threading
    import webbrowser

    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Timer(0.4, _open).start()


def _spec_web_legacy_foreground(spec: Path, ns) -> int:
    """The classic single-spec blocking server (fallback when the shared service lib is absent).

    The caller has already enforced ``--agent`` (the watch/serve path requires it up front),
    so submitted batches are tmux-delivered to the owning session here too — the lib-less
    fallback must not regress into store-only submits."""
    from .specweb.server import run_specweb

    return run_specweb(
        spec,
        host=ns.host or "127.0.0.1",
        port=ns.port,
        open_browser=ns.open_browser,
        seed=ns.seed,
        verbose=ns.verbose,
        exit_on_submit=ns.exit_on_submit,
        agent=ns.agent,
    )


def _spec_web_list(rest: list[str]) -> int:
    """``review spec-web list`` — every registered spec with its open-note count."""
    from .specweb import registry
    from .specweb.store import SpecStore

    specs = registry.list_specs()
    if not specs:
        print(
            "[review spec-web] no specs registered. Add one with `review spec-web add <spec.md>`."
        )
        return 0
    for rec in specs:
        try:
            comments = SpecStore(rec["path"]).all_comments()
        except Exception:  # noqa: BLE001
            comments = []
        openc = sum(1 for c in comments if c.get("status") in ("pending", "submitted"))
        flag = "" if rec["exists"] else "  (file missing)"
        print(
            f"  {rec['name']:<28} {openc} open / {len(comments)} total  {rec['path']}{flag}"
        )
    return 0


def _spec_web_remove(rest: list[str]) -> int:
    """``review spec-web remove <name>`` — unregister a spec (its comments are kept on disk)."""
    if not rest:
        print("[review spec-web] usage: review spec-web remove <name>", file=sys.stderr)
        return 2
    from .specweb import registry

    name = rest[0]
    if registry.unregister(name):
        print(
            f"[review spec-web] removed '{name}' (its comment store is left on disk)."
        )
        return 0
    print(f"[review spec-web] no registered spec named '{name}'.", file=sys.stderr)
    return 1


def _spec_web_watch(rest: list[str]) -> int:
    """``review spec-web watch <name|path> [--emit-current] [--exit-on-submit]`` — block until a
    fresh submit.

    ``--emit-current`` first re-emits the batch ALREADY in the store — the recovery path when a
    submit's live tmux delivery failed (a bare ``watch`` only fires on a LATER submit, so it
    would never re-surface the already-submitted batch)."""
    parser = argparse.ArgumentParser(prog="review spec-web watch", add_help=False)
    parser.add_argument("target", help="a registered spec NAME or a spec PATH")
    parser.add_argument("--exit-on-submit", dest="exit_on_submit", action="store_true")
    parser.add_argument(
        "--emit-current",
        dest="emit_current",
        action="store_true",
        help="re-emit the batch already in the store before watching (recover a failed live delivery)",
    )
    ns, _ = parser.parse_known_args(rest)
    from .specweb import registry
    from .specweb.server import watch_submits

    spec = registry.resolve(ns.target)
    if spec is None:
        spec = Path(ns.target).expanduser()
    if not spec.is_file():
        print(
            f"[review spec-web watch] no such spec (name or path): {ns.target}",
            file=sys.stderr,
        )
        return 1
    print(f"[review spec-web] watching {spec} for a submit (Ctrl-C to stop).")
    return watch_submits(
        spec, exit_on_submit=ns.exit_on_submit, emit_current=ns.emit_current
    )


def _spec_web_reply(argv: list[str]) -> int:
    """`review spec-web reply <comment-id> <answer> --spec <spec.md>`: the AGENT answers a
    reviewer's question/remark. Threads the reply into the store (so the spec-web UI shows
    it under that comment) and best-effort delivers it to the user via the `tg` CLI.

    The spec is required (the store is keyed per spec): pass it as ``--spec``. The reply is
    stamped with the agent author so the UI styles it distinctly.
    """
    parser = argparse.ArgumentParser(
        prog="review spec-web reply",
        description="Answer a reviewer's spec-web question/remark (shown in the UI + sent to tg).",
    )
    parser.add_argument(
        "comment_id",
        help="the id of the comment/question to answer (from the structured review)",
    )
    parser.add_argument("answer", help="the answer text")
    parser.add_argument(
        "--spec",
        required=True,
        metavar="FILE",
        help="path to the spec markdown file (the store is keyed per spec)",
    )
    parser.add_argument(
        "--no-tg",
        action="store_true",
        help="do not deliver the reply to Telegram (UI only)",
    )
    ns = parser.parse_args(argv)

    from .specweb.store import AGENT_AUTHOR, SpecStore

    spec = Path(ns.spec).expanduser()
    if not spec.is_file():
        print(f"[review spec-web reply] spec not found: {spec}", file=sys.stderr)
        return 1
    answer = (ns.answer or "").strip()
    if not answer:
        print("[review spec-web reply] answer is empty", file=sys.stderr)
        return 2

    store = SpecStore(spec)
    rec = store.add_reply(ns.comment_id, body=answer, author=AGENT_AUTHOR)
    if rec is None:
        print(
            f"[review spec-web reply] unknown comment id: {ns.comment_id}",
            file=sys.stderr,
        )
        return 1
    print(
        f"[review spec-web reply] replied to {ns.comment_id} (now {rec.get('status')}); shown in the spec-web UI.",
        flush=True,
    )

    if not ns.no_tg:
        _spec_web_reply_to_tg(spec, rec, answer)
    return 0


def _spec_web_reply_to_tg(spec: Path, comment: dict, answer: str) -> None:
    """Best-effort: deliver the agent's reply to the user via the `tg` CLI on PATH. NEVER
    raises — tg being absent/failing must not fail the reply (it is already in the store /
    UI). Logs the outcome."""
    import shutil
    import subprocess

    exe = shutil.which("tg")
    if not exe:
        print(
            "[review spec-web reply] tg not on PATH — reply saved to the UI only (no Telegram).",
            flush=True,
        )
        return
    question = (comment.get("body") or "").strip()
    quote = (comment.get("quote") or "").strip()
    kind = comment.get("kind") or "remark"

    def _clip(text: str, limit: int) -> str:
        # Bound the reviewer's free text so a long multi-paragraph remark can't blow past
        # Telegram's ~4096-char message limit (the answer is always shown in full).
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    # Plain-text message (no --format html) so we never have to escape the spec/question
    # free text. tg's own --title/--tag give it structure.
    lines = [f"Spec: {spec.name}"]
    if quote:
        lines.append(f"On: “{_clip(quote, 200)}”")
    lines.append(
        f"{'Question' if kind == 'question' else 'Remark'}: {_clip(question, 600)}"
    )
    lines.append(f"Agent answer: {_clip(answer, 3000)}")
    message = "\n".join(lines)
    try:
        proc = subprocess.run(
            [
                exe,
                "--tag",
                "ANSWER",
                "--title",
                f"Spec-web reply — {spec.name}",
                message,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            print("[review spec-web reply] delivered to Telegram via tg.", flush=True)
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            print(
                f"[review spec-web reply] tg delivery failed (exit {proc.returncode}): {err}",
                flush=True,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[review spec-web reply] tg delivery error: {exc}", flush=True)


def _run_mode_with_stats(
    mode: str,
    pool_models: list[str],
    dispatch,
    models_after=None,
    *,
    task_code: str | None = None,
    repo_id: str | None = None,
    diff_files: list[str] | None = None,
    diff_sha256: str | None = None,
) -> int:
    """Announce the ETA, time the run on a monotonic clock, and append a stat record.

    `mode` is the EXACT mode (review/just-ask/quorum/brainstorm) and `pool_models` is
    the list of backends DISPATCHED, used to KEY the up-front ETA (so `pool_size` is
    ground truth — for brainstorm that is the per-round persona slot count, which can
    exceed len(models)), not the dashboard parser's inferred/proxy values. `dispatch` is
    a zero-arg callable that runs the mode and returns its exit code. The per-call ok/fail
    tally is collected via panel.begin/end_call_tally so success/fail counts are real per
    backend call.

    `models_after` (optional) is a zero-arg callable read AFTER the run to get the models
    that ACTUALLY produced verdicts — used by the failover board path, where the final
    pool can differ from the planned one (a skipped/failed seat is backfilled from the
    reserve). When given and non-empty, its list is what lands in the stat record, so the
    recorded `pool_size`/`models` reflect what really ran; the ETA still keys on the
    planned `pool_models` (known up front). Without it, `pool_models` is recorded as-is.

    A run that dispatched ZERO backend calls (a clean-tree review with no diff, an
    early usage error) is NOT recorded: it has no real wall-clock to contribute and a
    ~0s record would drag every future ETA for that pool toward zero — defeating the
    whole point. The ETA line is still printed (it costs nothing and warns the agent),
    but only real runs land in the history. Stats failures NEVER affect the run.

    `repo_id`/`diff_files`/`diff_sha256` are the diff-identity fields (reviewlib.stats
    "Diff-identity binding") threaded straight through to `record_run` unchanged — this
    function doesn't compute or interpret them, it just carries them from the caller
    (which already has `cwd`/`diff` in scope) to the stat record.
    """
    import time

    pool_size = len(pool_models)
    announce_eta(mode, pool_size)
    begin_call_tally()
    started = datetime.now(timezone.utc)
    start = time.monotonic()
    old_task_env = os.environ.get("REVIEW_TASK_CODE")
    if task_code:
        os.environ["REVIEW_TASK_CODE"] = task_code
    rc: int | None = None
    try:
        rc = dispatch()
        return rc
    finally:
        if task_code:
            if old_task_env is None:
                os.environ.pop("REVIEW_TASK_CODE", None)
            else:
                os.environ["REVIEW_TASK_CODE"] = old_task_env
        elapsed = time.monotonic() - start
        tally = end_call_tally()
        ok_count, fail_count = tally["ok"], tally["fail"]
        recorded_models = pool_models
        if models_after is not None:
            try:
                actual = models_after()
            except Exception:  # noqa: BLE001 — stats must never break the run
                actual = None
            if actual:
                recorded_models = actual
        # Only record a run that actually dispatched at least one backend call. No
        # dispatch -> nothing real to time -> skip, so no-op invocations never poison
        # the ETA average.
        if ok_count or fail_count:
            # The mode handler's own exit code IS the run's verdict for every TEXT
            # panel mode (review/quorum/just-ask/brainstorm): each already returns 0
            # iff its own success criterion held (every seat produced a usable
            # verdict / the board didn't degrade) and nonzero otherwise — the same
            # signal the CLI exits the process with. Reuse it rather than inventing
            # a parallel verdict pipeline.
            #
            # KNOWN LIMITATION for `review` (diff review) specifically: this rc is
            # "every reviewer call completed technically" (reviewlib/modes/review.py's
            # `ok = all(r.returncode == 0 for r in results)`), NOT "every reviewer
            # approved the diff with no findings" — review-cli's text-panel reviewers
            # print free-text findings with no structured accept/reject grammar to
            # parse (unlike `qa`'s PASS/FAIL/BLOCKED verdict below), so there is no
            # richer existing signal to key on. This still closes the gap the CTO
            # decision (tg#7306 #1) targeted — a run whose backend calls genuinely
            # failed no longer counts — but a reviewer that ran cleanly and printed
            # "found 3 blocking issues" still counts as passed today. Building a real
            # content verdict for review mode is a separate, larger effort (issue
            # #137 tracks whether/how to close this).
            #
            # `qa` is the one mode where exit code is NOT a verdict: it is
            # deliberately REPORT-ONLY (reviewlib/qa/executor.py:verdict_to_exit_code)
            # — a FAIL verdict with real findings still exits 0 unless --strict, so
            # rc==0 would wrongly read as "passed" for a run that found bugs. Rather
            # than mis-record a qa run as passed, its verdict is UNKNOWN here (not
            # threaded through this generic wrapper) and fails closed — it simply
            # never counts toward the quorum gate until a dedicated fix threads qa's
            # parsed PASS/FAIL/BLOCKED verdict through instead of its exit code.
            #
            # brainstorm's exit 0 on a MID-RUN COLLAPSE (some good rounds, then a
            # dead panel, still synthesized — reviewlib/modes/brainstorm.py's
            # "documented partial-success behavior", CTO 2026-06-16) is intentionally
            # still treated as passed here, unlike qa: it is brainstorm's own,
            # deliberately-chosen definition of "produced a usable result", not an
            # accidental decoupling of exit code from verdict the way qa's is — qa
            # has a real PASS/FAIL/BLOCKED grammar that the exit code ignores by
            # design; brainstorm has no such grammar to ignore. Whether a degraded
            # brainstorm (or any non-`review`-mode iteration at all) SHOULD count
            # toward the self-merge-authority quorum is a separate, broader design
            # question tracked in issue #137 — out of scope for this "ran vs passed"
            # fix, which only had to stop mis-recording a KNOWN-not-passed run as
            # passed.
            #
            # `rc` is None only if `dispatch()` raised before returning; treat that
            # as not-passed (fail-closed) too — a run that never completed is not a
            # pass by any definition.
            if mode == "qa":
                verdict: bool | None = None
            elif rc is None:
                verdict = False
            else:
                verdict = rc == 0
            record_run(
                task_code=task_code,
                mode=mode,
                models=recorded_models,
                duration_seconds=elapsed,
                ok_count=ok_count,
                fail_count=fail_count,
                started=started,
                passed=verdict,
                repo_id=repo_id,
                diff_files=diff_files,
                diff_sha256=diff_sha256,
            )


def _call_with_task_env(task_code: str | None, fn):
    """Run a pre-stats helper while exposing the validated task code to log writers."""
    clean_task = normalize_task_code(task_code)
    if not clean_task:
        return fn()
    old_task_env = os.environ.get("REVIEW_TASK_CODE")
    os.environ["REVIEW_TASK_CODE"] = clean_task
    try:
        return fn()
    finally:
        if old_task_env is None:
            os.environ.pop("REVIEW_TASK_CODE", None)
        else:
            os.environ["REVIEW_TASK_CODE"] = old_task_env


# Subcommands that run a PERSISTENT server until Ctrl-C (`review dashboard`,
# `review spec-web`) — these are intentionally long-lived and must NOT be bounded by
# the run backstop, which would otherwise kill the server after the ceiling (or almost
# immediately under a lowered $REVIEW_BACKSTOP_SECONDS). The backstop is for the
# bounded review/model RUN paths only.
_SERVER_SUBCOMMANDS = frozenset({"dashboard", "spec-web"})


def _is_persistent_server_invocation(argv: list[str]) -> bool:
    """True when argv starts a PERSISTENT server that runs until Ctrl-C and so must bypass the
    `-o` tee + the run backstop. The short-lived management actions are NOT servers — they
    return immediately — so they go through the normal tee/backstop path like any instant
    subcommand:

      * ``spec-web reply …``  — returns immediately, not a server.
      * ``dashboard`` lifecycle actions (``start``/``status``/``stop``/``enable``/``disable``
        and the bare HELP) — return immediately. Only the FOREGROUND blocking server blocks:
        ad-hoc ``dashboard run`` and the hidden ``dashboard __serve`` it dispatches to.
    """
    if not argv or argv[0] not in _SERVER_SUBCOMMANDS:
        return False
    if argv[0] == "spec-web":
        return _spec_web_is_persistent(argv[1:])
    if argv[0] == "dashboard":
        # Only the blocking foreground server is persistent; everything else returns fast.
        # The managed-service parser accepts the global `--host`/`--port` options BEFORE the
        # action (`dashboard --port 7878 run`), so the action is NOT necessarily argv[1] — it
        # is the first NON-OPTION token. Misclassifying `--port N run` as non-persistent would
        # wrap the foreground server in the run backstop and let it be killed. `--host`/`--port`
        # are the only options that take a value here; skip the flag and its argument.
        return _dashboard_action(argv[1:]) in ("run", "__serve")
    return True


# spec-web actions that BLOCK (a persistent server / a submit-watch loop) and so must bypass the
# `-o` tee + run backstop; everything else (lifecycle management, register, list) returns fast.
_SPECWEB_FAST_ACTIONS = frozenset(
    {
        "reply",
        "start",
        "status",
        "stop",
        "enable",
        "disable",
        "add",
        "list",
        "remove",
        "-h",
        "--help",
    }
)


def _spec_web_is_persistent(rest: list[str]) -> bool:
    """True when ``review spec-web <rest>`` blocks (foreground daemon / submit-watch), so it must
    NOT be wrapped in the run backstop. A bare ``spec-web`` (help) and the fast management actions
    return immediately; ``run``/``__serve`` are the blocking daemon; ``watch`` always blocks;
    ``serve`` and the LEGACY positional ``spec-web <path>`` block on the submit-watch unless
    ``--no-watch``."""
    if not rest:
        return False  # bare `review spec-web` prints help + launches nothing
    sub = rest[0]
    if sub in _SPECWEB_FAST_ACTIONS:
        return False
    if sub in ("run", "__serve", "watch"):
        return True  # `watch` has no --no-watch: it exists only to block on the submit
    if sub == "serve":
        return "--no-watch" not in rest
    # A legacy positional `review spec-web <path>` (register + serve-in-daemon + watch, or the
    # lib-less foreground fallback) blocks unless `--no-watch`. `--no-watch` returns fast in
    # BOTH worlds: daemon mode registers + returns; the lib-less fallback REFUSES it (exit 4)
    # rather than silently blocking (see _spec_web_add).
    return "--no-watch" not in rest


def _dashboard_action(rest: list[str]) -> str | None:
    """The dashboard ACTION (run/start/…) from the tokens after ``dashboard``, or ``None``."""
    return _dashboard_action_with_index(rest)[0]


def _dashboard_action_with_index(rest: list[str]) -> tuple[str | None, int]:
    """``(action, index)`` for the dashboard action token, or ``(None, -1)``.

    Skips the global ``--host``/``--port`` options (and their values) that the managed-service
    parser allows before the action, so ``--port 7878 run`` resolves to ``run`` — matching how
    argparse itself parses it (see :func:`_dashboard_subcommand`). The index lets a caller drop
    exactly the action token (not a value that merely equals it)."""
    i = 0
    value_opts = ("--host", "--port")
    while i < len(rest):
        tok = rest[i]
        if tok in value_opts:
            i += 2  # skip the flag and its value
            continue
        if tok.startswith("-"):
            # `--port=7878` or any other valueless flag — skip the single token.
            i += 1
            continue
        return tok, i  # first non-option token == the action
    return None, -1


class _Tee(io.TextIOBase):
    """A write-through tee: every write goes to BOTH a live stream (the real stdout,
    so the user still sees the review as it streams) AND an in-memory buffer that
    `-o FILE` later persists. We mirror stdout rather than redirecting it so `-o`
    NEVER swallows the on-screen output (the task: "still also print to stdout").
    Only `write`/`flush` are exercised by `print()`; the rest delegates to the live
    stream so the object stays a drop-in `sys.stdout`."""

    def __init__(self, live: TextIO, buffer: io.StringIO) -> None:
        self._live = live
        self._buffer = buffer

    def write(self, s: str) -> int:
        self._buffer.write(s)
        return self._live.write(s)

    def flush(self) -> None:
        self._live.flush()

    def isatty(self) -> bool:
        return self._live.isatty()

    @property
    def encoding(self) -> str:
        return getattr(self._live, "encoding", "utf-8")


# Options that CONSUME the next token as their value (space-separated form). When the
# pre-scan for `-o` sees one of these, the FOLLOWING token is that option's value and
# must be passed through untouched — even if it happens to look like `-o`/`--output`
# (e.g. `review --just-ask --output` where `--output` is the question text, or a
# `--prompt -o…`). This keeps the light pre-scan from stealing another flag's value.
_VALUE_TAKING_OPTS = frozenset(
    {
        "-m",
        "--model",
        "-C",
        "--cwd",
        "--task",
        "-o",
        "--output",
        "--prompt",
        "--timeout",
        "--pool",
        "--preset",
        "--moderator",
        "--rounds",
        "--max-rounds",
        "--visual",
        "--before",
        "--intent",
        "--expect",
        "--check",
        "--vision-timeout",
        "--project",
        # `--retry N` is a diff-mode-only int option (reviewlib/modes/review.py). It consumes a
        # value, so the `-o` pre-scan must skip its argument — otherwise `--retry -o…`-shaped input
        # would have the retry count mis-read as the output flag.
        "--retry",
        # `review spec-web reply <id> <answer> --spec <path>`: the value after --spec is a spec
        # path that could look like an option (e.g. `--spec -odd-name.md`); list it so the `-o`
        # pre-scan never steals it.
        "--spec",
        # The qa mode's VALUE-taking flags (modes/qa.py): `--suites <glob/dir/file>`,
        # `--kind <shape>`, `--report <path>`, `--max-cases <N>`, plus the Phase-3 env flags
        # `--stage-url <URL>` and `--config <path>`. Their values can look like an option (a path,
        # `-1`), so the `-o` pre-scan must skip each one's argument. `--in-place` / `--keep-env`
        # are boolean flags (no value) and are deliberately NOT listed.
        "--suites",
        "--kind",
        "--report",
        "--max-cases",
        "--stage-url",
        "--config",
    }
)


def _extract_output_path(argv: list[str]) -> tuple[Path | None, list[str]]:
    """Pull the output flag OUT of argv before dispatch and return (path, remaining).

    Recognized forms: `-o FILE`, `--output FILE`, `--output=FILE`, `-o=FILE`, and the
    glued short `-oFILE`. `-o` is handled OUTSIDE the main argparse surface because the
    capture has to wrap the WHOLE dispatch (every mode prints its final result to
    stdout), and the bare subcommands (install-skill, dashboard, spec-web, …) never
    reach the main parser. A single light pre-scan here makes `-o` work uniformly for
    every path while the parser still advertises it in `--help`.

    Two safeguards keep the pre-scan from misreading another option's value as the
    output flag: (1) scanning STOPS at the first `--` (end-of-options), so a positional
    that starts with `-o` is kept verbatim; (2) a token that is the VALUE of a preceding
    value-taking option (`--just-ask --output`, `--prompt -o…`) is NOT intercepted — it
    is passed through so argparse still receives that option's argument. When the flag
    is absent the remaining list has the SAME contents as the input (a fresh list); a
    bare `-o` with no value is left in the remaining argv so argparse reports the usage
    error instead of a silent swallow."""
    out: Path | None = None
    rest: list[str] = []
    i = 0
    value_for_previous = False
    # `review task CODE --check` is a BOOLEAN flag (no value) — the sole exception to
    # `--check` meaning `--check NAME` (visual module force-activation) everywhere else.
    # Exclude it from the value-taking set for a `task` invocation so this pre-scan
    # doesn't mistake a following `-o FILE` for --check's (nonexistent) value.
    value_taking_opts = (
        _VALUE_TAKING_OPTS - {"--check"}
        if argv and argv[0] == "task"
        else _VALUE_TAKING_OPTS
    )
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # End of options: keep `--` and everything after it untouched.
            rest.extend(argv[i:])
            break
        # If the prior token we recognized as a value-taking option (space form), THIS
        # token is its value — pass it through, never read it as the output flag. Track
        # this as state, not by peeking at argv[i-1], because a *value* can itself equal
        # another value-taking flag string (e.g. `--task --output -o out.md`).
        if value_for_previous:
            rest.append(tok)
            value_for_previous = False
            i += 1
            continue
        if tok in ("-o", "--output"):
            if i + 1 < len(argv):
                out = Path(argv[i + 1]).expanduser()
                i += 2
                continue
            # No value — leave it for argparse to flag (don't silently swallow).
            rest.append(tok)
            i += 1
            continue
        if tok.startswith("--output="):
            out = Path(tok.split("=", 1)[1]).expanduser()
            i += 1
            continue
        if tok.startswith("-o="):
            # `-o=FILE` — accept it (symmetry with `--output=FILE`).
            out = Path(tok.split("=", 1)[1]).expanduser()
            i += 1
            continue
        if tok.startswith("-o") and len(tok) > 2:
            # `-oFILE` (glued short form).
            out = Path(tok[2:]).expanduser()
            i += 1
            continue
        rest.append(tok)
        if tok in value_taking_opts:
            value_for_previous = True
        i += 1
    return out, rest


_LEADING_MODE_VALUE_OPTS = frozenset(
    {
        "-m",
        "--model",
        "-C",
        "--cwd",
        "--task",
        "--timeout",
        "--pool",
        "--preset",
    }
)
_LEADING_MODE_FLAG_OPTS = frozenset({"--list-defaults", "--show-board"})
_LEADING_MODE_INLINE_SHORT_OPTS = ("-m", "-C")


def _is_leading_inline_short_option(tok: str) -> bool:
    """True only for glued short global forms like `-mMODEL`, never long flags."""
    return (
        len(tok) > 2
        and tok.startswith("-")
        and not tok.startswith("--")
        and any(
            tok.startswith(opt) and tok != opt
            for opt in _LEADING_MODE_INLINE_SHORT_OPTS
        )
    )


def _normalize_leading_mode_options(argv: list[str]) -> list[str]:
    """Allow truly-global options before a mode verb.

    The mode parser already accepts `review diff -m fable ...`; users also naturally type
    `review -m fable diff ...` because `-m` is advertised as global. Move only recognized
    global options that appear before a known mode verb to just after that verb, leaving
    unknown/management invocations untouched so argparse still reports the right error.
    """
    if not argv or (not argv[0].startswith("-") and argv[0] in known_subcommands()):
        return argv
    moved: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return argv
        if not tok.startswith("-"):
            if tok in known_subcommands() and moved:
                return [tok, *moved, *argv[i + 1 :]]
            return argv
        if tok in _LEADING_MODE_VALUE_OPTS:
            if i + 1 >= len(argv):
                return argv
            moved.extend([tok, argv[i + 1]])
            i += 2
            continue
        if tok in _LEADING_MODE_FLAG_OPTS:
            moved.append(tok)
            i += 1
            continue
        if any(
            tok.startswith(f"{opt}=")
            for opt in _LEADING_MODE_VALUE_OPTS
            if opt.startswith("--")
        ):
            moved.append(tok)
            i += 1
            continue
        if _is_leading_inline_short_option(tok):
            moved.append(tok)
            i += 1
            continue
        return argv
    return argv


def _write_output_file(path: Path, text: str) -> None:
    """Persist captured stdout to `path` via Python `open(...,"w")` — which bypasses
    the shell entirely, so it NEVER trips zsh `noclobber` the way `review … > FILE`
    does (the bug this flag exists to kill). ANSI escape sequences are stripped so the
    file is clean text even if the live stream was coloured. Parent dirs are created;
    an existing file is overwritten (that is the point). A bad path raises a clear
    OSError that the caller turns into a non-zero exit with an actionable message."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(strip_control_sequences(text), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Entry point: arm the internal run backstop around a review run, then dispatch.

    `review` advertises NO external timeout — agents must not wrap it in a short
    shell `timeout` (the panel/brainstorm modes only emit their synthesis at the very
    end). The ONLY time bound is this INTERNAL last-resort backstop, capped at <=4h
    (`reviewlib.backstop`): a watchdog that force-terminates a genuinely wedged run so
    "no external timeout" can never mean "runs forever". A healthy run finishes in
    minutes, far under the ceiling, and the watchdog is cancelled cleanly on return.

    The persistent SERVER subcommands (`dashboard`, `spec-web`) are deliberately
    long-lived (they run until Ctrl-C), so they bypass the backstop entirely — bounding
    them would kill the server at the ceiling, and a lowered env var would kill it almost
    at once (codex P2). Every other path (the review/panel run and the instant
    subcommands) is wrapped.

    This is also where `-o FILE` is handled: the flag is pre-scanned out of argv (so it
    works for every dispatch path, including the bare subcommands), and when present the
    whole dispatch runs under a stdout TEE whose captured text is persisted to FILE via
    Python — bypassing the shell redirect (and thus zsh noclobber). The file is always
    written; stdout still prints live.
    """
    raw = sys.argv[1:] if argv is None else argv
    output_path, raw = _extract_output_path(list(raw))
    raw = _normalize_leading_mode_options(raw)

    # A REMOVED flag (--mcp/--ln, or a removed mode flag) OR the removed `review review`
    # SUBCOMMAND verb is a USAGE error — it must behave like argparse's own usage errors
    # w.r.t. `-o`: print the structured error and exit WITHOUT writing the `-o` file.
    # Rejecting it INSIDE `_dispatch` only `return`s 2, which the tee path below treats as
    # "the dispatch completed" and would persist the (empty) captured stdout — truncating a
    # pre-existing `-o` target (codex P1/P2). Reject it here, before the tee is armed, so no
    # write happens. Both are pure argv pre-scans; the later calls in `_dispatch` are then
    # harmless no-ops.
    for _reject in (
        _reject_removed_flags,
        _reject_removed_subcommand,
        _reject_subcommand_only_flag_without_verb,
    ):
        rejected = _reject(raw)
        if rejected is not None:
            return rejected

    # The persistent SERVER subcommands stream until Ctrl-C — capturing/teeing their
    # output to a single `-o` file makes no sense (and the file would only be written
    # on shutdown), so `-o` is ignored for them and they bypass both the tee and the
    # backstop exactly as before. `review spec-web reply …` is the EXCEPTION: it is a
    # short-lived command, not the server, so it must NOT bypass — `-o` should work and
    # the backstop should bound it like any other instant subcommand.
    if _is_persistent_server_invocation(raw):
        return _dispatch(raw)

    if output_path is None:
        with run_backstop():
            return _dispatch(raw)

    # `-o FILE`: tee stdout (so the review STILL prints live) and persist the captured
    # text via Python open()/write — which sidesteps zsh `noclobber` (the failure mode
    # this flag fixes). The file is written even on a non-zero exit or empty result (a
    # caller that asked for a file gets one) — but NOT when the dispatch exits EARLY via
    # SystemExit. An argparse usage error or `--help` raises SystemExit before any review
    # ran; writing then would TRUNCATE a pre-existing `-o` target to empty/help-text — a
    # silent data-loss footgun (e.g. `review --bad-flag -o important.md`). So a SystemExit
    # propagates with NO write; the file is touched only when `_dispatch` actually
    # returned (the review path ran).
    captured = io.StringIO()
    real_stdout = sys.stdout
    rc = 1
    completed = False
    try:
        with contextlib.redirect_stdout(_Tee(real_stdout, captured)):
            with run_backstop():
                rc = _dispatch(raw)
                completed = True
    finally:
        # Only persist when the dispatch RAN to a return (completed). On a SystemExit
        # (argparse/--help) or any other propagating exception, skip the write so a
        # pre-existing target is never truncated by an early exit. The write outcome is
        # recorded but NOT returned from `finally` (a `return` there would swallow a
        # propagating exception); the final return below applies it only on a clean run.
        write_error: OSError | None = None
        if completed:
            try:
                _write_output_file(output_path, captured.getvalue())
            except OSError as exc:
                write_error = exc
                print(
                    f"[review-cli] -o: could not write {output_path}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
    return 1 if write_error is not None else rc


def _model_default_help(mode: ModeSpec | None) -> str:
    """The EFFECTIVE `--model` default for THIS parser's mode, for `--help` (ROADMAP: help
    must show ACTUAL defaults — esp `--model`). The default is MODE-AWARE because the
    runtime selection differs per mode (only the diff review runs the board):

      * diff review (mode.name == "review", and the top-level overview where mode is None):
        a `models:` list in config.yaml -> a priority roster for the failover board,
        else the active reviewer BOARD (run `review --show-board`).
      * brainstorm: a `brainstorm_models:` list -> those, else `models:`, else the built-in
        DEFAULT_MODELS — the board does NOT apply here.
      * just-ask / quorum: a `models:` list -> those, else DEFAULT_MODELS.

    Best-effort: any config read failure degrades to the built-in DEFAULT_MODELS phrasing
    (never raises in --help)."""
    try:
        config = load_config()
    except Exception:  # noqa: BLE001 — --help must never crash on a bad config
        config = {}

    def _fmt(models: list[str]) -> str:
        shown = ", ".join(models[:4]) + (", …" if len(models) > 4 else "")
        # This string is interpolated into an argparse `help=`, where `%` is formatting
        # syntax — an un-escaped `%` in a config model id (e.g. `models: ["bad%model"]`)
        # crashes `review --help` with "badly formed help string". Config values are
        # untrusted input here, so escape `%` -> `%%` (codex review). The static phrasing
        # below has no `%`, so escaping only the config-derived fragment is sufficient.
        return shown.replace("%", "%%")

    try:
        config_models = _split_models(config.get("models") or [])
    except Exception:  # noqa: BLE001
        config_models = []
    default_models = [_expand_alias(x) for x in DEFAULT_MODELS]
    default_visual_models = [_expand_alias(x) for x in VISUAL_MODELS]

    # visual: visual_models > VISUAL_MODELS. It deliberately does NOT inherit the text
    # review board or `models:` by default; a text-only board must not accidentally serve
    # screenshot verification.
    if mode is not None and mode.name == "visual":
        try:
            visual = _split_models(config.get("visual_models") or [])
        except Exception:  # noqa: BLE001
            visual = []
        if visual:
            return f"your config.yaml visual_models: {_fmt(visual)}"
        return f"{_fmt(default_visual_models)} (the visual defaults)"

    # brainstorm: brainstorm_models > models > DEFAULT_MODELS (no board).
    if mode is not None and mode.name == "brainstorm":
        try:
            bs = _split_models(config.get("brainstorm_models") or [])
        except Exception:  # noqa: BLE001
            bs = []
        if bs:
            return f"your config.yaml brainstorm_models: {_fmt(bs)}"
        if config_models:
            return f"your config.yaml models: {_fmt(config_models)}"
        return f"{_fmt(default_models)} (the built-in defaults)"

    # qa is SINGLE-SEAT and does NOT use the panel / config `models:` / DEFAULT_MODELS — it
    # selects ONE write/exec tester (claude default, codex via REVIEW_QA_TESTER / `-m codex`).
    # So its `--model` help must NOT advertise the panel defaults (review finding).
    if mode is not None and mode.name == "qa":
        return "claude (the qa tester; use `-m codex` or REVIEW_QA_TESTER=codex for the codex seat)"

    # just-ask / quorum: models > DEFAULT_MODELS (no board).
    if mode is not None and mode.name not in ("review",):
        if config_models:
            return f"your config.yaml models: {_fmt(config_models)}"
        return f"{_fmt(default_models)} (the built-in defaults)"

    # diff review (and the top-level overview, mode is None): models priority roster >
    # config board > active/default preset board.
    if config_models:
        return f"your config.yaml models priority roster: {_fmt(config_models)}"
    if isinstance(config.get("board"), list) and config.get("board"):
        try:
            board_models = [r.model for r in load_board(config)]
        except Exception:  # noqa: BLE001 — --help must never crash on a bad board
            board_models = []
        if board_models:
            return f"your config.yaml board: {_fmt(board_models)}"
    return (
        f"the {DEFAULT_PRESET!r} preset reviewer board "
        "(run `review --show-board`; see `review help config`)"
    )


def _moderator_default_help() -> str:
    """The EFFECTIVE auto-picked `--moderator` default, for `--help`. The moderator is
    chosen from MODERATOR_CANDIDATES (opus -> codex -> gemini) filtered to availability at
    run time; the help names that priority chain so the default is concrete, not vague.

    Escape `%` -> `%%`: this is interpolated into an argparse `help=`, where `%` is
    formatting syntax. The candidates are hardcoded today (no `%`), but escaping keeps the
    same defensive guarantee as `_model_default_help` so a future `%`-bearing candidate id
    can't crash `review --help` (gemini review)."""
    return " -> ".join(MODERATOR_CANDIDATES).replace("%", "%%")


def _add_global_options(
    parser: argparse.ArgumentParser, *, mode: ModeSpec | None
) -> None:
    """Add the TRULY-GLOBAL options — the only ones the top-level `review --help` should
    list (ROADMAP "Subcommand-only options belong in the subcommand help, not the global
    list"): `-m/--model`, `-C/--cwd`, `-o/--output`, `--timeout`, `--list-defaults`,
    `--show-board`, `--pool`. These apply to every path (the meta flags + every mode), so
    they sit on the top-level parser AND every mode parser.

    Configurable options show their EFFECTIVE default value (ROADMAP "Help must show ACTUAL
    defaults"), resolving the config cascade + the mode where relevant (`--model`)."""
    parser.add_argument(
        "-m",
        "--model",
        action="append",
        default=[],
        help=f"model/backend to run; repeat or comma-separate (default: {_model_default_help(mode)})",
    )
    parser.add_argument("-C", "--cwd", default=".", help="repository directory")
    parser.add_argument(
        "--task",
        metavar="CODE",
        default=None,
        help=(
            "task/issue code for this review iteration (required for recorded review "
            "modes; standalone visual without diff is exempt; may also be supplied by "
            "$REVIEW_TASK_CODE)"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        default=None,
        help=(
            "write the result to FILE via Python (creates parent dirs, overwrites) "
            "while still printing to stdout. Use this instead of `review … > FILE`, "
            "which fails under zsh noclobber."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "per-call timeout seconds; REST uses wall/request timeout; review/panel "
            "agent CLIs use idle/silence timeout with a 20m floor for values >=60 "
            "(set REVIEW_IDLE_TIMEOUT_SECONDS to shorten); qa/vision keep wall-clock "
            f"caps (requested defaults: review 1200, panel 240, qa {QA_TIMEOUT_DEFAULT})"
        ),
    )
    parser.add_argument(
        "--list-defaults", action="store_true", help="print default models and exit"
    )
    parser.add_argument(
        "--show-board",
        action="store_true",
        help="print the active reviewer board (model -> role, availability) and exit",
    )
    if mode is None or mode.name == "review":
        parser.add_argument(
            "--preset",
            choices=preset_names(),
            default=None,
            help=(
                "diff-review preset: light = quick/cheap preflight (pool 2, medium effort); "
                "default = routine change review (pool 4, high effort, excludes Fable/Sol); "
                "heavy = release/risky-change review (pool 4, highest effort, includes Fable/Sol). "
                f"If no config board/models are set, review diff uses {DEFAULT_PRESET!r}."
            ),
        )
    parser.add_argument(
        "--pool",
        type=int,
        default=None,
        metavar="N",
        help=(
            "how many of the board's seats to run (default "
            f"{preset_pool_size('default')} for default/heavy, {preset_pool_size('light')} "
            f"for light; {DEFAULT_POOL_SIZE} with no preset); the "
            "first N seats participate, the rest are kept in reserve. The board is "
            "never off — --pool only sizes it. N<=0 means all seats. Ignored for explicit -m."
        ),
    )
    parser.add_argument(
        "--effort",
        action="append",
        default=[],
        metavar="LEVEL|PROVIDER=LEVEL",
        help=(
            "run-scoped reasoning effort, overriding each seat's config effort for THIS run. "
            "A bare level (minimal/low/medium/high/xhigh/max) applies to every seat; "
            "PROVIDER=LEVEL (e.g. codex=high, opencode=max) overrides one backend route. "
            "Repeat or comma-separate; per-provider wins over the global level. "
            "Reaches the codex, claude, opencode, and omp reasoning-effort levers plus the "
            "screenshot vision call."
        ),
    )
    # NOTE: `--retry` is NOT global — it only applies to the diff REVIEW path (the failover
    # board + the flat `-m` panel), not brainstorm/quorum/just-ask (which call run_panel and
    # never use the retry wrapper). It lives on the diff mode's own option surface
    # (modes/review.py `_add_arguments`), so the top-level help isn't padded with a no-op flag
    # (AGENTS.md: the global list is only truly-global options). codex P1 on #46.


def _add_visual_options(
    parser: argparse.ArgumentParser, *, include_visual_flag: bool = True
) -> None:
    """Add the composable `--visual` feature flags as their own argument GROUP. These are
    SUBCOMMAND-scoped, NOT global (ROADMAP): `--visual` rides any subcommand, so they live
    on every MODE parser but must NOT clutter the top-level `review --help`. Grouping them
    makes `review <mode> --help` render them under a clear "visual verification" heading."""
    group_title = (
        "visual verification"
        if not include_visual_flag
        else "visual verification (the composable --visual flag; rides text subcommands)"
    )
    group = parser.add_argument_group(group_title)
    if include_visual_flag:
        group.add_argument(
            "--visual",
            metavar="IMAGE",
            help='image to verify/attach; rides text subcommands (e.g. `review brainstorm "Q" --task CODE --visual IMAGE`; prefer `review visual IMAGE` for standalone)',
        )
    group.add_argument(
        "--before",
        metavar="IMAGE",
        help="baseline image for diff-aware judgement / no-effect bypass",
    )
    group.add_argument(
        "--intent",
        metavar="TEXT",
        help="free-text edit intent (untrusted; may only tighten the contract)",
    )
    group.add_argument(
        "--expect",
        metavar="KIND",
        help="expectation kind: zero-diff|move|resize|style|wrap|insert|delete|text",
    )
    group.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="NAME",
        help="force-activate a visual module by name (repeatable)",
    )
    group.add_argument(
        "--json", action="store_true", help="emit the structured visual verdict as JSON"
    )
    group.add_argument(
        "--strict",
        action="store_true",
        help="exit 10 on a blocking visual verdict (gate use)",
    )
    group.add_argument(
        "--no-ai",
        action="store_true",
        help="run cvGate only (no vision call) — fast CI smoke / offline",
    )
    group.add_argument(
        "--no-local-model",
        action="store_true",
        help="disable the Stage-2a local pre-classifier (known-good cache cost-saver); flow = cvGate → vision (§3.1a)",
    )
    group.add_argument(
        "--vision-timeout",
        type=int,
        default=60,
        help="per vision-call timeout seconds (default 60)",
    )
    group.add_argument(
        "--project",
        default=None,
        help="project root for per-project visual modules (default --cwd)",
    )


def _add_mode_options(parser: argparse.ArgumentParser, *, mode: ModeSpec) -> None:
    """Add the full surface for an EXPLICIT `review <mode> …` subcommand parser: the global
    options, the diff-source flags, the mode-relevant flags (`--prompt` for the diff review,
    `--moderator` for the panel modes), the composable visual group, and the mode's own
    UNIQUE arguments (its positional question/topic, via `add_arguments`).

    `--diff`/`--staged` and `--prompt`/`--moderator` are scoped to the modes that use them
    (ROADMAP): `--prompt` is the diff review's prompt; `--moderator` steers quorum/brainstorm.
    They are harmless if a mode ignores them, but scoping keeps each `review <mode> --help`
    showing only what that mode actually reads."""
    _add_global_options(parser, mode=mode)

    # --diff / --staged select the diff source. They matter to the diff review (its diff is
    # required) and brainstorm/panel grounding; keep them on every mode parser (a mode that
    # ignores one is harmless) but OFF the top-level overview.
    parser.add_argument(
        "--diff",
        action="store_true",
        help="use the working-tree diff (default for the diff review; optional grounding for brainstorm)",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="use the staged diff (git diff --cached) instead of the working-tree diff",
    )

    # --prompt is the DIFF REVIEW's prompt override only.
    if mode.name == "review":
        parser.add_argument(
            "--prompt", default=DEFAULT_PROMPT, help="override the diff-review prompt"
        )
    # --moderator steers the quorum / brainstorm synthesis only.
    if mode.name in ("quorum", "brainstorm"):
        parser.add_argument(
            "--moderator",
            default=None,
            help=f"moderator backend (default: auto-pick, first available of {_moderator_default_help()})",
        )

    # The composable --visual feature rides any subcommand, so its group is on every mode
    # parser (but never the top-level overview). --rounds / --max-rounds are brainstorm-only
    # and added by the brainstorm mode's own add_arguments; they stay in _VALUE_TAKING_OPTS
    # so the mode-agnostic `-o` pre-scan treats them as value-taking.
    _add_visual_options(parser, include_visual_flag=(mode.name != "visual"))

    if mode.add_arguments is not None:
        mode.add_arguments(parser)


# Flags that live ONLY on a subcommand parser (NOT the global top-level parser), per the
# option-scoping. When one of these LEADS a no-subcommand invocation (e.g. the old pre-commit
# `review --staged`, or `review --visual shot.png`), the top-level parser would reject it with
# argparse's opaque "unrecognized arguments", losing the `review diff` migration pointer. The
# pre-parse guard below catches them and emits the friendly pointer instead.
_SUBCOMMAND_ONLY_FLAGS: frozenset[str] = frozenset(
    {
        "--diff",
        "--staged",
        "--prompt",
        "--moderator",
        "--rounds",
        "--max-rounds",
        "--visual",
        "--before",
        "--intent",
        "--expect",
        "--check",
        "--json",
        "--strict",
        "--no-ai",
        "--no-local-model",
        "--vision-timeout",
        "--project",
        "--retry",
        "--commit",
        # The qa mode's own flags (modes/qa.py); a verb-less `review --suites …` / `--kind …`
        # etc. must get the friendly "use the subcommand" pointer, not argparse's opaque error.
        # Phase 3 adds the SUT-env flags `--stage-url` / `--config` / `--keep-env`.
        "--suites",
        "--kind",
        "--in-place",
        "--report",
        "--max-cases",
        "--stage-url",
        "--config",
        "--keep-env",
    }
)

# The BARE management subcommands `_dispatch` handles directly (NOT mode verbs in
# known_subcommands(), and NOT the diff review): they have their OWN flag parsers (e.g.
# `review sessions -s <id> --diff --moderator …`). The verb-less migration guard must leave
# them alone — a `--diff`/`--moderator` after `sessions` belongs to that subparser, not a
# missing `review diff`.
_BARE_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "install-skill",
        "install-commit-hook",
        "install-hook",
        "dashboard",
        "sessions",
        "task",
        "stat",
        "trust-module",
        "register-module",
        "spec-web",
    }
)


def _reject_subcommand_only_flag_without_verb(argv: list[str]) -> int | None:
    """If a SUBCOMMAND-scoped flag appears with NO recognized subcommand, print the friendly
    `review diff` migration pointer + the usage code (2), instead of letting argparse reject
    it with an opaque "unrecognized arguments" that drops the pointer (codex review). Scans
    up to the first `--` (end-of-options), matching the `=value` form too. Returns 2 when such
    a flag leads a verb-less invocation, else None (a purely-global `review -C <repo>` falls
    through to the help pointer; a recognized mode OR bare management subcommand is left
    alone — those own their flags).

    Self-contained (checks the no-subcommand condition itself) so it is safe to call in
    `main()` BEFORE the `-o` tee is armed — a usage error must NOT write/truncate the `-o`
    file (like the removed-flags guards)."""
    if (
        argv
        and not argv[0].startswith("-")
        and (argv[0] in known_subcommands() or argv[0] in _BARE_SUBCOMMANDS)
    ):
        return None  # a recognized subcommand (mode or management) owns these flags
    for tok in argv:
        if tok == "--":
            break
        if tok.split("=", 1)[0] in _SUBCOMMAND_ONLY_FLAGS:
            use_line = (
                "  use:  review visual IMAGE [--diff] [options]\n"
                if tok.split("=", 1)[0] == "--visual"
                else "  use:  review diff --task CODE [options]   "
                "(e.g. `review diff --staged --task CODE`)\n"
            )
            print(
                "review: no subcommand given. The diff review is now `review diff` "
                "(a bare `review` no longer runs one), and that flag belongs to a "
                "subcommand.\n"
                f"{use_line}"
                "  (run `review --help` for all subcommands)",
                file=sys.stderr,
                flush=True,
            )
            return 2
    return None


def _help_topic_config() -> str:
    """The `review help config` deep reference (ROADMAP "Topic-based help across the
    ecosystem"): config file path + cascade, the keys, key/auth env vars, and how the
    reviewer board + model selection resolve. Kept in sync with config.py / backends.py
    behavior (help-docs-sync); a flag/behavior change updates this topic in the same commit."""
    return f"""\
review — configuration reference
================================

CONFIG FILE
  {CONFIG_PATH}
  Optional YAML. Absent / unparseable -> the built-in defaults apply (never an error).
  Keys:
    models:            list[str]  priority roster for `review diff`: the live pool is
                       selected from this full ordered set and the rest are reserve.
                       For just-ask/quorum it remains the flat default panel.
    brainstorm_models: list[str]  default panel for `review brainstorm` (falls back to
                       `models:`, then the built-in defaults). Unreachable backends are
                       dropped gracefully.
    visual_models:     list[str]  ordered vision backends for `review visual` and companion
                       visual fan-out. Separate from the text reviewer board; runtime
                       failures skip to the next vision backend.
    board:             list[seat]  override the built-in reviewer board (see BOARD below).
    unpaid_providers:  list[str]   providers whose billing/subscription is currently
                       unavailable; every direct or oc: seat under them is skipped before
                       any backend process/API call. Env equivalent:
                       REVIEW_UNPAID_PROVIDERS=commandcode,fireworks.
    timeout:           int         per-call timeout seconds for `review diff` (replaces the
                       configured/default request timeout; still overrideable
                       per-invocation by --timeout). Review/panel agent CLI backends treat
                       this as an idle/silence timeout with a 20m default floor for normal
                       review runs; REST backends use it as their HTTP request timeout.
                       `review qa` uses its timeout as a
                       wall-clock QA cost cap.
  Model ids accept the friendly aliases (e.g. `fable5` -> claude:claude-fable-5,
  `glm` -> zai:glm-5.2, `cc` -> commandcode).

SELECTION CASCADE, by mode:
  review diff           : explicit -m requested models  >  explicit --preset  >
                          config `models:` priority roster  >  config `board:`  >
                          default preset.
                          With configured `models:`/`board:` metadata, -m narrows that
                          board metadata to only the requested models; without config it is
                          the legacy flat exact panel unless an explicit preset supplies
                          metadata/effort for the requested models.
  review visual         : explicit -m  >  `visual_models:`  >  visual defaults.
  review brainstorm     : explicit -m  >  `brainstorm_models:`  >  `models:`  >  defaults.
  review just-ask/quorum: explicit -m  >  `models:`  >  the built-in defaults.
  review qa             : IGNORES config `models:` / `brainstorm_models:` / the defaults.
                          It picks ONE write/exec tester: `REVIEW_QA_TESTER=claude|codex`  >
                          a bare `-m claude|codex`  >  the `claude` default. (opencode is not
                          in v1.) The panel/board cascade does not apply to qa.
  Built-in defaults: {", ".join(_expand_alias(x) for x in DEFAULT_MODELS)}.
  Visual defaults:  {", ".join(_expand_alias(x) for x in VISUAL_MODELS)}.
  See the live default for each subcommand in `review <mode> --help` (the --model line).

REVIEWER BOARD + PRESETS (the diff-review default; `review --show-board` prints it live)
  A priority-ordered panel of seats, each model carrying its own
  role/lens. A plain `review diff` runs the `{DEFAULT_PRESET}` preset: pool 4, high effort,
  without Fable/Sol. Use `--preset light` for quick preflight (pool 2, medium effort) and
  `--preset heavy` for release/risky changes (Fable/Sol/Opus/GLM-cc at highest effort).
  `--pool N` sizes the selected board (`--pool 0` = all available). Explicit -m never lets config add extra seats; it narrows
  configured metadata when present, else uses the flat exact panel. To set the priority
  roster, configure `models:`. To add role/name/effort metadata (or a full board when
  `models:` is absent), set `board:` in config.yaml (a list of {{model, role, name,
  effort}} entries; `name` is the display label, `role` is a key into the built-in role
  lenses, and `effort` is one of minimal/low/medium/high/xhigh/max) or edit DEFAULT_BOARD in
  reviewlib/config.py.

KEYS / AUTH (resolved from the process env first, then the shared .env)
  Shared .env:  ~/.config/review-cli/.env   (override with $GEMINI_ENV_FILE)
  Env vars:
    GEMINI_API_KEY (or GOOGLE_API_KEY)  — gemini seat.
    ZAI_API_KEY    (or ZHIPU_API_KEY)   — z.ai / GLM seat.
    COMMANDCODE_API_KEY                 — commandcode seats (a `user_...` token ONLY).
    ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN (+ optional ANTHROPIC_BASE_URL) — claude API.
    REVIEW_CLAUDE_MODE=api|cli, REVIEW_<BACKEND>_MODE=api|cli — transport selection.
    REVIEW_UNPAID_PROVIDERS=provider[,provider...] — skip disabled/unpaid providers early.
    REVIEW_IDLE_TIMEOUT_SECONDS=N       — override the review/panel subprocess idle window;
                                          0 disables idle reap and uses wall-clock --timeout.
    --timeout applies to every backend seat. For review/panel subprocess agent CLIs, backend
      stdout/stderr resets the idle timer; a fully silent seat is reaped after the idle
      window. Values under 60s stay exact for tests/probes; otherwise the normal 20m floor
      applies unless REVIEW_IDLE_TIMEOUT_SECONDS is set. QA and vision calls keep wall-clock
      timeout caps.
    REVIEW_DIFF_MAX_BYTES=N             — dispatch-time diff-size cap (default 300000);
                                          <= 0 disables it. See `review stat`'s section
                                          in README.md.
    REVIEW_SEAT_COOLDOWN_SECONDS=N      — cross-invocation cooldown window for a
                                          chronically-unavailable claude seat (Fable;
                                          NOT wired into opencode/commandcode backends
                                          yet — see review-cli#226). Unset:
                                          the window ESCALATES per consecutive failure
                                          (10min, 30min, 2h, then 8h cap), resetting to
                                          10min after a success or a 24h quiet period.
                                          Set: that fixed window every time, no
                                          escalation; <= 0 disables cooldowns entirely.
    REVIEW_SEAT_COOLDOWN_FILE=PATH      — override the cooldown store location (default
                                          ~/.config/review-cli/seat-cooldown.json).
    REVIEW_TRIVIAL_DELTA_LINES=N        — pre-commit gate: max changed lines tolerated
                                          against the last reviewed baseline before a
                                          restage forces a fresh full review (default 10);
                                          0 disables the tolerance (exact-hash match only).
  codex / opencode / omp carry their own CLI auth (no key here).

See also: `review --help` (overview), `review --show-board`, `review <mode> --help`.
"""


# Deep help TOPICS advertised from the main `review --help` and served by `review help
# <topic>` / `review --help <topic>` (ROADMAP "Topic-based help across the ecosystem").
# topic -> (one-line summary for the main-help listing, zero-arg renderer). Add a topic =
# add an entry here; the main-help pointer + dispatch pick it up with no other edit.
HELP_TOPICS: dict[str, tuple[str, "object"]] = {
    "config": (
        "config file, the model/board selection cascade, keys/auth",
        _help_topic_config,
    ),
}


def _subcommand_epilog() -> str:
    topics = "\n".join(
        f"  review help {t:<8} {summary}" for t, (summary, _) in HELP_TOPICS.items()
    )
    return (
        "subcommands:\n"
        + "\n".join(f"  {m.subcommand:<11} {m.summary}" for m in iter_modes())
        + (
            "\n  dashboard   managed web dashboard over review-cli runs (run/start/status/stop/enable/disable)"
            "\n  sessions    list / resume brainstorm sessions (-a all, -s <id> resume)"
            "\n  task        show review iterations and transcripts for one task code"
            "\n  stat        per-harness/per-model usage + health report from the real call logs"
            "\n  spec-web    multi-spec web reviewer daemon (start/status/stop/add <spec>; also `spec-web <spec>`)"
            "\n  install-skill / install-commit-hook / install-hook tg / register-module"
            "\n\nhelp topics (deep help — `review help <topic>` or `review --help <topic>`):\n"
            + topics
            + "\n  see `review help config` for configuration."
        )
    )


def _build_top_level_parser() -> argparse.ArgumentParser:
    """Build the TOP-LEVEL `review` parser — the overview shown by a bare `review` and by
    `review --help`. It advertises the SUBCOMMAND list (the diff review is `review diff`
    now; a bare `review` no longer runs a diff review — it prints this help) and carries
    only the TRULY GLOBAL options + the board/meta flags (`--list-defaults` / `--show-board`
    / `--pool`). Mode/visual-only flags live on their own subparsers (scoped help)."""
    parser = argparse.ArgumentParser(
        prog="review",
        description=(
            "Run read-only code reviews / AI panels across multiple model backends "
            "(one narrow opt-in exception: `review diff --staged --commit` checkpoints "
            "a commit). Everything is a SUBCOMMAND: `review diff` (review the git diff), "
            "`review brainstorm`, `review just-ask`, `review quorum`. A bare `review` "
            "(no subcommand) prints this help — it does NOT run a diff review; use "
            "`review diff` for that."
        ),
        epilog=_subcommand_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Only the truly-global options — NOT the mode/visual-only flags (ROADMAP: those belong
    # in the subcommand help, not the global list). The top-level parser only ever serves the
    # meta flags (--list-defaults / --show-board / --help) + the help fall-through; it never
    # dispatches a review, so it does not need --prompt / --diff / --moderator / the visual
    # group.
    _add_global_options(parser, mode=None)
    return parser


def _build_mode_parser(mode: ModeSpec) -> argparse.ArgumentParser:
    """Build the argparse surface for an EXPLICIT `review <mode> …` subcommand (its prog
    is `review <subcommand>` and it carries the global options + the mode-relevant flags +
    the composable visual group + the mode's own positional)."""
    parser = argparse.ArgumentParser(
        prog=f"review {mode.subcommand}",
        description=mode.summary,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_mode_options(parser, mode=mode)
    return parser


def _reject_removed_flags(argv: list[str]) -> int | None:
    """Reject flags this redesign REMOVED with a clear, actionable error instead of letting
    them silently mis-parse (mode flags) or hit argparse's opaque `unrecognized arguments`
    (the no-replacement flags). Two classes:

      * REMOVED_MODE_FLAGS (`--brainstorm`/`--quorum`/`--just-ask`) → "use the subcommand";
      * REMOVED_FLAGS (`--mcp`/`--ln`) → a 3-part what/why/how-to-fix error (the `--mcp`
        case is the dead review-MCP entrypoint a stale `~/.claude/mcp/mcp.json` still spawns;
        the error tells the user to drop that registration — see structured-exit-codes).

    Returns the stable usage exit code (2) when a removed flag is present, else None. Scans
    only up to the first `--` (end-of-options), so the same string appearing as a positional
    value (e.g. a quote that literally contains '--quorum') is untouched."""
    for tok in argv:
        if tok == "--":
            break
        # `--brainstorm=foo` / `--mcp=foo` form too.
        bare = tok.split("=", 1)[0]
        sub = REMOVED_MODE_FLAGS.get(bare)
        if sub is not None:
            task_hint = (
                " --task CODE" if sub in {"brainstorm", "just-ask", "quorum"} else ""
            )
            print(
                f"review: `{bare}` is no longer a flag — it is now the `{sub}` subcommand.\n"
                f'  use:  review {sub} "<your text>"{task_hint} [options]\n'
                f"  (modes are subcommands now: brainstorm / just-ask / quorum; "
                f"run `review --help`)",
                file=sys.stderr,
                flush=True,
            )
            return 2
        removed = REMOVED_FLAGS.get(bare)
        if removed is not None:
            print(
                f"review: `{bare}` was removed and is no longer accepted.\n"
                f"  why:  {removed.reason}\n"
                f"  fix:  {removed.fix}",
                file=sys.stderr,
                flush=True,
            )
            return 2
    return None


def _reject_removed_subcommand(argv: list[str]) -> int | None:
    """Reject the renamed-away SUBCOMMAND verb `review review` (the diff review is `review
    diff` now) with a one-line `review diff` pointer + the stable usage code (2), else None.
    A pure argv check so it can run in `main()` BEFORE the `-o` tee is armed — a usage error
    must NOT write/truncate the `-o` file (codex P1), exactly like the removed FLAGS. It scans
    past leading global options because `review -m fable review` should get the same helpful
    pointer as `review review`. The later call in `_dispatch` is then a harmless no-op."""
    removed = _leading_removed_subcommand(argv)
    if removed:
        replacement = REMOVED_SUBCOMMANDS[removed]
        print(
            f"review: `review {removed}` is no longer a subcommand — the diff review is "
            f"now `review {replacement}`.\n"
            f"  use:  review {replacement} --task CODE [options]\n"
            f"  (run `review --help` for all subcommands)",
            file=sys.stderr,
            flush=True,
        )
        return 2
    return None


def _leading_removed_subcommand(argv: list[str]) -> str | None:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return None
        if tok in REMOVED_SUBCOMMANDS:
            return tok
        if tok in _LEADING_MODE_VALUE_OPTS:
            i += 2
            continue
        if any(
            tok.startswith(f"{opt}=")
            for opt in _LEADING_MODE_VALUE_OPTS
            if opt.startswith("--")
        ):
            i += 1
            continue
        if tok in _LEADING_MODE_FLAG_OPTS:
            i += 1
            continue
        if _is_leading_inline_short_option(tok):
            i += 1
            continue
        return None
    return None


def _help_subcommand(rest: list[str]) -> int:
    """`review help [<topic>]` — deep topic help (ROADMAP "Topic-based help across the
    ecosystem"). Bare `review help` lists the topics; `review help <topic>` prints that
    topic. An unknown topic lists the available ones + exits 2 (usage). Also reached as
    `review --help <topic>` (see the main dispatch). Topics live in HELP_TOPICS so adding a
    topic needs no edit here.

    A USAGE error here `raise`s SystemExit(2), it does NOT `return` — like argparse's own
    usage errors and the bare-`review` help path below. `main()`'s `-o` tee only persists the
    captured output on a `return` (a "the dispatch ran" signal); a `return 2` here would let
    that tee TRUNCATE a pre-existing `-o` target to empty on `review help bogus -o file.md`
    (premium merge-gate finding, same data-loss class as #37). SystemExit propagates through
    the tee's `finally` with `completed=False`, so no write happens. The SUCCESS paths (the
    topic listing / a valid topic) still `return 0`: that output is real and SHOULD be teed."""
    if not rest:
        print(
            "review help topics:\n"
            + "\n".join(
                f"  {t:<10} {summary}" for t, (summary, _) in HELP_TOPICS.items()
            )
            + "\n\nRun `review help <topic>` (or `review --help <topic>`) for the full reference."
        )
        return 0
    # Exactly one topic token — extra trailing args are a usage error, not silently dropped
    # (codex review: `review help config --bogus` / `review --help config extra` must NOT
    # exit 0 ignoring the tail).
    if len(rest) > 1:
        print(
            f"review help: takes a single topic, got extra arguments: {' '.join(rest[1:])}\n"
            f"  use:  review help <topic>   (topics: {', '.join(HELP_TOPICS)})",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    topic = rest[0]
    entry = HELP_TOPICS.get(topic)
    if entry is None:
        known = ", ".join(HELP_TOPICS)
        print(
            f"review help: unknown topic '{topic}'. Known topics: {known}.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(2)
    _summary, render = entry
    text = render()
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def _config_default_pool(config: dict) -> int | None:
    """A `pool:` default from config.yaml (a positive int), or None when absent/invalid.

    Lets a personal config pin the default review pool size (e.g. 3 while a provider is
    disabled). A non-positive / non-integer value is ignored (falls back to the preset
    default) rather than erroring — `pool: 0`/`--pool 0` "all seats" stays a CLI-only knob."""
    raw = config.get("pool")
    if isinstance(
        raw, bool
    ):  # bool is an int subclass; a stray `pool: true` is not a size
        return None
    if isinstance(raw, int) and raw > 0:
        return raw
    return None


def _seats_of(board: list) -> tuple[tuple[str, str], ...]:
    """Board seats as (model, display-name) pairs for the pool-selection guard."""
    return tuple((r.model, r.display) for r in board)


def _default_review_board(
    config: dict, config_models: list[str], config_has_board: bool, default_pool: int
):
    """The board a no-`-m`/no-`--pool` run would use, with its default pool size — the
    precedence the guard offers as the 'drop your override' fallback (config `models:` >
    config `board:` > the default preset). `default_pool` is the pool size a no-override run
    would ACTUALLY use (config `pool:` > preset default), so a proposal's promised size
    matches what re-running would do."""
    if config_models:
        return board_from_models(config_models, config), default_pool
    if config_has_board:
        return load_board(config), default_pool
    return load_board(config, preset=DEFAULT_PRESET), default_pool


def _pool_guard_candidates(
    config: dict,
    config_models: list[str],
    config_has_board: bool,
    default_pool: int,
) -> list[Candidate]:
    """Fallback options the guard proposes when a selection can't converge: the default
    board plus every preset. Boards are built (cheap, no availability probe — the guard
    probes lazily only on the non-converge path); a malformed board is skipped, not fatal.
    A preset identical to the default board is de-duped so the proposal isn't redundant."""
    candidates: list[Candidate] = []
    default_seats: tuple[tuple[str, str], ...] | None = None
    try:
        board, pool = _default_review_board(
            config, config_models, config_has_board, default_pool
        )
        default_seats = _seats_of(board)
        candidates.append(
            Candidate(
                label="default",
                why="drop -m / --pool and run the default board (`review diff`)",
                board=default_seats,
                pool_size=pool,
            )
        )
    except BoardConfigError:
        pass
    for name in preset_names():
        try:
            seats = _seats_of(load_board(config, preset=name))
        except BoardConfigError:
            continue
        if seats == default_seats:
            continue
        candidates.append(
            Candidate(
                label=f"preset:{name}",
                why=f"use the {name!r} preset (`review diff --preset {name}`)",
                board=seats,
                pool_size=preset_pool_size(name),
            )
        )
    return candidates


def _warn_if_panel_padded(models: list[str]) -> None:
    """Operator-facing stderr notice for the flat quorum panel, fired iff
    `models` contains a repeat -- the only way a repeat can appear here is
    `expand_flat_models_with_reuse` reusing a model, since its input `src`
    (DEFAULT_MODELS or config `models:`) is already distinct and `-m` never
    reaches this call site (`explicit_models` is handled by an earlier,
    separate branch that bypasses padding entirely). Mirrors the board path's
    existing failover promotion message (reviewlib.panel.run_board_with_failover)
    so the token spend is attributable instead of a silent surprise."""
    if len(set(models)) == len(models):
        return
    counts = {m: models.count(m) for m in dict.fromkeys(models)}
    repeated = ", ".join(f"{m} x{n}" for m, n in counts.items() if n > 1)
    print(
        f"[review-cli] panel padded — reusing {repeated} across multiple seats "
        "(some models near their usage limit)",
        file=sys.stderr,
        flush=True,
    )


def _chain_aware_available(model: str) -> bool:
    """The failover-chain-aware liveness probe: True iff `model` OR any later provider in
    its `reviewlib.provider_failover` chain is reachable and paid.

    This is THE single liveness predicate every pre-dispatch decision about a seat must
    share — the pool guard, the startup pool/reserve SPLIT (`split_pool_reserve`, both here
    for the ETA and in `modes.review._mode_review_board` for the REAL dispatch), and the
    ETA's `planned_pool`. Before this fix the guard alone became chain-aware while the split
    stayed on raw `backend_available`, so the guard could approve a pool size the split then
    silently shrank (a two-seat board where one seat's head is down but its failover
    alternate is live: the guard says 'fine, 2 live', the split — still raw — says '1 live',
    and dispatch quietly runs a smaller pool than what was approved). Codex P1 on review of
    #157: 'the chain-aware guard approves seats that board dispatch still removes.'"""
    from .provider_failover import any_provider_available

    return any_provider_available(
        model,
        available=backends.backend_available,
        unpaid=backends.runtime_provider_marked_unpaid,
    )


def _evaluate_pool_or_bail(
    config: dict,
    config_models: list[str],
    config_has_board: bool,
    user_seats: tuple[tuple[str, str], ...],
    explicit_models: list[str],
    pool_arg: int | None,
    default_pool: int,
) -> int | None:
    """Pre-dispatch foolproofing (reviewlib.pool_guard): when the resolved review selection
    (`user_seats` = (model, name) pairs — a board OR the flat `-m` list) can't converge,
    print a proposal / targeted error and return its exit code; otherwise return None to
    proceed. Inert under the fake backend (every seat live -> PROCEED)."""
    # A config `pool: N` default is a SOFT target (graceful auto-shrink), NOT a hard request:
    # only an EXPLICIT `-m` / `--pool N` sets `explicit=True` so the guard enforces the
    # requested size and proposes when the live subset can't fill it. A bare `review diff`
    # with a config `pool:` still runs the live board (down to the min_converge floor) rather
    # than nagging — an explicit flag is a deliberate ask; a config default is a preference.
    if explicit_models:
        requested, explicit = (
            len({default_distinct_key(m) for m in explicit_models}),
            True,
        )
    elif pool_arg is not None and pool_arg > 0:
        requested, explicit = pool_arg, True
    else:
        requested, explicit = 0, False

    def _guard_reason(model: str) -> str | None:
        # `backend_unavailable_reason` probes only the requested (head) spelling, and — by
        # construction — already agrees with `_chain_aware_available` whenever the head
        # itself is what's down (it embeds the same `runtime_provider_marked_unpaid` check
        # `any_provider_available` uses). The one gap: if it ever returned None while
        # `_chain_aware_available` says the WHOLE chain is down (every provider
        # unavailable/unpaid, a shape only reachable with mismatched injected predicates
        # today, e.g. in a test), the guard would bail with a blank reason. Fail safe with a
        # synthesized one rather than ship a misleading empty message (review of #157).
        reason = backends.backend_unavailable_reason(model)
        if reason is not None or _chain_aware_available(model):
            return reason
        return f"{model}: no provider in its failover chain is currently available"

    decision = evaluate_selection(
        user_board=user_seats,
        requested_size=requested,
        explicit=explicit,
        # THUNK: the fallback boards (default + presets) are re-resolved only on the
        # non-converge path, so a converging happy-path run never rebuilds them (and never
        # re-emits load_board's stderr warnings for a malformed config board — glm review).
        candidates=lambda: _pool_guard_candidates(
            config, config_models, config_has_board, default_pool
        ),
        available=_chain_aware_available,
        reason=_guard_reason,
    )
    if decision.kind == PROCEED:
        return None
    print(decision.text, file=sys.stderr, flush=True)
    return decision.exit_code


def _dispatch(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _normalize_leading_mode_options(list(argv))
    # `review help [<topic>]` — deep topic help. Wired here (a bare management subcommand
    # like `dashboard`), before mode resolution, so it stays off the main argparse surface.
    if argv and argv[0] == "help":
        return _help_subcommand(argv[1:])
    # `review --help <topic>` is an alias for `review help <topic>` (ROADMAP advertises
    # both spellings). Route ANY non-option token after `--help`/`-h` through the topic
    # handler — so a known topic prints, and a TYPO (`review --help confg`) gets the same
    # unknown-topic usage error (exit 2) as `review help confg`, not argparse's normal help
    # (exit 0) (codex review). A bare `--help` (no token) falls through to argparse's help.
    if len(argv) >= 2 and argv[0] in ("--help", "-h") and not argv[1].startswith("-"):
        # Pass ALL tokens after --help (not just argv[1]) so extra trailing args are caught
        # by _help_subcommand's usage check rather than silently dropped (codex review).
        return _help_subcommand(argv[1:])
    if argv == ["install-skill"]:
        return install_skill()
    if argv == ["install-commit-hook"]:
        return install_commit_hook()
    if argv and argv[0] == "install-hook":
        if argv[1:] == ["tg"]:
            return install_hook_tg()
        print("usage: review install-hook tg", file=sys.stderr)
        return 2
    # `review --reviewlib-dir` — print the directory of the `reviewlib` package this `review`
    # actually runs, then exit. Used by the managed-dashboard service to detect the live-symlink
    # trap (the `review` on PATH resolving to a DIFFERENT checkout than the one wiring the
    # service), so autostart never launches the wrong code. Deliberately introspection-only.
    if argv == ["--reviewlib-dir"]:
        import reviewlib

        print(Path(reviewlib.__file__).resolve().parent)
        return 0
    # `review dashboard [--port N] [--no-open]` — local-only web dashboard over the
    # review-cli logs + overseer annotations. Kept as a bare subcommand (like
    # install-skill) so it doesn't bloat the main review argparse surface.
    if argv and argv[0] == "dashboard":
        return _dashboard_subcommand(argv[1:])
    # `review sessions [-a] [-s <id>]` — list / resume brainstorm sessions parsed from
    # the discussion logs. A bare MANAGEMENT subcommand (like dashboard), NOT a fan-out
    # mode, so it is wired here and stays off the main review argparse surface.
    if argv and argv[0] == "sessions":
        return _sessions_subcommand(argv[1:])
    # `review task CODE` — task-scoped run-stat iterations + log transcripts.
    if argv and argv[0] == "task":
        return _task_subcommand(argv[1:])
    # `review stat` — detailed per-harness/per-model token-burn + health report parsed
    # from the real on-disk call logs. A bare MANAGEMENT subcommand (like task/dashboard/
    # sessions), NOT a fan-out mode.
    if argv and argv[0] == "stat":
        return _stat_subcommand(argv[1:])
    # Per-project visual-module subcommands (§6). Kept as bare subcommands (like
    # install-skill) so they don't clutter the main review argparse surface. Project
    # modules load by default (trust-by-default); trust-module only pins under the
    # opt-in REVIEW_UNTRUSTED_MODULES=1 guard (the rare untrusted-repo case).
    if argv and argv[0] == "trust-module":
        from .features.visual.registry import trust_module

        if len(argv) < 2:
            print(
                "usage: review trust-module <name> [--project DIR]  (only needed under REVIEW_UNTRUSTED_MODULES=1)",
                file=sys.stderr,
            )
            return 2
        proj = None
        rest = argv[2:]
        if "--project" in rest:
            i = rest.index("--project")
            proj = Path(rest[i + 1]).expanduser() if i + 1 < len(rest) else None
        return trust_module(argv[1], project=proj)
    if argv and argv[0] == "register-module":
        from .features.visual.registry import register_module

        if len(argv) < 2:
            print("usage: review register-module <path-to-manifest>", file=sys.stderr)
            return 2
        return register_module(argv[1])
    # `review spec-web <spec.md>` — interactive web reviewer for ANY markdown spec.
    # Kept as a bare subcommand (like install-skill / register-module) so it stays off
    # the main review argparse surface; it has its own small flag parser.
    if argv and argv[0] == "spec-web":
        return _spec_web(argv[1:])

    # The removed mode flags (--brainstorm/--quorum/--just-ask) are now subcommands —
    # reject them with a helpful pointer rather than mis-parsing the value (§2).
    rc = _reject_removed_flags(argv)
    if rc is not None:
        return rc

    # The removed SUBCOMMAND verb `review review` (the old stuttering diff review) prints a
    # one-line "use `review diff`" pointer and exits with the usage code — like the removed
    # mode flags. Done BEFORE the help fall-through so a stale `review review …` is
    # diagnosed, not silently turned into a help dump. (Pre-rejected in `main()` before the
    # `-o` tee, so this is a no-op when reached via `main`; it still fires for a direct
    # `_dispatch` call, e.g. in tests.)
    rc = _reject_removed_subcommand(argv)
    if rc is not None:
        return rc

    # --- Subcommand resolution (§2/§4). A recognized leading VERB selects its mode and runs
    # the per-mode parser. ANYTHING else — a bare `review`, `review --flag …` with no verb,
    # an unknown verb — routes to the TOP-LEVEL parser, which serves --help / --list-defaults
    # / --show-board and otherwise prints HELP (a bare `review` no longer runs a diff review:
    # that was the mistake this migration fixes — use `review diff`). ----------------------
    is_subcommand = (
        bool(argv) and not argv[0].startswith("-") and argv[0] in known_subcommands()
    )
    if is_subcommand:
        mode = get_mode(argv[0])
        assert mode is not None  # known_subcommands() guarantees it
        rest = argv[1:]
        parser = _build_mode_parser(mode)
    else:
        # No recognized subcommand. A SUBCOMMAND-scoped flag here (e.g. the old pre-commit
        # `review --staged`, or `review --visual …`) would hit argparse's opaque
        # "unrecognized arguments" on the top-level parser — losing the `review diff`
        # migration pointer. Catch those leading mode/visual-only flags BEFORE argparse and
        # emit the friendly pointer (the friendly path for GLOBAL flags without a verb is the
        # help fall-through below). Then parse the meta flags off the top-level parser; if
        # none short-circuit, fall through to the HELP path (no implicit diff review).
        rc = _reject_subcommand_only_flag_without_verb(argv)
        if rc is not None:
            return rc
        mode = (
            diff_mode()
        )  # only used by the meta-flag handlers (--list-defaults / --show-board)
        rest = argv
        parser = _build_top_level_parser()

    args = parser.parse_args(rest)

    # `--retry N` is wired by EXPORTING $REVIEW_RETRY_COUNT for the rest of this process, so
    # the single in-seat-retry reader (reviewlib.retry.retry_count, called deep in the panel)
    # honours flag, env, and default with ONE precedence rule — the flag wins over a
    # pre-existing env, an unset flag leaves the env (or the built-in default) in force. The
    # board path never has to thread the value through every panel signature. Clamped to the
    # ceiling so a stray `--retry 9999` can't pin a dead seat for minutes.
    if getattr(args, "retry", None) is not None:
        os.environ["REVIEW_RETRY_COUNT"] = str(
            max(0, min(args.retry, max_retry_count()))
        )

    # Run-scoped `--effort` (global flag): parse ONCE into an EffortOverride here so both the
    # board path (applied CLI-side onto the seats) and the flat-panel modes (threaded via
    # ModeContext) resolve against the same value. A bad level is user input for THIS run —
    # fail loudly with exit 2, not the config board's warn-and-drop.
    try:
        effort_override = parse_effort_flag(getattr(args, "effort", None))
    except EffortValueError as exc:
        print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
        return 2

    config = load_config()
    backends.configure_unpaid_providers(config.get("unpaid_providers"))
    from .provider_failover import configure_provider_chains

    configure_provider_chains(config.get("provider_chains"))

    # `models:` / `visual_models:` from config, stripped + alias-expanded + blanks dropped
    # (same rule as _split_models for -m). An "effectively empty" list — absent, or only
    # blank/whitespace entries — is NOT a real preference.
    config_models = _split_models(config.get("models") or [])
    config_visual_models = _split_models(config.get("visual_models") or [])
    config_has_board = isinstance(config.get("board"), list) and bool(
        config.get("board")
    )
    explicit_models = _split_models(args.model)
    explicit_preset = getattr(args, "preset", None) is not None
    preset_applies = mode.name == "review"
    if explicit_preset and preset_applies:
        active_preset = args.preset
    elif (
        preset_applies
        and not explicit_models
        and not config_models
        and not config_has_board
    ):
        active_preset = DEFAULT_PRESET
    else:
        active_preset = None
    # Default pool size precedence: explicit `--pool N` > an explicit `--preset` > a
    # config `pool:` default > the preset/built-in default. The config key lets a personal
    # config.yaml pin a smaller board (e.g. 3 while a provider is disabled) without passing
    # `--pool` every call; an explicit flag/preset still wins.
    if args.pool is not None:
        effective_pool_size = args.pool
    elif explicit_preset and preset_applies:
        effective_pool_size = preset_pool_size(active_preset)
    elif _config_default_pool(config) is not None:
        effective_pool_size = _config_default_pool(config)
    else:
        effective_pool_size = preset_pool_size(active_preset)

    if args.list_defaults:
        if explicit_models:
            effective = explicit_models
        elif mode.name == "visual":
            effective = config_visual_models or [
                _expand_alias(x) for x in VISUAL_MODELS
            ]
        elif mode.name == "review" and active_preset is not None:
            try:
                effective = [r.model for r in load_board(config, preset=active_preset)]
            except BoardConfigError as exc:
                print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
                return 2
        elif mode.name == "review" and config_models:
            effective = config_models
        elif mode.name == "review" and config_has_board:
            try:
                effective = [r.model for r in load_board(config)]
            except BoardConfigError as exc:
                print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
                return 2
        elif mode.name == "brainstorm":
            effective = (
                _split_models(config.get("brainstorm_models") or [])
                or config_models
                or [_expand_alias(x) for x in DEFAULT_MODELS]
            )
        else:
            effective = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        print("\n".join(effective))
        return 0

    if args.show_board:
        # Resolve cwd up front so the agentic/diff-only labels reflect whether opencode
        # would actually run in a real repo for THIS -C (it's diff-only outside a repo).
        return _show_board(
            config,
            effective_pool_size,
            _effective_cwd(args.cwd),
            preset=active_preset,
            explicit_models=explicit_models,
        )

    piped_input = _read_stdin_if_piped()
    task_code: str | None = None
    requires_task_code = is_subcommand and (
        mode.name != "visual"
        or getattr(args, "diff", False)
        or getattr(args, "staged", False)
        or piped_input is not None
    )
    if requires_task_code:
        raw_task = getattr(args, "task", None) or os.environ.get("REVIEW_TASK_CODE")
        try:
            task_code = normalize_task_code(raw_task)
        except ValueError as exc:
            print(
                f"[review-cli] invalid --task CODE: {exc}", file=sys.stderr, flush=True
            )
            return 2
        if task_code is None:
            print(
                "[review-cli] missing required --task CODE for review iteration history.\n"
                "  use: review <mode> --task CODE ...\n"
                "  or set REVIEW_TASK_CODE=CODE for automation.",
                file=sys.stderr,
                flush=True,
            )
            return 2
        args.task = task_code

    # A bare `review` (or `review --flag …` with no verb / an unknown verb) reaches here
    # without a meta flag to serve: print the HELP/usage instead of silently running a diff
    # review. There are three shapes:
    #   * args passed (`review -C <repo>`)            -> usage error (exit 2): point at `review diff`;
    #   * a diff piped on stdin (`git diff | review`)  -> usage error (exit 2): the classic
    #       piped-diff review is `git diff | review diff` now — a bare `review` here used to
    #       run a diff review, so silently exiting 0 would turn it into a no-op SUCCESS that
    #       a script can't detect (codex P1). Fail loud, pointing at `review diff`;
    #   * truly bare (`review`, no args, TTY stdin)    -> print the overview help, exit 0.
    #
    # Raise SystemExit (do NOT `return`): like argparse's own --help / usage errors, a
    # help/usage dump must NOT write the `-o` file — `main()`'s tee only persists on a
    # `return` (a "the dispatch ran" signal), so a `return` here would truncate a
    # pre-existing `-o` target with the help text / an empty buffer. SystemExit propagates
    # through the tee's `finally` with `completed=False`, so no write happens.
    if not is_subcommand:
        piped_diff = (not rest) and (piped_input is not None)
        usage_error = bool(rest) or piped_diff
        parser.print_help(sys.stderr if usage_error else sys.stdout)
        if piped_diff:
            print(
                "\nreview: a diff was piped in but no subcommand given. The diff review is "
                "now `review diff` (a bare `review` no longer runs one). "
                "Run `git diff | review diff --task CODE`.",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(2)
        if rest:
            print(
                "\nreview: no subcommand given. The diff review is now `review diff` "
                "(a bare `review` no longer runs one). Run `review diff --task CODE [options]`.",
                file=sys.stderr,
                flush=True,
            )
            raise SystemExit(2)
        raise SystemExit(0)

    if mode.name == "visual" and not args.visual:
        parser.error("the following arguments are required: IMAGE")

    # Suppress the "reviewing it as-is" non-repo warning on the REVIEW-mode required-diff
    # path: there a non-repo hard-fails via `_fail_not_a_repo` (the authoritative message),
    # so the "as-is" promise would contradict it. The no-git modes (panel) and the
    # tolerant `--visual` review (which DOES proceed as-is) keep the warning.
    _review_required = mode.name == "review" and (args.staged or args.visual is None)
    cwd = _effective_cwd(args.cwd, warn=not _review_required)
    is_brainstorm = mode.name == "brainstorm"
    is_visual_subcommand = mode.name == "visual"
    # A "panel mode" is any non-review mode (brainstorm / just-ask / quorum): the diff is
    # OPTIONAL context for it, its calls are long-running (announce live-log paths), and
    # its per-call timeout default is the shorter PANEL_TIMEOUT_DEFAULT.
    panel_mode = mode.name not in ("review", "visual")
    # Precedence: explicit -m > config > code default. Brainstorm prefers
    # config.brainstorm_models and drops unreachable backends gracefully (so a
    # missing GEMINI_API_KEY never aborts the run). Explicit -m is honored as-is.
    # One usage-limit SNAPSHOT for this whole dispatch (board mode takes the same
    # approach in modes/review.py) — every seat/candidate checked below reads the
    # SAME sample set instead of each re-globbing + re-parsing the tg-ctl usage
    # file independently (and possibly disagreeing mid-selection if it changes).
    _usage_snapshot = usage_limits.load_snapshot()

    def _usage_percent(model: str) -> float | None:
        return usage_percent_for_model(model, samples=_usage_snapshot)

    if explicit_models:
        models = explicit_models
    elif is_brainstorm:
        src = (
            _split_models(config.get("brainstorm_models") or [])
            or config_models
            or [_expand_alias(x) for x in DEFAULT_MODELS]
        )
        models = [m for m in src if backends.backend_available(m)]
        if not models:
            models = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        else:
            # Reuse-aware panel: pad back up to the AVAILABILITY-filtered count
            # (not `len(src)`) when some of the reachable models are near their
            # usage limit, instead of silently running a smaller panel (Alex,
            # 2026-08-18). Target is `len(models)`, not `len(src)`, so this ONLY
            # compensates usage-limit exclusions -- it must never fight brainstorm's
            # existing "drop unreachable backends gracefully" shrink above.
            models = expand_flat_models_with_reuse(
                models, len(models), usage_percent=_usage_percent
            )
            _warn_if_panel_padded(models)
    else:
        src = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        if mode.name == "quorum":
            # Scoped to quorum specifically: `review` builds its OWN board
            # from `config_models`/`load_board` (never reads this `models`
            # var) and `visual`/`qa` use their own model lists too — this
            # `else` branch is also their fallthrough, so padding it
            # unconditionally printed a spurious "panel padded" warning for a
            # panel that was never actually dispatched (k3/Fable review
            # finding). `just-ask` is EXCLUDED too, deliberately: it sends the
            # identical prompt to every seat with no per-seat role/lens (see
            # config's role-less-board carve-out for the same reasoning), so a
            # duplicated seat there is pure cost with zero added diversity —
            # unlike quorum, which labels + discloses duplicates to its
            # moderator so a repeated model is at least an INFORMED tradeoff.
            #
            # Availability-filter BEFORE padding (k3 review finding, round 3):
            # `_is_near_limit` fails OPEN on unknown usage data, so an
            # UNREACHABLE model (missing API key/CLI — unknown to
            # usage_percent, hence "not excluded") could otherwise survive
            # padding while a REACHABLE-but-near-limit model gets excluded —
            # e.g. src=[claude:opus@90%, gemini(keyless)] would pad to
            # ["gemini","gemini"], a panel with ZERO live seats, worse than
            # quorum's pre-reuse behavior of just dispatching both and letting
            # the dead one fail per-call. Filtering first keeps padding
            # scoped to seats that can actually answer; the all-unreachable
            # case is handled below (dispatch `src` unfiltered, no exclusion
            # — k3 review finding, round 5).
            #
            # Target `len(reachable)`, NOT `len(src)` (k3/Fable review finding,
            # round 4): padding must compensate ONLY usage-limit exclusions,
            # exactly like the brainstorm branch above — targeting `len(src)`
            # would ALSO duplicate a live model to paper over a plain
            # unreachable one (a real paid call replacing what used to be a
            # free per-call failure), which is a different, unrequested
            # feature and made `_warn_if_panel_padded`'s "near their usage
            # limit" text false in that case.
            #
            # `_chain_aware_available`, NOT raw `backend_available` (k3/Fable
            # review finding, round 6): the raw probe false-negatives on a
            # seat whose HEAD provider is down but has a live failover
            # alternate (e.g. no ZAI_API_KEY but an authenticated `oc:zai`) —
            # exactly the case `_chain_aware_available`'s own docstring exists
            # to cover for every OTHER pre-dispatch liveness decision (the
            # pool guard, the board split, the ETA). Using the raw probe here
            # would silently drop a chain-recoverable model from the quorum
            # panel instead of just letting `run_panel`'s own provider
            # failover (panel.py) route around the down head provider.
            reachable = [m for m in src if _chain_aware_available(m)]
            if reachable:
                models = expand_flat_models_with_reuse(
                    reachable, len(reachable), usage_percent=_usage_percent
                )
                _warn_if_panel_padded(models)
            else:
                # EVERYTHING reads unreachable -- dispatch `src` UNFILTERED
                # and skip usage-limit exclusion entirely (k3 review finding,
                # round 5): the comment above claimed this fallback matches
                # brainstorm's "identical" one, but it didn't -- brainstorm's
                # actual all-unreachable fallback (`config_models or
                # [_expand_alias(x) for x in DEFAULT_MODELS]`, a few lines up)
                # dispatches the raw list with NO exclusion, letting each dead
                # seat fail per-call (and its provider-chain failover a real
                # chance to recover a false-negative availability probe).
                # Applying exclusion here too would silently drop a near-limit
                # model in favor of duplicating another EQUALLY DEAD one --
                # pure noise when nothing is going to answer regardless.
                models = src
        else:
            models = src

    visual_mode = args.visual is not None
    if explicit_models:
        visual_models = explicit_models
    else:
        visual_models = config_visual_models or [
            _expand_alias(x) for x in VISUAL_MODELS
        ]
    # Timeout default by mode. qa is the carve-out: it is technically a "panel mode" (non-
    # review), but a tester run boots a SUT and drives a whole suite with an un-caged agent —
    # tens of minutes, not the short PANEL_TIMEOUT_DEFAULT (240s) the chat panels use. Give it
    # its OWN long default (it leans on the <=4h backstop, not a 4-minute cap). Review keeps
    # 1200s; the other panel modes (brainstorm/just-ask/quorum) keep the short panel default.
    # Timeout precedence: --timeout flag > config.yaml `timeout:` > mode default.
    # `config_timeout` lets you set a persistent short default for iterate-review
    # workflows without passing --timeout every time.
    config_timeout = config.get("timeout")
    if args.timeout is not None:
        timeout = args.timeout
    elif mode.name == "qa":
        timeout = QA_TIMEOUT_DEFAULT
    elif panel_mode:
        timeout = PANEL_TIMEOUT_DEFAULT
    elif config_timeout is not None:
        timeout = int(config_timeout)
    else:
        timeout = 1200

    # Reviewer board (HYP-741): the diff-review panel assigns each model its own
    # role/lens and keeps a reserve. Precedence:
    #   explicit -m  >  config `models:` priority roster  >  config/default board.
    # With no configured board/models, explicit -m keeps the legacy flat panel. When a
    # config board/models roster exists, explicit -m NARROWS that configured board to the
    # requested models only, preserving matching role/name metadata but never adding extra
    # configured seats. A configured `models:` list is the full priority-ordered roster from
    # which the active pool + reserve are selected; optional `board:` entries can still
    # provide role/name metadata for those models. The board is NEVER disabled — `--pool N`
    # only sizes how many of its seats run (default 4; the rest are reserve). `use_board` is
    # a cheap boolean gate computed now; the actual board resolution + cost-safety validation
    # (and the --pool slice) runs LATER
    # (validate_board, below) — after the standalone-visual path has had its chance to
    # short-circuit, so a malformed `board:` never blocks the board-unrelated standalone
    # `review visual` pipeline (codex P2). It still fires
    # BEFORE the COMPANION visual fan-out, so a doomed config never spends a paid vision
    # call.
    use_board = not panel_mode and (
        not explicit_models
        or bool(config_models)
        or config_has_board
        or active_preset is not None
    )
    board: list | None = None
    board_validated = False

    def validate_board() -> int | None:
        """Resolve + validate the FULL priority-ordered reviewer board for the default
        review path, once. The board is loaded whole (NOT sliced to --pool here): the
        failover pool path (mode_review) does the startup failover — selecting the top
        configured number of AVAILABLE seats by priority — and keeps the rest as the reserve that
        backfills a seat which fails mid-run. Returns an exit code (2) on an all-malformed
        `board:` config, else None. No-op when the board does not apply (panel mode or
        explicit -m with no configured board/models)."""
        nonlocal board, board_validated
        if board_validated or not use_board:
            return None
        board_validated = True
        try:
            if explicit_models:
                try:
                    board = board_from_models(
                        explicit_models, config, preset=active_preset
                    )
                except BoardConfigError as exc:
                    print(
                        f"[review-cli] {exc}; ignoring malformed board metadata for explicit -m",
                        file=sys.stderr,
                        flush=True,
                    )
                    board = board_from_models(explicit_models, {}, preset=active_preset)
            elif active_preset is not None:
                board = load_board(config, preset=active_preset)
            elif config_models:
                board = board_from_models(config_models, config)
            else:
                board = load_board(config)
        except BoardConfigError as exc:
            print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
            return 2
        # Run-scoped `--effort` OVERRIDES each seat's config effort for this run (falls back
        # to the seat value where the flag says nothing). Applied once, here, so the failover
        # board mode_review consumes carries the effective effort — no double application.
        if board is not None:
            board = apply_effort_override(board, effort_override)
        return None

    # Panel modes are interactive and long-running, so announce each streamed
    # backend's live-log path to stderr; the plain review path stays quiet. The mode
    # descriptor declares this (announce_logs); brainstorm/just-ask/quorum opt in.
    if mode.announce_logs:
        backends._ANNOUNCE_LOGS = True

    # Diff acquisition. Panel modes treat the diff as optional context. The review mode
    # REQUIRES a diff. With --visual + the review mode, the diff still drives the routing
    # (§3): a present diff → the diff-review companion, an absent diff → the standalone
    # pipeline — so we MUST still try to discover it, but a missing diff / non-repo must
    # degrade to standalone rather than abort.
    diff = piped_input
    # A piped diff is NOT the git index, so it must not satisfy the staged commit gate
    # even under `--staged` (the stamp/marker mean "the staged index was reviewed", and
    # `printf ... | review --staged` reviews arbitrary stdin, not `git diff --cached`).
    # Record the provenance so the review handler can suppress the stamp/marker for it.
    diff_from_stdin = diff is not None
    # brainstorm treats the diff as OPTIONAL grounding context even with --staged/--diff,
    # so it must NOT take the hard-fail `needs_diff` path: a non-repo `-C` or a failing
    # `git diff [--cached]` degrades to pure ideation (diff == ""), not an abort. Only the
    # review mode (no --visual) genuinely REQUIRES a diff; --staged on a review still
    # hard-requires it (the pre-commit gate). So brainstorm is excluded from needs_diff
    # and routed through the caught/optional probe below.
    needs_diff = (
        (args.staged or (mode.name == "review" and not visual_mode))
        and not is_brainstorm
        and not is_visual_subcommand
    )
    if diff is None and needs_diff:
        # This path attaches the working-tree / staged diff. Outside a git repo it must NOT
        # raise a raw `git diff` traceback. Two cases:
        #   * REVIEW mode (not panel_mode): the diff is genuinely REQUIRED, so a non-repo is
        #     a user error — fail GRACEFULLY with the 3-part message + stable EXIT_NOT_A_REPO.
        #   * PANEL mode (just-ask / quorum) with --staged: the diff is OPTIONAL context
        #     (diff_policy="none"), so a non-repo degrades to no-context ("") — NOT a hard
        #     error, and never the "run just-ask" message at someone already running it.
        # A piped diff short-circuited above (diff is not None), so the stdin path never
        # reaches here — it works without a repo.
        if not _is_git_repo(cwd):
            if panel_mode:
                diff = ""  # optional context (diff_policy="none") -> degrade, never hard-fail
            else:
                return _fail_not_a_repo(cwd)
        elif panel_mode:
            # In a repo but the diff is OPTIONAL context for a panel mode: a `git diff`
            # failure (e.g. an unborn HEAD with --staged, a partial repo) degrades to
            # no-context, exactly like the `--diff` / brainstorm siblings below — never a
            # raw traceback.
            try:
                diff = _git_diff(cwd, args.staged)
            except RuntimeError:
                diff = ""
        else:
            # REQUIRED path, in a real repo. `_is_git_repo` passing does NOT guarantee `git
            # diff` succeeds (a wedged/timed-out git, a corrupt index -> `_git_diff` raises
            # RuntimeError). The diff is required here, so we can't degrade to "" — but we must
            # still NOT traceback: fail GRACEFULLY with a structured error + stable exit.
            try:
                diff = _git_diff(cwd, args.staged)
            except RuntimeError as exc:
                return _fail_git_diff(cwd, exc)
    elif diff is None and visual_mode and mode.name == "review":
        # --visual riding the review mode: probe the working-tree diff to decide
        # companion-vs-standalone, but tolerate "no diff / not a git repo".
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    elif diff is None and is_visual_subcommand and (args.diff or args.staged):
        # `review visual IMAGE` is standalone by default. `--diff` / `--staged` explicitly
        # opt into the companion diff-review path, but still degrade to standalone when no
        # diff can be obtained.
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    elif diff is None and is_brainstorm:
        # brainstorm picks up the staged (--staged) or working-tree diff as OPTIONAL
        # grounding context so you can brainstorm ABOUT a specific change. The diff is
        # never required: an absent diff / non-repo / git failure degrades to pure
        # ideation (diff == ""). `_read_stdin_if_piped` already returns the diff for a
        # NON-EMPTY pipe (precedence); empty/`/dev/null` stdin reads as None here, so we
        # still probe the working tree — matching every other mode and the documented
        # `review brainstorm "Q" < /dev/null` convention (an empty redirect must NOT
        # suppress grounding). `--diff` is the explicit opt-in spelling of the same probe.
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    elif diff is None and panel_mode and args.diff:
        # just-ask / quorum: the diff is "none" policy (a question, not a change), so it
        # is NOT auto-grabbed. `--diff` is the explicit OPT-IN to attach the working-tree
        # diff as context (the staged counterpart is the `needs_diff` path above). It
        # degrades gracefully to no-context on a non-repo / git failure.
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    diff = diff or ""

    # Diff-identity binding (reviewlib.stats "Diff-identity binding"): captured from
    # the FULL, uncapped diff — computed BEFORE the dispatch-time cap below and before
    # mode_review's own internal capping, so a huge diff's identity is never truncated
    # away. `repo_id` is independent of `diff` (cwd alone), so it's computed even for a
    # diff-less just-ask/quorum/brainstorm run — that still lets --check catch a
    # cross-REPO mismatch (incident #1) even with no file-set signal to add.
    repo_id = _compute_repo_id(cwd)
    diff_files = extract_diff_files(diff)
    diff_sha256 = diff_content_hash(diff) if diff else None

    # Review-stamp integrity (round-5 review finding, k3+Opus, on this same
    # diff-identity feature): the pre-commit gate's stamp must certify the diff
    # actually DISPATCHED to the models, not whatever happens to be staged
    # MINUTES later when the multi-model panel finishes. Captured HERE,
    # immediately adjacent to the `diff` capture above (a millisecond gap, not
    # the minutes-long panel-run gap `_write_review_stamp` used to have when it
    # independently re-ran `git diff --cached` at stamp-WRITE time) — see
    # `reviewlib.install._write_review_stamp`'s docstring for the full story
    # (why it can't just hash `diff` directly: the pre-commit hook's own
    # verification is UNPREFIXED, `diff` is prefixed via `_git_diff`'s
    # `diff.noprefix` fix). Only meaningful for `--staged` (the only case
    # `_write_review_stamp` is ever reached from); None otherwise.
    stamp_diff_hash = _stamp_hash_for_staged_diff(cwd) if args.staged else None

    # Dispatch-time diff cap for the flat PANEL modes (brainstorm/quorum/just-ask) —
    # see reviewlib.backends.cap_diff_for_dispatch's docstring for why this is NOT
    # applied to `review`/`visual` (mode_review owns capping its own two dispatch
    # paths itself, so it can keep the UNCAPPED canonical diff for the --commit
    # checkpoint's integrity check; these three modes have no such requirement).
    # brainstorm in particular auto-probes the diff by DEFAULT (no --diff needed), so
    # without this an oversized diff is sent uncapped to every persona every round —
    # the worst token-burn multiplier the 2026-08 investigation found (codex/kimi
    # review finding on this feature's own PR). A piped diff (`diff_from_stdin`) is
    # exempt, matching mode_review's identical exemption.
    #
    # codex review finding (round 2 on this feature's own PR): each of these three
    # modes ALSO caps at its own dispatch boundary (`cap_diff_for_dispatch` is called
    # again inside mode_brainstorm/mode_quorum/mode_just_ask, so a direct library
    # caller bypassing this CLI layer is still protected). Capping HERE and there both
    # is a genuine double-application: harmless at the DEFAULT cap (the first call's
    # output is already <= cap, so the second is a true no-op), but NOT idempotent
    # when `$REVIEW_DIFF_MAX_BYTES` is set below the truncation marker's own length —
    # the second call then re-truncates the FIRST call's marker text and reports ITS
    # byte count as "the full diff", not the real original diff's size. Rather than
    # remove either capping point (the mode-level one is the ONLY guard for a direct
    # caller; this CLI-level one is what `test_cli_brainstorm_oversized_worktree_diff_
    # is_capped_for_dispatch` pins), thread whether THIS layer already capped it so the
    # mode-level call becomes a genuine no-op for the CLI path instead of a second real
    # application — see `diff_already_capped` in `ModeContext.extra` below.
    diff_already_capped = False
    if (
        diff
        and not diff_from_stdin
        and mode.name in ("brainstorm", "quorum", "just-ask")
    ):
        capped_diff = backends.cap_diff_for_dispatch(diff)
        diff_already_capped = capped_diff != diff
        diff = capped_diff

    # --- --visual composition (§2.1). Build the visual context ONCE; thread it into
    # whichever consumer runs. cvGate fires here regardless of mode (a broken render
    # is flagged before any model call). -----------------------------------------
    visual_ctx = None
    if visual_mode:
        # qa does NOT consume --visual: the tester drives a LIVE system and produces its OWN
        # proof (screenshots/logs), so an input image has no place in its prompt. Reject it
        # HERE — before the (paid) vision pipeline runs — rather than letting cli.py spend a
        # vision call and the qa handler silently drop the result (review finding). This is a
        # tiny mode-aware guard, not mode-dispatch surgery.
        if mode.name == "qa":
            print(
                "[review-cli] qa: --visual is not supported (the tester produces its OWN "
                "visual proof by driving the SUT). Drop --visual.",
                file=sys.stderr,
                flush=True,
            )
            return 2

        from .features.visual.compose import build_mode_visual_context

        # STANDALONE: --visual with no companion mode AND no diff → the verdict pipeline.
        # NOT recorded in run-stats / no ETA: this is a single-backend vision pipeline
        # (select_vision_backend picks ONE model, and run_pipeline can return before any
        # vision call), not a multi-model / multi-round text panel — recording the whole
        # candidate list as the "pool" would mis-key its history with a bogus pool_size
        # and duration. The ETA store deliberately covers only the slow text panel modes
        # an agent might wrongly short-timeout (codex P2).
        if not panel_mode and not diff.strip():
            from .features.visual.visual_cli import run_visual_standalone

            return run_visual_standalone(
                args.visual,
                before=args.before,
                expect=args.expect,
                intent=args.intent,
                requested_checks=list(args.check),
                models=visual_models,
                no_ai=args.no_ai,
                # Stage-2a cost-saver default ON; --no-local-model OR `local_model: false`
                # in config.yaml disables it (CLI flag wins over config).
                local_model=(not args.no_local_model)
                and (config.get("local_model", True) is not False),
                vision_timeout=args.vision_timeout,
                as_json=args.json,
                strict=args.strict,
                # Per-project module discovery defaults to the CLI cwd (-C), NOT the
                # process cwd, so `review visual shot.png -C <repo>` finds
                # <repo>/.review/visual-modules.json (codex P2).
                project=args.project or str(cwd),
                effort_override=effort_override,
            )

        # COMPANION: a mode (or the default diff-review) runs WITH the image as context.
        # Validate the board BEFORE the (potentially paid) vision fan-out so an
        # all-malformed `board:` fails fast and never spends a vision call on a config
        # that is going to error anyway (codex P2). Standalone visual already returned
        # above, so this never touches the board-unrelated standalone path.
        rc = validate_board()
        if rc is not None:
            return rc
        # Stage 2: the image is delivered to a vision model (the per-mode fan-out) unless
        # --no-ai, and the grounded observation is folded into the mode prompt.
        visual_ctx = _call_with_task_env(
            task_code,
            lambda: build_mode_visual_context(
                Path(args.visual).expanduser(),
                before=Path(args.before).expanduser() if args.before else None,
                expect=args.expect,
                intent=args.intent,
                models=[] if args.no_ai else visual_models,
                requested_checks=list(args.check),
                vision_timeout=args.vision_timeout,
                require_vision=not args.no_ai,
                effort_override=effort_override,
            ),
        )
        # The cvGate pre-filter BLOCKS the companion run on an unambiguously-broken
        # render (codex P2): a blank/unreadable/error-overlay image must short-circuit
        # the mode, not merely be mentioned in prompt text (else `review --staged
        # --visual blank.png` would run the review and stamp success). Exit 10 under
        # --strict (the gate/hook block code), else a non-zero advisory exit.
        if visual_ctx.prefilter_verdict == "rollback":
            print(
                f"[review visual] ROLLBACK (pre-filter, mode blocked): {visual_ctx.prefilter_reason}"
            )
            # An unreadable/missing image is a USAGE error (exit 1), matching the
            # standalone exit-code map — scripts/hooks rely on the distinction between
            # "unreadable input" (1) and "blocking content verdict under --strict" (10).
            if "unreadable" in visual_ctx.prefilter_reason:
                return 1
            return 10 if args.strict else 1
        if visual_ctx.vision_error:
            print(
                f"[review visual] UNVERIFIED (mode blocked): {visual_ctx.vision_error}"
            )
            if visual_ctx.vision_timed_out:
                return 124
            return 10 if args.strict else 1

    # Build the resolved ModeContext handed to the mode's handler (thin over the lib).
    # `with_visual` folds the --visual companion context into the mode's prompt/topic
    # (identity when there is none). Moderators are resolved for the panel/brainstorm
    # modes; the review handler ignores them.
    def _with_visual_text(text: str) -> str:
        return _with_visual(text, visual_ctx)

    # --moderator is scoped to the modes that USE a moderator (quorum / brainstorm); a flat
    # panel like just-ask has no `--moderator` on its parser, so read it defensively. A mode
    # with no moderator flag -> None -> pick_moderators falls back to the auto-pick chain
    # (harmless: just-ask ignores the resolved moderators anyway).
    moderator_arg = getattr(args, "moderator", None)
    moderators = pick_moderators(moderator_arg, models) if panel_mode else []
    ctx = ModeContext(
        args=args,
        models=models,
        diff=diff,
        cwd=cwd,
        timeout=timeout,
        with_visual=_with_visual_text,
        visual_ctx=visual_ctx,
        moderators=moderators,
        effort_override=effort_override,
        extra={
            "diff_from_stdin": diff_from_stdin,
            "diff_already_capped": diff_already_capped,
            "stamp_diff_hash": stamp_diff_hash,
        },
    )

    # The recorded mode is the EXACT mode (a brainstorm of 4 is nothing like a plain
    # review of 4), and `pool_models` is what is ACTUALLY DISPATCHED so pool_size is
    # ground truth. A --visual companion is recorded under its base text mode: the
    # vision context above already ran (cvGate + the bounded <=--vision-timeout call),
    # and the multi-minute cost an agent might wrongly short-timeout is the text panel
    # that follows — which IS inside the wrapper below — so the base-mode key is the
    # honest one (codex P2: don't split history on a tag whose timing we exclude).
    if is_brainstorm:
        # Brainstorm dispatches max(3, len(panel)) persona slots PER ROUND, so the real
        # per-round pool — and the ETA key — is the slot count, not len(models) (codex
        # P2: don't undercount a 1-2 model panel). brainstorm_pool mirrors that.
        return _run_mode_with_stats(
            mode.stats_mode,
            brainstorm_pool(models),
            lambda: mode.handler(ctx),
            task_code=task_code,
            repo_id=repo_id,
            diff_files=diff_files,
            diff_sha256=diff_sha256,
        )

    if mode.name == "qa":
        # qa is SINGLE-SEAT: the handler runs exactly ONE write/exec tester (claude default /
        # codex via REVIEW_QA_TESTER / -m), ignoring --pool and the panel. Record its run-stats
        # / ETA under a pool of ONE — not len(DEFAULT_MODELS) — so the ETA store isn't polluted
        # with a fake multi-model pool size (review finding). The recorded seat is the ACTUAL
        # resolved backend, not a model alias.
        from .qa.executor import resolved_tester_backend

        qa_seat = [resolved_tester_backend(_split_models(args.model or []))]
        return _run_mode_with_stats(
            mode.stats_mode,
            qa_seat,
            lambda: mode.handler(ctx),
            task_code=task_code,
            repo_id=repo_id,
            diff_files=diff_files,
            diff_sha256=diff_sha256,
        )

    if mode.name not in ("review", "visual"):
        # just-ask / quorum: a flat multi-model panel; pool_size == len(models).
        return _run_mode_with_stats(
            mode.stats_mode,
            models,
            lambda: mode.handler(ctx),
            task_code=task_code,
            repo_id=repo_id,
            diff_files=diff_files,
            diff_sha256=diff_sha256,
        )

    # The review mode. Validate the board now if it wasn't already (the no-visual path);
    # an all-malformed `board:` exits 2 before the panel runs. The --visual companion
    # context folds into each per-reviewer prompt via args.prompt (handler's with_visual).
    rc = validate_board()
    if rc is not None:
        return rc
    if board:
        # Foolproofing (review mode + a NON-EMPTY diff ONLY): bail with a proposal / targeted
        # per-provider error when the live subset can't satisfy the requested pool size,
        # instead of silently running a degenerate panel (reviewlib.pool_guard). Gated on:
        #   * mode.name == "review" — the proposal/error text is review-specific
        #     (`review diff --preset …`, "review pool"), so it must NOT fire for the other
        #     panel modes (quorum / brainstorm / just-ask / visual) that reach this dispatch;
        #   * a non-empty diff — an EMPTY diff runs NO panel (mode_review short-circuits with
        #     "No diff to review", exit 1), so there is no pool to assemble and the guard must
        #     not preempt that no-op with an EXIT_UNSATISFIED. Inert on the happy path + fake
        #     backend.
        if mode.name == "review" and diff.strip():
            guard_rc = _evaluate_pool_or_bail(
                config,
                config_models,
                config_has_board,
                _seats_of(board),
                explicit_models,
                args.pool,
                _config_default_pool(config) or DEFAULT_POOL_SIZE,
            )
            if guard_rc is not None:
                return guard_rc
        review_pool_size = len(board) if explicit_models else effective_pool_size
        # Failover pool. The PLANNED pool keys the up-front ETA: the top `--pool`
        # AVAILABLE seats by priority (startup failover — the same selection mode_review
        # makes). The RECORDED models come from the failover outcome (the seats that
        # actually produced verdicts, after any mid-run backfill), via outcome_sink.
        if explicit_models:
            planned_pool = list(board)
        else:
            # Chain-aware (`_chain_aware_available`), matching the pool guard above AND the
            # REAL dispatch split in `modes.review._mode_review_board` — a raw
            # `backend_available` here would let the ETA plan a smaller pool than the guard
            # just approved (codex P1 on review of #157).
            planned_pool, _ = split_pool_reserve(
                board,
                review_pool_size,
                lambda r: _chain_aware_available(r.model),
            )
        eta_models = [r.model for r in planned_pool]
        outcome_sink: list = []
        ctx.extra.update(
            board=board,
            pool_size=review_pool_size,
            outcome_sink=outcome_sink,
            exact_board=bool(explicit_models),
            # None for an explicit -m board (exact_board — usage-limit awareness
            # doesn't apply to a hand-picked, non-failover roster) or when a
            # config/default board is priced out entirely — matches this
            # function's own "explicit -m is honored as-is" precedent above.
            usage_percent=None if explicit_models else _usage_percent,
        )

        def _ran_models() -> list[str]:
            if explicit_models:
                return [r.model for r in board]
            # The BARE model ids that produced verdicts (a backfilled reserve under its
            # real id), so the stat record keys on what actually ran — not labels.
            return outcome_sink[0].usable_models if outcome_sink else []

        return _run_mode_with_stats(
            mode.stats_mode,
            eta_models,
            lambda: mode.handler(ctx),
            models_after=_ran_models,
            task_code=task_code,
            repo_id=repo_id,
            diff_files=diff_files,
            diff_sha256=diff_sha256,
        )
    # Flat review path (no board): explicit `-m` with no configured board/models. The
    # foolproofing guard STILL applies here — an explicit selection whose live subset can't
    # converge must not silently run a degenerate flat panel either (the board path isn't the
    # only advertised case). The user's selection IS `models` (the flat `-m` list). Gated to
    # the review mode ONLY (the flat dispatch is shared by quorum / brainstorm / just-ask,
    # whose panels keep their own behaviour and never see review-specific proposal text) AND
    # to a non-empty diff (an empty diff runs no panel — see the board-path note above).
    if mode.name == "review" and diff.strip():
        guard_rc = _evaluate_pool_or_bail(
            config,
            config_models,
            config_has_board,
            tuple((m, m) for m in models),
            explicit_models,
            args.pool,
            _config_default_pool(config) or DEFAULT_POOL_SIZE,
        )
        if guard_rc is not None:
            return guard_rc
    # ctx.extra has no "board" key, so the handler reads board=None and takes the legacy
    # flat call shape.
    return _run_mode_with_stats(
        mode.stats_mode,
        models,
        lambda: mode.handler(ctx),
        task_code=task_code,
        repo_id=repo_id,
        diff_files=diff_files,
        diff_sha256=diff_sha256,
    )


def _seat_reads_repo(model: str, cwd_is_repo: bool) -> bool:
    """True iff this seat's backend runs AGENTICALLY in the real repo (reads any file),
    False if it only sees the diff embedded in the prompt (a raw keyed-HTTP call).

    Agentic backends (codex, opencode, the claude CLI) run read-only inside `-C` and can
    open project files beyond the diff. Raw-API backends (gemini, z.ai, commandcode, and
    the claude API path) are stateless HTTP calls with no workspace, so they review only
    the diff. This is purely for the `--show-board` label — it never affects routing.

    `cwd_is_repo` matters for opencode: it is agentic ONLY when `-C` is a real git repo
    (it falls back to a diff-only isolated temp dir otherwise), so the label mirrors
    `review_opencode`'s own `_opencode_runs_in_repo(cwd)` check rather than claiming every
    `oc:` seat is agentic regardless of where it would run. The caller resolves this bit
    ONCE (a single `git rev-parse`) and passes it in, so labeling N seats stays O(1)
    subprocesses, not O(N)."""
    backend = backends.resolve_backend(model)
    if backend is backends.review_codex:
        return True
    if backend is backends.review_omp:
        # omp runs read-only (`--tools read,grep,glob`) in the real cwd like codex —
        # agentic regardless of whether `-C` is a git repo.
        return True
    if backend is backends.review_opencode:
        return cwd_is_repo
    if backend is backends.review_claude:
        # claude is agentic ONLY via the CLI path; the API path has no workspace.
        mode = os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
        if mode == "api":
            return False
        if mode == "cli":
            return True
        # Auto-pick mirrors the dispatcher: CLI when the binary is present.
        return backends._have_claude_cli()
    return False


def _show_board(
    config: dict,
    pool_size: int = DEFAULT_POOL_SIZE,
    cwd: Path | None = None,
    *,
    preset: str | None = None,
    explicit_models: list[str] | None = None,
) -> int:
    """Print the active reviewer board as a PRIORITY-ordered failover pool.

    The selected board (explicit preset, else config.yaml `models:`/`board:`, else the
    raw built-in DEFAULT_BOARD) is listed in priority order (strongest first). Each seat
    shows its display name, role, model, backend availability (key/CLI present), and a
    failover TIER:
      * `pool`    — one of the top-`pool_size` AVAILABLE seats that a plain `review`
                    actually runs (startup failover: a higher-priority but UNAVAILABLE
                    seat is skipped and the next available one is pulled into the pool);
      * `reserve` — available, below the pool cut; backfills a pool seat that fails
                    mid-run (mid-run failover);
      * `unavail` — backend not reachable right now; it can't sit in the pool, but a
                    run-time "unavailable" reply still triggers a reserve backfill.
    Read-only — no model is called, no key is printed."""
    config_models = _split_models(config.get("models") or [])
    exact_models = explicit_models or []
    try:
        if exact_models:
            try:
                board = board_from_models(exact_models, config, preset=preset)
            except BoardConfigError as exc:
                print(
                    f"[review-cli] {exc}; ignoring malformed board metadata for explicit -m",
                    file=sys.stderr,
                    flush=True,
                )
                board = board_from_models(exact_models, {}, preset=preset)
            source = f"explicit -m{f' + preset:{preset}' if preset else ''}"
        elif preset:
            board = load_board(config, preset=preset)
            source = f"preset:{preset}"
        elif config_models:
            board = board_from_models(config_models, config)
            source = "config.yaml (models:)"
        else:
            board = load_board(config)
            source = (
                "config.yaml (board:)"
                if isinstance(config.get("board"), list) and config.get("board")
                else "default"
            )
    except BoardConfigError as exc:
        print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
        return 2
    # The LIVE pool/reserve split is by PRIORITY + AVAILABILITY (the same split the
    # failover review path makes), not by raw seat index — an unavailable top seat is
    # skipped so the pool fills from the next available priority. Probe each seat ONCE by
    # index (handles a board with the same model in two seats), then walk the available
    # seats in priority order, tagging the first `pool_filled` `pool` and the rest
    # `reserve`. `pool_filled` is how many of the AVAILABLE seats the pool size selects.
    avail = [backends.backend_available(r.model) for r in board]
    available_count = sum(avail)
    exact_board = bool(exact_models)
    pool_filled = (
        len(board) if exact_board else _effective_pool_size(available_count, pool_size)
    )
    sized = " (sized by preset/--pool)" if pool_size != DEFAULT_POOL_SIZE else ""
    if exact_board:
        print(
            f"Reviewer board ({len(board)} explicit seats, source: {source}; "
            "exact -m run = every LIVE listed seat is attempted, --pool is ignored — but if the "
            "live subset can't fill the request the pre-dispatch guard PROPOSES a fitting "
            "board/preset instead of running a degenerate panel):\n"
        )
    else:
        pool_target = (
            "all AVAILABLE seats" if pool_size <= 0 else f"top {pool_size} AVAILABLE"
        )
        print(
            f"Reviewer board ({len(board)} seats, priority-ordered, source: {source}; "
            f"live pool = {pool_target} by priority{sized}, "
            f"{pool_filled} filled, the rest reserve — size with --pool N):\n"
        )
    name_w = max((len(r.display) for r in board), default=0)
    role_w = max((len(r.role or "general") for r in board), default=0)
    effort_w = max((len(r.effort or "-") for r in board), default=1)
    # Resolve the repo bit ONCE (a single git rev-parse) for the opencode scope label,
    # rather than per seat in the loop.
    cwd_is_repo = backends._opencode_runs_in_repo(cwd or Path.cwd())
    seen_available = 0  # how many AVAILABLE seats walked so far (priority order)
    for index, reviewer in enumerate(board):
        available = avail[index]
        if available:
            status = "available"
        elif backends.runtime_provider_marked_unpaid(reviewer.model):
            status = (
                "will attempt (provider unpaid/disabled)"
                if exact_board
                else "SKIPPED (provider unpaid/disabled)"
            )
        else:
            status = (
                "will attempt (no key/CLI)" if exact_board else "SKIPPED (no key/CLI)"
            )
        role = (reviewer.role or "general").ljust(role_w)
        effort = (reviewer.effort or "-").ljust(effort_w)
        if exact_board:
            tier = "explicit"
        elif not available:
            tier = "unavail"
        else:
            tier = "pool   " if seen_available < pool_filled else "reserve"
            seen_available += 1
        prio = f"#{index + 1}"
        scope = (
            "agentic" if _seat_reads_repo(reviewer.model, cwd_is_repo) else "diff-only"
        )
        print(
            f"  {prio:>3}  [{tier}]  {reviewer.display.ljust(name_w)}  {role}  "
            f"{reviewer.model}  [{status}]  ({scope})  effort={effort}"
        )
    print(
        "\nScope: `agentic` seats (codex / opencode / omp / claude-CLI) run read-only in the "
        "real repo and can read any project file; `diff-only` seats (gemini / z.ai / "
        "commandcode / claude-API) are stateless HTTP calls that see only the diff."
    )
    if exact_board:
        print(
            "\nWith explicit `-m`, review-cli attempts exactly the listed models in order. "
            "`--pool` and reserve failover do not slice or reorder explicit seats."
        )
    elif pool_size <= 0:
        print(
            "\nA plain `review diff` runs all AVAILABLE seats by priority (--pool 0); "
            "a higher-priority seat that is unavailable is skipped, and a seat that "
            "fails mid-run is recorded while the remaining available seats continue. "
            "`--pool N` sizes the pool."
        )
    else:
        print(
            f"\nA plain `review diff` runs the top {pool_size} AVAILABLE seats by "
            f"priority (--pool {pool_size}); a higher-priority seat that is "
            f"unavailable (or fails mid-run) is replaced by the next-priority reserve so the "
            f"pool keeps {pool_size} working reviewers. `--pool N` sizes the pool; "
            f"`--pool 0` runs all available seats."
        )
    if not all(avail):
        print(
            "Unavailable reviewers drop out and are backfilled from the reserve; the "
            "board degrades gracefully only if the reserve is exhausted. The default "
            "agentic seats (`oc:…` Kimi/GLM/Qwen/DeepSeek, codex, claude) need their CLI "
            "on PATH — `oc:` seats need the `opencode` binary plus its own provider auth "
            "(`opencode auth login`), NOT review-cli's COMMANDCODE_API_KEY/ZAI_API_KEY. "
            "gemini needs GEMINI_API_KEY. (COMMANDCODE_API_KEY/ZAI_API_KEY only power the "
            "diff-only `commandcode:`/`zai:` backends for `-m cc`/`-m glm` and config seats.)"
        )
    return 0


def _with_visual(text: str, visual_ctx) -> str:
    """Fold the --visual composition context into a mode's prompt/question/topic.

    This is the composition seam (§2.1): the image's described context (and cvGate
    outcome) is appended so the companion mode reasons about the render. The full
    per-call multimodal fan-out (routing each model call through call_ai_vision with
    the image attached) is Stage 2; this is where it plugs in."""
    if visual_ctx is None:
        return text
    return text + visual_ctx.context_note


# Re-export for legacy callers that imported subprocess off the entry module.
__all__ = ["main", "subprocess"]
