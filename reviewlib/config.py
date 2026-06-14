"""Config + model selection: defaults, aliases, ~/.config/review-cli/config.yaml.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).
"""
from __future__ import annotations

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
    # common-code (commandcode / DeepSeek family) — OpenAI-compatible keyed HTTP.
    "commoncode": "common-code",
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
    """Read ~/.config/review-cli/config.yaml (keys: models, brainstorm_models).
    Returns {} if absent/unparseable so the code default always applies."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _split_models(raw: list[str]) -> list[str]:
    values: list[str] = []
    for item in raw:
        for part in item.split(","):
            part = part.strip()
            if part:
                values.append(_expand_alias(part))
    return values
