"""pipeline — the standalone verdict orchestrator (§3).

    contract → cvGate → [optional local pre-classifier] → visionClient → policyEngine → verdict

This is the standalone pass of §2.1: `review visual shot.png`.
When a companion mode is present, the CLI does NOT run this pipeline as an isolated
pass — it threads the image + the active modules' questions into that mode's model call
(see `reviewlib.cli`). The per-stage machinery here is the same machinery a companion
mode reuses.

The Stage-2a local pre-classifier (§3.1a) is wired in at the marked hook below, between
cvGate's pass-through and the vision call. The HONEST v1 (`preclassifier.py`) is a
known-good perceptual cache (NOT a trained ML model — that is a §3.1a follow-up): a
render that perceptually matches a previously-`keep`ed render short-circuits to `keep`
and SKIPS the paid vision call. It is enabled by default, gated off with
`--no-local-model` (`local_model=False`), and can NEVER auto-reject — any miss escalates
to vision as before.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...config import EffortOverride

from .contract import VisualExpectation, derive_contract
from .cv_gate import cv_gate, detect_media_type, prepare_image_for_vision
from .module_api import ModuleVerdict, VisualContext
from .modules.builtins import builtin_modules
from .policy_engine import Verdict, decide_from_cv, decide_from_vision
from .preclassifier import KnownGoodCache, modules_signature
from .vision_client import (
    VisionBlock,
    VISION_VERDICTS,
    build_output_schema,
    call_ai_vision,
    capability_for,
    effort_for_model,
    encode_image,
    select_vision_backend,
    select_vision_backends,
)


def _candidate_modules(project: Path | None):
    """All modules eligible for this run: the trusted built-ins (§4) PLUS the
    per-project contributed modules the registry discovered (§6). Project modules load
    by default (trust-by-default); under the opt-in REVIEW_UNTRUSTED_MODULES guard a
    quarantined contributed module is ABSENT here (never a block)."""
    mods = list(builtin_modules())
    try:
        from .registry import load_modules

        contributed, _quarantined = load_modules(project=project)
        mods.extend(contributed)
    except Exception:  # noqa: BLE001 — a registry failure must never break a verification
        pass
    return mods


def _active_modules(ctx: VisualContext, project: Path | None = None):
    """Module selection (§4 step 2): keep the candidates whose `activates(ctx)` is True.
    `--check <name>` force-activates a named module regardless of its rule. Built-ins
    self-activate by default; a contributed module gates on its `activates_on` tags."""
    mods = []
    for m in _candidate_modules(project):
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
    project: Path | None = None,
    local_model: bool = True,
    known_good_cache: KnownGoodCache | None = None,
    effort_override: EffortOverride | None = None,
) -> Verdict:
    """Run the full standalone pipeline and return a final `Verdict`.

    `no_ai=True` runs cvGate only (the §10 Stage-1 gate / `--no-ai` offline smoke):
    a cvGate reject/bypass is terminal; a pass-through becomes `human_review`
    (CV cannot keep on its own — §3.1, no symmetric SSIM auto-keep).

    `local_model=True` (default) enables the Stage-2a known-good-cache pre-classifier
    (§3.1a): a render that perceptually matches a previously-`keep`ed render
    short-circuits to `keep` WITHOUT the paid vision call. `--no-local-model`
    (`local_model=False`) disables it → flow is cvGate → vision unchanged. The cache is
    NEVER an authority: it can only short-circuit a confident keep-match, never
    auto-reject (that is cvGate's job)."""
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
    modules = _active_modules(ctx, project)

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

    # ----- Stage 2a (§3.1a): the local pre-classifier — HONEST v1 = a known-good
    # perceptual cache (NOT a trained ML model; that is a §3.1a follow-up). It sits HERE,
    # between cvGate pass-through and the vision call. If the render perceptually MATCHES
    # a previously-`keep`ed render (within a tight tolerance), short-circuit to `keep`
    # and SKIP the paid vision call (the cost-saver). On any MISS, fall through to
    # call_ai_vision below — the cache NEVER auto-rejects and never resolves ambiguity.
    # Disabled by --no-local-model (local_model=False) → flow is cvGate → vision as
    # before. No-VLM, no language channel → injection-immune by construction (§5).
    # ---------------------------------------------------------------------------
    cache = known_good_cache if known_good_cache is not None else KnownGoodCache()
    # Resolve the ACTUAL selected backend ONCE, here, so it can key the cache (below) AND
    # drive the vision call (Stage 2) without a double-resolve. Keying on the SELECTED
    # backend — not the raw --model LIST — is what makes the cache safe across backend
    # regimes: when availability changes (a new key added/removed) the same request can
    # resolve to a different backend, and a keep cached under backend A must NOT
    # short-circuit a run that now selects backend B (codex P2). `no_ai` skips vision, so
    # only resolve when we may actually call it.
    vision_backends = [] if no_ai else _ordered_vision_backends(models or [])
    backend = vision_backends[0] if vision_backends else None
    # The context key MUST fold in EVERY verdict input a cached keep is conditioned on
    # (codex P1/P2): project + intent + expect + the active --check set + the --before
    # baseline + a signature of the ACTIVE modules (names + source hashes) + the SELECTED
    # vision backend. A run with extra checks, a different baseline, a changed/added
    # module, or a different selected backend is a DIFFERENT namespace, so it never reuses
    # a keep earned under laxer conditions (which would bypass a vision-only module veto /
    # baseline comparison / a stricter backend).
    cache_context = cache.context_key(
        project=project, intent=intent, expect=expect,
        requested_checks=requested_checks, before=before,
        modules_signature=modules_signature(modules),
        selected_backend=backend,
    )
    if local_model and not no_ai:
        try:
            if cache.lookup(after, context=cache_context):
                return Verdict(
                    final="keep",
                    reason="known-good cache hit: pixel-identical to a previously-kept render (vision call skipped)",
                    source="local_model",
                    module_verdicts=module_cv,
                    cv_signals=signals,
                )
        except Exception:  # noqa: BLE001 — the cost-saver must never break a verification
            pass

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

    # --- Stage 2: visionClient (the primary judge). The backend was resolved above.
    questions = [q for m in modules for q in m.vision_questions(ctx)]
    schema = build_output_schema(_vision_field_names(modules))
    cap = capability_for(backend) if backend else None
    blocks = _build_blocks(after, before, expectation, signals, questions, cap)
    vision = _call_ai_vision_with_fallback(
        vision_backends, blocks=blocks, expectation=expectation, cv_signals=signals,
        output_schema=schema, timeout_s=vision_timeout, effort_override=effort_override,
    )

    # --- Stage 3: judge phase + policy decision. --------------------------------
    module_judgements = [m.judge(ctx, vision) for m in modules] if vision.available and vision.verdict else module_cv
    verdict = decide_from_vision(vision, signals, module_judgements)

    # --- Stage 2a populate: a FINAL `keep` (post-policy, not merely the model's raw
    # keep) seeds the known-good cache so the NEXT identical render short-circuits for
    # free. Only a final keep — a policy-downgraded/low-confidence keep must NOT be
    # learned as known-good. Best-effort; never breaks the verification. ------------
    # Key the populate by the ACTUAL backend that produced the keep (vision.backend),
    # not the first candidate.  If a fallback ran, the keep was earned by the fallback
    # backend; storing it under the primary candidate would let a future primary-backend
    # run reuse a keep that was never reviewed by that backend. (P1 fix.)
    # Intentional trade-off: a fallback-keyed keep is NOT reused by a later run that
    # re-selects the primary — the lookup namespace (primary) differs from the populate
    # namespace (fallback), so the next run calls fallback again.  This is conservative:
    # it never trusts a keep across backends in either direction.
    if local_model and verdict.final == "keep":
        try:
            actual_backend = vision.backend if vision.backend else backend
            populate_context = cache.context_key(
                project=project, intent=intent, expect=expect,
                requested_checks=requested_checks, before=before,
                modules_signature=modules_signature(modules),
                selected_backend=actual_backend,
            )
            cache.remember(after, context=populate_context)
        except Exception:  # noqa: BLE001
            pass
    return verdict


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


def _ordered_vision_backends(models: list[str]) -> list[str]:
    first = select_vision_backend(models)
    if first is None:
        return []
    ordered = select_vision_backends(models)
    return [first] + [model for model in ordered if model != first]


def _call_ai_vision_with_fallback(models: list[str], *, effort_override: EffortOverride | None = None, **kwargs) -> object:
    last = None
    for model in models:
        verdict = call_ai_vision(model, effort=effort_for_model(effort_override, model), **kwargs)
        if verdict.available and verdict.verdict in VISION_VERDICTS:
            return verdict
        last = verdict
    if last is not None:
        return last
    return call_ai_vision(None, **kwargs)
