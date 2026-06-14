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
    _commandcode_key,
    _ensure_opencode_readonly_agent,
    _gemini_key,
    _payload,
    _which,
    _zai_key,
    backend_available,
    resolve_backend,
    resolve_backend_mode,
    review_claude,
    review_codex,
    review_commandcode,
    review_gemini,
    review_opencode,
    review_zai,
)
from .config import (
    CONFIG_PATH,
    DEFAULT_BOARD,
    DEFAULT_MODELS,
    DEFAULT_PROMPT,
    MODEL_ALIASES,
    MODERATOR_CANDIDATES,
    PANEL_TIMEOUT_DEFAULT,
    REVIEW_ROLES,
    BoardReviewer,
    _expand_alias,
    _split_models,
    load_board,
    load_config,
)
from .panel import (
    PanelJob,
    begin_call_tally,
    build_board_jobs,
    end_call_tally,
    format_result,
    pick_moderator,
    run_panel,
    run_single,
)
from .backstop import (
    BACKSTOP_EXIT_CODE,
    MAX_BACKSTOP_SECONDS,
    backstop_seconds,
    run_backstop,
)
from .process import (
    _kill_tree,
    _open_log,
    _run,
    _run_streamed,
    kill_live_children,
    log_dir,
    write_sidecar_log,
)
from .stats import (
    announce_eta,
    estimate_eta,
    eta_line,
    fmt_duration,
    record_run,
    stats_path,
)

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
    "write_sidecar_log",
    "_which",
    "_payload",
    "_gemini_key",
    "_zai_key",
    "_commandcode_key",
    "_ensure_opencode_readonly_agent",
    "review_codex",
    "review_gemini",
    "review_claude",
    "review_opencode",
    "review_zai",
    "review_commandcode",
    "resolve_backend",
    "resolve_backend_mode",
    "backend_available",
    "format_result",
    "pick_moderator",
    "run_panel",
    "run_single",
    "begin_call_tally",
    "end_call_tally",
    "build_board_jobs",
    "record_run",
    "estimate_eta",
    "eta_line",
    "announce_eta",
    "fmt_duration",
    "stats_path",
    "run_backstop",
    "backstop_seconds",
    "kill_live_children",
    "MAX_BACKSTOP_SECONDS",
    "BACKSTOP_EXIT_CODE",
    "load_config",
    "load_board",
    "_split_models",
    "_expand_alias",
    "BoardReviewer",
    "DEFAULT_BOARD",
    "REVIEW_ROLES",
    "DEFAULT_MODELS",
    "DEFAULT_PROMPT",
    "MODEL_ALIASES",
    "CONFIG_PATH",
    "PANEL_TIMEOUT_DEFAULT",
    "MODERATOR_CANDIDATES",
]
