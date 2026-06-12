"""pipeline — the standalone verdict orchestrator (§3).

    contract → cvGate → [optional local pre-classifier] → visionClient → policyEngine → verdict

This is the mode-less pass of §2.1: `review --visual shot.png` with no companion mode.
When a companion mode is present, the CLI does NOT run this pipeline as an isolated
pass — it threads the image + the active modules' questions into that mode's model call
(see `reviewlib.cli`). The per-stage machinery here is the same machinery a companion
mode reuses.

The Stage-2a local pre-classifier (§3.1a) slots in at the marked hook below, between
cvGate's pass-through and the vision call. It is absent in Stage 1 (and inert by
default thereafter): the hook is a clearly-marked extension point, not yet a stage.
"""
from __future__ import annotations

from pathlib import Path

from .contract import VisualExpectation, derive_contract
from .cv_gate import cv_gate, detect_media_type, prepare_image_for_vision
from .module_api import ModuleVerdict, VisualContext
from .modules.builtins import builtin_modules
from .policy_engine import Verdict, decide_from_cv, decide_from_vision
from .vision_client import (
    VisionBlock,
    build_output_schema,
    call_ai_vision,
    capability_for,
    encode_image,
    select_vision_backend,
)


def _active_modules(ctx: VisualContext):
    """Built-in module selection (§4 step 2). Per-project module discovery (§6) is
    Stage 3 — here only the trusted built-ins are considered. `--check <name>`
    force-activates a named module; otherwise modules self-select via activates()."""
    mods = []
    for m in builtin_modules():
        forced = m.name in ctx.requested_checks
        if forced or m.activates(ctx):
            mods.append(m)
    return mods


def _vision_field_names(modules) -> list[str]:
    fields = []
    for m in modules:
        fld = getattr(m, "_vision_field", "")
        if fld:
            fields.append(fld)
    return fields


