"""reviewlib — the review CLI, decomposed into a package (Stage 0).

The single-file `bin/review` was split into focused modules (process, backends,
config, panel, install, modes/) with a thin `bin/review` shim that imports
`reviewlib.cli:main`. This `__init__` re-exports the stable public surface so
callers and tests can `from reviewlib import _run_streamed, review_claude, ...`
without caring which submodule each symbol lives in.

NOTE: `review_claude` and the other backends resolve `_which` / `_run_streamed`
through the `reviewlib.backends` module namespace, so tests that monkeypatch
those must patch `reviewlib.backends` (not this façade).
"""
from __future__ import annotations

# Re-exported for callers/tests that referenced these off the old single-file module
# (e.g. `review.subprocess`, `review.Path`).
import subprocess  # noqa: F401
from pathlib import Path  # noqa: F401

from .backends import (
    ReviewResult,
    _ensure_opencode_readonly_agent,
    _gemini_key,
    _payload,
    _which,
    backend_available,
    resolve_backend,
    review_claude,
    review_codex,
    review_gemini,
    review_opencode,
)
from .config import (
    CONFIG_PATH,
    DEFAULT_MODELS,
    DEFAULT_PROMPT,
    MODEL_ALIASES,
    MODERATOR_CANDIDATES,
    PANEL_TIMEOUT_DEFAULT,
    _expand_alias,
    _split_models,
    load_config,
)
from .panel import PanelJob, format_result, pick_moderator, run_panel, run_single
from .process import _kill_tree, _open_log, _run, _run_streamed, log_dir

__all__ = [
    "subprocess",
    "Path",
    "ReviewResult",
    "PanelJob",
    "_run",
    "_run_streamed",
    "_kill_tree",
    "_open_log",
    "log_dir",
    "_which",
    "_payload",
    "_gemini_key",
    "_ensure_opencode_readonly_agent",
    "review_codex",
    "review_gemini",
    "review_claude",
    "review_opencode",
    "resolve_backend",
    "backend_available",
    "format_result",
    "pick_moderator",
    "run_panel",
    "run_single",
    "load_config",
    "_split_models",
    "_expand_alias",
    "DEFAULT_MODELS",
    "DEFAULT_PROMPT",
    "MODEL_ALIASES",
    "CONFIG_PATH",
    "PANEL_TIMEOUT_DEFAULT",
    "MODERATOR_CANDIDATES",
]
