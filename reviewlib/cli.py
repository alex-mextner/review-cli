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
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from . import backends
from .backends import _which  # re-export for tests/compat  # noqa: F401
from .backstop import run_backstop
from .config import (
    DEFAULT_MODELS,
    DEFAULT_POOL_SIZE,
    DEFAULT_PROMPT,
    PANEL_TIMEOUT_DEFAULT,
    BoardConfigError,
    _expand_alias,
    _effective_pool_size,
    _split_models,
    load_board,
    load_config,
    split_pool_reserve,
)
from .install import install_commit_hook, install_skill
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
from .process import _run
from .stats import announce_eta, record_run

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
        proc = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, timeout=10)
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
        "  fix: run a mode that needs no git — `review just-ask \"...\"` / "
        "`review quorum \"...\"` / `review brainstorm \"...\"` — or cd into a repo and re-run.",
        file=sys.stderr, flush=True,
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
        "(`git diff | review diff`).",
        file=sys.stderr, flush=True,
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
    surfaces as the RuntimeError above — a clean one-line error, not a silent wrong result."""
    args = ["git", "diff", "--no-ext-diff"]
    if staged:
        args.append("--cached")
    try:
        proc = _run(args, cwd=cwd, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git diff could not run: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff failed")
    return proc.stdout


def _read_stdin_if_piped() -> str | None:
    if sys.stdin.isatty():
        return None
    data = sys.stdin.read()
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
        # exactly like a non-repo dir — same defensive catch as `_is_git_repo`.
        try:
            proc = _run(["git", "rev-parse", "--show-toplevel"], cwd=resolved, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
    if warn:
        print(f"[review-cli] warning: {resolved} is not inside a git repository; "
              "reviewing it as-is — pass -C <project-root> to point review at your repo.",
              file=sys.stderr, flush=True)
    return resolved


def _dashboard_subcommand(rest: list[str]) -> int:
    """Parse `dashboard [--host H] [--port N] [--no-open]` and start the web server.

    Binds 127.0.0.1 by default; ``--host 0.0.0.0`` exposes it over Tailscale (mirrors
    ``review spec-web``). Imported lazily so the dashboard's stdlib HTTP stack never loads
    on the hot review path (and a stray import error in dashboard code can't break
    `review`)."""
    sub = argparse.ArgumentParser(prog="review dashboard", description="Web dashboard for review-cli runs.")
    sub.add_argument("--host", default="127.0.0.1",
                     help="interface to bind (default: 127.0.0.1 loopback-only; 0.0.0.0 exposes over Tailscale)")
    sub.add_argument("--port", type=int, default=None, help="port to bind (default: a free ephemeral port)")
    sub.add_argument("--no-open", action="store_true", help="do not open a browser window")
    sub.add_argument("--verbose", action="store_true", help="log every HTTP request to stderr")
    ns = sub.parse_args(rest)
    from .dashboard import run_dashboard

    return run_dashboard(port=ns.port, host=ns.host, open_browser=not ns.no_open, verbose=ns.verbose)


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
    sub.add_argument("-a", "--all", action="store_true",
                     help="list ALL sessions incl. dead/interrupted (no synthesis); default lists recent completed")
    sub.add_argument("-s", "--resume", metavar="ID", default=None,
                     help="resume the session with this id (short id or unambiguous prefix): continue the round loop and synthesize")
    sub.add_argument("--force", action="store_true",
                     help="with --resume on an already-completed session, re-synthesize anyway")
    # Resume reuses the saved panel/moderator by default; -m / --moderator override.
    sub.add_argument("-m", "--model", action="append", default=[],
                     help="override the resume panel (repeat or comma-separate); default = the saved session's panel")
    sub.add_argument("-C", "--cwd", default=".", help="repository directory (resume diff/agentic cwd)")
    sub.add_argument("--moderator", default=None, help="override the resume moderator; default = the saved session's moderator")
    # Grounding diff on resume: the original `--diff`/`--staged` grounding is NOT persisted
    # in the discussion log, so a resumed grounded brainstorm would otherwise continue
    # UNgrounded. These flags re-attach the current working-tree (--diff) or staged
    # (--staged) diff as grounding for the resumed rounds + synthesis (opt-in, like the
    # brainstorm mode's own grounding). Absent -> the resume runs ungrounded.
    sub.add_argument("--diff", action="store_true", help="re-attach the working-tree diff as grounding for the resumed rounds")
    sub.add_argument("--staged", action="store_true", help="re-attach the staged diff (git diff --cached) as grounding for the resumed rounds")
    sub.add_argument("--timeout", type=int, default=None,
                     help=f"per-call timeout seconds for the resumed rounds (default {PANEL_TIMEOUT_DEFAULT})")
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
    header = "all sessions (incl. interrupted)" if ns.all else "recent completed sessions"
    print(f"Brainstorm {header} — newest first; resume with `review sessions -s <id>`:\n")
    for s in sessions:
        ts = s.timestamp.strftime("%Y-%m-%d %H:%M UTC") if s.timestamp else "?"
        topic = (s.topic[:60] + "…") if len(s.topic) > 61 else (s.topic or "(no topic)")
        print(f"  {s.session_id}  [{s.status:<11}]  r{s.completed_rounds}  {ts}  {topic}")
    return 0


def _resume_session_cli(ns: argparse.Namespace) -> int:
    """Resolve the saved session by id and continue its brainstorm. Thin over
    `reviewlib.sessions.resume_session`; resolves the panel/moderator (saved unless
    overridden) and reports the clean errors (unknown id / ambiguous prefix / already
    complete) with actionable messages + meaningful exit codes."""
    from . import backends, sessions as _sessions

    try:
        sess = _sessions.find_session(ns.resume)
    except _sessions.AmbiguousSessionError as exc:
        print(f"[review sessions] {exc}", file=sys.stderr, flush=True)
        return 2
    if sess is None:
        print(f"[review sessions] no session with id '{ns.resume}'. "
              "Run `review sessions -a` to list available ids.", file=sys.stderr, flush=True)
        return 2

    cwd = _effective_cwd(ns.cwd)
    # Panel: explicit -m override > the saved session panel (dropping unreachable
    # backends so a vanished key never aborts) > whatever the saved panel was.
    explicit_models = _split_models(ns.model)
    if explicit_models:
        models = explicit_models
    else:
        models = [m for m in sess.panel if backends.backend_available(m)] or sess.panel or list(DEFAULT_MODELS)
    # Moderator: explicit --moderator override > the saved session moderator > picked.
    # The log records the moderator FALLBACK CHAIN joined with `>` (e.g. `claude:..>codex`),
    # so the saved value must be SPLIT back into candidates — passing the whole `a>b>c`
    # string as one explicit seed would make `pick_moderators` try an invalid single
    # backend id before falling back. Take the FIRST (highest-priority) saved candidate as
    # the explicit seed; pick_moderators rebuilds the rest of the priority order.
    saved_mod = (sess.moderator.split(">")[0].strip() if sess.moderator else "")
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

    print(f"[review sessions] resuming '{sess.session_id}' ({sess.status}, "
          f"{sess.completed_rounds} round(s) done): {sess.topic}", file=sys.stderr, flush=True)

    # Panel modes announce their live-log paths (the resumed rounds stream to the log).
    backends._ANNOUNCE_LOGS = True
    try:
        return _sessions.resume_session(
            sess, models=models, cwd=cwd, timeout=timeout,
            moderators=moderators, diff=diff, force=ns.force,
        )
    except _sessions.SessionAlreadyCompleteError as exc:
        # A refused resume (already-complete, no --force) did NO requested work. Return the
        # same non-zero code the unknown/ambiguous-id paths use so scripts and hooks can
        # tell a refusal from a real resume — exit 0 here was indistinguishable from success
        # (codex P2: CTO sided with the bot over the prior "intentional" exit-0 choice).
        print(f"[review sessions] {exc}", file=sys.stderr, flush=True)
        return 2


def _spec_web(argv: list[str]) -> int:
    """`review spec-web <spec.md> [--host H] [--port N] [--seed f.json] [--open]`.

    Interactive web server to review a markdown spec: select text -> ask a question /
    comment, accumulate a pending batch, submit the review (delivered to the launching
    agent), answer inline. Reusable for ANY spec. See reviewlib.specweb for the full design.

    Also dispatches the `reply` subcommand: `review spec-web reply <comment-id> <answer>`
    lets the AGENT answer a reviewer's question — it threads the reply into the store
    (shown in the UI) and delivers it to the user via tg.
    """
    if argv and argv[0] == "reply":
        return _spec_web_reply(argv[1:])

    parser = argparse.ArgumentParser(prog="review spec-web", description="Interactive web reviewer for a markdown spec.")
    parser.add_argument("spec", help="path to the spec markdown file")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1; use 0.0.0.0 to expose over Tailscale)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: a free ephemeral port)")
    parser.add_argument("--seed", metavar="FILE", default=None, help="import an initial review thread from a JSON file before serving")
    parser.add_argument("--exit-on-submit", dest="exit_on_submit", action="store_true",
                        help="stop the server after the first Submit (the blocking call returns once the review is delivered)")
    parser.add_argument("--open", dest="open_browser", action="store_true", help="open the URL in a browser on startup")
    parser.add_argument("--verbose", action="store_true", help="verbose request logging")
    ns = parser.parse_args(argv)

    from .specweb.server import run_specweb

    spec = Path(ns.spec).expanduser()
    if not spec.is_file():
        print(f"[review spec-web] spec not found: {spec}", file=sys.stderr)
        return 1

    return run_specweb(
        spec,
        host=ns.host,
        port=ns.port,
        open_browser=ns.open_browser,
        seed=ns.seed,
        verbose=ns.verbose,
        exit_on_submit=ns.exit_on_submit,
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
    parser.add_argument("comment_id", help="the id of the comment/question to answer (from the structured review)")
    parser.add_argument("answer", help="the answer text")
    parser.add_argument("--spec", required=True, metavar="FILE", help="path to the spec markdown file (the store is keyed per spec)")
    parser.add_argument("--no-tg", action="store_true", help="do not deliver the reply to Telegram (UI only)")
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
        print(f"[review spec-web reply] unknown comment id: {ns.comment_id}", file=sys.stderr)
        return 1
    print(f"[review spec-web reply] replied to {ns.comment_id} (now {rec.get('status')}); shown in the spec-web UI.", flush=True)

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
        print("[review spec-web reply] tg not on PATH — reply saved to the UI only (no Telegram).", flush=True)
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
    lines.append(f"{'Question' if kind == 'question' else 'Remark'}: {_clip(question, 600)}")
    lines.append(f"Agent answer: {_clip(answer, 3000)}")
    message = "\n".join(lines)
    try:
        proc = subprocess.run(
            [exe, "--tag", "ANSWER", "--title", f"Spec-web reply — {spec.name}", message],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0:
            print("[review spec-web reply] delivered to Telegram via tg.", flush=True)
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            print(f"[review spec-web reply] tg delivery failed (exit {proc.returncode}): {err}", flush=True)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[review spec-web reply] tg delivery error: {exc}", flush=True)


def _run_mode_with_stats(mode: str, pool_models: list[str], dispatch, models_after=None) -> int:
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
    """
    import time

    pool_size = len(pool_models)
    announce_eta(mode, pool_size)
    begin_call_tally()
    started = datetime.now(timezone.utc)
    start = time.monotonic()
    try:
        return dispatch()
    finally:
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
            record_run(
                mode=mode,
                models=recorded_models,
                duration_seconds=elapsed,
                ok_count=ok_count,
                fail_count=fail_count,
                started=started,
            )


