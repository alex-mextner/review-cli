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
from .modes.just_ask import mode_just_ask
from .modes.quorum import mode_quorum
from .modes.review import mode_review
from .panel import begin_call_tally, end_call_tally, pick_moderators
from .process import _run
from .stats import announce_eta, record_run


def _git_diff(cwd: Path, staged: bool) -> str:
    args = ["git", "diff", "--no-ext-diff"]
    if staged:
        args.append("--cached")
    proc = _run(args, cwd=cwd, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git diff failed")
    return proc.stdout


def _read_stdin_if_piped() -> str | None:
    if sys.stdin.isatty():
        return None
    data = sys.stdin.read()
    return data if data else None


def _effective_cwd(raw: str) -> Path:
    """Resolve the review cwd, preferring the enclosing git repository root.

    Agents commonly invoke `review` from a scratch / temp directory and forget
    -C, so the diff and the claude-p workspace silently point at the wrong place
    (often /tmp) and the review is empty or about the wrong code. Resolve to the
    git toplevel when inside a repo (also robust to being run from a subdir), and
    warn loudly when the cwd is not a git repo at all so the mistake is visible
    instead of producing a misleading review. Pass -C <project-root> to be exact.
    """
    resolved = Path(raw).expanduser().resolve()
    if resolved.is_dir():
        proc = _run(["git", "rev-parse", "--show-toplevel"], cwd=resolved, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
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


def _spec_web(argv: list[str]) -> int:
    """`review spec-web <spec.md> [--host H] [--port N] [--seed f.json] [--export] [--open]`.

    Interactive web server to review a markdown spec: select text -> ask a question /
    comment, accumulate a pending batch, submit the review, answer inline. Reusable for
    ANY spec. See reviewlib.specweb for the full design.
    """
    parser = argparse.ArgumentParser(prog="review spec-web", description="Interactive web reviewer for a markdown spec.")
    parser.add_argument("spec", help="path to the spec markdown file")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default 127.0.0.1; use 0.0.0.0 to expose over Tailscale)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default: a free ephemeral port)")
    parser.add_argument("--seed", metavar="FILE", default=None, help="import an initial review thread from a JSON file before serving")
    parser.add_argument("--export", action="store_true", help="print the review as markdown to stdout and exit (no server)")
    parser.add_argument("--open", dest="open_browser", action="store_true", help="open the URL in a browser on startup")
    parser.add_argument("--verbose", action="store_true", help="verbose request logging")
    ns = parser.parse_args(argv)

    from .specweb.server import run_specweb
    from .specweb.store import SpecStore

    spec = Path(ns.spec).expanduser()
    if not spec.is_file():
        print(f"[review spec-web] spec not found: {spec}", file=sys.stderr)
        return 1

    if ns.export:
        # --export dumps the persisted review as markdown and exits (after an optional
        # seed import), so it doubles as the CLI export path.
        if ns.seed:
            import json as _json

            try:
                payload = _json.loads(Path(ns.seed).expanduser().read_text(encoding="utf-8"))
                replace = bool(payload.get("replace")) if isinstance(payload, dict) else False
                SpecStore(spec).import_thread(payload, replace=replace)
            except (OSError, ValueError) as exc:
                print(f"[review spec-web] bad seed: {exc}", file=sys.stderr)
                return 1
        sys.stdout.write(SpecStore(spec).export_markdown())
        return 0

    return run_specweb(
        spec,
        host=ns.host,
        port=ns.port,
        open_browser=ns.open_browser,
        seed=ns.seed,
        verbose=ns.verbose,
    )


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
    "--pool", "--moderator", "--rounds", "--max-rounds", "--just-ask", "--quorum",
    "--brainstorm", "--visual", "--before", "--intent", "--expect", "--check",
    "--vision-timeout", "--project",
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

    # The persistent SERVER subcommands stream until Ctrl-C — capturing/teeing their
    # output to a single `-o` file makes no sense (and the file would only be written
    # on shutdown), so `-o` is ignored for them and they bypass both the tee and the
    # backstop exactly as before.
    if raw and raw[0] in _SERVER_SUBCOMMANDS:
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
    parser = argparse.ArgumentParser(description="Run read-only code reviews across multiple model backends.")
    parser.add_argument("-m", "--model", action="append", default=[], help="model/backend to run; repeat or comma-separate")
    parser.add_argument("-C", "--cwd", default=".", help="repository directory")
    parser.add_argument(
        "-o", "--output", metavar="FILE", default=None,
        help=(
            "write the review result to FILE via Python (creates parent dirs, "
            "overwrites) while still printing to stdout. Use this instead of "
            "`review … > FILE`, which fails under zsh noclobber."
        ),
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="review prompt")
    parser.add_argument("--staged", action="store_true", help="review staged diff instead of unstaged diff")
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="per-call timeout seconds (default 1200 for review, 240 for panel modes)",
    )
    parser.add_argument("--list-defaults", action="store_true", help="print default models and exit")
    parser.add_argument("--show-board", action="store_true", help="print the active reviewer board (model -> role, availability) and exit")
    parser.add_argument(
        "--pool",
        type=int,
        default=DEFAULT_POOL_SIZE,
        metavar="N",
        help=(
            f"how many of the board's seats to run (default {DEFAULT_POOL_SIZE}); the "
            "first N seats participate, the rest are kept in reserve. The board is "
            "never off — --pool only sizes it. N<=0 means all seats."
        ),
    )
    parser.add_argument("--moderator", default=None, help="moderator backend for --quorum/--brainstorm")
    parser.add_argument("--rounds", type=int, default=5, help="brainstorm minimum rounds (min & default 5)")
    parser.add_argument("--max-rounds", type=int, default=8, help="brainstorm hard cap on rounds")
    panel = parser.add_mutually_exclusive_group()
    panel.add_argument("--just-ask", metavar="QUESTION", help="ask all backends a plain question, no diff required")
    panel.add_argument("--quorum", metavar="QUESTION", help="experts answer + moderator finds quorum/disagreement")
    panel.add_argument("--brainstorm", metavar="TOPIC", help="multi-round persona ideation with a moderator")
    # --visual is a COMPOSABLE flag, NOT a mode: it is deliberately OUTSIDE the
    # mutually-exclusive panel group so it can combine with any mode (§2.1).
    parser.add_argument("--visual", metavar="IMAGE", help="image to verify/attach; composable with any mode (alone = the verdict pipeline)")
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
    args = parser.parse_args(argv)

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

    cwd = _effective_cwd(args.cwd)
    explicit_models = _split_models(args.model)
    # Precedence: explicit -m > config > code default. Brainstorm prefers
    # config.brainstorm_models and drops unreachable backends gracefully (so a
    # missing GEMINI_API_KEY never aborts the run). Explicit -m is honored as-is.
    if explicit_models:
        models = explicit_models
    elif args.brainstorm is not None:
        src = _split_models(config.get("brainstorm_models") or []) or config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
        models = [m for m in src if backends.backend_available(m)]
        if not models:
            models = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]
    else:
        models = config_models or [_expand_alias(x) for x in DEFAULT_MODELS]

    panel_mode = args.just_ask or args.quorum or args.brainstorm
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
    # backend's live-log path to stderr; the plain review path stays quiet.
    if panel_mode:
        backends._ANNOUNCE_LOGS = True

    # Diff acquisition. Panel modes treat the diff as optional context. The default
    # review (no panel mode) requires a diff. With --visual + the DEFAULT review, the
    # diff still drives the routing (§2.1): a present diff → the diff-review companion,
    # an absent diff → the standalone pipeline — so we MUST still try to discover it,
    # but a missing diff / non-repo must degrade to standalone rather than abort.
    diff = _read_stdin_if_piped()
    # --brainstorm treats the diff as OPTIONAL grounding context even with --staged, so
    # it must NOT take the hard-fail `needs_diff` path: a non-repo `-C` or a failing
    # `git diff [--cached]` degrades to pure ideation (diff == ""), not an abort. Only a
    # default review (no panel mode, no --visual) genuinely REQUIRES a diff; --staged on
    # such a review still hard-requires it (the pre-commit gate). So brainstorm is
    # excluded from needs_diff and routed through the caught/optional probe below.
    needs_diff = (args.staged or (not panel_mode and not visual_mode)) and args.brainstorm is None
    if diff is None and needs_diff:
        diff = _git_diff(cwd, args.staged)
    elif diff is None and visual_mode and not panel_mode:
        # --visual riding the default review: probe the working-tree diff to decide
        # companion-vs-standalone, but tolerate "no diff / not a git repo".
        try:
            diff = _git_diff(cwd, args.staged)
        except RuntimeError:
            diff = ""
    elif diff is None and args.brainstorm is not None:
        # --brainstorm picks up the staged (--staged) or working-tree diff as OPTIONAL
        # grounding context so you can brainstorm ABOUT a specific change. The diff is
        # never required: an absent diff / non-repo / git failure degrades to pure
        # ideation (diff == ""). `_read_stdin_if_piped` already returns the diff for a
        # NON-EMPTY pipe (precedence); empty/`/dev/null` stdin reads as None here, so we
        # still probe the working tree — matching every other mode and the documented
        # `… --brainstorm "Q" < /dev/null` convention (an empty redirect must NOT suppress
        # grounding).
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

    # The recorded mode is the EXACT mode (a brainstorm of 4 is nothing like a plain
    # review of 4), and `pool_models` is what is ACTUALLY DISPATCHED so pool_size is
    # ground truth. A --visual companion is recorded under its base text mode: the
    # vision context above already ran (cvGate + the bounded <=--vision-timeout call),
    # and the multi-minute cost an agent might wrongly short-timeout is the text panel
    # that follows — which IS inside the wrapper below — so the base-mode key is the
    # honest one (codex P2: don't split history on a tag whose timing we exclude).
    if args.just_ask is not None:
        return _run_mode_with_stats(
            "just-ask", models,
            lambda: mode_just_ask(_with_visual(args.just_ask, visual_ctx), models, diff, cwd, timeout),
        )
    if args.quorum is not None:
        return _run_mode_with_stats(
            "quorum", models,
            lambda: mode_quorum(_with_visual(args.quorum, visual_ctx), models, diff, cwd, timeout, pick_moderators(args.moderator, models)),
        )
    if args.brainstorm is not None:
        # Brainstorm dispatches max(3, len(panel)) persona slots PER ROUND (mode_brainstorm
        # fills <3-model panels by repeating models: panel[slot % len(panel)]), so the
        # real per-round pool — and the ETA key — is the slot count, not len(models).
        # Recording the raw models list would undercount a 1-2 model panel and mis-key
        # its history (codex P2). Mirror that exact slot assignment so pool_size matches.
        if models:
            slot_count = max(3, len(models))
            brainstorm_pool = [models[slot % len(models)] for slot in range(slot_count)]
        else:
            brainstorm_pool = models
        return _run_mode_with_stats(
            "brainstorm", brainstorm_pool,
            lambda: mode_brainstorm(
                _with_visual(args.brainstorm, visual_ctx), models, cwd, timeout,
                pick_moderators(args.moderator, models), args.rounds, args.max_rounds,
                # When there IS a diff (working-tree, --staged, or piped), the personas
                # see it as grounding context so you can brainstorm ABOUT a specific
                # change. No diff -> pure ideation, exactly as before.
                diff=diff,
            ),
        )

    # Default plain review. Validate the board now if it wasn't already (the no-visual
    # path); an all-malformed `board:` exits 2 before the panel runs. The --visual
    # companion context folds into each per-reviewer prompt via args.prompt.
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

        def _ran_models() -> list[str]:
            # The BARE model ids that produced verdicts (a backfilled reserve under its
            # real id), so the stat record keys on what actually ran — not labels.
            return outcome_sink[0].usable_models if outcome_sink else []

        return _run_mode_with_stats(
            "review", eta_models,
            lambda: mode_review(
                models, _with_visual(args.prompt, visual_ctx), diff, cwd, timeout,
                args.staged, board=board, pool_size=args.pool, outcome_sink=outcome_sink,
            ),
            models_after=_ran_models,
        )
    return _run_mode_with_stats(
        "review", models,
        lambda: mode_review(
            models, _with_visual(args.prompt, visual_ctx), diff, cwd, timeout, args.staged, board=board,
        ),
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
    print(f"\nA plain `review` runs the top {DEFAULT_POOL_SIZE} AVAILABLE seats by "
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
