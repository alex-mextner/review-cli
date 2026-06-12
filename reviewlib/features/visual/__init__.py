"""reviewlib.features.visual — image-only visual-verification pipeline.

Stage 1 (core): the deterministic cvGate, the multimodal callAIVision path, the
policy engine, the contract, and the orchestrating pipeline — plus the three built-in
modules. `review --visual <image>` runs this standalone (the §2.1 mode-less case) and
the same machinery is threaded into a companion mode's call when `--visual` rides
`--brainstorm`/`--quorum`/the default diff-review.

Stage 2 (this build) adds: the per-project module registry + TOFU trust (registry.py),
the contributed selection-highlight reference module (contrib/), live vision dispatch for
ALL providers (anthropic/openai/gemini in vision_client), and the per-mode multimodal
fan-out (compose._run_fanout delivers the image to a vision model and folds the grounded
observation into each persona/voter prompt).

Stages NOT yet built (clear extension points left in place):
  * Stage 2a — the optional local pre-classifier (§3.1a): the marked hook point in
    pipeline.run_pipeline, between cvGate pass-through and the vision call.
  * Stage 3 — the `tg --photo` pre-send hook integration (§7).
"""
from __future__ import annotations

from .compose import VisualComposition, build_mode_visual_context
from .contract import BBox, VisualExpectation, derive_contract
from .cv_gate import CvError, CvGateResult, CvSignals, compute_signals, cv_gate
from .module_api import ModuleVerdict, VisualContext, VisualModule
from .pipeline import run_pipeline
from .policy_engine import Verdict, decide_from_cv, decide_from_vision, exit_code_for
from .registry import (
    ContributedModule,
    ModuleSpec,
    RegistryEnv,
    discover_specs,
    load_modules,
    register_module,
    trust_module,
)
from .vision_client import (
    VisionBlock,
    VisionVerdict,
    build_request,
    call_ai_vision,
    capability_for,
    select_vision_backend,
    vision_backend_available,
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
    "ContributedModule",
    "ModuleSpec",
    "RegistryEnv",
    "discover_specs",
    "load_modules",
    "register_module",
    "trust_module",
    "VisionBlock",
    "VisionVerdict",
    "build_request",
    "call_ai_vision",
    "capability_for",
    "select_vision_backend",
    "vision_backend_available",
]
