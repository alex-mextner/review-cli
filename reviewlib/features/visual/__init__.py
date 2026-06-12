"""reviewlib.features.visual — image-only visual-verification pipeline.

Stage 1 (core): the deterministic cvGate, the multimodal callAIVision path, the
policy engine, the contract, and the orchestrating pipeline — plus the three built-in
modules. `review --visual <image>` runs this standalone (the §2.1 mode-less case) and
the same machinery is threaded into a companion mode's call when `--visual` rides
`--brainstorm`/`--quorum`/the default diff-review.

Stages NOT yet built (clear extension points left in place):
  * Stage 2a — the optional local pre-classifier (§3.1a): the marked hook point in
    pipeline.run_pipeline, between cvGate pass-through and the vision call.
  * Stage 2 — live Anthropic/OpenAI vision dispatch (the request BUILDERS are complete
    and tested; only the HTTP send is Stage-2 in vision_client.call_ai_vision).
  * Stage 3 — per-project module discovery + TOFU trust (registry.py).
"""
from __future__ import annotations

from .compose import VisualComposition, build_mode_visual_context
from .contract import BBox, VisualExpectation, derive_contract
from .cv_gate import CvError, CvGateResult, CvSignals, compute_signals, cv_gate
from .module_api import ModuleVerdict, VisualContext, VisualModule
from .pipeline import run_pipeline
from .policy_engine import Verdict, decide_from_cv, decide_from_vision, exit_code_for
from .vision_client import (
    VisionBlock,
    VisionVerdict,
    build_request,
    call_ai_vision,
    capability_for,
    select_vision_backend,
)

__all__ = [
    "VisualComposition",
    "build_mode_visual_context",
    "BBox",
    "VisualExpectation",
    "derive_contract",
    "CvError",
    "CvGateResult",
    "CvSignals",
    "compute_signals",
    "cv_gate",
    "ModuleVerdict",
    "VisualContext",
    "VisualModule",
    "run_pipeline",
    "Verdict",
    "decide_from_cv",
    "decide_from_vision",
    "exit_code_for",
    "VisionBlock",
    "VisionVerdict",
    "build_request",
    "call_ai_vision",
    "capability_for",
    "select_vision_backend",
]