# Subcommands that run a PERSISTENT server until Ctrl-C (`review dashboard`,
# `review spec-web`) — these are intentionally long-lived and must NOT be bounded by
# the run backstop, which would otherwise kill the server after the ceiling (or almost
# immediately under a lowered $REVIEW_BACKSTOP_SECONDS). The backstop is for the
# bounded review/model RUN paths only.
_SERVER_SUBCOMMANDS = frozenset({"dashboard", "spec-web"})


def _is_persistent_server_invocation(argv: list[str]) -> bool:
    """True when argv starts a PERSISTENT server (`dashboard`, `spec-web <spec>`) that runs
    until Ctrl-C and so must bypass the `-o` tee + the run backstop. The short-lived
    `review spec-web reply …` is NOT a server — it returns immediately — so it is excluded
    and goes through the normal tee/backstop path like any instant subcommand."""
    if not argv or argv[0] not in _SERVER_SUBCOMMANDS:
        return False
    if argv[0] == "spec-web" and len(argv) > 1 and argv[1] == "reply":
        return False
    return True


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
_VALUE_TAKING_OPTS = frozenset({
    "-m", "--model", "-C", "--cwd", "-o", "--output", "--prompt", "--timeout",
    "--pool", "--moderator", "--rounds", "--max-rounds",
    "--visual", "--before", "--intent", "--expect", "--check",
    "--vision-timeout", "--project",
    # `review spec-web reply <id> <answer> --spec <path>`: the value after --spec is a spec
    # path that could look like an option (e.g. `--spec -odd-name.md`); list it so the `-o`
    # pre-scan never steals it.
    "--spec",
})


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
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            # End of options: keep `--` and everything after it untouched.
            rest.extend(argv[i:])
            break
        # If the PREVIOUS token is a value-taking option (space form), THIS token is its
        # value — pass it through, never read it as the output flag.
        if i > 0 and argv[i - 1] in _VALUE_TAKING_OPTS:
            rest.append(tok)
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
        i += 1
    return out, rest


