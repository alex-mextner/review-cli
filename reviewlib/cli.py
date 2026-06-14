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
    _expand_alias,
    _split_models,
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


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv == ["install-skill"]:
        return install_skill()
    if argv == ["install-commit-hook"]:
        return install_commit_hook()
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

    if args.list_defaults:
        effective = config.get("models") or DEFAULT_MODELS
        print("\n".join(_expand_alias(m) for m in effective))
        return 0

    cwd = Path(args.cwd).expanduser().resolve()
    explicit_models = _split_models(args.model)
    # Precedence: explicit -m > config > code default. Brainstorm prefers
    # config.brainstorm_models and drops unreachable backends gracefully (so a
    # missing GEMINI_API_KEY never aborts the run). Explicit -m is honored as-is.
    if explicit_models:
        models = explicit_models
    elif args.brainstorm is not None:
        src = config.get("brainstorm_models") or config.get("models") or DEFAULT_MODELS
        models = [m for m in (_expand_alias(x) for x in src) if backends.backend_available(m)]
        if not models:
            models = [_expand_alias(x) for x in (config.get("models") or DEFAULT_MODELS)]
    else:
        models = [_expand_alias(x) for x in (config.get("models") or DEFAULT_MODELS)]

    panel_mode = args.just_ask or args.quorum or args.brainstorm
    visual_mode = args.visual is not None
    timeout = args.timeout if args.timeout is not None else (PANEL_TIMEOUT_DEFAULT if panel_mode else 1200)

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

    return mode_review(models, _with_visual(args.prompt, visual_ctx), diff, cwd, timeout, args.staged)


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
