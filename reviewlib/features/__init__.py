"""reviewlib.features — per-feature module packages.

Features are SUBPACKAGES of reviewlib (not a top-level `features` package), so the
whole tool has a single top-level package and a single sys.path insertion point —
no squatting of a generic `features` name in site-packages. See the Stage 0
packaging decision (docs/architecture-visual-verification.md §8.1).
"""
