"""--just-ask: multi-model answer to a plain question (no diff required).

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).
"""
from __future__ import annotations

from pathlib import Path

from ..panel import PanelJob, format_result, run_panel
from . import _diff_context_block


def mode_just_ask(question: str, models: list[str], diff: str, cwd: Path, timeout: int) -> int:
    prompt = (
        "Answer this question directly and concisely. Do not edit files or run commands.\n\n"
        f"QUESTION:\n{question}" + _diff_context_block(diff)
    )
    jobs = [PanelJob(model=model, prompt=prompt, diff="") for model in models]
    results = run_panel(jobs, cwd, timeout)
    print("\n\n---\n\n".join(format_result(r) for r in results))
    return 0 if all(r.returncode == 0 for r in results) else 1
