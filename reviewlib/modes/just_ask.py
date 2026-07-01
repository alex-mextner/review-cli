"""just-ask: multi-model answer to a plain question (diff optional).

`review just-ask "<question>"` — a single-shot panel question. Originally the
`--just-ask` flag (Stage 0 decomposition); now a first-class SUBCOMMAND backed by the
self-describing `MODE` descriptor at the bottom of this file (see `modes/contract.py`).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from ..panel import PanelJob, format_result, run_panel
from . import _diff_context_block, _visual_images
from .contract import ModeContext, ModeSpec


def mode_just_ask(
    question: str, models: list[str], diff: str, cwd: Path, timeout: int,
    visual_images: tuple[Path, ...] = (),
) -> int:
    prompt = (
        "Answer this question directly and concisely. Do not edit files or run commands.\n\n"
        f"QUESTION:\n{question}" + _diff_context_block(diff)
    )
    jobs = [PanelJob(model=model, prompt=prompt, diff="", images=visual_images) for model in models]
    results = run_panel(jobs, cwd, timeout)
    print("\n\n---\n\n".join(format_result(r) for r in results))
    return 0 if all(r.returncode == 0 for r in results) else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", help="the question to ask every backend")


def _handler(ctx: ModeContext) -> int:
    images = _visual_images(ctx)
    if images:
        return mode_just_ask(
            ctx.with_visual(ctx.args.question), ctx.models, ctx.diff, ctx.cwd, ctx.timeout,
            images,
        )
    return mode_just_ask(
        ctx.with_visual(ctx.args.question), ctx.models, ctx.diff, ctx.cwd, ctx.timeout,
    )


MODE = ModeSpec(
    name="just-ask",
    subcommand="just-ask",
    diff_policy="none",
    stats_mode="just-ask",
    summary="single-shot panel question (diff optional)",
    handler=_handler,
    add_arguments=_add_arguments,
    announce_logs=True,
    aliases=("ask",),
)
