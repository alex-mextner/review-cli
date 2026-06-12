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
from .panel import pick_moderator
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
    timeout = args.timeout if args.timeout is not None else (PANEL_TIMEOUT_DEFAULT if panel_mode else 1200)

    # Panel modes are interactive and long-running, so announce each streamed
    # backend's live-log path to stderr; the plain review path stays quiet.
    if panel_mode:
        backends._ANNOUNCE_LOGS = True

    # Diff is optional in panel modes (piped/--staged used as context if present).
    diff = _read_stdin_if_piped()
    if diff is None and (args.staged or not panel_mode):
        diff = _git_diff(cwd, args.staged)
    diff = diff or ""

    if args.just_ask is not None:
        return mode_just_ask(args.just_ask, models, diff, cwd, timeout)
    if args.quorum is not None:
        return mode_quorum(args.quorum, models, diff, cwd, timeout, pick_moderator(args.moderator, models))
    if args.brainstorm is not None:
        return mode_brainstorm(
            args.brainstorm, models, cwd, timeout, pick_moderator(args.moderator, models), args.rounds, args.max_rounds
        )

    return mode_review(models, args.prompt, diff, cwd, timeout, args.staged)


# Re-export for legacy callers that imported subprocess off the entry module.
__all__ = ["main", "subprocess"]
