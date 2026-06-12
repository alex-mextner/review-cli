"""Review modes: plain diff review, --just-ask, --quorum, --brainstorm.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).
"""
from __future__ import annotations


def _diff_context_block(diff: str) -> str:
    if not diff.strip():
        return ""
    return f"\n\nAdditional context — a git diff:\n\n```diff\n{diff}\n```"
