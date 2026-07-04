"""selection-highlight — the worked per-project contributed module (§6).

This is the reference implementation of a *contributed* `VisualModule`: it is NOT
built into review. HyperIDE / hyper-canvas-draft ships it via its project manifest
(`.review/visual-modules.json`, `activates_on: ["selection"]`); the registry discovers,
TOFU-trusts, and loads it.

What it checks: a HyperCanvas selection frame is a 2px solid `rgb(59,130,246)` rectangle
outline (shared/canvas-interaction/overlay-renderer.ts SELECTION_BORDER). On a full
screenshot that outline is so thin a vision model routinely fails to tell whether it is
present at all — so "selection works" proofs slip through with NO frame drawn. This
module removes the guesswork with the SAME deterministic colour+shape detector as
`bin/frames-check`: it isolates frame pixels by colour, keeps only the ones whose shape
is a hollow thin rectangle, counts them, and turns "selection expected but no outline
present" into a hard veto.

REUSE: the detection is `bin/frames-check`'s `detect_frames`, imported by path so the
two never diverge (the bin script is the canonical detector; this module is its
VisualModule wrapper). A small self-contained fallback keeps the module working if the
bin script is unavailable in a given install layout.

Entry-point contract (what the registry loads): this file exposes a top-level `MODULE`
object satisfying the `VisualModule` Protocol.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from reviewlib.features.visual.intent_keywords import intent_mentions_tag
from reviewlib.features.visual.module_api import ModuleVerdict, VisualContext

# HyperCanvas selection outline (overlay-renderer.ts SELECTION_BORDER).
SELECTION_COLOR = (59, 130, 246)
# frames-check defaults — a hollow, big-enough rectangle in the selection blue.
_FUZZ = 14
_MIN_DIM = 24
_MAX_FILL = 0.30
_MIN_AREA = 150

# Tags that turn this module on (the manifest's activates_on, mirrored here so the
# module is self-describing even when loaded outside the registry, e.g. in a test).
ACTIVATES_ON = ("selection",)
_NAME = "selection-highlight"
_VISION_FIELD = "selection_present"


def _frames_check_detect():
    """Import `detect_frames` from the canonical `bin/frames-check` script by path.

    Returns the callable, or None if the script can't be located/imported (then the
    self-contained fallback is used). The script lives at <repo>/bin/frames-check;
    from this file that is parents[4]/bin/frames-check."""
    candidates = [
        Path(__file__).resolve().parents[4]
        / "bin"
        / "frames-check",  # repo/bin/frames-check
    ]
    import shutil

    on_path = shutil.which("frames-check")
    if on_path:
        candidates.append(Path(on_path))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "_frames_check_canonical", path
            )
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "detect_frames", None)
            if callable(fn):
                return fn
        except Exception:  # noqa: BLE001 — fall back to the self-contained detector
            continue
    return None


def detect_selection_frames(image: Path) -> list[dict]:
    """Detect HyperCanvas selection frames in `image`. Each frame: {x,y,w,h,area,
    fill_ratio}. Reuses bin/frames-check's `detect_frames` (canonical) when available."""
    detect = _frames_check_detect()
    if detect is not None:
        frames, mask = detect(
            Path(image), SELECTION_COLOR, _FUZZ, _MIN_DIM, _MAX_FILL, _MIN_AREA
        )
        try:
            Path(mask).unlink(missing_ok=True)
        except OSError:
            pass
        return frames
    return _fallback_detect(Path(image))


