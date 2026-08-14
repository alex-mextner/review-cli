"""just-ask: multi-model answer to a plain question (diff optional).

`review just-ask "<question>"` — a single-shot panel question. Originally the
`--just-ask` flag (Stage 0 decomposition); now a first-class SUBCOMMAND backed by the
self-describing `MODE` descriptor at the bottom of this file (see `modes/contract.py`).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from ..backends import cap_diff_for_dispatch
from ..panel import (
    PanelJob,
    format_result,
    recount_round_by_usability,
    result_is_usable,
    run_panel,
)
from . import _diff_context_block, _run_effort, _visual_images
from .contract import ModeContext, ModeSpec

if TYPE_CHECKING:
    from ..config import EffortOverride


def mode_just_ask(
    question: str,
    models: list[str],
    diff: str,
    cwd: Path,
    timeout: int,
    visual_images: tuple[Path, ...] = (),
    effort_override: "EffortOverride | None" = None,
    diff_from_stdin: bool = False,
    diff_already_capped: bool = False,
) -> int:
    # codex review finding: the dispatch-time diff cap used to be applied ONLY by
    # cli.py's `_dispatch`, so a caller reaching this function directly (a library
    # consumer, an MCP seam, a test) could still send an uncapped diff to every seat —
    # capped here too, at the mode's own dispatch boundary, mirroring mode_review /
    # mode_brainstorm. Same stdin exemption: a piped diff was already an explicit,
    # deliberate scope choice by the caller.
    #
    # codex review finding (round 2): capping AGAIN when the CLI layer already capped
    # it (`diff_already_capped`) used to be silently harmless at the default cap (the
    # first call's output is already <= cap, so a second call is a true no-op) but NOT
    # idempotent when `$REVIEW_DIFF_MAX_BYTES` is set below the truncation marker's own
    # length — a second application would re-truncate the FIRST call's marker text and
    # report ITS byte count as "the full diff". `diff_already_capped` (default False, so
    # a direct library caller bypassing the CLI is still protected) skips the redundant
    # second call entirely rather than relying on it happening to be a no-op.
    dispatch_diff = (
        diff if diff_from_stdin or diff_already_capped else cap_diff_for_dispatch(diff)
    )
    prompt = (
        "Answer this question directly and concisely. Do not edit files or run commands.\n\n"
        f"QUESTION:\n{question}" + _diff_context_block(dispatch_diff)
    )
    jobs = [
        PanelJob(
            model=model,
            prompt=prompt,
            diff="",
            images=visual_images,
            effort=_run_effort(effort_override, model),
        )
        for model in models
    ]
    results = run_panel(jobs, cwd, timeout)
    # glm-5.2 review finding (2026-08 seat-cooldown feature): `run_panel`'s own
    # auto-tally counts a cached-cooldown sentinel (rc=0, non-empty "unavailable"
    # body) as `ok` in run-stats — the same distortion brainstorm's
    # `recount_round_by_usability` call already fixes for its own rounds. Applied
    # here too so just-ask's run-stats never disagree with the `result_is_usable`
    # verdict this function's own return value already uses below.
    recount_round_by_usability(results)
    print("\n\n---\n\n".join(format_result(r) for r in results))
    # codex review finding (2026-08 seat-cooldown feature): a cached-cooldown-skip
    # result deliberately mirrors a live "is currently unavailable" sentinel — rc=0,
    # non-empty body — so a plain `returncode == 0` check reports SUCCESS for `review
    # just-ask -m fable` during a cooldown window even though no seat produced a real
    # answer. `result_is_usable` is the same predicate `mode_review`'s flat/board paths
    # already use for exactly this reason.
    return 0 if all(result_is_usable(r) for r in results) else 1


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("question", help="the question to ask every backend")


def _handler(ctx: ModeContext) -> int:
    images = _visual_images(ctx)
    diff_from_stdin = bool(ctx.extra.get("diff_from_stdin", False))
    diff_already_capped = bool(ctx.extra.get("diff_already_capped", False))
    if images:
        return mode_just_ask(
            ctx.with_visual(ctx.args.question),
            ctx.models,
            ctx.diff,
            ctx.cwd,
            ctx.timeout,
            images,
            effort_override=ctx.effort_override,
            diff_from_stdin=diff_from_stdin,
            diff_already_capped=diff_already_capped,
        )
    return mode_just_ask(
        ctx.with_visual(ctx.args.question),
        ctx.models,
        ctx.diff,
        ctx.cwd,
        ctx.timeout,
        effort_override=ctx.effort_override,
        diff_from_stdin=diff_from_stdin,
        diff_already_capped=diff_already_capped,
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
