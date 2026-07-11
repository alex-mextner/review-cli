"""Config + model selection: defaults, aliases, ~/.config/review-cli/config.yaml.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). The reviewer-board layer (role lenses +
`board:` config, HYP-741) is additive: with no `board:` in config.yaml the legacy
DEFAULT_MODELS panel is untouched.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

DEFAULT_PROMPT = (
    "Review this uncommitted git diff for bugs, regressions, security issues, "
    "and missing tests. Return only actionable findings. Do not edit files."
)
# Canonical Kimi seat — the SINGLE source of truth for "the Kimi model the defaults use",
# referenced by BOTH the flat DEFAULT_MODELS panel below and the priority-4 seat of
# DEFAULT_BOARD. It routes through the commandcode gateway, NOT the old
# `oc:fireworks/.../kimi-k2p6-turbo` route, which ran on the suspended Fireworks `glide`
# account and is dead (ROADMAP §7, review-cli#25). One constant so the flat panel and the
# board can never drift back to the dead route — the staleness that motivated this fix
# existed precisely because the Kimi seat was spelled independently in two places and only
# the board was kept current (the flat panel rotted on the dead Fireworks route).
KIMI_SEAT = "commandcode:moonshotai/Kimi-K2.7-Code"
SOL_SEAT = "codex:gpt-5.6-sol"

# Canonical GLM-5.2-via-commandcode seat — the SINGLE source of truth for "GLM 5.2 routed
# through the Command Code gateway" (as opposed to the z.ai-subscription route used by the
# lower-priority `oc:zai/glm-5.2` seat). It is the priority-4 board seat, immediately after
# Opus. The wire id is byte-exact against the commandcode gateway
# /models catalog (`zai-org/GLM-5.2`), verified live. This is DIFF-ONLY (a stateless keyed-
# HTTP POST through review_commandcode, like Gemini): opencode's `commandcode` provider does
# NOT register this model, so the agentic `oc:commandcode/zai-org/GLM-5.2` form errors — the
# diff-only route is the one that actually reaches it. Read-only by construction (it POSTs
# only the diff; no repo access, no tools, no exec), so it needs no `-s read-only` cage.
GLM_COMMANDCODE_SEAT = "commandcode:zai-org/GLM-5.2"


def _agentic(seat: str) -> str:
    """Turn a diff-only keyed-HTTP seat (`provider:model`) into its AGENTIC opencode
    form (`oc:provider/model`), so the SAME model runs read-only INSIDE the repo and can
    read any project file — not just the diff in the prompt (review-cli#24).

    opencode registers `commandcode` and `zai` as custom OpenAI-compatible providers
    (`~/.config/opencode/opencode.json`), so the exact model ids the diff-only
    `review_commandcode` / `review_zai` REST backends POST are reachable agentically with
    no new wire id — only the transport changes. resolve_backend routes the `oc:` prefix
    to review_opencode; everything after `oc:` is opencode's `provider/model` selector.

    Deriving the board's agentic seats FROM the diff-only constants keeps ONE source of
    truth for each model id: the flat DEFAULT_MODELS panel (which has no failover/reserve,
    so it stays on the robust key-only commandcode route) and the board's agentic seat can
    never drift to different model ids, and neither can drift back to the dead Fireworks
    route (review-cli#25). The flip is transport-only — a single, reversible rewrite.

    Idempotent + canonical: a seat that is ALREADY agentic is returned in the CANONICAL
    `oc:` spelling — `oc:foo/bar` unchanged, and the `opencode:foo/bar` alias normalized to
    `oc:foo/bar`. Both spellings resolve to review_opencode and run the same
    `opencode -m foo/bar`, which the dashboard attributes to `oc:foo/bar`; canonicalizing
    here keeps the board seat id == the attributed id, so a seat can't split into a
    `no_data` board row plus a separate `oc:` health row (review-cli#24, codex review).
    Wrapping twice can never produce a nonsense `oc:oc/...` id."""
    if seat.startswith("oc:"):
        return seat
    if seat.startswith("opencode:"):
        return "oc:" + seat[len("opencode:"):]
    if ":" not in seat:
        return f"oc:{seat}"
    provider, model = seat.split(":", 1)
    return f"oc:{provider}/{model}"


# Code default keeps a self-sufficient panel (incl. opencode). Personal model
# preferences live in ~/.config/review-cli/config.yaml (keys: models,
# brainstorm_models) and override this — see load_config().
DEFAULT_MODELS = ("codex", "gemini", KIMI_SEAT)
# Visual review uses a separate priority list from the text reviewer board. The visual
# pipeline selects the first reachable vision-capable backend, so a dead/paywalled Opus is
# skipped automatically and the next vision seat is promoted.
VISUAL_MODELS = (
    "claude:claude-opus-4-8",
    "oc:zai/glm-4.5v",
    "oc:commandcode/moonshotai/Kimi-K2.7-Code",
    "gemini",
)
# Friendly aliases for claude models, expanded in _split_models() and the
# default/config paths, so `-m fable5` == `-m claude:claude-fable-5`.
MODEL_ALIASES = {
    "fable": "claude:claude-fable-5",
    "fable5": "claude:claude-fable-5",
    "sol": SOL_SEAT,
    "gpt56sol": SOL_SEAT,
    # z.ai (Zhipu / GLM) — OpenAI-compatible keyed HTTP backend. Bare `zai` resolves
    # directly in resolve_backend (env ZAI_MODEL / glm-5.2 default — the newest GLM,
    # reachable on the Coding-Plan endpoint). These aliases pin specific GLM model ids;
    # `glm`/`glm52` point at the newest (glm-5.2), the rest pin older releases.
    "glm": "zai:glm-5.2",
    "glm52": "zai:glm-5.2",
    "glm51": "zai:glm-5.1",
    "glm47": "zai:glm-4.7",
    "glm46": "zai:glm-4.6",
    "glm45": "zai:glm-4.5",
    # commandcode — Command Code's OpenAI-compatible Provider API (keyed HTTP).
    # `cc` is a short hand; the legacy `commoncode`/`common-code` spellings still
    # resolve via resolve_backend, so old configs keep working.
    "commandcode": "commandcode",
    "commoncode": "commandcode",
    "cc": "commandcode",
}
CONFIG_PATH = Path.home() / ".config" / "review-cli" / "config.yaml"

# Short default for the interactive multi-call modes; classic review keeps 1200s.
PANEL_TIMEOUT_DEFAULT = 240
# qa (the agent-as-tester mode) carve-out: a tester run boots a SUT and drives a whole
# suite with an un-caged agent — tens of minutes, NOT the 4-minute chat-panel default. It
# gets its own long per-run default (45 min) and still leans on the <=4h run backstop, so a
# real qa run is never cut off by the short panel cap (review-qa.md §3 timeout carve-out).
QA_TIMEOUT_DEFAULT = 2700
# Moderator priority for --quorum/--brainstorm. opus first now that the headless
# claude backend works reliably (deterministic workspace auto-trust); codex and
# gemini are the fallbacks. pick_moderators() filters this to available backends
# and run_moderator() walks it at run time, so a candidate that passes the cheap
# availability probe but dies at run time (e.g. an Anthropic-disabled model like
# fable) never leaves the panel without a synthesis.
MODERATOR_CANDIDATES = ("claude:claude-opus-4-8", "codex", "gemini")


def _expand_alias(model: str) -> str:
    return MODEL_ALIASES.get(model.lower(), model)


def load_config() -> dict:
    """Read ~/.config/review-cli/config.yaml (keys: models, brainstorm_models, board).
    Returns {} if absent/unparseable so the code default always applies."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# --- Reviewer board (HYP-741) ---------------------------------------------------
# A "board" is a panel where each reviewer model carries its OWN ROLE/lens. The
# lens is a short instruction appended to DEFAULT_PROMPT so each model focuses on a
# distinct, non-overlapping facet of the diff — instead of every model doing the
# same generic pass. The board is built into the SAME default-review path (one
# PanelJob per reviewer, run in parallel); the opus-first moderator is unchanged.
#
# Role lenses: role id -> a tight sentence appended to the generic prompt. Kept
# non-overlapping so the panel covers the diff broadly without N duplicate reviews.
REVIEW_ROLES = {
    "architect": (
        "Focus specifically on ARCHITECTURE and DESIGN: design coherence, API shape, "
        "abstraction boundaries, separation of concerns, and whether the change fits "
        "the existing structure. Skip low-level style nits."
    ),
    "correctness": (
        "Focus specifically on CORRECTNESS: logic bugs, regressions, edge cases, "
        "null/undefined handling, async/await and race conditions, off-by-one errors, "
        "and incorrect control flow. Skip style and naming."
    ),
    "consistency": (
        "Focus specifically on CROSS-FILE CONSISTENCY: dead or dangling references, "
        "contract drift between caller and callee, mismatched signatures, and whole-repo "
        "coherence. Use the broad context to catch things a single-file view misses."
    ),
    "performance": (
        "Focus specifically on PERFORMANCE: algorithmic complexity, hot paths, "
        "unnecessary allocations, async/concurrency overhead, and N+1 query or loop "
        "patterns. Flag only changes with a real performance impact."
    ),
    "quality": (
        "Focus specifically on CODE QUALITY: readability, naming, duplication, "
        "code smells, and idiomatic use of the language/framework. Suggest concrete, "
        "actionable cleanups."
    ),
    "security": (
        "Focus specifically on SECURITY: injection, broken authn/authz, secret "
        "handling, unsafe deserialization, path traversal, and SSRF. Flag exploitable "
        "issues, not theoretical ones."
    ),
    "tests": (
        "Focus specifically on TESTS: missing tests for new behavior, untested branches, "
        "boundary conditions, and error-path coverage. Point at the exact cases that "
        "should be tested but aren't."
    ),
    "contracts": (
        "Focus specifically on PUBLIC API SHAPE and CONTRACTS: exported function/type "
        "signatures, interface design, backward compatibility, breaking changes to "
        "callers, and whether new types/return values are coherent and future-proof. "
        "Skip internal-only refactors with no external surface."
    ),
}


@dataclass(frozen=True)
class BoardReviewer:
    """One seat on the reviewer board: a backend model + its review role.

    `model` is the backend string (e.g. `claude:claude-opus-4-8`,
    `commandcode:deepseek/deepseek-v4-pro`). `role` is a key into REVIEW_ROLES;
    `display` is a short human name for the --show-board listing / result label.
    `role_lens` resolves the lens text (empty string for an unknown role, so the
    job falls back to the generic prompt — never a crash)."""

    model: str
    role: str
    display: str
    effort: str | None = None

    @property
    def role_lens(self) -> str:
        return REVIEW_ROLES.get(self.role, "")


# DEFAULT_BOARD: the raw 10-seat board used as the source of truth for built-in presets
# and for custom/config fallback paths. A plain `review diff` runs the default preset,
# not this tuple directly. The board is ordered by *priority* — strongest model first,
# weakest last — NOT by role. Priority drives the FAILOVER pool: the selected board runs
# the top-N AVAILABLE seats, skipping a higher-priority seat whose backend isn't reachable
# and promoting the next-priority reserve to keep a full pool (startup failover); a seat
# that fails *during* the run is likewise replaced by the next reserve (mid-run failover).
# See select_pool() / panel.run_board_with_failover().
#
# Each seat still carries its OWN role/lens — the lens is what makes a multi-model panel
# cover the diff broadly instead of N duplicate passes. Priority decides WHO sits; the
# role decides WITH WHAT LENS they review. So the lens travels with the seat: when a
# higher-priority seat is skipped/replaced, the promoted reserve brings its own lens.
#
# To RE-RANK the board, just reorder this tuple (top = highest priority). Model ids are
# byte-exact against the provider catalogs (commandcode gateway /models, z.ai Coding-Plan)
# — do not alter the strings. Each is the TOP available version of its model family
# (fable-5, Sol, opus-4-8, GLM-5.2-via-gateway, Kimi-K2.7, codex/GPT-5.5,
# Qwen3.7-Max, deepseek-v4-pro, Gemini, glm-5.2-via-z.ai).
#
# AGENTIC BY DEFAULT (review-cli#24): every board seat that CAN read the repo does. The
# two claude seats run via the agentic claude CLI; Codex via the codex CLI
# (`codex exec -s read-only -C <cwd>`); Kimi/z.ai-GLM/Qwen/DeepSeek through opencode
# (`oc:provider/model`, built by `_agentic()` from the diff-only constant) so they ALSO
# run read-only inside `-C` and can open ANY project file — not just the diff in the
# prompt. The board has a reserve, so an `oc:` seat that opencode can't reach on a given
# host probes UNAVAILABLE and is backfilled (startup or mid-run failover) — the board
# degrades gracefully rather than blocking. Two seats stay diff-only stateless HTTP calls:
# Gemini (no agentic transport) and the priority-4 GLM-5.2-via-commandcode seat
# (`GLM_COMMANDCODE_SEAT` — opencode's commandcode provider does not register this GLM id, so
# the agentic form errors; the keyed-HTTP route is the one that reaches it). Both are read-
# only by construction (they POST only the diff). The diff-only `commandcode:`/`zai:` REST
# backends stay available for `-m cc`/`-m glm` and config boards on hosts without opencode;
# the board just prefers the agentic transport when one exists.
#
# The FLAT DEFAULT_MODELS panel deliberately keeps the diff-only commandcode Kimi seat
# (KIMI_SEAT): that panel has NO reserve/failover, so an opencode-less host would silently
# shrink it; the board can absorb that via its reserve, the flat panel cannot. Same model
# id (`_agentic(KIMI_SEAT)`), transport-only difference — one source of truth, no drift.
#
# OPTIONAL HEAVYWEIGHTS (NOT in the default board): add them to a config.yaml `board:`
# list if you want a 1M-context resilience / holistic pass:
#   - { model: "oc:commandcode/MiniMaxAI/MiniMax-M3", role: performance, name: MiniMax }   # 1M ctx, agentic
#   - { model: "oc:commandcode/nvidia/nemotron-3-ultra-550b-a55b", role: architect, name: Nemotron }  # 550B, 1M ctx
DEFAULT_BOARD = (
    # priority 1 — Fable 5 (Anthropic flagship). Currently paywalled/"unavailable", so
    # the failover skips it at startup (the cheap probe can't see the paywall, but its
    # run-time "currently unavailable" body is treated as a failure and backfilled).
    BoardReviewer("claude:claude-fable-5", "architect", "Fable"),
    # priority 2 — Sol through Codex CLI, immediately after Fable.
    BoardReviewer(SOL_SEAT, "consistency", "Sol"),
    # priority 3 — Opus 4.8. Also the moderator (MODERATOR_CANDIDATES[0]).
    BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
    # priority 4 — GLM-5.2 via the Command Code gateway (CTO directive: directly under Opus).
    # DIFF-ONLY (keyed HTTP through review_commandcode, like Gemini): opencode's commandcode
    # provider does NOT register `zai-org/GLM-5.2`, so the agentic `oc:commandcode/...` form
    # errors — the diff-only route is the one that actually reaches it. Read-only by
    # construction (POSTs only the diff; no repo/tools/exec). Distinct from the lower-priority
    # `oc:zai/glm-5.2` seat (same model FAMILY, different provider/transport: z.ai vs gateway).
    # ROLE: `performance` — NOT `correctness` (which would duplicate Opus's lens). Inserting
    # GLM-cc carries `performance` to keep the default top-4 pool's lens coverage intact
    # (architect/consistency/correctness/performance), instead of dropping performance from a
    # plain `review diff` and duplicating correctness (review of #57).
    BoardReviewer(GLM_COMMANDCODE_SEAT, "performance", "GLM-cc"),
    # priority 5 — Kimi K2.7, AGENTIC through opencode (reads the repo). Same model id as
    # the flat panel's KIMI_SEAT (one source of truth via `_agentic`); transport-only diff.
    BoardReviewer(_agentic(KIMI_SEAT), "quality", "Kimi"),
    # priority 6 — Codex: the agentic codex CLI route (reads the whole repo), NOT the
    # diff-only `commandcode:gpt-5.5` HTTP route. GPT-5.5 is codex; the agentic route wins.
    BoardReviewer("codex", "consistency", "Codex"),
    # priority 7 — Qwen3.7-Max, AGENTIC through opencode's commandcode provider (reads the repo).
    BoardReviewer(_agentic("commandcode:Qwen/Qwen3.7-Max"), "security", "Qwen"),
    # priority 8 — DeepSeek-V4-Pro, AGENTIC through opencode's commandcode provider (reads the repo).
    BoardReviewer(_agentic("commandcode:deepseek/deepseek-v4-pro"), "tests", "DeepSeek"),
    # priority 9 — Gemini.
    BoardReviewer("gemini", "contracts", "Gemini"),
    # priority 10 (LAST-RESORT reserve) — GLM-5.2 (his z.ai subscription, the newest GLM),
    # AGENTIC through opencode's `zai` provider. DELIBERATELY DEPRIORITIZED to the bottom of
    # the reserve (review-cli#65): this seat is observed to be PATHOLOGICALLY SLOW under load,
    # so promoting it onto the failover critical path stalls the pool's path to a verdict. It
    # stays on the board (still backfills when every faster reserve is also exhausted), but it
    # no longer blocks a fast verdict — Qwen / DeepSeek / Gemini are promoted before it. To
    # re-rank, move this line up; its position IS its priority.
    BoardReviewer(_agentic("zai:glm-5.2"), "quality", "GLM"),
)

# Presets are named, opinionated board+pool bundles for day-to-day review selection.
# They deliberately sit above the raw DEFAULT_BOARD: custom config still owns custom
# boards/models, while `--preset` gives the CLI a predictable canned roster.
DEFAULT_PRESET_BOARD = tuple(
    BoardReviewer(r.model, r.role, r.display, "high")
    for r in DEFAULT_BOARD
    if r.model not in {"claude:claude-fable-5", SOL_SEAT}
)
HEAVY_PRESET_BOARD = tuple(
    BoardReviewer(r.model, r.role, r.display, "xhigh" if i < 4 else "max")
    for i, r in enumerate(DEFAULT_BOARD)
)
LIGHT_PRESET_BOARD = tuple(
    BoardReviewer(r.model, r.role, r.display, "medium")
    for r in DEFAULT_PRESET_BOARD
)

PRESET_BOARDS = {
    "default": DEFAULT_PRESET_BOARD,
    "heavy": HEAVY_PRESET_BOARD,
    "light": LIGHT_PRESET_BOARD,
}
PRESET_POOL_SIZES = {
    "default": 4,
    "heavy": 4,
    "light": 2,
}
DEFAULT_PRESET = "default"
EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})


# DEFAULT_POOL_SIZE: how many of the board's seats a plain `review` runs by default.
# The board (DEFAULT_BOARD or a config `board:`) is a priority-ordered list; by default
# the top 4 AVAILABLE seats participate and the remaining (lower-priority) seats are the
# RESERVE that backfills a skipped/failed seat. The board is NEVER disabled — `--pool`
# only sizes how many seats run. See select_pool() / panel.run_board_with_failover().
DEFAULT_POOL_SIZE = 4


def preset_names() -> tuple[str, ...]:
    return tuple(PRESET_BOARDS)


def preset_pool_size(name: str | None) -> int:
    if not name:
        return DEFAULT_POOL_SIZE
    try:
        return PRESET_POOL_SIZES[name]
    except KeyError as exc:
        raise ValueError(f"unknown preset {name!r}; expected one of {', '.join(preset_names())}") from exc


def preset_board(name: str | None) -> list[BoardReviewer]:
    if not name:
        return [replace(r) for r in PRESET_BOARDS[DEFAULT_PRESET]]
    try:
        return [replace(r) for r in PRESET_BOARDS[name]]
    except KeyError as exc:
        raise ValueError(f"unknown preset {name!r}; expected one of {', '.join(preset_names())}") from exc


def _normalize_effort(value: object) -> str | None:
    effort = value.strip().lower() if isinstance(value, str) and value.strip() else None
    if effort is None:
        return None
    if effort not in EFFORT_LEVELS:
        print(
            f"[review-cli] config board entry has unsupported effort {effort!r}; "
            f"expected one of {', '.join(sorted(EFFORT_LEVELS))}; ignoring effort",
            file=sys.stderr,
            flush=True,
        )
        return None
    return effort


def _always_available(_reviewer: BoardReviewer) -> bool:
    """Default availability predicate: every seat counts as available (the legacy
    'no failover here, availability is checked downstream' behaviour)."""
    return True


def _effective_pool_size(seat_count: int, pool: int) -> int:
    """How many seats a `--pool N` request actually selects from `seat_count` available
    seats: `pool <= 0` means ALL, a `pool` larger than the count is clamped to the count,
    otherwise exactly `pool`. The single source of truth for select_pool +
    split_pool_reserve so the two can never drift."""
    if pool <= 0 or pool >= seat_count:
        return seat_count
    return pool


def select_pool(
    board: list[BoardReviewer],
    pool: int,
    available: Callable[[BoardReviewer], bool] = _always_available,
) -> list[BoardReviewer]:
    """Pick the top `pool` AVAILABLE seats by priority; the rest are the reserve.

    The board is EXPECTED to be priority-ordered (strongest first) — that order IS the
    priority. Startup failover: a higher-priority seat whose backend is NOT reachable
    (per `available`) is SKIPPED and the next-priority seat pulled up, so the run still
    starts with `pool` WORKING seats when enough reachable seats exist. The default
    `available` treats every seat as reachable, so callers that check availability
    downstream get "the first `pool` seats in priority order".

    The selection is deterministic. `pool <= 0` means "all (available) seats", and a
    `pool` larger than the available count is clamped — a caller can never ask for more
    seats than exist. An empty board stays empty. Returns a NEW list, never a view."""
    seats = [r for r in board if available(r)]
    return list(seats[: _effective_pool_size(len(seats), pool)])


def split_pool_reserve(
    board: list[BoardReviewer],
    pool: int,
    available: Callable[[BoardReviewer], bool] = _always_available,
) -> tuple[list[BoardReviewer], list[BoardReviewer]]:
    """Split the AVAILABLE board into (pool, reserve) by priority, for failover.

    `pool` = the top-N available seats (startup failover — same slice select_pool makes).
    `reserve` = the remaining available seats, in priority order, which back-fill a pool
    seat that fails mid-run. Unavailable seats are in NEITHER list (they can't run). The
    two lists are disjoint and together hold every available seat, in priority order. The
    `board` is EXPECTED to be priority-ordered (its order is the priority)."""
    seats = [r for r in board if available(r)]
    n = _effective_pool_size(len(seats), pool)
    return list(seats[:n]), list(seats[n:])


def _display_name(model: str) -> str:
    """A short, human-friendly label for a board reviewer derived from its model
    string — used when config.yaml omits an explicit name. Takes the last path
    segment of the provider id (so `commandcode:deepseek/deepseek-v4-pro` -> the
    provider tail, `claude:claude-opus-4-8` -> `claude-opus-4-8`)."""
    tail = model.split(":", 1)[1] if ":" in model else model
    return tail.rsplit("/", 1)[-1]


class BoardConfigError(ValueError):
    """A `board:` key was present and non-empty in config.yaml but contained NO
    usable reviewer (every entry malformed). Raised instead of silently falling
    back to the paid DEFAULT_BOARD — a user who deliberately configured a board
    must get a clear error, not an unexpected paid default-board run (cost-safety)."""


def _resolve_capability_model(spec: str) -> str | None:
    """Resolve a board entry's `capability:` value to a concrete review-cli seat from the
    shared `models.yaml` manifest, or `None` when it can't be resolved (no manifest on this
    host / unknown capability / role with no review-cli route).

    Two forms, both ADDITIVE to the literal `model:` form:
      * `capability: role:reasoning` (or any `roles:` key — architect/vision/code/fast) ->
        the manifest's `roles:` map resolves the symbolic lens to a concrete pinned id;
      * `capability: vision` (a bare capability tag) -> the strongest manifest entry carrying
        that tag.

    The manifest is the ecosystem source of truth (rig-cli#8); when it isn't reachable this
    returns `None` and the caller skips the entry with a warning — the board keeps working off
    its literal-`model:` entries / the hardcoded DEFAULT_BOARD, never a crash."""
    from . import manifest as _manifest

    spec = spec.strip()
    if spec.startswith("role:"):
        return _manifest.resolve_role(spec[len("role:"):].strip())
    return _manifest.resolve_capability(spec)


def _matching_role_name(spec: str, manifest_mod) -> str | None:
    """The CANONICAL manifest role name a bare `capability:` value names (case-insensitive), or
    `None` when it isn't a role name. A `role:`-prefixed spec is not a bare-capability collision,
    so it returns None. Returns the manifest's own casing so a hint/lookup never dead-ends on a
    case mismatch (`Vision` -> `vision`)."""
    if spec.startswith("role:"):
        return None
    lowered = spec.lower()
    for name in manifest_mod.role_names():
        if name.lower() == lowered:
            return name
    return None


def _capability_fail_reason(capability: str) -> str:
    """A clear reason string for an unresolved `capability:` entry. The common footgun is a
    BARE value that is a ROLE name (`reasoning`/`architect`/`fast`) rather than a capability
    TAG — the two namespaces overlap, so `capability: reasoning` scans tags (not the `roles:`
    map) and `capability: architect` resolves to nothing. Detect that and hint `role:<x>`."""
    from . import manifest as _manifest

    spec = capability.strip()
    # Only hint `role:<x>` when the value is PURELY a role name — NOT a real capability tag.
    # `vision`/`reasoning`/`code` are BOTH tags and roles; if `capability: vision` failed it's
    # because no model carries the tag (a manifest gap), so claiming "vision is a ROLE name, not
    # a tag" would be factually wrong. A pure role (`architect`/`fast`) genuinely isn't a tag.
    if spec.lower() not in _manifest.KNOWN_CAPABILITIES:
        role_match = _matching_role_name(spec, _manifest)
        if role_match is not None:
            # Suggest the role's CANONICAL casing (the manifest key), so the hint never dead-ends
            # on a case mismatch (`capability: Architect` -> hint `role:architect`, which resolves).
            return (f"capability {spec!r} is a ROLE name, not a capability tag — write "
                    f"'capability: role:{role_match}' to use the manifest's role map")
    return (f"capability {spec!r} could not be resolved from the model manifest "
            "(unknown capability tag / no model carries the tag / unknown role / no manifest "
            "reachable — is agent-tools lib/contracts/models.yaml present?)")


def _resolve_entry_model(entry: dict) -> str | None:
    """The concrete seat string for one board entry, from EITHER a literal `model:` or a
    manifest-resolved `capability:` (mutually exclusive; `model:` wins if both are present).
    Returns `None` (with a warning) when neither yields a usable seat, so the caller skips
    the entry — the board degrades, never crashes."""
    model = entry.get("model")
    if isinstance(model, str) and model.strip():
        return _expand_alias(model.strip())
    capability = entry.get("capability")
    if isinstance(capability, str) and capability.strip():
        resolved = _resolve_capability_model(capability)
        if resolved is None:
            print(f"[review-cli] board entry ignored ({_capability_fail_reason(capability)}): "
                  f"{entry!r}", file=sys.stderr, flush=True)
            return None
        # Run the resolved seat through the SAME alias normalization the literal `model:` path
        # uses, so an equivalent value (e.g. a manifest id that happens to be an alias) can't
        # behave differently between the two paths.
        return _expand_alias(resolved)
    print(f"[review-cli] board entry ignored (missing 'model' or 'capability'): {entry!r}",
          file=sys.stderr, flush=True)
    return None


def _parse_board_entry(entry: object) -> BoardReviewer | None:
    """Parse ONE config `board:` entry into a BoardReviewer, or `None` (with a warning) when
    it's unusable so `load_board` skips it. A seat comes from a literal `model:` or a
    capability-resolved `capability:` (rig-cli#8)."""
    if not isinstance(entry, dict):
        print(f"[review-cli] board entry ignored (not a mapping): {entry!r}",
              file=sys.stderr, flush=True)
        return None
    model = _resolve_entry_model(entry)
    if model is None:
        return None
    role = entry.get("role")
    role = role.strip() if isinstance(role, str) else ""
    if role and role not in REVIEW_ROLES:
        print(f"[review-cli] board reviewer {model!r}: unknown role {role!r} — "
              f"using the generic review prompt (known roles: "
              f"{', '.join(sorted(REVIEW_ROLES))})", file=sys.stderr, flush=True)
    name = entry.get("name")
    display = name.strip() if isinstance(name, str) and name.strip() else _display_name(model)
    effort = _normalize_effort(entry.get("effort"))
    return BoardReviewer(model=model, role=role, display=display, effort=effort)


def load_board(config: dict | None = None, *, preset: str | None = None) -> list[BoardReviewer]:
    """Resolve the active reviewer board.

    A `board:` key in config.yaml (a list of `{model | capability, role[, name]}`
    mappings) overrides the built-in default preset. A seat names its model EITHER
    literally (`model: claude:claude-opus-4-8`) OR by CAPABILITY resolved from the
    shared `models.yaml` manifest (`capability: vision` / `capability: role:reasoning`,
    rig-cli#8) — the latter degrades to "skip this entry with a warning" when the
    manifest isn't reachable, so a manifest-less host still runs its literal seats.
    Validation degrades gracefully but is cost-safe — it never silently substitutes
    the paid default board for a board the user explicitly configured:
      * an ABSENT / non-list / empty `board:`  -> fall back to DEFAULT_PRESET_BOARD
        (no preference expressed, the safe default is intended);
      * a PRESENT non-empty `board:` with SOME valid entries -> keep the valid
        ones; each bad entry is skipped with a warning;
      * an entry with neither a usable `model` nor a resolvable `capability` ->
        skipped with a warning;
      * an unknown `role` -> kept, but the reviewer uses the generic prompt
        (role_lens == "") and a warning is logged — the board degrades;
      * a PRESENT non-empty `board:` whose entries are ALL malformed (no usable
        reviewer survives) -> raise BoardConfigError. This is NOT a silent
        fall-back to DEFAULT_PRESET_BOARD: the user asked for a specific board and got
        nothing parseable, so erroring loudly beats secretly running the paid
        default board."""
    config = load_config() if config is None else config
    if preset:
        return preset_board(preset)
    raw = config.get("board")
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_PRESET_BOARD)
    board: list[BoardReviewer] = []
    for entry in raw:
        reviewer = _parse_board_entry(entry)
        if reviewer is not None:
            board.append(reviewer)
    # NOTE: duplicate models across seats are INTENTIONALLY allowed (the show-board path tags
    # per-SEAT, not per-model — a duplicate model in the reserve must read `reserve`, not
    # `pool`; see test_show_board_tags_by_seat_not_model_for_duplicate_models). Capability
    # resolution can make a collision likelier, but de-duping here would override that
    # deliberate, tested behavior — so a board with two identical seats is kept as-is.
    if not board:
        # `board:` was present and non-empty but nothing parsed. Do NOT fall back
        # to the paid DEFAULT_BOARD — error loudly so the user fixes the config.
        raise BoardConfigError(
            f"config.yaml `board:` has {len(raw)} entr"
            f"{'y' if len(raw) == 1 else 'ies'} but none is usable (every entry "
            "is malformed — not a mapping, or missing a 'model' / a resolvable "
            "'capability'). Fix the board entries, or remove the `board:` key to "
            "use the default reviewer board."
        )
    return board


