"""Reference implementations of *contributed* visual modules (§6).

Unlike `modules/` (the built-ins, trusted implicitly), these are reference impls of
per-project contributed modules — a real project ships its own module file in its repo
and points `.review/visual-modules.json` at it. `selection_highlight.py` is the worked
example HyperIDE contributes; it lives here so review's own fixtures/tests have a
concrete contributed module to load through the registry (TOFU-trusted like any other).
"""
