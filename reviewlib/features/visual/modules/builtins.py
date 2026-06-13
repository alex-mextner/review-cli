"""The three built-in visual modules (§4): style-presence, blank-frame, error-overlay.

Each wraps a cvGate signal as a `VisualModule`. They activate by default on any
`--visual` run, contribute a question to the single vision call, and turn their CV
suspicion into a `block` sub-verdict (a hard veto in the policy engine). They are the
module-shaped expression of the cvGate heuristics — so the same signal can be
force-run (`--check style-presence`) and can vote in the vision/judge phases.
"""
from __future__ import annotations

from ..module_api import ModuleVerdict, VisualContext
from ..vision_client import VisionVerdict


class _SignalModule:
    """Shared base: a module backed by one boolean CvSignals flag + a vision question."""

    name = "base"
    _flag = ""
    _question = ""
    _block_reason = ""
    _vision_field = ""

    def activates(self, ctx: VisualContext) -> bool:
        # Built-ins are the generic "is this a real styled render" check; they activate
        # by default on every --visual run (and are, of course, also force-activatable).
        return True

    def cv_check(self, ctx: VisualContext) -> ModuleVerdict | None:
        sig = ctx.cv_signals
        if sig is None:
            return None
        if getattr(sig, self._flag, False):
            return ModuleVerdict(
                module=self.name, decision="block", confidence=0.95,
                reason=self._block_reason,
            )
        return ModuleVerdict(module=self.name, decision="pass", confidence=0.6, reason="cv ok")

    def vision_questions(self, ctx: VisualContext) -> list[str]:
        return [self._question]

    def judge(self, ctx: VisualContext, vision: VisionVerdict) -> ModuleVerdict:
        # If the model answered our boolean field as the bad case, block.
        if self._vision_field and vision.module_answers.get(self._vision_field) is True:
            return ModuleVerdict(
                module=self.name, decision="block", confidence=vision.confidence,
                reason=f"vision confirmed {self._vision_field}",
            )
        # Otherwise defer to the CV opinion (re-run it so the judge is self-contained).
        cv = self.cv_check(ctx)
        return cv or ModuleVerdict(module=self.name, decision="abstain", confidence=0.0, reason="no opinion")


class StylePresenceModule(_SignalModule):
    name = "style-presence"
    _flag = "unstyled_suspected"
    _question = (
        "Is this a bare, UNSTYLED render — default browser serif text on a blank page "
        "with no themed surfaces or chrome (i.e. CSS failed to load)? Answer in the "
        "`unstyled` boolean field."
    )
    _block_reason = "unstyled render: bare default-serif text, no CSS"
    _vision_field = "unstyled"


class BlankFrameModule(_SignalModule):
    name = "blank-frame"
    _flag = "blank_suspected"
    _question = (
        "Is the canvas blank — a solid single-colour fill with no content (a failed "
        "mount / FOUC)? Answer in the `blank` boolean field."
    )
    _block_reason = "blank/solid canvas (failed mount / FOUC)"
    _vision_field = "blank"


class ErrorOverlayModule(_SignalModule):
    name = "error-overlay"
    _flag = "overlay_suspected"
    _question = (
        "Is a dev-server or runtime ERROR OVERLAY visible (e.g. a red 'Failed to "
        "compile' banner / stack trace)? Answer in the `error_overlay` boolean field."
    )
    _block_reason = "dev-server / runtime error-overlay signature"
    _vision_field = "error_overlay"


def builtin_modules() -> list[_SignalModule]:
    return [StylePresenceModule(), BlankFrameModule(), ErrorOverlayModule()]
