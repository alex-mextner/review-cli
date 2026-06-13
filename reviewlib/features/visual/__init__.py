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

Stage 2a (this build) adds the optional local pre-classifier (§3.1a) as its HONEST v1:
the known-good perceptual cache (preclassifier.KnownGoodCache), wired at the marked hook
in pipeline.run_pipeline between cvGate pass-through and the vision call. A render that
perceptually matches a previously-`keep`ed render short-circuits to keep WITHOUT the paid
vision call; toggle off with --no-local-model. This is a cache, NOT a trained ML model —
the trained LightGBM/CNN classifier (§3.1a) remains a follow-up for when a labeled corpus
exists.

Stages NOT yet built (clear extension points left in place):
  * Stage 2a (trained model) — the LightGBM/tiny-CNN classifier of §3.1a, the follow-up
    to the known-good cache shipped here; slots in behind the same --no-local-model flag.
  * Stage 3 — the `tg --photo` pre-send hook integration (§7).
"""
from __future__ import annotations

from .compose import VisualComposition, build_mode_visual_context
from .contract import BBox, VisualExpectation, derive_contract
from .cv_gate import CvError, CvGateResult, CvSignals, compute_signals, cv_gate
from .module_api import ModuleVerdict, VisualContext, VisualModule
from .pipeline import run_pipeline
from .policy_engine import Verdict, decide_from_cv, decide_from_vision, exit_code_for
from .preclassifier import KnownGoodCache, hamming, modules_signature, perceptual_ahash
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
    "KnownGoodCache",
    "hamming",
    "modules_signature",
    "perceptual_ahash",
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