# Strip ANSI/VT100 escape sequences from the captured text before it lands in the `-o`
# file. review-cli emits plain text today, but a backend's passed-through output (or
# future coloured formatting on a TTY — the tee delegates isatty to the real stdout)
# could carry escapes; the saved file must stay clean markdown regardless, while the
# LIVE stream keeps whatever it had. Covers both CSI sequences (`\x1b[ … m`, colours /
# cursor moves) and OSC sequences (`\x1b] … BEL/ST`, e.g. hyperlinks / window titles).
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"        # CSI: ESC [ … final-byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: ESC ] … (BEL | ST)
)


def _write_output_file(path: Path, text: str) -> None:
    """Persist captured stdout to `path` via Python `open(...,"w")` — which bypasses
    the shell entirely, so it NEVER trips zsh `noclobber` the way `review … > FILE`
    does (the bug this flag exists to kill). ANSI escape sequences are stripped so the
    file is clean text even if the live stream was coloured. Parent dirs are created;
    an existing file is overwritten (that is the point). A bad path raises a clear
    OSError that the caller turns into a non-zero exit with an actionable message."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_ANSI_ESCAPE_RE.sub("", text), encoding="utf-8")


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

    # A REMOVED flag (--mcp/--ln, or a removed mode flag) OR the removed `review review`
    # SUBCOMMAND verb is a USAGE error — it must behave like argparse's own usage errors
    # w.r.t. `-o`: print the structured error and exit WITHOUT writing the `-o` file.
    # Rejecting it INSIDE `_dispatch` only `return`s 2, which the tee path below treats as
    # "the dispatch completed" and would persist the (empty) captured stdout — truncating a
    # pre-existing `-o` target (codex P1/P2). Reject it here, before the tee is armed, so no
    # write happens. Both are pure argv pre-scans; the later calls in `_dispatch` are then
    # harmless no-ops.
    for _reject in (_reject_removed_flags, _reject_removed_subcommand):
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
                print(f"[review-cli] -o: could not write {output_path}: {exc}",
                      file=sys.stderr, flush=True)
    return 1 if write_error is not None else rc




def _add_shared_options(parser: argparse.ArgumentParser, *, mode: ModeSpec | None) -> None:
    """Add the SHARED options every mode subcommand understands. `--list-defaults` /
    `--show-board` / `--pool` are review/board meta-flags that stay available on the
    default (review) parser; the panel/brainstorm/visual-only flags are available to the
    relevant modes too (a flag a mode ignores is harmless). Mode-UNIQUE arguments (the
    positional question/topic) are added by the mode's own `add_arguments`."""
    parser.add_argument("-m", "--model", action="append", default=[], help="model/backend to run; repeat or comma-separate")
    parser.add_argument("-C", "--cwd", default=".", help="repository directory")
    parser.add_argument(
        "-o", "--output", metavar="FILE", default=None,
        help=(
            "write the result to FILE via Python (creates parent dirs, overwrites) "
            "while still printing to stdout. Use this instead of `review … > FILE`, "
            "which fails under zsh noclobber."
        ),
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="review prompt (review mode)")
    # --diff is an explicit, composable alias for the working-tree diff. It is the
    # DEFAULT for the review mode (which always reviews the diff) and an OPTIONAL
    # grounding source for brainstorm — `review brainstorm "…" --diff` reads the
    # working-tree diff as context. --staged is its staged counterpart.
    parser.add_argument("--diff", action="store_true", help="use the working-tree diff (default for review; optional grounding for brainstorm)")
    parser.add_argument("--staged", action="store_true", help="use the staged diff (git diff --cached) instead of the working-tree diff")
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="per-call timeout seconds (default 1200 for review, 240 for panel modes)",
    )
    parser.add_argument("--list-defaults", action="store_true", help="print default models and exit")
    parser.add_argument("--show-board", action="store_true", help="print the active reviewer board (model -> role, availability) and exit")
    parser.add_argument(
        "--pool", type=int, default=DEFAULT_POOL_SIZE, metavar="N",
        help=(
            f"how many of the board's seats to run (default {DEFAULT_POOL_SIZE}); the "
            "first N seats participate, the rest are kept in reserve. The board is "
            "never off — --pool only sizes it. N<=0 means all seats."
        ),
    )
    parser.add_argument("--moderator", default=None, help="moderator backend for quorum / brainstorm")
    # --rounds / --max-rounds are brainstorm-only and added by the brainstorm mode's own
    # add_arguments (so `review just-ask --rounds 5` correctly errors). They are still in
    # _VALUE_TAKING_OPTS so the mode-agnostic `-o` pre-scan treats them as value-taking.
    # --visual is a COMPOSABLE flag, NOT a mode: it rides any subcommand (diff /
    # brainstorm / just-ask / quorum). On `review diff --visual <img>` with NO diff present
    # it runs the standalone verdict pipeline (§3); with a diff it is the companion review.
    parser.add_argument("--visual", metavar="IMAGE", help="image to verify/attach; rides any subcommand (e.g. `review diff --visual`; standalone verdict pipeline when no diff)")
    parser.add_argument("--before", metavar="IMAGE", help="baseline image for diff-aware judgement / no-effect bypass")
    parser.add_argument("--intent", metavar="TEXT", help="free-text edit intent (untrusted; may only tighten the contract)")
    parser.add_argument("--expect", metavar="KIND", help="expectation kind: zero-diff|move|resize|style|wrap|insert|delete|text")
    parser.add_argument("--check", action="append", default=[], metavar="NAME", help="force-activate a visual module by name (repeatable)")
    parser.add_argument("--json", action="store_true", help="emit the structured visual verdict as JSON")
    parser.add_argument("--strict", action="store_true", help="exit 10 on a blocking visual verdict (gate use)")
    parser.add_argument("--no-ai", action="store_true", help="run cvGate only (no vision call) — fast CI smoke / offline")
    parser.add_argument("--no-local-model", action="store_true", help="disable the Stage-2a local pre-classifier (known-good cache cost-saver); flow = cvGate → vision (§3.1a)")
    parser.add_argument("--vision-timeout", type=int, default=60, help="per vision-call timeout seconds (default 60)")
    parser.add_argument("--project", default=None, help="project root for per-project visual modules (default --cwd)")
    if mode is not None and mode.add_arguments is not None:
        mode.add_arguments(parser)


