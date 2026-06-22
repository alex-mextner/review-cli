"""qa: the write/exec agentic TESTER subsystem (Phases 2+3 of ``review qa``).

WHAT THIS PACKAGE IS. ``reviewlib/modes/qa.py`` is the thin MODE (CLI surface + the
no-suites gate). This package is the ENGINE behind it: (Phase 2) the write/exec launcher
that spawns ONE un-caged backend (claude default, codex alternate) inside an isolated
``git worktree`` of the System-Under-Test, the tester SYSTEM PROMPT builder, and the
``## QA RESULTS`` tail parser; and (Phase 3) the deterministic SUT-ENV lifecycle
(``env.py`` + ``config.py``) that stands the env up BEFORE the executor drives it —
stage-detect → reuse / ``qa/setup.sh`` hook / compose bring-up → health-gate → GUARANTEED
teardown of only what this run brought up. See ``docs/specs/review-qa.md`` §7.2/§8/§9.

WHY IT IS A SEPARATE PACKAGE FROM THE READ-ONLY BOARD. Every other review backend is
read-only BY CONSTRUCTION — codex ``-s read-only``, the claude ``--disallowedTools``
spawn, the opencode deny-all agent (``reviewlib/backends.py`` lines 74/99/233/1175).
qa needs the OPPOSITE capability profile, so it deliberately does NOT ride
``run_panel``/the failover board and never calls ``_ensure_opencode_readonly_agent``.
Keeping the un-caged path in its OWN module quarantines the one place review-cli grants
write/exec, so a future hardening pass can see it as the explicit exception it is — there
is a matching note next to ``_READONLY_AGENT_DENIED_PERMISSIONS`` in ``backends.py``.

INVARIANT: nothing here may be reached BEFORE ``modes/qa.py``'s no-suites gate has
returned a non-empty suite list — a write/exec agent must never spawn for an empty run.
"""
from __future__ import annotations

from .config import QaConfigError, SutConfig, load_qa_config
from .env import EnvError, EnvHandle, EnvMode, bring_up_env
from .executor import (
    DirtyInPlaceError,
    QaRunOutcome,
    SutIsolationError,
    build_tester_prompt,
    parse_qa_results,
    run_tester,
    verdict_to_exit_code,
)

__all__ = [
    "DirtyInPlaceError",
    "EnvError",
    "EnvHandle",
    "EnvMode",
    "QaConfigError",
    "QaRunOutcome",
    "SutConfig",
    "SutIsolationError",
    "bring_up_env",
    "build_tester_prompt",
    "load_qa_config",
    "parse_qa_results",
    "run_tester",
    "verdict_to_exit_code",
]
