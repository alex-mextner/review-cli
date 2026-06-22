"""Config + model selection: defaults, aliases, ~/.config/review-cli/config.yaml.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). The reviewer-board layer (role lenses +
`board:` config, HYP-741) is additive: with no `board:` in config.yaml the legacy
DEFAULT_MODELS panel is untouched.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
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

# Canonical GLM-5.2-via-commandcode seat — the SINGLE source of truth for "GLM 5.2 routed
# through the Command Code gateway" (as opposed to the z.ai-subscription route used by the
# lower-priority `oc:zai/glm-5.2` seat). It is the priority-3 board seat (directly under
# Opus, per the CTO directive). The wire id is byte-exact against the commandcode gateway
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
# Friendly aliases for claude models, expanded in _split_models() and the
# default/config paths, so `-m fable5` == `-m claude:claude-fable-5`.
MODEL_ALIASES = {
    "fable": "claude:claude-fable-5",
    "fable5": "claude:claude-fable-5",
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

    @property
    def role_lens(self) -> str:
        return REVIEW_ROLES.get(self.role, "")


# DEFAULT_BOARD: the out-of-the-box 9-seat board, so the board works WITHOUT a config
# file. The board is ordered by *priority* — strongest model first, weakest last — NOT
# by role. Priority drives the FAILOVER pool: a plain `review` runs the top-N AVAILABLE
# seats (default 4), skipping a higher-priority seat whose backend isn't reachable and
# promoting the next-priority reserve to keep a full pool (startup failover); a seat that
# fails *during* the run is likewise replaced by the next reserve (mid-run failover). See
# select_pool() / panel.run_board_with_failover().
#
# Each seat still carries its OWN role/lens — the lens is what makes a multi-model panel
# cover the diff broadly instead of N duplicate passes. Priority decides WHO sits; the
# role decides WITH WHAT LENS they review. So the lens travels with the seat: when a
# higher-priority seat is skipped/replaced, the promoted reserve brings its own lens.
#
# To RE-RANK the board, just reorder this tuple (top = highest priority). Model ids are
# byte-exact against the provider catalogs (commandcode gateway /models, z.ai Coding-Plan)
# — do not alter the strings. Each is the TOP available version of its model family
# (fable-5, opus-4-8, GLM-5.2-via-gateway, codex/GPT-5.5, Kimi-K2.7, glm-5.2-via-z.ai,
# Qwen3.7-Max, deepseek-v4-pro).
#
# AGENTIC BY DEFAULT (review-cli#24): every board seat that CAN read the repo does. The
# two claude seats run via the agentic claude CLI; Codex via the codex CLI
# (`codex exec -s read-only -C <cwd>`); Kimi/z.ai-GLM/Qwen/DeepSeek through opencode
# (`oc:provider/model`, built by `_agentic()` from the diff-only constant) so they ALSO
# run read-only inside `-C` and can open ANY project file — not just the diff in the
# prompt. The board has a reserve, so an `oc:` seat that opencode can't reach on a given
# host probes UNAVAILABLE and is backfilled (startup or mid-run failover) — the board
# degrades gracefully rather than blocking. Two seats stay diff-only stateless HTTP calls:
# Gemini (no agentic transport) and the priority-3 GLM-5.2-via-commandcode seat
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
    # priority 2 — Opus 4.8. Also the moderator (MODERATOR_CANDIDATES[0]).
    BoardReviewer("claude:claude-opus-4-8", "correctness", "Opus"),
    # priority 3 — GLM-5.2 via the Command Code gateway (CTO directive: directly under Opus).
    # DIFF-ONLY (keyed HTTP through review_commandcode, like Gemini): opencode's commandcode
    # provider does NOT register `zai-org/GLM-5.2`, so the agentic `oc:commandcode/...` form
    # errors — the diff-only route is the one that actually reaches it. Read-only by
    # construction (POSTs only the diff; no repo/tools/exec). Distinct from the lower-priority
    # `oc:zai/glm-5.2` seat (same model FAMILY, different provider/transport: z.ai vs gateway).
    # ROLE: `performance` — NOT `correctness` (which would duplicate Opus's lens). Inserting
    # this seat at #3 pushes Kimi (the old `performance` seat) to #5/reserve, so GLM-cc carries
    # `performance` to keep the default top-4 pool's lens coverage intact
    # (architect/correctness/performance/consistency), instead of dropping performance from a
    # plain `review diff` and duplicating correctness (review of #57).
    BoardReviewer(GLM_COMMANDCODE_SEAT, "performance", "GLM-cc"),
    # priority 4 — Codex: the agentic codex CLI route (reads the whole repo), NOT the
    # diff-only `commandcode:gpt-5.5` HTTP route. GPT-5.5 is codex; the agentic route wins.
    BoardReviewer("codex", "consistency", "Codex"),
    # priority 5 — Kimi K2.7, AGENTIC through opencode (reads the repo). Same model id as
    # the flat panel's KIMI_SEAT (one source of truth via `_agentic`); transport-only diff.
    BoardReviewer(_agentic(KIMI_SEAT), "performance", "Kimi"),
    # priority 6 — GLM-5.2 (his z.ai subscription, the newest GLM), AGENTIC through
    # opencode's `zai` provider (reads the repo) instead of the diff-only z.ai REST call.
    BoardReviewer(_agentic("zai:glm-5.2"), "quality", "GLM"),
    # priority 7 — Qwen3.7-Max, AGENTIC through opencode's commandcode provider (reads the repo).
    BoardReviewer(_agentic("commandcode:Qwen/Qwen3.7-Max"), "security", "Qwen"),
    # priority 8 — DeepSeek-V4-Pro, AGENTIC through opencode's commandcode provider (reads the repo).
    BoardReviewer(_agentic("commandcode:deepseek/deepseek-v4-pro"), "tests", "DeepSeek"),
    # priority 9 — Gemini.
    BoardReviewer("gemini", "contracts", "Gemini"),
)


# DEFAULT_POOL_SIZE: how many of the board's seats a plain `review` runs by default.
# The board (DEFAULT_BOARD or a config `board:`) is a priority-ordered list; by default
# the top 4 AVAILABLE seats participate and the remaining (lower-priority) seats are the
# RESERVE that backfills a skipped/failed seat. The board is NEVER disabled — `--pool`
# only sizes how many seats run. See select_pool() / panel.run_board_with_failover().
DEFAULT_POOL_SIZE = 4


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
    must get a clear error, not an unexpected 8-model paid run (cost-safety)."""


