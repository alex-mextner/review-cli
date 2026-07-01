"""Review modes: plain diff review, --just-ask, --quorum, --brainstorm.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).
"""
from __future__ import annotations

from pathlib import Path


def _diff_context_block(diff: str) -> str:
    if not diff.strip():
        return ""
    return f"\n\nAdditional context — a git diff:\n\n```diff\n{diff}\n```"


def _visual_images(ctx) -> tuple[Path, ...]:
    # Suppress raw image attachments when --no-ai is set: vision fan-out is disabled
    # and panels must not receive image bytes even if a --visual path was given. (P1 fix.)
    if getattr(getattr(ctx, "args", None), "no_ai", False):
        return ()
    image_path = getattr(getattr(ctx, "visual_ctx", None), "image_path", None)
    return (Path(image_path),) if image_path else ()
