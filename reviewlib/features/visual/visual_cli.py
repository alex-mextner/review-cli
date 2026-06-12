"""`review --visual` standalone dispatch (the §2.1 mode-less case).

Owns: running `run_pipeline`, rendering the verdict (human-readable or `--json`), and
mapping the verdict to the process exit code (§2). Kept out of `reviewlib.cli` so the
entry stays a thin argparse-and-dispatch shim.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .pipeline import run_pipeline
from .policy_engine import exit_code_for

# Human-readable one-liner glyphs per verdict.
_GLYPH = {
    "keep": "KEEP",
    "rollback": "ROLLBACK",
    "repair": "REPAIR",
    "human_review": "HUMAN-REVIEW",
    "unverified": "UNVERIFIED",
}


def run_visual_standalone(
    image: str,
    *,
    before: str | None,
    expect: str | None,
    intent: str | None,
    requested_checks: list[str],
    models: list[str],
    no_ai: bool,
    vision_timeout: int,
    as_json: bool,
    strict: bool,
    project: str | None = None,
) -> int:
    after = Path(image).expanduser()
    before_path = Path(before).expanduser() if before else None

    verdict = run_pipeline(
        after,
        before=before_path,
        expect=expect,
        intent=intent,
        requested_checks=requested_checks,
        models=models,
        no_ai=no_ai,
        vision_timeout=vision_timeout,
        project=Path(project).expanduser() if project else None,
    )
    code = exit_code_for(verdict, strict=strict)

    if as_json:
        out = verdict.to_dict()
        out["exit_code"] = code
        print(json.dumps(out, indent=2))
    else:
        glyph = _GLYPH.get(verdict.final, verdict.final.upper())
        print(f"[review --visual] {glyph}: {verdict.reason}")
        if verdict.vision_verdict and verdict.vision_verdict != verdict.final:
            print(f"  vision said: {verdict.vision_verdict} (confidence {verdict.confidence:.2f})")
        if verdict.note and verdict.note != verdict.reason:
            print(f"  note: {verdict.note}")
        if verdict.module_verdicts:
            for m in verdict.module_verdicts:
                print(f"  module {m.module}: {m.decision} — {m.reason}")
        if code != 0:
            print(f"  exit {code}", file=sys.stderr)
    return code
