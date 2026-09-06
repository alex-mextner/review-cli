"""ModeSpec — the descriptor a review MODE implements (mirrors the visual
`features/visual/contract.py` + `module_api.py` pattern, generalized to the core
review modes).

A review mode is a self-describing module: it declares the SUBCOMMAND it registers
(`review <subcommand> …`), whether it wants a diff by DEFAULT, the positional/option
arguments it adds to the shared parser, and the handler that runs it over the shared
lib (panel / backends / config engine). Adding a mode = drop a module that exposes a
top-level `MODE = ModeSpec(...)` and list it in `registry.MODES`; no `cli.py` surgery.

This mirrors the visual MODULE contract exactly:
  * visual: a `VisualModule` declares `activates()` (WHEN), contributes work, lives in
    `features/visual/modules/` + per-project manifests, and is discovered by
    `features/visual/registry.py`.
  * modes:  a `ModeSpec` declares its `subcommand` (the verb that selects it),
    `wants_diff` (the default diff policy), `add_arguments` (its CLI surface), and
    `handler` (the thin-over-the-lib dispatch). It is discovered by `modes/registry.py`.

The handler is deliberately THIN over `reviewlib` (panel.py / backends.py / config.py):
the same engine is reusable by an MCP wrapper or another CLI (a future research-cli /
task-cli `just-ask`) without dragging the argparse surface along — see AGENTS.md
"lib | cli | mcp".
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import EffortOverride

# How a mode treats the git diff by default, BEFORE any composable flag (`--staged`,
# a piped diff) is applied:
#   * "require"  — the mode needs a diff; an absent diff / non-repo is a hard error
#                  (the default diff-review pre-commit path).
#   * "optional" — the mode can use a diff as grounding context but never requires one
#                  (brainstorm: with a diff it reasons ABOUT the change, without it is
#                  pure ideation); a piped/--staged/working-tree diff is picked up when
#                  present and degrades silently to "" when absent.
#   * "none"     — the mode ignores the diff unless one is explicitly piped/--staged in
#                  as optional context (just-ask / quorum: a question, diff is context).
DIFF_POLICIES = ("require", "optional", "none")


@dataclass(frozen=True)
class ModeContext:
    """Everything a mode handler needs, resolved once by the CLI and handed in.

    The handler stays thin: it reads what it needs off this context and calls the lib.
    `args` is the parsed argparse namespace for the mode's own subparser (so a mode can
    read its own options); the rest are the shared, already-resolved inputs.
    """

    args: argparse.Namespace
    models: list[str]
    diff: str
    cwd: "PathLike"  # pathlib.Path — annotated loosely to avoid a hard import here
    timeout: int
    # The composition seam (§2.1): folds the --visual context into the mode's
    # prompt/question/topic. Identity-returns text when there is no --visual context.
    with_visual: Callable[[str], str]
    # The parsed --visual companion context (or None). A mode that wants to thread the
    # image into its prompt uses `with_visual`; this is exposed for completeness.
    visual_ctx: object | None = None
    # The moderator backends resolved from --moderator (for panel/brainstorm modes).
    moderators: list[str] = field(default_factory=list)
    # The run-scoped `--effort` override (config.EffortOverride, imported under TYPE_CHECKING
    # so the runtime stays cycle-free). Panel modes that build PanelJobs from a flat model
    # list (quorum / just-ask / brainstorm) resolve each job's effort through it
    # (`effort_for(model)`); None / an empty override leaves every job at its default (no
    # effort). The board path (review mode) applies it CLI-side onto the seats, not here.
    effort_override: "EffortOverride | None" = None
    # Mode-specific extras resolved by the CLI that don't fit the shared fields above.
    # Kept as an open dict so the registry contract doesn't grow a field per mode; a mode
    # that needs none ignores it. The currently defined keys are:
    #   * "board"        — list[BoardReviewer] | None : the failover board (None = flat;
    #                                                   `review` mode only).
    #   * "pool_size"    — int                        : the --pool size (board path only;
    #                                                   `review` mode only).
    #   * "outcome_sink" — list[FailoverOutcome]      : sink the board path appends its
    #                                                   outcome to, so the CLI can report
    #                                                   the models that actually ran
    #                                                   (`review` mode only).
    #   * "diff_from_stdin"     — bool : the diff came from a piped stdin (brainstorm/
    #                                    quorum/just-ask read this to exempt a piped diff
    #                                    from their own dispatch-time cap).
    #   * "diff_already_capped" — bool : the CLI's `_dispatch` already ran
    #                                    `cap_diff_for_dispatch` on `diff` before this
    #                                    context was built (brainstorm/quorum/just-ask
    #                                    only). Lets the mode's own dispatch-boundary
    #                                    capping — which exists so a direct library
    #                                    caller bypassing this CLI layer is still
    #                                    protected — skip a REDUNDANT second application:
    #                                    harmless at the default cap (a second call on an
    #                                    already-<=cap diff is a no-op anyway), but a real
    #                                    correctness gap when `$REVIEW_DIFF_MAX_BYTES` is
    #                                    set below the truncation marker's own length (the
    #                                    second call would re-truncate the FIRST call's
    #                                    marker text and report ITS size as "the full
    #                                    diff" — codex review finding, 2026-08 seat-
    #                                    cooldown/diff-cap feature, round 2).
    # A mode adding a new extra key should document it here.
    extra: dict = field(default_factory=dict)


# The handler runs the mode and returns its process exit code. It receives the fully
# resolved ModeContext — it never re-parses argv or re-acquires the diff.
ModeHandler = Callable[[ModeContext], int]

# A mode contributes its own argparse arguments to a subparser. Shared options
# (-m/-C/--pool/--moderator/--visual/…) are added by the CLI to every mode's parser;
# `add_arguments` only adds what is UNIQUE to the mode (its positional question/topic,
# any mode-only flags). May be None for a mode with no extra arguments.
AddArguments = Callable[[argparse.ArgumentParser], None]


@dataclass(frozen=True)
class ModeSpec:
    """A self-describing review mode (the core analogue of a visual `VisualModule`)."""

    # The mode's stable name (matches the module file / the recorded stats `mode`).
    name: str
    # The subcommand verb that selects it: `review <subcommand> …`.
    subcommand: str
    # The default diff policy (see DIFF_POLICIES). The mode's wiring uses this to decide
    # whether to hard-require, optionally pick up, or ignore the diff.
    diff_policy: str
    # The stats key (mode label) recorded for a run of this mode. Usually == name, but a
    # mode may record under a different label (e.g. the hyphen form "just-ask").
    stats_mode: str
    # One-line summary for `review --help` / the subcommand list.
    summary: str
    # The thin-over-the-lib handler.
    handler: ModeHandler
    # Adds the mode's UNIQUE arguments to its subparser (positional question/topic etc.).
    add_arguments: AddArguments | None = None
    # Whether the mode announces each backend's live-log path to stderr (the long-running
    # panel/brainstorm modes do; the plain review path stays quiet).
    announce_logs: bool = False
    # Aliases that also select this subcommand (e.g. "ask" for just-ask). Optional.
    aliases: tuple[str, ...] = ()


# Loose alias so the dataclass annotation above does not force a pathlib import at the
# top of every consumer; the CLI always passes a real pathlib.Path.
PathLike = object