def _subcommand_epilog() -> str:
    return "subcommands:\n" + "\n".join(
        f"  {m.subcommand:<11} {m.summary}" for m in iter_modes()
    ) + (
        "\n  dashboard   local web dashboard over review-cli runs"
        "\n  sessions    list / resume brainstorm sessions (-a all, -s <id> resume)"
        "\n  spec-web    interactive web reviewer for a markdown spec"
        "\n  install-skill / install-commit-hook / register-module"
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
            "Run read-only code reviews / AI panels across multiple model backends. "
            "Everything is a SUBCOMMAND: `review diff` (review the git diff), "
            "`review brainstorm`, `review just-ask`, `review quorum`. A bare `review` "
            "(no subcommand) prints this help — it does NOT run a diff review; use "
            "`review diff` for that."
        ),
        epilog=_subcommand_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_shared_options(parser, mode=None)
    return parser


def _build_mode_parser(mode: ModeSpec) -> argparse.ArgumentParser:
    """Build the argparse surface for an EXPLICIT `review <mode> …` subcommand (its prog
    is `review <subcommand>` and it carries the mode's own positional/flags)."""
    parser = argparse.ArgumentParser(
        prog=f"review {mode.subcommand}", description=mode.summary,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_shared_options(parser, mode=mode)
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
            print(
                f"review: `{bare}` is no longer a flag — it is now the `{sub}` subcommand.\n"
                f"  use:  review {sub} \"<your text>\" [options]\n"
                f"  (modes are subcommands now: brainstorm / just-ask / quorum; "
                f"run `review --help`)",
                file=sys.stderr, flush=True,
            )
            return 2
        removed = REMOVED_FLAGS.get(bare)
        if removed is not None:
            print(
                f"review: `{bare}` was removed and is no longer accepted.\n"
                f"  why:  {removed.reason}\n"
                f"  fix:  {removed.fix}",
                file=sys.stderr, flush=True,
            )
            return 2
    return None


def _reject_removed_subcommand(argv: list[str]) -> int | None:
    """Reject the renamed-away SUBCOMMAND verb `review review` (the diff review is `review
    diff` now) with a one-line `review diff` pointer + the stable usage code (2), else None.
    A pure argv check (argv[0] only) so it can run in `main()` BEFORE the `-o` tee is armed —
    a usage error must NOT write/truncate the `-o` file (codex P1), exactly like the removed
    FLAGS. The later call in `_dispatch` is then a harmless no-op."""
    if argv and argv[0] in REMOVED_SUBCOMMANDS:
        replacement = REMOVED_SUBCOMMANDS[argv[0]]
        print(
            f"review: `review {argv[0]}` is no longer a subcommand — the diff review is "
            f"now `review {replacement}`.\n"
            f"  use:  review {replacement} [options]\n"
            f"  (run `review --help` for all subcommands)",
            file=sys.stderr, flush=True,
        )
        return 2
    return None


def _dispatch(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv == ["install-skill"]:
        return install_skill()
    if argv == ["install-commit-hook"]:
        return install_commit_hook()
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
    # Per-project visual-module subcommands (§6). Kept as bare subcommands (like
    # install-skill) so they don't clutter the main review argparse surface. Project
    # modules load by default (trust-by-default); trust-module only pins under the
    # opt-in REVIEW_UNTRUSTED_MODULES=1 guard (the rare untrusted-repo case).
    if argv and argv[0] == "trust-module":
        from .features.visual.registry import trust_module

        if len(argv) < 2:
            print("usage: review trust-module <name> [--project DIR]  (only needed under REVIEW_UNTRUSTED_MODULES=1)", file=sys.stderr)
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
    is_subcommand = bool(argv) and not argv[0].startswith("-") and argv[0] in known_subcommands()
    if is_subcommand:
        mode = get_mode(argv[0])
        assert mode is not None  # known_subcommands() guarantees it
        rest = argv[1:]
        parser = _build_mode_parser(mode)
    else:
        # No recognized subcommand. Parse the meta flags off the top-level parser; if none
        # short-circuit below, fall through to the HELP path (no implicit diff review).
        mode = diff_mode()  # only used by the meta-flag handlers (--list-defaults / --show-board)
        rest = argv
        parser = _build_top_level_parser()

    args = parser.parse_args(rest)

    config = load_config()

    # `models:` from config, stripped + alias-expanded + blanks dropped (same rule as
    # _split_models for -m). An "effectively empty" list — absent, or only
    # blank/whitespace entries — is NOT a real preference: it must NOT count as a
    # configured models list (else it would disable the board AND feed blank model
    # names to the panel). Computed up-front so --list-defaults reports the SAME
    # effective, normalized list the review path actually uses.
    config_models = _split_models(config.get("models") or [])

    if args.list_defaults:
        effective = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        print("\n".join(effective))
        return 0

    if args.show_board:
        # Resolve cwd up front so the agentic/diff-only labels reflect whether opencode
        # would actually run in a real repo for THIS -C (it's diff-only outside a repo).
        return _show_board(config, args.pool, _effective_cwd(args.cwd))

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
        piped_diff = (not rest) and (_read_stdin_if_piped() is not None)
        usage_error = bool(rest) or piped_diff
        parser.print_help(sys.stderr if usage_error else sys.stdout)
        if piped_diff:
            print(
                "\nreview: a diff was piped in but no subcommand given. The diff review is "
                "now `review diff` (a bare `review` no longer runs one). "
                "Run `git diff | review diff`.",
                file=sys.stderr, flush=True,
            )
            raise SystemExit(2)
        if rest:
            print(
                "\nreview: no subcommand given. The diff review is now `review diff` "
                "(a bare `review` no longer runs one). Run `review diff [options]`.",
                file=sys.stderr, flush=True,
            )
            raise SystemExit(2)
        raise SystemExit(0)

    # Suppress the "reviewing it as-is" non-repo warning on the REVIEW-mode required-diff
    # path: there a non-repo hard-fails via `_fail_not_a_repo` (the authoritative message),
    # so the "as-is" promise would contradict it. The no-git modes (panel) and the
    # tolerant `--visual` review (which DOES proceed as-is) keep the warning.
    _review_required = mode.name == "review" and (args.staged or args.visual is None)
    cwd = _effective_cwd(args.cwd, warn=not _review_required)
    explicit_models = _split_models(args.model)
    is_brainstorm = mode.name == "brainstorm"
    # A "panel mode" is any non-review mode (brainstorm / just-ask / quorum): the diff is
    # OPTIONAL context for it, its calls are long-running (announce live-log paths), and
    # its per-call timeout default is the shorter PANEL_TIMEOUT_DEFAULT.
    panel_mode = mode.name != "review"
    # Precedence: explicit -m > config > code default. Brainstorm prefers
    # config.brainstorm_models and drops unreachable backends gracefully (so a
    # missing GEMINI_API_KEY never aborts the run). Explicit -m is honored as-is.
    if explicit_models:
        models = explicit_models
    elif is_brainstorm:
        src = _split_models(config.get("brainstorm_models") or []) or config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        models = [m for m in src if backends.backend_available(m)]
        if not models:
            models = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
    else:
        models = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]

    visual_mode = args.visual is not None
    timeout = args.timeout if args.timeout is not None else (PANEL_TIMEOUT_DEFAULT if panel_mode else 1200)

    # Reviewer board (HYP-741): the default plain-review panel assigns each model its
    # own role/lens. Precedence is COST-SAFETY first — the board runs only when the user
    # expressed NO model preference at all:
    #   explicit -m  >  explicit `models:` in config.yaml  >  default board.
    # So a configured `models:` gets exactly those (the flat panel), NOT the board. The
    # board applies on the DEFAULT diff review (no panel mode) with neither -m nor config
    # models. The board is NEVER disabled — `--pool N` only sizes how many of its seats
    # run (default 4 of the 8-seat board; the rest are a reserve). `use_board` is a cheap
    # boolean gate computed now; the actual load_board + cost-safety validation (and the
    # --pool slice) runs LATER (validate_board, below) — after the standalone-visual path
    # has had its chance to short-circuit, so a malformed `board:` never blocks the
    # board-unrelated standalone `review --visual` pipeline (codex P2). It still fires
    # BEFORE the COMPANION visual fan-out, so a doomed config never spends a paid vision
    # call.
    has_config_models = bool(config_models)  # filtered above: blanks-only counts as none
    use_board = not panel_mode and not explicit_models and not has_config_models
    board: list | None = None
    board_validated = False

    def validate_board() -> int | None:
        """Resolve + validate the FULL priority-ordered reviewer board for the default
        review path, once. The board is loaded whole (NOT sliced to --pool here): the
        failover pool path (mode_review) does the startup failover — selecting the top
        `args.pool` AVAILABLE seats by priority — and keeps the rest as the reserve that
        backfills a seat which fails mid-run. Returns an exit code (2) on an all-malformed
        `board:` config, else None. No-op when the board does not apply (panel mode / -m /
        config models)."""
        nonlocal board, board_validated
        if board_validated or not use_board:
            return None
        board_validated = True
        try:
            board = load_board(config)
        except BoardConfigError as exc:
            print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
            return 2
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
    diff = _read_stdin_if_piped()
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
    needs_diff = (args.staged or (not panel_mode and not visual_mode)) and not is_brainstorm
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
    elif diff is None and visual_mode and not panel_mode:
        # --visual riding the review mode: probe the working-tree diff to decide
        # companion-vs-standalone, but tolerate "no diff / not a git repo".
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

    # --- --visual composition (§2.1). Build the visual context ONCE; thread it into
    # whichever consumer runs. cvGate fires here regardless of mode (a broken render
    # is flagged before any model call). -----------------------------------------
    visual_ctx = None
    if visual_mode:
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
                models=models,
                no_ai=args.no_ai,
                # Stage-2a cost-saver default ON; --no-local-model OR `local_model: false`
                # in config.yaml disables it (CLI flag wins over config).
                local_model=(not args.no_local_model) and (config.get("local_model", True) is not False),
                vision_timeout=args.vision_timeout,
                as_json=args.json,
                strict=args.strict,
                # Per-project module discovery defaults to the CLI cwd (-C), NOT the
                # process cwd, so `review --visual shot.png -C <repo>` finds
                # <repo>/.review/visual-modules.json (codex P2).
                project=args.project or str(cwd),
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
        visual_ctx = build_mode_visual_context(
            Path(args.visual).expanduser(),
            before=Path(args.before).expanduser() if args.before else None,
            expect=args.expect,
            intent=args.intent,
            models=[] if args.no_ai else models,
            requested_checks=list(args.check),
            vision_timeout=args.vision_timeout,
        )
        # The cvGate pre-filter BLOCKS the companion run on an unambiguously-broken
        # render (codex P2): a blank/unreadable/error-overlay image must short-circuit
        # the mode, not merely be mentioned in prompt text (else `review --staged
        # --visual blank.png` would run the review and stamp success). Exit 10 under
        # --strict (the gate/hook block code), else a non-zero advisory exit.
        if visual_ctx.prefilter_verdict == "rollback":
            print(f"[review --visual] ROLLBACK (pre-filter, mode blocked): {visual_ctx.prefilter_reason}")
            # An unreadable/missing image is a USAGE error (exit 1), matching the
            # standalone exit-code map — scripts/hooks rely on the distinction between
            # "unreadable input" (1) and "blocking content verdict under --strict" (10).
            if "unreadable" in visual_ctx.prefilter_reason:
                return 1
            return 10 if args.strict else 1

    # Build the resolved ModeContext handed to the mode's handler (thin over the lib).
    # `with_visual` folds the --visual companion context into the mode's prompt/topic
    # (identity when there is none). Moderators are resolved for the panel/brainstorm
    # modes; the review handler ignores them.
    def _with_visual_text(text: str) -> str:
        return _with_visual(text, visual_ctx)

    moderators = pick_moderators(args.moderator, models) if panel_mode else []
    ctx = ModeContext(
        args=args, models=models, diff=diff, cwd=cwd, timeout=timeout,
        with_visual=_with_visual_text, visual_ctx=visual_ctx, moderators=moderators,
        extra={"diff_from_stdin": diff_from_stdin},
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
            mode.stats_mode, brainstorm_pool(models),
            lambda: mode.handler(ctx),
        )

    if mode.name != "review":
        # just-ask / quorum: a flat multi-model panel; pool_size == len(models).
        return _run_mode_with_stats(
            mode.stats_mode, models, lambda: mode.handler(ctx),
        )

    # The review mode. Validate the board now if it wasn't already (the no-visual path);
    # an all-malformed `board:` exits 2 before the panel runs. The --visual companion
    # context folds into each per-reviewer prompt via args.prompt (handler's with_visual).
    rc = validate_board()
    if rc is not None:
        return rc
    if board:
        # Failover pool. The PLANNED pool keys the up-front ETA: the top `--pool`
        # AVAILABLE seats by priority (startup failover — the same selection mode_review
        # makes). The RECORDED models come from the failover outcome (the seats that
        # actually produced verdicts, after any mid-run backfill), via outcome_sink.
        planned_pool, _ = split_pool_reserve(
            board, args.pool, lambda r: backends.backend_available(r.model),
        )
        eta_models = [r.model for r in planned_pool]
        outcome_sink: list = []
        ctx.extra.update(board=board, pool_size=args.pool, outcome_sink=outcome_sink)

        def _ran_models() -> list[str]:
            # The BARE model ids that produced verdicts (a backfilled reserve under its
            # real id), so the stat record keys on what actually ran — not labels.
            return outcome_sink[0].usable_models if outcome_sink else []

        return _run_mode_with_stats(
            mode.stats_mode, eta_models, lambda: mode.handler(ctx),
            models_after=_ran_models,
        )
    # Flat review path (no board): ctx.extra has no "board" key, so the handler reads
    # board=None and takes the legacy flat call shape.
    return _run_mode_with_stats(
        mode.stats_mode, models, lambda: mode.handler(ctx),
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
        try:
            backends._which("claude-p")
            return True
        except RuntimeError:
            return False
    return False


def _show_board(config: dict, pool_size: int = DEFAULT_POOL_SIZE, cwd: Path | None = None) -> int:
    """Print the active reviewer board as a PRIORITY-ordered failover pool.

    The board (config.yaml `board:` if set, else the built-in DEFAULT_BOARD) is listed
    in priority order (strongest first). Each seat shows its display name, role, model,
    backend availability (key/CLI present), and a failover TIER:
      * `pool`    — one of the top-`pool_size` AVAILABLE seats that a plain `review`
                    actually runs (startup failover: a higher-priority but UNAVAILABLE
                    seat is skipped and the next available one is pulled into the pool);
      * `reserve` — available, below the pool cut; backfills a pool seat that fails
                    mid-run (mid-run failover);
      * `unavail` — backend not reachable right now; it can't sit in the pool, but a
                    run-time "unavailable" reply still triggers a reserve backfill.
    Read-only — no model is called, no key is printed."""
    try:
        board = load_board(config)
    except BoardConfigError as exc:
        print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
        return 2
    source = "config.yaml (board:)" if isinstance(config.get("board"), list) and config.get("board") else "default"
    # The LIVE pool/reserve split is by PRIORITY + AVAILABILITY (the same split the
    # failover review path makes), not by raw seat index — an unavailable top seat is
    # skipped so the pool fills from the next available priority. Probe each seat ONCE by
    # index (handles a board with the same model in two seats), then walk the available
    # seats in priority order, tagging the first `pool_filled` `pool` and the rest
    # `reserve`. `pool_filled` is how many of the AVAILABLE seats the pool size selects.
    avail = [backends.backend_available(r.model) for r in board]
    available_count = sum(avail)
    pool_filled = _effective_pool_size(available_count, pool_size)
    sized = " (sized by --pool)" if pool_size != DEFAULT_POOL_SIZE else ""
    pool_target = "all available" if pool_size <= 0 else pool_size
    print(f"Reviewer board ({len(board)} seats, priority-ordered, source: {source}; "
          f"live pool = top {pool_target} AVAILABLE by priority{sized}, "
          f"{pool_filled} filled, the rest reserve — size with --pool N):\n")
    name_w = max((len(r.display) for r in board), default=0)
    role_w = max((len(r.role or "general") for r in board), default=0)
    # Resolve the repo bit ONCE (a single git rev-parse) for the opencode scope label,
    # rather than per seat in the loop.
    cwd_is_repo = backends._opencode_runs_in_repo(cwd or Path.cwd())
    seen_available = 0  # how many AVAILABLE seats walked so far (priority order)
    for index, reviewer in enumerate(board):
        available = avail[index]
        status = "available" if available else "SKIPPED (no key/CLI)"
        role = (reviewer.role or "general").ljust(role_w)
        if not available:
            tier = "unavail"
        else:
            tier = "pool   " if seen_available < pool_filled else "reserve"
            seen_available += 1
        prio = f"#{index + 1}"
        scope = "agentic" if _seat_reads_repo(reviewer.model, cwd_is_repo) else "diff-only"
        print(f"  {prio:>3}  [{tier}]  {reviewer.display.ljust(name_w)}  {role}  "
              f"{reviewer.model}  [{status}]  ({scope})")
    print("\nScope: `agentic` seats (codex / opencode / claude-CLI) run read-only in the "
          "real repo and can read any project file; `diff-only` seats (gemini / z.ai / "
          "commandcode / claude-API) are stateless HTTP calls that see only the diff.")
    print(f"\nA plain `review diff` runs the top {DEFAULT_POOL_SIZE} AVAILABLE seats by "
          f"priority (--pool {DEFAULT_POOL_SIZE}); a higher-priority seat that is "
          f"unavailable (or fails mid-run) is replaced by the next-priority reserve so the "
          f"pool keeps {DEFAULT_POOL_SIZE} working reviewers. `--pool N` sizes the pool; "
          f"`--pool 0` runs all available seats.")
    if not all(avail):
        print("Unavailable reviewers drop out and are backfilled from the reserve; the "
              "board degrades gracefully only if the reserve is exhausted. commandcode "
              "reviewers need COMMANDCODE_API_KEY, gemini needs GEMINI_API_KEY, "
              "codex/claude need their CLI on PATH.")
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
