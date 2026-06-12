"""VisualExpectation — the machine-derived verification contract (§4.1).

The contract is derived from the trusted, machine-side inputs (`--expect` and, when
a `--before` image is supplied, the CV diff). The actor's free-text `--intent` is
UNTRUSTED and may only *tighten* the contract (raise risk, narrow the diff policy),
never loosen it — see §5/§6. Stage 1 ships the derivation skeleton; the bbox hints
stay empty until a module or a `--before` diff supplies them (the image-only world
has no DOM to source exact rects from).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# The expectation kinds the contract understands (§4.1). 'style' is the default when
# nothing more specific is inferred — a local change is expected, unrelated layout
# drift is a regression.
EXPECT_KINDS = (
    "zero-diff",
    "move",
    "resize",
    "style",
    "wrap",
    "insert",
    "delete",
    "text",
)

# How much visual change the contract tolerates. Drives cvGate thresholds: a
# blank-canvas reject is only fatal when diff_policy != 'global' (a full-page
# repaint legitimately changes everything).
DIFF_POLICIES = ("zero", "local", "regional", "global")
RISK_LEVELS = ("low", "normal", "high")

# Kinds that expect NO visual drift — any real change is a regression.
_ZERO_DRIFT_KINDS = frozenset({"zero-diff", "wrap"})


@dataclass(frozen=True)
class BBox:
    """A pixel-space bounding box (left, top, width, height)."""

    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class VisualExpectation:
    kind: str
    diff_policy: str
    risk: str
    # Bbox hints are optional in the image-only world; usually [] unless a module or
    # a --before diff supplies them.
    allowed_change_regions: list[BBox] = field(default_factory=list)
    invariant_regions: list[BBox] = field(default_factory=list)


def _diff_policy_for(kind: str) -> str:
    if kind in _ZERO_DRIFT_KINDS:
        return "zero"
    if kind in ("move", "resize", "insert", "delete"):
        return "regional"
    # style / text → a local change is expected.
    return "local"


def derive_contract(
    expect: str | None,
    intent: str | None,
    *,
    has_before: bool = False,
) -> VisualExpectation:
    """Derive a VisualExpectation from --expect + (untrusted) --intent.

    The kind comes from `--expect` (machine-side); an unknown/absent value defaults
    to 'style'. `intent` is UNTRUSTED and may only TIGHTEN the result: an intent that
    mentions a regression-sensitive change can raise risk, but it can never relax the
    diff policy below what the kind implies. Stage 1's intent influence is deliberately
    narrow (risk bump only); the full intent→contract NLP is out of scope here.
    """
    kind = (expect or "").strip().lower()
    if kind not in EXPECT_KINDS:
        kind = "style"

    diff_policy = _diff_policy_for(kind)

    # Risk floor by kind: zero-drift expectations are inherently high-risk (any change
    # is a regression). Everything else starts 'normal'.
    risk = "high" if kind in _ZERO_DRIFT_KINDS else "normal"

    # Intent may only TIGHTEN: a regression-sensitive intent raises risk; it can never
    # lower it. (Loosening via prose is the injection vector §5 forbids.)
    if intent and _intent_raises_risk(intent) and risk != "high":
        risk = "high"

    return VisualExpectation(kind=kind, diff_policy=diff_policy, risk=risk)


# Words in an actor's free-text intent that justify TIGHTENING the contract (raising
# risk). Matching is a heuristic and only ever makes the contract stricter, so a
# false positive is safe (it can at most force an extra check), and an injected
# "ignore previous instructions" string cannot loosen anything.
_RISK_WORDS = ("regression", "do not change", "must not", "pixel-perfect", "critical", "no change")


def _intent_raises_risk(intent: str) -> bool:
    low = intent.lower()
    return any(word in low for word in _RISK_WORDS)
