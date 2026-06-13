"""The `VisualModule` contract (§4) — the interface a visual check implements.

A module is a small object declaring WHEN it activates, an optional pixel-level
`cv_check`, the `vision_questions` it contributes to the (single) vision call, and a
`judge` that combines its CV opinion with the model's answers into a sub-verdict.

Stage 1 ships the types + the Protocol + the three built-in modules' shape. The
per-project discovery / TOFU registry (§6) lands in Stage 2 (see `registry.py`); this
file deliberately has no discovery code, only the contract every module (built-in or
contributed) implements.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .contract import VisualExpectation

if TYPE_CHECKING:  # avoid an import cycle: vision_client imports module types too.
    from .vision_client import VisionVerdict

# A module sub-verdict decision. Modules can only ever make the final verdict
# STRICTER ('block' is a hard veto, §3.3.5) or stay out of the way ('abstain').
MODULE_DECISIONS = ("pass", "block", "abstain")


@dataclass(frozen=True)
class VisualContext:
    after_image: bytes
    before_image: bytes | None
    expectation: VisualExpectation
    cv_signals: "CvSignals"
    intent: str | None
    requested_checks: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ModuleVerdict:
    module: str
    decision: str  # one of MODULE_DECISIONS
    confidence: float
    questions: list[str] = field(default_factory=list)
    reason: str = ""


@runtime_checkable
class VisualModule(Protocol):
    name: str

    def activates(self, ctx: VisualContext) -> bool:
        """Declare WHEN this module runs (e.g. selection-highlight only when the
        verification concerns 'selection'). `--check <name>` force-activates."""
        ...

    def cv_check(self, ctx: VisualContext) -> ModuleVerdict | None:
        """Optional pixel-level check the module owns (None = no CV opinion). A
        `block` here can short-circuit before any vision call."""
        ...

    def vision_questions(self, ctx: VisualContext) -> list[str]:
        """Questions appended to the single vision prompt for this run."""
        ...

    def judge(self, ctx: VisualContext, vision: "VisionVerdict") -> ModuleVerdict:
        """Combine the module's CV opinion + the model's answers into a sub-verdict."""
        ...


# Re-exported here for the type annotation above; the concrete dataclass lives in
# cv_gate (it is what cvGate emits). Imported lazily to avoid a cycle at module load.
if TYPE_CHECKING:
    from .cv_gate import CvSignals