def load_board(config: dict | None = None) -> list[BoardReviewer]:
    """Resolve the active reviewer board.

    A `board:` key in config.yaml (a list of `{model, role[, name]}` mappings)
    overrides the built-in DEFAULT_BOARD. Validation degrades gracefully but is
    cost-safe — it never silently substitutes the paid default board for a board
    the user explicitly configured:
      * an ABSENT / non-list / empty `board:`  -> fall back to DEFAULT_BOARD
        (no preference expressed, the default is intended);
      * a PRESENT non-empty `board:` with SOME valid entries -> keep the valid
        ones; each bad entry is skipped with a warning;
      * an entry without a usable `model` -> skipped with a warning;
      * an unknown `role` -> kept, but the reviewer uses the generic prompt
        (role_lens == "") and a warning is logged — the board degrades;
      * a PRESENT non-empty `board:` whose entries are ALL malformed (no usable
        reviewer survives) -> raise BoardConfigError. This is NOT a silent
        fall-back to DEFAULT_BOARD: the user asked for a specific board and got
        nothing parseable, so erroring loudly beats secretly running the paid
        8-model panel."""
    config = load_config() if config is None else config
    raw = config.get("board")
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_BOARD)
    board: list[BoardReviewer] = []
    for entry in raw:
        if not isinstance(entry, dict):
            print(f"[review-cli] board entry ignored (not a mapping): {entry!r}",
                  file=sys.stderr, flush=True)
            continue
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            print(f"[review-cli] board entry ignored (missing 'model'): {entry!r}",
                  file=sys.stderr, flush=True)
            continue
        model = _expand_alias(model.strip())
        role = entry.get("role")
        role = role.strip() if isinstance(role, str) else ""
        if role and role not in REVIEW_ROLES:
            print(f"[review-cli] board reviewer {model!r}: unknown role {role!r} — "
                  f"using the generic review prompt (known roles: "
                  f"{', '.join(sorted(REVIEW_ROLES))})", file=sys.stderr, flush=True)
        name = entry.get("name")
        display = name.strip() if isinstance(name, str) and name.strip() else _display_name(model)
        board.append(BoardReviewer(model=model, role=role, display=display))
    if not board:
        # `board:` was present and non-empty but nothing parsed. Do NOT fall back
        # to the paid DEFAULT_BOARD — error loudly so the user fixes the config.
        raise BoardConfigError(
            f"config.yaml `board:` has {len(raw)} entr"
            f"{'y' if len(raw) == 1 else 'ies'} but none is usable (every entry "
            "is malformed — not a mapping, or missing a 'model'). Fix the board "
            "entries, or remove the `board:` key to use the default reviewer board."
        )
    return board


def _split_models(raw: list[str]) -> list[str]:
    values: list[str] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part:
                values.append(_expand_alias(part))
    return values