def board_from_models(
    models: list[str], config: dict | None = None, *, preset: str | None = None,
) -> list[BoardReviewer]:
    """Build a priority reviewer board from a config `models:` roster.

    `models:` owns the seat order. A matching config `board:` entry, selected preset, or
    built-in DEFAULT_BOARD seat supplies role/name metadata; unknown models stay usable
    with the generic review lens and a display name derived from the model id. When a
    preset is explicit, it overlays config metadata so preset effort cannot be downgraded
    by a saved board entry.
    """
    metadata: dict[str, BoardReviewer] = {reviewer.model: reviewer for reviewer in DEFAULT_BOARD}
    raw_board = (config or {}).get("board") if isinstance(config, dict) else None
    if isinstance(raw_board, list):
        for entry in raw_board:
            reviewer = _parse_board_entry(entry)
            if reviewer is not None:
                metadata[reviewer.model] = reviewer
    preset_effort_by_model: dict[str, str | None] = {}
    preset_default_effort: str | None = None
    if preset:
        preset_reviewers = preset_board(preset)
        preset_effort_by_model = {reviewer.model: reviewer.effort for reviewer in preset_reviewers}
        preset_default_effort = next((reviewer.effort for reviewer in preset_reviewers if reviewer.effort), None)
        for reviewer in preset_reviewers:
            base = metadata.get(reviewer.model)
            if base is None:
                metadata[reviewer.model] = reviewer
            else:
                metadata[reviewer.model] = BoardReviewer(
                    model=base.model,
                    role=base.role,
                    display=base.display,
                    effort=reviewer.effort,
                )

    board: list[BoardReviewer] = []
    for model in models:
        reviewer = metadata.get(model)
        preset_model_effort = preset_effort_by_model.get(model, preset_default_effort) if preset else None
        if reviewer is None:
            board.append(BoardReviewer(model=model, role="", display=_display_name(model), effort=preset_model_effort))
        else:
            board.append(BoardReviewer(
                model=model,
                role=reviewer.role,
                display=reviewer.display,
                effort=preset_model_effort if preset else reviewer.effort,
            ))
    return board


def _split_models(raw: list[str]) -> list[str]:
    values: list[str] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part:
                values.append(_expand_alias(part))
    return values
