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
    # z.ai (Zhipu / GLM) — OpenAI-compatible keyed HTTP backend. Bare `zai`/`glm`
    # resolve directly in resolve_backend (env ZAI_MODEL / glm-4.6 default); these
    # aliases pin specific GLM model ids for `-m glm46`/`-m glm45`.
    "glm46": "zai:glm-4.6",
    "glm45": "zai:glm-4.5",
    "glm": "zai:glm-4.6",
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


# DEFAULT_BOARD: the out-of-the-box panel, so the board works WITHOUT a config
# file. Model ids are byte-exact against the commandcode gateway /models catalog
# (HYP-741) — do not alter the strings. Reviewers whose backend isn't available
# (no key / no CLI) are skipped at run time by the caller, not here.
DEFAULT_BOARD = (
    BoardReviewer("claude:claude-opus-4-8", "architect", "Opus"),
    BoardReviewer("codex", "correctness", "Codex"),
    BoardReviewer("gemini", "consistency", "Gemini"),
    BoardReviewer("commandcode:deepseek/deepseek-v4-pro", "performance", "DeepSeek"),
    BoardReviewer("commandcode:moonshotai/Kimi-K2.7-Code", "quality", "Kimi"),
    BoardReviewer("commandcode:Qwen/Qwen3.7-Max", "security", "Qwen"),
    BoardReviewer("commandcode:zai-org/GLM-5.1", "tests", "GLM"),
)


def _display_name(model: str) -> str:
    """A short, human-friendly label for a board reviewer derived from its model
    string — used when config.yaml omits an explicit name. Takes the last path
    segment of the provider id (so `commandcode:deepseek/deepseek-v4-pro` -> the
    provider tail, `claude:claude-opus-4-8` -> `claude-opus-4-8`)."""
    tail = model.split(":", 1)[1] if ":" in model else model
    return tail.rsplit("/", 1)[-1]


def load_board(config: dict | None = None) -> list[BoardReviewer]:
    """Resolve the active reviewer board.

    A `board:` key in config.yaml (a list of `{model, role[, name]}` mappings)
    overrides the built-in DEFAULT_BOARD. Validation never crashes the run:
      * a non-list / empty `board:`  -> fall back to DEFAULT_BOARD;
      * an entry without a usable `model` -> skipped with a warning;
      * an unknown `role` -> kept, but the reviewer uses the generic prompt
        (role_lens == "") and a warning is logged — the board degrades, it does
        not abort.
    With no `board:` configured at all, returns DEFAULT_BOARD (zero regression:
    the caller decides whether to use the board or the legacy models list)."""
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
    return board or list(DEFAULT_BOARD)


def _split_models(raw: list[str]) -> list[str]:
    values: list[str] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part:
                values.append(_expand_alias(part))
    return values
