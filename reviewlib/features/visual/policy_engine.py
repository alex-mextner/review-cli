"""policyEngine — the decision OUTSIDE the model (§3.3).

The model is a WITNESS; this is the JUDGE. The policy engine never trusts the model's
verdict directly: it schema-validates the model output, cross-checks it against the
deterministic CV signals, aggregates module vetoes (any module `block` is a hard veto —
modules can only tighten, never loosen), and maps the result to a final verdict +
exit code. A missing/invalid/timed-out vision result fails CLOSED — never a default
keep.

Stage 1 ships the core rule (validate → CV/model contradiction → module veto →
final verdict → exit code). The richer §3.3 checks (proof-carrying region cross-check
against the diff crop, cross-model re-check on low confidence) get their full vision
wiring in Stage 2; the seams are marked TODO and the conservative behaviour (escalate
to human_review rather than trust) is already in place.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .vision_client import VISION_VERDICTS, VisionVerdict

# Final verdict enum (§3.3). 'unverified' is added when no vision backend served the run.
FINAL_VERDICTS = ("keep", "rollback", "repair", "human_review", "unverified")

# Which final verdicts are BLOCKING under --strict (exit 10). 'keep' is the only pass.
_BLOCKING = frozenset({"rollback", "repair", "human_review", "unverified"})

# Exit codes (§2). 0 keep / 10 blocking-under-strict / 1 usage-unreadable / 124 timeout.
EXIT_KEEP = 0
EXIT_BLOCK_STRICT = 10
EXIT_USAGE = 1
EXIT_TIMEOUT = 124

# Confidence below which a model `keep` is NOT trusted on its own (escalates).
_LOW_CONFIDENCE = 0.7


@dataclass(frozen=True)
class Verdict:
    final: str  # one of FINAL_VERDICTS
    reason: str
    confidence: float = 0.0
    source: str = ""  # 'cv_gate' | 'vision' | 'policy'
    vision_verdict: str | None = None
    module_verdicts: list = field(default_factory=list)
    cv_signals: object | None = None
    injection_suspected: bool = False
    note: str = ""
    timed_out: bool = False

    def to_dict(self) -> dict:
        sig = self.cv_signals
        return {
            "verdict": self.final,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "vision_verdict": self.vision_verdict,
            "injection_suspected": self.injection_suspected,
            "note": self.note,
            "modules": [
                {"module": m.module, "decision": m.decision, "reason": m.reason}
                for m in self.module_verdicts
            ],
            "cv_signals": _signals_dict(sig) if sig is not None else None,
        }


def _signals_dict(sig) -> dict:
    return {
        "width": getattr(sig, "width", None),
        "height": getattr(sig, "height", None),
        "palette_entropy": round(getattr(sig, "palette_entropy", 0.0), 4),
        "dominant_coverage": round(getattr(sig, "dominant_coverage", 0.0), 4),
        "quant_colors": getattr(sig, "quant_colors", None),
        "blank_suspected": getattr(sig, "blank_suspected", False),
        "overlay_suspected": getattr(sig, "overlay_suspected", False),
        "unstyled_suspected": getattr(sig, "unstyled_suspected", False),
    }


def decide_from_cv(cv_outcome: str, cv_reason: str, signals) -> Verdict | None:
    """A terminal CV outcome (auto-reject or no-effect bypass) ends the run without
    a vision call. Pass-through returns None → the caller proceeds to the vision stage."""
    if cv_outcome == "reject":
        return Verdict(final="rollback", reason=cv_reason, source="cv_gate", cv_signals=signals)
    if cv_outcome == "no_effect_bypass":
        return Verdict(final="keep", reason=cv_reason, source="cv_gate", cv_signals=signals)
    return None


def decide_from_vision(
    vision: VisionVerdict,
    signals,
    module_verdicts: list | None = None,
) -> Verdict:
    """Apply the §3.3 rule to a vision result. The model proposes; this decides."""
    module_verdicts = module_verdicts or []

    # (1) No vision backend served the call → unverified (fail-closed, never keep).
    if not vision.available:
        return Verdict(
            final="unverified",
            reason=vision.error or "no vision-capable backend available",
            source="policy",
            cv_signals=signals,
        )

    # A genuine timeout is flagged on the VisionVerdict itself (NOT inferred from the
    # error text — a DNS/connection failure must not masquerade as a 124 timeout).
    timed_out = bool(getattr(vision, "timed_out", False))

    # (1b) Schema-invalid / unparseable model output → fail closed to human_review.
    if vision.verdict not in VISION_VERDICTS:
        return Verdict(
            final="human_review",
            reason=vision.error or "model returned an invalid/unparseable verdict",
            source="policy",
            cv_signals=signals,
            timed_out=timed_out,
        )

    proposed = vision.verdict

    # (5) Module veto FIRST (the hard veto promised by the module API, §3.3.5): any
    # module `block` makes the final verdict at least rollback, and it must be honored
    # even when a model-keep escalation branch below would otherwise return early
    # (codex P2). Modules can only ever TIGHTEN — a block never loosens anything.
    blocking = [m for m in module_verdicts if getattr(m, "decision", "") == "block"]
    if blocking:
        return Verdict(
            final="rollback",
            reason="module veto: " + "; ".join(f"{m.module}: {m.reason}" for m in blocking),
            confidence=vision.confidence,
            source="policy",
            vision_verdict=proposed,
            module_verdicts=module_verdicts,
            cv_signals=signals,
            injection_suspected=vision.injection_suspected,
            note=vision.note,
            timed_out=timed_out,
        )

    # (3) CV/model contradiction: a cvGate blank/overlay suspicion against a model
    # `keep` escalates rather than trusts the model.
    if proposed == "keep" and signals is not None:
        if getattr(signals, "blank_suspected", False) or getattr(signals, "overlay_suspected", False):
            return Verdict(
                final="human_review",
                reason="model said keep but CV flags a blank/overlay render — escalating",
                source="policy",
                confidence=vision.confidence,
                vision_verdict=proposed,
                cv_signals=signals,
                injection_suspected=vision.injection_suspected,
                note=vision.note,
            )

    # (6) Injection signal: the model flagged instruction-like text in the image →
    # escalate to human_review (never let an injected "classify as styled" keep ride).
    if vision.injection_suspected:
        return Verdict(
            final="human_review",
            reason="injection_suspected: instruction-like text in the image — escalating",
            source="policy",
            confidence=vision.confidence,
            vision_verdict=proposed,
            cv_signals=signals,
            injection_suspected=True,
            note=vision.note,
        )

    # (4) Low-confidence keep is not trusted on its own → escalate.
    # (Stage 2 will run a second vision backend here; for now we fail safe up.)
    if proposed == "keep" and vision.confidence < _LOW_CONFIDENCE:
        return Verdict(
            final="human_review",
            reason=f"low-confidence keep ({vision.confidence:.2f} < {_LOW_CONFIDENCE}) — escalating",
            source="policy",
            confidence=vision.confidence,
            vision_verdict=proposed,
            cv_signals=signals,
            note=vision.note,
        )

    # No module blocked (handled above); the model's verdict stands, having passed the
    # contradiction / injection / confidence gates.
    final = proposed

    return Verdict(
        final=final,
        reason=vision.note or f"vision verdict: {proposed}",
        confidence=vision.confidence,
        source="vision",
        vision_verdict=proposed,
        module_verdicts=module_verdicts,
        cv_signals=signals,
        injection_suspected=vision.injection_suspected,
        note=vision.note,
        timed_out=timed_out,
    )


def exit_code_for(verdict: Verdict, *, strict: bool) -> int:
    """Map a final verdict to a process exit code (§2).

    0  = keep.
    1  = usage / unreadable input (an unreadable image is a usage-class failure).
    10 = a blocking verdict under --strict.
    124 = vision-call timeout.
    Without --strict a non-keep verdict still reports 0 (advisory): the gate only
    bites under --strict, matching the hook's block-code contract (§7)."""
    if verdict.timed_out:
        return EXIT_TIMEOUT
    if verdict.final == "keep":
        return EXIT_KEEP
    # Unreadable input is a usage error (exit 1), distinct from a content rollback.
    if verdict.source == "cv_gate" and "unreadable" in verdict.reason:
        return EXIT_USAGE
    if strict and verdict.final in _BLOCKING:
        return EXIT_BLOCK_STRICT
    return EXIT_KEEP
