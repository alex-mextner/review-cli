"""Config + model selection: defaults, aliases, ~/.config/review-cli/config.yaml.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). The reviewer-board layer (role lenses +
`board:` config, HYP-741) is additive: with no `board:` in config.yaml the legacy
DEFAULT_MODELS panel is untouched.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROMPT = (
    "Review this uncommitted git diff for bugs, regressions, security issues, "
    "and missing tests. Return only actionable findings. Do not edit files."
)
# Code default keeps a self-sufficient panel (incl. opencode). Personal model
# preferences live in ~/.config/review-cli/config.yaml (keys: models,
# brainstorm_models) and override this — see load_config().
DEFAULT_MODELS = ("codex", "gemini", "oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo")
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


# DEFAULT_BOARD: the out-of-the-box 8-seat panel, so the board works WITHOUT a
# config file. Model ids are byte-exact against the provider catalogs (commandcode
# gateway /models, z.ai Coding-Plan) — do not alter the strings. Reviewers whose
# backend isn't available (no key / no CLI) are skipped at run time by the caller,
# not here.
#
# The `tests` seat goes DIRECT to z.ai (`zai:glm-5.2`, the newest GLM reachable on
# the Coding-Plan endpoint) via the z.ai backend / the user's GLM subscription —
# not through the commandcode gateway. The `contracts` seat is gpt-5.5 via
# commandcode, focused on public API shape / backward-compat.
#
# OPTIONAL HEAVYWEIGHTS (NOT enabled by default — the board stays at 8): add them
# to a config.yaml `board:` list if you want a 1M-context resilience / holistic pass:
#   - { model: "commandcode:MiniMaxAI/MiniMax-M3", role: performance, name: MiniMax }   # 1M ctx
#   - { model: "commandcode:nvidia/nemotron-3-ultra-550b-a55b", role: architect, name: Nemotron }  # 550B, 1M ctx
# (any role works; see REVIEW_ROLES — e.g. resilience-flavored via `architect`/`consistency`.)
DEFAULT_BOARD = (
    BoardReviewer("claude:claude-opus-4-8", "architect", "Opus"),
    BoardReviewer("codex", "correctness", "Codex"),
    BoardReviewer("gemini", "consistency", "Gemini"),
    BoardReviewer("commandcode:deepseek/deepseek-v4-pro", "performance", "DeepSeek"),
    BoardReviewer("commandcode:moonshotai/Kimi-K2.7-Code", "quality", "Kimi"),
    BoardReviewer("commandcode:Qwen/Qwen3.7-Max", "security", "Qwen"),
    BoardReviewer("zai:glm-5.2", "tests", "GLM"),
    BoardReviewer("commandcode:gpt-5.5", "contracts", "GPT-5.5"),
)


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