def _fallback_detect(image: Path) -> list[dict]:
    """Self-contained copy of the frames-check colour+shape detection (used only when
    the canonical bin script can't be imported). Same ImageMagick connected-components
    pipeline."""
    import re
    import subprocess
    import tempfile

    r, g, b = SELECTION_COLOR
    mask = Path(tempfile.mkstemp(suffix="-sel-mask.png")[1])
    proc = subprocess.run(
        [
            "magick",
            str(image),
            "-fuzz",
            f"{_FUZZ}%",
            "-fill",
            "white",
            "-opaque",
            f"rgb({r},{g},{b})",
            "-fuzz",
            "0",
            "-fill",
            "black",
            "+opaque",
            "white",
            "-define",
            "connected-components:verbose=true",
            "-define",
            f"connected-components:area-threshold={_MIN_AREA}",
            "-connected-components",
            "8",
            str(mask),
        ],
        capture_output=True,
        text=True,
    )
    mask.unlink(missing_ok=True)
    line_re = re.compile(
        r"^\s*\d+:\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s+[\d.,]+\s+(\d+)\s+s?rgba?\(([^)]+)\)"
    )
    frames: list[dict] = []
    for line in (proc.stdout + proc.stderr).splitlines():
        m = line_re.match(line)
        if not m:
            continue
        w, h, x, y, area = (int(m.group(i)) for i in range(1, 6))
        if not m.group(6).replace(" ", "").startswith("255,255,255"):
            continue
        bbox_area = w * h
        if bbox_area == 0:
            continue
        fill_ratio = area / bbox_area
        if min(w, h) >= _MIN_DIM and area >= _MIN_AREA and fill_ratio <= _MAX_FILL:
            frames.append(
                {
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "area": area,
                    "fill_ratio": round(fill_ratio, 4),
                }
            )
    frames.sort(key=lambda f: (f["y"], f["x"]))
    return frames


class _SelectionHighlightModule:
    name = _NAME
    activates_on = ACTIVATES_ON
    _vision_field = _VISION_FIELD

    def activates(self, ctx: VisualContext) -> bool:
        # The registry already gates on activates_on; this same check makes the module
        # usable standalone (in a test, or if a built-in path ever lists it). Active
        # when "selection" (or the module name) is requested, or the intent mentions it.
        requested = {c.lower() for c in ctx.requested_checks}
        if self.name.lower() in requested:
            return True
        tags = {t.lower() for t in self.activates_on}
        if requested & tags:
            return True
        # Free-text intent match: any tag mentioned verbatim OR via a registered
        # non-English synonym (e.g. a Russian caption saying "selected" — tg#6188).
        return bool(
            ctx.intent and any(intent_mentions_tag(ctx.intent, tag) for tag in tags)
        )

    def cv_check(self, ctx: VisualContext) -> ModuleVerdict | None:
        """Deterministic colour+shape detection. When a selection is expected and NO
        outline is present → BLOCK (a hard veto, short-circuits before any vision call).
        When present → pass. The check runs on the after-image written to a temp file
        (cvGate works on paths; the context carries bytes)."""
        import tempfile

        tmp = Path(tempfile.mkstemp(suffix="-sel-after.png")[1])
        try:
            tmp.write_bytes(ctx.after_image)
            frames = detect_selection_frames(tmp)
        except Exception as exc:  # noqa: BLE001 — a detector failure must not crash the run
            return ModuleVerdict(
                module=self.name,
                decision="abstain",
                confidence=0.0,
                reason=f"detector error: {exc}",
            )
        finally:
            tmp.unlink(missing_ok=True)
        if frames:
            return ModuleVerdict(
                module=self.name,
                decision="pass",
                confidence=0.9,
                reason=f"selection outline present ({len(frames)} frame(s))",
            )
        return ModuleVerdict(
            module=self.name,
            decision="block",
            confidence=0.9,
            reason="selection expected but NO 2px rgb(59,130,246) outline detected",
        )

    def vision_questions(self, ctx: VisualContext) -> list[str]:
        return [
            "Is a thin 2px BLUE (rgb(59,130,246)) selection outline drawn as a hollow "
            "rectangle around exactly one element? Answer in the `selection_present` "
            "boolean field (true only if a real selection frame is visible)."
        ]

    def judge(self, ctx: VisualContext, vision) -> ModuleVerdict:
        # If the model confirms the outline is absent, veto regardless of CV borderline.
        answer = getattr(vision, "module_answers", {}).get(self._vision_field)
        if answer is False:
            return ModuleVerdict(
                module=self.name,
                decision="block",
                confidence=getattr(vision, "confidence", 0.0),
                reason="vision confirmed no selection outline present",
            )
        # Otherwise defer to the deterministic CV opinion (self-contained).
        cv = self.cv_check(ctx)
        return cv or ModuleVerdict(
            module=self.name, decision="abstain", confidence=0.0, reason="no opinion"
        )


MODULE = _SelectionHighlightModule()
