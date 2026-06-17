"""Mode registry — the core analogue of `features/visual/registry.py`.

The visual feature discovers self-describing `VisualModule`s (built-in + per-project
manifests) and folds them into one pipeline. This registry does the same for the core
review MODES: each mode is a self-describing module that exposes a top-level
`MODE = ModeSpec(...)` (see `modes/contract.py`); the registry collects them so:

  * the CLI dispatches `review <subcommand>` to the right mode WITHOUT a per-mode
    `if args.x is not None` ladder — it looks the verb up here;
  * `review --help` lists the modes from this single source of truth;
  * adding a mode = drop a `modes/<name>.py` exposing a `MODE` descriptor and list it
    in `MODES` below. No `cli.py` surgery.

Built-in modes are listed explicitly (like the visual feature's built-in modules in
`features/visual/modules/`): an explicit list is auditable and import-order-stable, and
keeps the hot review path free of a directory scan. A future "drop a Python file into a
modes plugin dir" discovery step would mirror `features/visual/registry.discover_specs`
— the contract (`ModeSpec` + a top-level `MODE`) is already discovery-ready.
"""
from __future__ import annotations

from dataclasses import dataclass

from .brainstorm import MODE as _BRAINSTORM_MODE
from .brainstorm import brainstorm_pool
from .contract import ModeSpec
from .just_ask import MODE as _JUST_ASK_MODE
from .quorum import MODE as _QUORUM_MODE
from .review import MODE as _REVIEW_MODE

# The built-in review modes, in the order they appear in `--help`. `review` is first
# (it is the default). Each entry is a self-describing `ModeSpec` exposed by its module
# as a top-level `MODE` — exactly how a visual module exposes a top-level `MODULE`.
MODES: tuple[ModeSpec, ...] = (
    _REVIEW_MODE,
    _BRAINSTORM_MODE,
    _JUST_ASK_MODE,
    _QUORUM_MODE,
)

# The diff-review mode's stable `name` (its run-stats key / dispatch identity). Its
# user-facing SUBCOMMAND is `diff` (renamed from the stuttering `review review`); a bare
# `review` no longer runs it — bare `review` prints HELP, and the diff review is reached
# explicitly via `review diff`.
DIFF_MODE_NAME = "review"


def iter_modes() -> tuple[ModeSpec, ...]:
    """All registered modes, in help order."""
    return MODES


def get_mode(name_or_subcommand: str) -> ModeSpec | None:
    """Resolve a mode by its subcommand verb (or one of its aliases). Returns None for
    an unrecognized verb so the CLI can fall back to the default mode."""
    for mode in MODES:
        if name_or_subcommand == mode.subcommand or name_or_subcommand in mode.aliases:
            return mode
    return None


def diff_mode() -> ModeSpec:
    """The diff-review mode (subcommand `diff`). Resolved by stable NAME, not subcommand,
    so the lookup is stable even as the verb evolved (`review` -> `review diff`)."""
    for mode in MODES:
        if mode.name == DIFF_MODE_NAME:
            return mode
    raise AssertionError("DIFF_MODE_NAME must name a registered mode")


_KNOWN_SUBCOMMANDS: frozenset[str] = frozenset(
    {m.subcommand for m in MODES} | {a for m in MODES for a in m.aliases}
)


def known_subcommands() -> frozenset[str]:
    """Every verb (subcommand + aliases) that selects a mode — the set the CLI checks to
    decide whether argv[0] is a mode subcommand or should fall through to the default.
    Precomputed once (MODES is a module constant)."""
    return _KNOWN_SUBCOMMANDS


# The flags this redesign REMOVED (the old mode flags). The CLI rejects them with a
# helpful "use the subcommand" message instead of silently treating the value as a
# positional. Maps the dead flag -> the subcommand that replaces it.
REMOVED_MODE_FLAGS: dict[str, str] = {
    "--brainstorm": "brainstorm",
    "--quorum": "quorum",
    "--just-ask": "just-ask",
}


# Subcommand VERBS that were renamed away. `review review …` (the old stuttering diff
# review) is gone — the diff review is `review diff` now. Rather than silently running it
# (the old default-mode behavior, which was the mistake this migration fixes) or letting
# it fall through to a confusing parse, the CLI prints a one-line "use `review <new>`"
# pointer and exits with the usage code — exactly like the removed mode FLAGS above.
# Maps the dead verb -> the subcommand that replaces it.
REMOVED_SUBCOMMANDS: dict[str, str] = {
    "review": "diff",
}


@dataclass(frozen=True)
class RemovedFlag:
    """A flag that was REMOVED outright — it has NO replacement subcommand (unlike the
    mode flags in REMOVED_MODE_FLAGS, which map to a verb). `reason` is WHY it is gone
    (the PR / refactor that dropped it); `fix` is the concrete HOW-TO-FIX line (what to
    remove and from where). The CLI prints both as a structured 3-part error (what / why /
    how-to-fix) instead of argparse's bare `unrecognized arguments`, so a stale launcher —
    e.g. an MCP server still spawning `review --mcp` — is diagnosable, not a silent failure."""

    reason: str
    fix: str


# Flags REMOVED with no replacement (distinct from REMOVED_MODE_FLAGS). The headline case
# is `--mcp`: the review-MCP entrypoint was dropped in the subcommand refactor (agent-tools
# #32) — review is a CLI + skill, not an MCP server — but stale registrations
# (`~/.claude/mcp/mcp.json`, a rig.yaml `mcp.review.command`) still invoke `review --mcp`,
# which argparse rejected with an opaque `unrecognized arguments: --mcp`. We give the dead
# flag a real, actionable error instead. `--ln` (the old line-number companion) was likewise
# dropped and gets the same treatment.
REMOVED_FLAGS: dict[str, RemovedFlag] = {
    "--mcp": RemovedFlag(
        reason="the `review --mcp` MCP entrypoint was removed in the subcommand refactor "
        "(agent-tools #32) — review is a CLI + skill, not an MCP server.",
        fix="remove the review MCP server registration: delete the `review` entry from "
        "~/.claude/mcp/mcp.json (and any rig.yaml `mcp.review` / `mcp.items.review` block), "
        "then re-run `rig apply`. Use the `review` CLI or its skill directly instead.",
    ),
    "--ln": RemovedFlag(
        reason="the `--ln` line-number flag was removed in the subcommand refactor.",
        fix="drop `--ln` from the invocation; it no longer does anything.",
    ),
}


# Re-export brainstorm's slot-pool helper so the CLI's stats wrapper can key the ETA on
# the per-round persona-slot count without reaching into the brainstorm module directly.
__all__ = [
    "MODES",
    "DIFF_MODE_NAME",
    "REMOVED_MODE_FLAGS",
    "REMOVED_SUBCOMMANDS",
    "REMOVED_FLAGS",
    "RemovedFlag",
    "iter_modes",
    "get_mode",
    "diff_mode",
    "known_subcommands",
    "brainstorm_pool",
]