def run_pipeline(
    after: Path,
    *,
    before: Path | None = None,
    expect: str | None = None,
    intent: str | None = None,
    requested_checks: list[str] | None = None,
    models: list[str] | None = None,
    no_ai: bool = False,
    vision_timeout: int = 60,
) -> Verdict:
    """Run the full standalone pipeline and return a final `Verdict`.

    `no_ai=True` runs cvGate only (the §10 Stage-1 gate / `--no-ai` offline smoke):
    a cvGate reject/bypass is terminal; a pass-through becomes `human_review`
    (CV cannot keep on its own — §3.1, no symmetric SSIM auto-keep)."""
    requested_checks = requested_checks or []
    expectation: VisualExpectation = derive_contract(expect, intent, has_before=before is not None)

    # --- Stage 1: cvGate (the cheap deterministic pre-filter). It validates the input
    # image (unreadable → usage reject) and auto-rejects the unambiguously-broken set
    # FIRST, so a missing/blank/error image is handled deterministically before any
    # contract-level fail-close below (codex P2). --------------------------------
    gate = cv_gate(after, before=before, diff_policy=expectation.diff_policy)
    cv_decision = decide_from_cv(gate.outcome, gate.reason, gate.signals)
    if cv_decision is not None:
        return cv_decision  # terminal: auto-reject or no-effect bypass, no vision call.

    signals = gate.signals

    # --- A zero-drift contract (--expect zero-diff / wrap) is only VERIFIABLE with a
    # --before baseline. On a validated PASS-THROUGH with no baseline there is nothing
    # to compare against, so a model `keep` could pass unverified drift → fail CLOSED to
    # human_review rather than accept an unverifiable zero-diff claim (codex P2). This
    # runs AFTER cvGate so a missing/broken image is still a deterministic reject above.
    if expectation.diff_policy == "zero" and before is None:
        return Verdict(
            final="human_review",
            reason="zero-diff/wrap expectation requires --before to verify (no baseline supplied)",
            source="policy",
            cv_signals=signals,
        )
    ctx = VisualContext(
        after_image=after.read_bytes(),
        before_image=before.read_bytes() if before and before.exists() else None,
        expectation=expectation,
        cv_signals=signals,
        intent=intent,
        requested_checks=requested_checks,
    )
    modules = _active_modules(ctx)

    # Module CV phase (§4 step 3): a module `block` can short-circuit before any
    # vision call (cheap, module-scoped — like cvGate but owned by a module).
    module_cv: list[ModuleVerdict] = []
    for m in modules:
        mv = m.cv_check(ctx)
        if mv is not None:
            module_cv.append(mv)
            if mv.decision == "block":
                return Verdict(
                    final="rollback", reason=f"module veto: {mv.module}: {mv.reason}",
                    source="policy", module_verdicts=module_cv, cv_signals=signals,
                )

    # ----- Stage 2a HOOK POINT (§3.1a): the optional local pre-classifier slots in
    # HERE, between cvGate pass-through and the vision call. It is ABSENT in Stage 1.
    # When built (Stage 2a) it would: load its model artifact (unless --no-local-model);
    # score the pass-through into smooth|minor|broken; on a CONFIDENT-CLEAR score
    # short-circuit (return a Verdict, skipping the paid vision call); on ambiguity
    # fall through to call_ai_vision below. It is NEVER the authority — see §3.1a.
    # No-op in Stage 1: control flows straight to the vision stage.
    # ---------------------------------------------------------------------------

    if no_ai:
        # CV-only: pass-through with no AI cannot auto-keep (§3.1). Surface as
        # human_review so a gate neither falsely passes nor falsely fails an
        # ambiguous render offline.
        return Verdict(
            final="human_review",
            reason="cvGate pass-through with --no-ai: no vision verdict available (CV cannot keep)",
            source="cv_gate",
            module_verdicts=module_cv,
            cv_signals=signals,
        )

    # --- Stage 2: visionClient (the primary judge). -----------------------------
    backend = select_vision_backend(models or [])
    questions = [q for m in modules for q in m.vision_questions(ctx)]
    schema = build_output_schema(_vision_field_names(modules))
    cap = capability_for(backend) if backend else None
    blocks = _build_blocks(after, before, expectation, signals, questions, cap)
    vision = call_ai_vision(
        backend, blocks=blocks, expectation=expectation, cv_signals=signals,
        output_schema=schema, timeout_s=vision_timeout,
    )

    # --- Stage 3: judge phase + policy decision. --------------------------------
    module_judgements = [m.judge(ctx, vision) for m in modules] if vision.available and vision.verdict else module_cv
    return decide_from_vision(vision, signals, module_judgements)


def _build_blocks(after, before, expectation, signals, questions, cap=None) -> list[VisionBlock]:
    text = (
        "Verify this render. Expectation contract: "
        f"kind={expectation.kind}, diff_policy={expectation.diff_policy}, risk={expectation.risk}. "
    )
    if signals is not None:
        text += (
            f"CV signals: entropy={signals.palette_entropy:.3f}, "
            f"dominant_coverage={signals.dominant_coverage:.3f}, quant_colors={signals.quant_colors}. "
        )
    if questions:
        text += "Answer these specific checks in their named boolean fields: " + " ".join(questions)
    blocks = [VisionBlock(kind="text", text=text)]
    if before is not None and before.exists():
        blocks.append(_image_block(before, "before", cap))
    blocks.append(_image_block(after, "after", cap))
    return blocks


def _image_block(image, label: str, cap) -> VisionBlock:
    """Build one image block, downscaling to the selected provider's limits (codex P2)
    so an over-cap retina/full-page PNG is not rejected by the API."""
    if cap is not None:
        data, media_type = prepare_image_for_vision(
            image, max_long_side=cap.preferred_long_side, max_bytes=cap.max_image_bytes
        )
    else:
        data, media_type = image.read_bytes(), detect_media_type(image)
    return VisionBlock(kind="image", label=label, media_type=media_type, data_base64=encode_image(data))
