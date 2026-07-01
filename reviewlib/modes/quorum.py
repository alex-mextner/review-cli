"""quorum: experts answer + a moderator finds consensus/disagreement.

`review quorum "<question>"` — a two-phase structured panel (experts cite evidence,
a moderator finds quorum/disagreement). Originally the `--quorum` flag (Stage 0
decomposition); now a first-class SUBCOMMAND backed by the `MODE` descriptor at the
bottom of this file (see `modes/contract.py`).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..panel import PanelJob, format_result, run_moderator, run_panel
from . import _diff_context_block, _visual_images
from .contract import ModeContext, ModeSpec


def mode_quorum(
    question: str, models: list[str], diff: str, cwd: Path, timeout: int,
    moderators: list[str], visual_images: tuple[Path, ...] = (),
) -> int:
    expert_prompt = (
        "You are one expert on a panel. Give a clear RECOMMENDATION on the question below. "
        "Cite concrete evidence for every claim (file path, line number, command output, "
        "or a verifiable fact). If you do not have an evidence base to answer, say exactly "
        "'INSUFFICIENT EVIDENCE' and explain what you would need — do NOT guess. "
        "Do not edit files or run commands.\n\n"
        f"QUESTION:\n{question}" + _diff_context_block(diff)
    )
    jobs = [PanelJob(model=model, prompt=expert_prompt, diff="", images=visual_images) for model in models]
    expert_results = run_panel(jobs, cwd, timeout)

    transcript = "\n\n".join(
        f"### Expert: {r.model} [{'ok' if r.returncode == 0 else f'exit {r.returncode}'}]\n"
        f"{(r.stdout.strip() or r.stderr.strip() or '(no output)')}"
        for r in expert_results
    )
    mod_prompt = (
        "You are the MODERATOR of an expert panel. Below are independent expert answers to "
        "one question. Produce a structured summary with exactly these sections:\n"
        "1. QUORUM — points where a majority of experts agree AND cite evidence "
        "(state the point, who agrees, and the evidence).\n"
        "2. DISAGREEMENT / NO QUORUM — points where experts conflict or no majority exists.\n"
        "3. ABSTAINED — experts who said INSUFFICIENT EVIDENCE, and on what.\n"
        "Do not invent agreement. Do not edit files.\n\n"
        f"QUESTION:\n{question}\n\n=== EXPERT ANSWERS ===\n{transcript}"
    )
    mod_result = run_moderator(moderators, mod_prompt, cwd, timeout)

    out = ["# Expert answers", "\n\n---\n\n".join(format_result(r) for r in expert_results)]
    out += ["\n# Moderator summary", format_result(mod_result)]
    print("\n\n".join(out))
    ok = all(r.returncode == 0 for r in expert_results) and mod_result.returncode == 0
    return 0 if ok else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", help="the question to put to the expert quorum")


def _handler(ctx: ModeContext) -> int:
    images = _visual_images(ctx)
    if images:
        return mode_quorum(
            ctx.with_visual(ctx.args.question), ctx.models, ctx.diff, ctx.cwd, ctx.timeout,
            ctx.moderators, images,
        )
    return mode_quorum(
        ctx.with_visual(ctx.args.question), ctx.models, ctx.diff, ctx.cwd, ctx.timeout,
        ctx.moderators,
    )


MODE = ModeSpec(
    name="quorum",
    subcommand="quorum",
    diff_policy="none",
    stats_mode="quorum",
    summary="experts cite evidence + a moderator finds quorum/disagreement",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=True,
)
