"""review CLI entry: argparse + dispatch only.

This is the package entry point (`[project.scripts] review = "reviewlib.cli:main"`
and the target of the thin `bin/review` shim). It owns argument parsing, diff
acquisition, model selection, and dispatch to the mode functions. All behaviour
lives in the sibling modules — this file is the thin entry the Stage 0
decomposition was about.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import backends
from .backends import _which  # re-export for tests/compat  # noqa: F401
from .config import (
    DEFAULT_MODELS,
    DEFAULT_PROMPT,
    PANEL_TIMEOUT_DEFAULT,
    BoardConfigError,
    _expand_alias,
    _split_models,
    load_board,
    load_config,
)
from .install import install_commit_hook, install_skill
from .modes.brainstorm import mode_brainstorm
from .modes.just_ask import mode_just_ask
from .modes.quorum import mode_quorum
from .modes.review import mode_review
from .panel import pick_moderators
from .process import _run


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
    """Parse `dashboard [--port N] [--no-open]` and start the local-only web server.

    Imported lazily so the dashboard's stdlib HTTP stack never loads on the hot review
    path (and a stray import error in dashboard code can't break `review`)."""
    sub = argparse.ArgumentParser(prog="review dashboard", description="Local-only web dashboard for review-cli runs.")
    sub.add_argument("--port", type=int, default=None, help="port to bind on 127.0.0.1 (default: a free ephemeral port)")
    sub.add_argument("--no-open", action="store_true", help="do not open a browser window")
    sub.add_argument("--verbose", action="store_true", help="log every HTTP request to stderr")
    ns = sub.parse_args(rest)
    from .dashboard import run_dashboard

    return run_dashboard(port=ns.port, open_browser=not ns.no_open, verbose=ns.verbose)


def main(argv: list[str] | None = None) -> int:
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
    parser = argparse.ArgumentParser(description="Run read-only code reviews across multiple model backends.")
    parser.add_argument("-m", "--model", action="append", default=[], help="model/backend to run; repeat or comma-separate")
    parser.add_argument("-C", "--cwd", default=".", help="repository directory")
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
    parser.add_argument("--no-board", action="store_true", help="disable the reviewer board; use the plain models list instead")
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
        return _show_board(config)

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
    # own role/lens. Precedence is COST-SAFETY first — the paid 8-model board runs only
    # when the user expressed NO model preference at all:
    #   explicit -m  >  explicit `models:` in config.yaml  >  default board.
    # So a configured `models:` gets exactly those (the flat panel), NOT the paid board;
    # --no-board also forces the flat path. The board applies only on the DEFAULT diff
    # review (no panel mode) with neither -m nor config models. `use_board` is a cheap
    # boolean gate computed now; the actual load_board + cost-safety validation runs
    # LATER (validate_board, below) — after the standalone-visual path has had its chance
    # to short-circuit, so a malformed `board:` never blocks the board-unrelated
    # standalone `review --visual` pipeline (codex P2). It still fires BEFORE the COMPANION
    # visual fan-out, so a doomed config never spends a paid vision call.
    has_config_models = bool(config_models)  # filtered above: blanks-only counts as none
    use_board = not panel_mode and not explicit_models and not has_config_models and not args.no_board
    board: list | None = None
    board_validated = False

    def validate_board() -> int | None:
        """Resolve + validate the reviewer board for the default review path, once.
        Returns an exit code (2) on an all-malformed `board:` config, else None. No-op
        when the board does not apply (panel mode / -m / config models / --no-board)."""
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
    needs_diff = args.staged or (not panel_mode and not visual_mode)
    if diff is None and needs_diff:
        diff = _git_diff(cwd, args.staged)
    elif diff is None and visual_mode and not panel_mode:
        # --visual riding the default review: probe the working-tree diff to decide
        # companion-vs-standalone, but tolerate "no diff / not a git repo".
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

    if args.just_ask is not None:
        return mode_just_ask(_with_visual(args.just_ask, visual_ctx), models, diff, cwd, timeout)
    if args.quorum is not None:
        return mode_quorum(_with_visual(args.quorum, visual_ctx), models, diff, cwd, timeout, pick_moderators(args.moderator, models))
    if args.brainstorm is not None:
        return mode_brainstorm(
            _with_visual(args.brainstorm, visual_ctx), models, cwd, timeout,
            pick_moderators(args.moderator, models), args.rounds, args.max_rounds
        )

    # Default plain review. Validate the board now if it wasn't already (the no-visual
    # path); an all-malformed `board:` exits 2 before the panel runs. The --visual
    # companion context folds into each per-reviewer prompt via args.prompt.
    rc = validate_board()
    if rc is not None:
        return rc
    return mode_review(
        models, _with_visual(args.prompt, visual_ctx), diff, cwd, timeout, args.staged, board=board,
    )


def _show_board(config: dict) -> int:
    """Print the active reviewer board: each reviewer's display name, role, model,
    and whether its backend is currently available (key/CLI present). Sourced from
    config.yaml `board:` if set, else the built-in DEFAULT_BOARD. Read-only — no
    model is called, no key is printed."""
    try:
        board = load_board(config)
    except BoardConfigError as exc:
        print(f"[review-cli] {exc}", file=sys.stderr, flush=True)
        return 2
    source = "config.yaml (board:)" if isinstance(config.get("board"), list) and config.get("board") else "default"
    print(f"Reviewer board ({len(board)} seats, source: {source}):\n")
    name_w = max((len(r.display) for r in board), default=0)
    role_w = max((len(r.role or "general") for r in board), default=0)
    for reviewer in board:
        available = backends.backend_available(reviewer.model)
        status = "available" if available else "SKIPPED (no key/CLI)"
        role = (reviewer.role or "general").ljust(role_w)
        print(f"  {reviewer.display.ljust(name_w)}  {role}  {reviewer.model}  [{status}]")
    if any(not backends.backend_available(r.model) for r in board):
        print("\nSkipped reviewers drop out of the panel at run time; the board degrades "
              "gracefully. commandcode reviewers need COMMANDCODE_API_KEY, gemini needs "
              "GEMINI_API_KEY, codex/claude need their CLI on PATH.")
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
