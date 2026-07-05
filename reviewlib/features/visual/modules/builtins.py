"""Built-in visual modules (§4): style-presence, blank-frame, error-overlay, error-text.

The first three wrap cvGate signals as `VisualModule`s. `error-text` is vision-only:
there is no reliable pixel heuristic for arbitrary error/failure text, so it abstains
from CV and asks the existing single vision call one extra semantic question.
"""
from __future__ import annotations

from ..module_api import ModuleVerdict, VisualContext, VisualModule
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


class ErrorTextModule:
    """Vision-only check for visible runtime error/failure text anywhere in the UI.

    There is no pixel heuristic for this: a runtime error can render as plain text in
    a console/log panel, toast, status bar, or any other surface. `cv_check` therefore
    always abstains; the existing single vision call gets one extra semantic question.
    """

    name = "error-text"
    _vision_field = "error_text_visible"
    _question = (
        "Look for visible text ANYWHERE in the image — including inside a console/log "
        "panel, a toast, a status bar, or any other UI surface — that reads as an actual "
        "runtime error, exception, or failure diagnostic (e.g. mentions of 'error', "
        "'exception', 'fail'/'failure', 'crash', 'traceback', '404'/'not found', or a "
        "similar failure message). Only answer true for a genuine error/failure diagnostic, "
        "NOT for a benign occurrence of these words (a filename, a doc string, a UI label "
        "like a menu item, or a summary line such as '0 errors'). Answer in the "
        "`error_text_visible` boolean field."
    )
    _block_reason = "vision confirmed visible error/exception/failure text in the screenshot"

    def activates(self, ctx: VisualContext) -> bool:
        return True

    def cv_check(self, ctx: VisualContext) -> ModuleVerdict | None:
        return None

    def vision_questions(self, ctx: VisualContext) -> list[str]:
        return [self._question]

    def judge(self, ctx: VisualContext, vision: VisionVerdict) -> ModuleVerdict:
        if vision.module_answers.get(self._vision_field) is True:
            return ModuleVerdict(
                module=self.name,
                decision="block",
                confidence=vision.confidence,
                reason=self._block_reason,
            )
        return ModuleVerdict(
            module=self.name, decision="abstain", confidence=0.0, reason="no opinion"
        )


def builtin_modules() -> list[VisualModule]:
    return [
        StylePresenceModule(),
        BlankFrameModule(),
        ErrorOverlayModule(),
        ErrorTextModule(),
    ]
