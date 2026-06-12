"""Synthetic fixture images for the visual-verification CV tests.

No committed binary blobs — every fixture is generated deterministically at test
time (Pillow, already a transitive dep via the repo's image tooling). Covers the
four cvGate auto-reject classes plus a clearly-styled pass-through render. Mirrors
the golden-image suite the Stage-1 gate calls for (§10).
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT = ImageFont.load_default()


def blank_white(path: Path, size: tuple[int, int] = (400, 300)) -> Path:
    """A pure-white canvas — the classic failed-mount / blank render."""
    Image.new("RGB", size, "white").save(path)
    return path


def solid_fill(path: Path, color: str = "rgb(30,30,46)", size: tuple[int, int] = (400, 300)) -> Path:
    """A single solid colour filling the whole frame."""
    Image.new("RGB", size, color).save(path)
    return path


def unstyled_render(path: Path, size: tuple[int, int] = (400, 300)) -> Path:
    """Black serif text on bare white, no chrome — the no-CSS FOUC signature."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    lines = [
        "Welcome to My Site",
        "This is unstyled body text on a white page.",
        "No CSS has loaded; the browser default serif shows.",
        "Another plain paragraph with nothing themed around it.",
    ]
    y = 30
    for line in lines:
        d.text((20, y), line, fill="black", font=_FONT)
        y += 40
    img.save(path)
    return path


def error_overlay(path: Path, size: tuple[int, int] = (400, 300)) -> Path:
    """A dev-server-style dark error overlay with a large red text block."""
    img = Image.new("RGB", size, "rgb(20,20,20)")
    d = ImageDraw.Draw(img)
    # The classic CRA/Vite red overlay banner.
    d.rectangle([0, 0, size[0], 70], fill="rgb(174,30,30)")
    d.text((20, 20), "Failed to compile", fill="white", font=_FONT)
    d.text((20, 100), "Module not found: Can't resolve './App'", fill="rgb(255,120,120)", font=_FONT)
    d.text((20, 130), "  at ./src/index.js:3:1", fill="rgb(200,200,200)", font=_FONT)
    img.save(path)
    return path


def dark_ui_render(path: Path, size: tuple[int, int] = (400, 300)) -> Path:
    """A legitimately-styled DARK monochrome UI (terminal/dashboard): near-black bg with
    light grayscale text. Near-grayscale + low entropy + high bg coverage like an
    unstyled page, but a DARK background — must NOT trip the (near-white) unstyled
    heuristic, so it stands in for 'a valid dark render that pixel CV must pass through.'"""
    img = Image.new("RGB", size, "rgb(18,18,18)")
    d = ImageDraw.Draw(img)
    lines = ["$ npm run build", "  compiled successfully", "  ready on :3000", "  watching for changes"]
    y = 30
    for line in lines:
        d.text((20, y), line, fill="rgb(210,210,210)", font=_FONT)
        y += 40
    img.save(path)
    return path


def styled_render(path: Path, size: tuple[int, int] = (400, 300)) -> Path:
    """A properly-styled render: themed surfaces, accent colours, varied palette.

    Deterministic (seeded) so the test is stable, but rich enough that none of the
    auto-reject heuristics fire — it must reach pass-through.
    """
    rng = random.Random(42)
    img = Image.new("RGB", size, "rgb(245,247,250)")
    d = ImageDraw.Draw(img)
    # A header bar, a sidebar, a few cards with distinct accent colours.
    d.rectangle([0, 0, size[0], 56], fill="rgb(37,99,235)")
    d.rectangle([0, 56, 90, size[1]], fill="rgb(30,41,59)")
    accents = [(16, 185, 129), (239, 68, 68), (234, 179, 8), (139, 92, 246), (14, 165, 233)]
    for i in range(6):
        x = 110 + (i % 3) * 95
        y = 80 + (i // 3) * 100
        col = accents[rng.randrange(len(accents))]
        d.rounded_rectangle([x, y, x + 80, y + 80], radius=8, fill=col)
        d.rectangle([x + 8, y + 60, x + 72, y + 70], fill="rgb(255,255,255)")
    img.save(path)
    return path
