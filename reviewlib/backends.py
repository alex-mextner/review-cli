"""Model backends: codex / gemini / claude / opencode, and backend resolution.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). Each backend builds a TEXT payload and
returns a ReviewResult. `_ANNOUNCE_LOGS` is a module-global toggled on by the
panel modes so streamed calls print their live-log path to stderr.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .process import _run, _run_streamed

GEMINI_ENV_FALLBACKS = (
    Path.home() / ".config" / "review-cli" / ".env",
    Path("/Users/ultra/xp/ExpenseSyncBot/.env"),
)

# When True, streamed backend calls print their live-log path to stderr at start so
# the user knows what to `tail -f`. Enabled by the panel modes (--just-ask/--quorum/
# --brainstorm); the plain single-diff review path keeps stderr quiet.
_ANNOUNCE_LOGS = False


@dataclass(frozen=True)
class ReviewResult:
    model: str
    command: str
    returncode: int
    stdout: str
    stderr: str


def _which(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} not found on PATH")
    return path


# Backends that re-argv the prompt cap out near ARG_MAX (~1 MB): opencode passes
# the message as argv; claude-p reads stdin but its INNER `claude` exec re-argv's
# it. Feeding via stdin (claude/codex) removes review-cli's own argv overhead, but
# can't lift that inner ceiling — so warn well before it, loudly not silently.
_PAYLOAD_WARN_BYTES = 600_000


def _payload(prompt: str, diff: str = "") -> str:
    out = prompt if not diff.strip() else f"{prompt}\n\n```diff\n{diff}\n```"
    if len(out.encode("utf-8")) > _PAYLOAD_WARN_BYTES:
        print(
            f"[review-cli] WARNING: payload is {len(out)} bytes — nearing the ~1 MB "
            "ARG_MAX ceiling; opencode (argv-only) and claude-p's inner exec may fail. "
            "Use fewer --max-rounds or a smaller diff.",
            file=sys.stderr, flush=True,
        )
    return out


def review_codex(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    codex_model = model.split(":", 1)[1] if ":" in model else None
    argv = [_which("codex"), "exec", "-s", "read-only", "-C", str(cwd), "--ephemeral"]
    if codex_model:
        argv += ["-m", codex_model]
    argv.append("-")
    command = " ".join(argv[:-1]) + " -"
    proc = _run_streamed(
        argv, cwd=cwd, input_text=_payload(prompt, diff), timeout=timeout,
        backend="codex", announce=_ANNOUNCE_LOGS,
    )
    return ReviewResult(model=model, command=command, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _ensure_opencode_readonly_agent(_project: Path, _oc_model: str) -> None:
    agent = Path.home() / ".config" / "opencode" / "agents" / "read-only-reviewer.md"
    if agent.is_file():
        return
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_text(
        textwrap.dedent(
            """\
            ---
            description: Read-only code reviewer for diff inspection.
            mode: primary
            permission:
              bash: deny
              edit: deny
              write: deny
              webfetch: deny
              task: deny
              todowrite: deny
              websearch: deny
              lsp: deny
              skill: deny
            ---
            You are a read-only code reviewer. Do not edit files, write files, run
            shell commands, or ask questions. Return only actionable findings.
            """
        ),
        encoding="utf-8",
    )


def review_opencode(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    oc_model = model.split(":", 1)[1] if ":" in model else model
    with tempfile.TemporaryDirectory(prefix="review-cli-opencode-") as tmp_raw:
        tmp = Path(tmp_raw)
        _run(["git", "init", "-q"], cwd=tmp, timeout=30)
        _ensure_opencode_readonly_agent(tmp, oc_model)
        if diff.strip():
            context = tmp / "review.diff"
            context.write_text(diff, encoding="utf-8")
            message = (
                f"{prompt}\n\nYou are running outside the source repo; do not edit files. "
                f"Review this diff:\n\n```diff\n{diff}\n```"
            )
        else:
            message = (
                f"{prompt}\n\nYou are running outside any source repo; do not edit files "
                "or run shell commands. Answer based on the prompt alone."
            )
        argv = [
            _which("opencode"),
            "run",
            "--agent",
            "read-only-reviewer",
            "-m",
            oc_model,
            message,
        ]
        proc = _run_streamed(argv, cwd=tmp, timeout=timeout, backend="opencode", announce=_ANNOUNCE_LOGS)
    command = f"opencode run --agent read-only-reviewer -m {oc_model} <prompt-with-diff>"
    return ReviewResult(model=model, command=command, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _read_env_key(env_file: Path, var: str = "GEMINI_API_KEY") -> str | None:
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{var}="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def _resolve_key(env_names: tuple, fallback_var: str) -> str | None:
    """Resolve a provider API key from the environment, then the same fallback files
    review already reads (GEMINI_ENV_FILE override, else GEMINI_ENV_FALLBACKS — the
    shared `~/.config/review-cli/.env` and the personal env). Returns None if unset.

    Precedence is KEY-NAME-FIRST, not path-first: env var beats every file, and among
    the files the canonical/primary key name wins over an alias REGARDLESS of which
    .env file each lives in. So `COMMANDCODE_API_KEY` in a later fallback file beats
    `DEEPSEEK_API_KEY` in an earlier one — the precedence a caller reading the env-var
    order would expect. (A path-first loop would let an earlier file's alias shadow a
    later file's primary key, a surprising and non-deterministic ordering.)

    REUSE (§6.4 / CTO D9): vision providers piggyback on the SAME config surface review
    already uses for Gemini — no new per-provider egress config is invented."""
    # env var wins over any file, in declared order (primary name first).
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    env_file = os.environ.get("GEMINI_ENV_FILE")
    paths = (Path(env_file),) if env_file else GEMINI_ENV_FALLBACKS
    # File lookup is name-priority-first: fallback_var (the canonical/primary name),
    # then the remaining accepted aliases, each checked across ALL .env files before
    # moving to the next name. This keeps key-name precedence deterministic and
    # independent of file ordering, mirroring the env-var lookup above.
    file_vars = (fallback_var, *(n for n in env_names if n != fallback_var))
    for var in file_vars:
        for path in paths:
            key = _read_env_key(path, var)
            if key:
                return key
    return None


def _gemini_key() -> str:
    key = _resolve_key(("GEMINI_API_KEY", "GOOGLE_API_KEY"), "GEMINI_API_KEY")
    if key:
        return key
    raise RuntimeError("GEMINI_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env")


# NOTE: `_anthropic_key` / `_openai_key` were added in 30163c5 ONLY for the REST vision
# path. That path is gone (vision now invokes the agent CLIs — claude/codex — which carry
# their own auth, exactly like review's TEXT backends). Gemini stays the one key-based
# vision exception via `_gemini_key`. The two orphaned helpers are removed with the REST
# adapters; `_resolve_key` stays — `_gemini_key` still uses it.


def review_gemini(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    gemini_model = model.split(":", 1)[1] if ":" in model else os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    key = _gemini_key()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": _payload(prompt, diff)}]}],
        "toolConfig": {"functionCallingConfig": {"mode": "NONE"}},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        usage = payload.get("usageMetadata", {})
        stdout = text.strip() + f"\n\nprompt_tokens={usage.get('promptTokenCount', 0)} output_tokens={usage.get('candidatesTokenCount', 0)}\n"
        return ReviewResult(model=model, command=f"Gemini API {gemini_model}", returncode=0, stdout=stdout, stderr="")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        return ReviewResult(model=model, command=f"Gemini API {gemini_model}", returncode=exc.code, stdout="", stderr=body_text)


# --- Backend transport mode (api | cli) ----------------------------------------
# A backend can run as a REST `api` call, a `cli` subprocess, or both. The claude
# backend (PR #8) established the selector: a per-backend `REVIEW_<NAME>_MODE` env
# var forces one variant (else the backend auto-picks). This generalises that ONE
# mechanism so every backend declares its supported modes and resolves the forced
# mode the same way — no second, parallel selector. commandcode and z.ai are
# api-only (no commandcode/z.ai CLI exists); claude supports both; codex/opencode
# are cli-only. `resolve_backend_mode` is the single entry point: it reads
# `REVIEW_<NAME>_MODE`, validates it against the backend's `supported` modes, and
# returns the chosen mode (or `default` when unset). A forced mode the backend does
# NOT support is a hard, explicit error — never a silent fall-through to the wrong
# transport (e.g. forcing `cli` on commandcode must fail loudly, not POST anyway).


def _mode_env_var(name: str) -> str:
    """The env var that forces a backend's transport mode, e.g. commandcode ->
    REVIEW_COMMANDCODE_MODE. Mirrors PR #8's REVIEW_CLAUDE_MODE naming so the
    whole family is discoverable from one rule."""
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in name.upper())
    return f"REVIEW_{sanitized}_MODE"


def resolve_backend_mode(name: str, supported: tuple[str, ...], default: str) -> str:
    """Resolve a backend's transport mode from REVIEW_<NAME>_MODE, validated against
    `supported`. Returns the forced mode if set and supported, else `default`.

    Raises RuntimeError on an explicitly-forced mode the backend does not support —
    that is a user configuration error (e.g. REVIEW_COMMANDCODE_MODE=cli when no
    commandcode CLI exists) and must surface loudly, not silently run the api path.
    An empty/unset value selects `default` (the backend's own auto-pick)."""
    forced = os.environ.get(_mode_env_var(name), "").strip().lower()
    if not forced:
        return default
    if forced not in supported:
        raise RuntimeError(
            f"{_mode_env_var(name)}={forced!r} is not a supported mode for the "
            f"'{name}' backend (supported: {', '.join(supported)})"
        )
    return forced


# --- OpenAI-compatible keyed HTTP backends (z.ai / commandcode) -----------------
# Both z.ai (Zhipu / GLM) and commandcode expose an OpenAI-compatible
# /chat/completions API. Unlike review_gemini's bespoke `contents`/`parts` shape,
# these speak the standard OpenAI request body ({"model", "messages":[{role,content}]}
# + Authorization: Bearer). The two backends share one request builder so the wire
# shape stays identical; only the endpoint, key, and default model differ.
#
# Both are API-ONLY: no z.ai or commandcode CLI exists on PATH, so a forced
# `cli` mode is rejected by resolve_backend_mode rather than silently POSTing.


def _parse_openai_choice(payload: object) -> str:
    """Pull assistant text out of an OpenAI-compatible response, tolerating any shape.

    A provider can return a 2xx body that is valid JSON but NOT the expected object
    (e.g. `[]`, `{"choices":[null]}`, `{"choices":[{"message":[]}]}`). Each access is
    type-guarded so a wrong shape yields "" instead of raising AttributeError/
    TypeError/IndexError out of the backend (those would crash the whole run).

    REASONING MODELS (e.g. z.ai glm-5.2) return `message.content` (the final answer)
    PLUS `message.reasoning_content` (the chain of thought). We read `content` for the
    review text. When `content` is empty/missing but `reasoning_content` is present —
    a reasoning model with a low output-token budget can spend it all on reasoning and
    emit no final answer — we fall back to the reasoning text (prefixed so the reader
    knows what they're getting) rather than fail-closed as "empty output". A non-string
    reasoning field is ignored (type-guarded), never a crash."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return "[reasoning_content — no final answer returned]\n\n" + reasoning
    return content if isinstance(content, str) else ""


def _parse_openai_usage(payload: object) -> tuple[int, int]:
    """Return (prompt_tokens, output_tokens) from a response, 0/0 on any wrong shape."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    prompt = usage.get("prompt_tokens", 0)
    output = usage.get("completion_tokens", 0)
    return (prompt if isinstance(prompt, int) else 0, output if isinstance(output, int) else 0)


def _openai_compatible_request(
    *, model: str, api_model: str, label: str, base_url: str, key: str,
    prompt: str, diff: str, timeout: int, extra_body: dict | None = None,
) -> ReviewResult:
    """POST an OpenAI-compatible chat/completions request and return a ReviewResult.

    `model` is the REQUESTED backend string (e.g. `zai`, `commandcode:deepseek/deepseek-v4-flash`)
    and is preserved in ReviewResult.model — mode_review keys results by the requested
    string, so substituting the resolved provider id here would KeyError. `api_model`
    is the resolved provider model id sent on the wire (e.g. glm-4.6, deepseek/deepseek-v4-flash).

    `extra_body` merges provider-specific request fields into the body while keeping the
    shared OpenAI wire shape generic. Both current callers (z.ai, commandcode) pass None;
    the hook stays for any future provider that needs a non-standard field.

    `base_url` is the endpoint root (e.g. https://api.z.ai/api/paas/v4); the
    /chat/completions suffix is appended here so callers pass the same value users
    would set in any OpenAI-compatible client. EVERY failure mode maps to a non-zero
    returncode with the error on stderr — HTTP status errors, connection refused /
    DNS / socket timeouts (URLError, OSError, TimeoutError), malformed JSON
    (JSONDecodeError), and valid-but-wrong-shape JSON (type-guarded parse) — so the
    panel treats a failed call as a dead backend rather than crashing the whole run."""
    url = base_url.rstrip("/") + "/chat/completions"
    command = f"{label} API {api_model}"
    body = {
        "model": api_model,
        "messages": [{"role": "user", "content": _payload(prompt, diff)}],
        "stream": False,
    }
    if extra_body:
        body.update(extra_body)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        text = _parse_openai_choice(payload)
        if not text.strip():
            # A 2xx whose body carries NO assistant content (`[]`, `{"choices":[null]}`,
            # `{"error":...}` with HTTP 200, an empty completion) is NOT a successful
            # review — it has nothing to review with. Returning rc=0 here would let
            # mode_review write a "reviewed" stamp and satisfy the commit gate with an
            # empty result. Fail-closed: map it to a non-zero dead-backend result.
            return ReviewResult(
                model=model, command=command, returncode=1, stdout="",
                stderr=f"{label} API returned no assistant content: {raw[:500]}",
            )
        prompt_tokens, output_tokens = _parse_openai_usage(payload)
        stdout = text.strip() + (
            f"\n\nprompt_tokens={prompt_tokens} output_tokens={output_tokens}\n"
        )
        return ReviewResult(model=model, command=command, returncode=0, stdout=stdout, stderr="")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        return ReviewResult(model=model, command=command, returncode=exc.code, stdout="", stderr=body_text)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Connection refused / DNS failure / socket timeout — no HTTP status. URLError
        # and TimeoutError are both OSError subclasses; the wide catch normalises any
        # transport-level failure to a dead-backend result instead of crashing.
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="",
            stderr=f"{label} API request failed: {exc}",
        )
    except (json.JSONDecodeError, ValueError) as exc:
        # 2xx with a non-JSON / truncated body — the provider returned garbage. Treat
        # it as a failed call rather than letting the decode error escape.
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="",
            stderr=f"{label} API returned a malformed response: {exc}",
        )


# z.ai (Zhipu / GLM) — OpenAI-compatible /chat/completions, Bearer-keyed.
# DEFAULT base is the GLM Coding Plan endpoint (/api/coding/paas/v4): only that
# endpoint serves the flagship glm-5.2 (the STANDARD /api/paas/v4 endpoint tops out
# at glm-5.1 on its /models catalog). So a Coding-Plan key gets glm-5.2 out of the
# box; a standard-plan user overrides the base via ZAI_BASE_URL=https://api.z.ai/api/paas/v4
# (and picks a model their plan serves, e.g. ZAI_MODEL=glm-5.1). glm-5.2 is a
# REASONING model — it returns message.reasoning_content alongside message.content;
# _parse_openai_choice reads content and falls back to reasoning_content if content
# is empty (a low output budget can leave only the reasoning).
ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
ZAI_DEFAULT_MODEL = "glm-5.2"


def _zai_key() -> str:
    key = _resolve_key(("ZAI_API_KEY", "ZHIPU_API_KEY"), "ZAI_API_KEY")
    if key:
        return key
    raise RuntimeError("ZAI_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env")


ZAI_SUPPORTED_MODES = ("api",)  # z.ai is REST-only; no z.ai CLI exists.


def review_zai(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    # api-only: a forced REVIEW_ZAI_MODE=cli is a config error, surfaced as a
    # dead-backend result instead of silently running the api path.
    try:
        resolve_backend_mode("zai", ZAI_SUPPORTED_MODES, "api")
    except RuntimeError as exc:
        return ReviewResult(model=model, command="z.ai", returncode=1, stdout="", stderr=str(exc))
    zai_model = model.split(":", 1)[1] if ":" in model else os.environ.get("ZAI_MODEL", ZAI_DEFAULT_MODEL)
    base_url = os.environ.get("ZAI_BASE_URL", ZAI_DEFAULT_BASE_URL)
    key = _zai_key()
    return _openai_compatible_request(
        model=model, api_model=zai_model, label="z.ai", base_url=base_url, key=key,
        prompt=prompt, diff=diff, timeout=timeout,
    )


# commandcode — Command Code's OpenAI-compatible Provider API (API-only).
# The CTO confirmed: "Command code cli нет, есть api key" — there is no commandcode
# CLI, only an API key (format `user_...`). So commandcode is a keyed HTTP backend
# (same OpenAI wire shape as z.ai), NOT a subprocess. Endpoint verified from the
# Command Code Provider API docs (https://commandcode.ai/docs/provider-api):
#   base  https://api.commandcode.ai/provider/v1
#   POST  /chat/completions   (OpenAI/OSS models — what this backend speaks)
#   POST  /messages           (Anthropic models — use review_claude_api instead)
#   auth  Authorization: Bearer $COMMANDCODE_API_KEY   (the same CLI key)
# On /chat/completions the model id is provider-prefixed (e.g. deepseek/deepseek-v4-
# flash); a Claude id there 400s and is directed to /messages, so Anthropic models
# must go through the claude backend (REVIEW_CLAUDE_MODE=api), not here.
# Base URL + model are overridable via COMMANDCODE_BASE_URL / COMMANDCODE_MODEL.
COMMANDCODE_DEFAULT_BASE_URL = "https://api.commandcode.ai/provider/v1"
# Default to an OpenAI-shape (provider-prefixed) model the /chat/completions path
# serves. DeepSeek V4 Flash is a cheap, capable default for diff review; override
# with COMMANDCODE_MODEL or a `commandcode:<model>` suffix for anything else.
COMMANDCODE_DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
COMMANDCODE_SUPPORTED_MODES = ("api",)  # API-only — no commandcode CLI exists.

# NOTE (HYP-741): the earlier `common-code` placeholder injected DeepSeek's
# `{"thinking":{"type":"disabled"}}` field to reproduce the legacy non-thinking
# `deepseek-chat` default. That field is DeepSeek-API-specific and is NOT sent here:
# the default transport is the Command Code GATEWAY (api.commandcode.ai), which need
# not accept an unknown body field — sending it risks a 400. A user who points
# COMMANDCODE_BASE_URL at the raw DeepSeek endpoint and wants the non-thinking mode
# can pass it explicitly via the model id; we don't inject a provider-specific field
# behind their back onto a gateway. `_openai_compatible_request`'s generic
# `extra_body` hook stays available for any future provider that needs it.


def _commandcode_key() -> str:
    # ONLY COMMANDCODE_API_KEY. The key is a Command Code `user_...` token, NOT a
    # DeepSeek key — accepting DEEPSEEK_API_KEY here would silently POST a DeepSeek
    # credential to api.commandcode.ai (a different host), which both fails to auth
    # AND leaks the key cross-provider. So no alias key names: the canonical name is
    # the only one that resolves. (Codex P1, HYP-741.)
    key = _resolve_key(("COMMANDCODE_API_KEY",), "COMMANDCODE_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "COMMANDCODE_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env"
    )


def review_commandcode(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    # API-only: a forced REVIEW_COMMANDCODE_MODE=cli is a config error (there is no
    # commandcode CLI), surfaced as a dead-backend result, never a silent api POST.
    try:
        resolve_backend_mode("commandcode", COMMANDCODE_SUPPORTED_MODES, "api")
    except RuntimeError as exc:
        return ReviewResult(model=model, command="commandcode", returncode=1, stdout="", stderr=str(exc))
    has_suffix = ":" in model
    env_model = os.environ.get("COMMANDCODE_MODEL")
    cc_model = model.split(":", 1)[1] if has_suffix else (env_model or COMMANDCODE_DEFAULT_MODEL)
    base_url = os.environ.get("COMMANDCODE_BASE_URL") or COMMANDCODE_DEFAULT_BASE_URL
    key = _commandcode_key()
    return _openai_compatible_request(
        model=model, api_model=cc_model, label="commandcode", base_url=base_url, key=key,
        prompt=prompt, diff=diff, timeout=timeout,
    )


# A non-default User-Agent: some Anthropic-compatible gateways (e.g. CommandCode,
# behind Cloudflare) 403 the bare urllib UA with "error code: 1010".
_ANTHROPIC_UA = "review-cli (anthropic-compatible client)"
_CLAUDE_REVIEW_SYSTEM = (
    "You are running inside review-cli in a headless read-only diff review. "
    "Do not use tools, inspect files, ask for permissions, or plan tool work. "
    "Answer only from the prompt and diff supplied by the user."
)


def _anthropic_api_config() -> dict | None:
    """Resolve Anthropic-compatible API config (auth header + base url), or None.

    Mirrors the Anthropic SDK / claude CLI env surface so the SAME vars drive
    both review's API backend and any local claude CLI: ANTHROPIC_API_KEY (sent
    as ``x-api-key``) or ANTHROPIC_AUTH_TOKEN (sent as ``Authorization: Bearer``,
    for gateways / OAuth), plus ANTHROPIC_BASE_URL (default api.anthropic.com,
    point it at e.g. CommandCode). Keys also fall back to review's shared env
    files via _resolve_key. Returns None when no key is set, so the dispatcher
    can fall back to the CLI backend."""
    apikey = _resolve_key(("ANTHROPIC_API_KEY",), "ANTHROPIC_API_KEY")
    if apikey:
        auth = ("x-api-key", apikey)
    else:
        token = _resolve_key(("ANTHROPIC_AUTH_TOKEN",), "ANTHROPIC_AUTH_TOKEN")
        if not token:
            return None
        auth = ("Authorization", f"Bearer {token}")
    base = (os.environ.get("ANTHROPIC_BASE_URL", "").strip() or "https://api.anthropic.com").rstrip("/")
    return {"base": base, "auth": auth}


def review_claude_api(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    """Anthropic Messages API backend — works WITHOUT the claude CLI (needs only a
    key). POSTs to ``{ANTHROPIC_BASE_URL}/v1/messages``; the default base is
    Anthropic, but any Anthropic-compatible gateway works (e.g. CommandCode via
    ANTHROPIC_BASE_URL). ``cwd`` is unused — the API has no workspace, which is
    exactly why this variant runs where the CLI cannot."""
    claude_model = model.split(":", 1)[1] if ":" in model else os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
    cfg = _anthropic_api_config()
    command = f"Anthropic API {claude_model}"
    if cfg is None:
        return ReviewResult(model=model, command=command, returncode=1, stdout="",
                            stderr="claude API mode: no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN configured")
    try:
        max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "16000"))
    except ValueError:
        max_tokens = 16000
    body = {
        "model": claude_model,
        "max_tokens": max_tokens,
        "system": _CLAUDE_REVIEW_SYSTEM,
        "messages": [{"role": "user", "content": _payload(prompt, diff)}],
    }
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "User-Agent": _ANTHROPIC_UA,
        cfg["auth"][0]: cfg["auth"][1],
    }
    data = json.dumps(body).encode("utf-8")
    url = cfg["base"] + "/v1/messages"
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    command = f"Anthropic API {claude_model} @ {cfg['base']}"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        parts = payload.get("content", []) if isinstance(payload, dict) else []
        text = "".join(
            p.get("text", "") for p in parts
            if isinstance(p, dict) and p.get("type") == "text"
        )
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        stdout = text.strip() + (
            f"\n\ninput_tokens={usage.get('input_tokens', 0)} "
            f"output_tokens={usage.get('output_tokens', 0)}\n"
        )
        # Empty success is a failure for the panel/moderator fallback path.
        return ReviewResult(model=model, command=command,
                            returncode=0 if text.strip() else 1, stdout=stdout, stderr="")
    except urllib.error.HTTPError as exc:
        return ReviewResult(model=model, command=command, returncode=exc.code, stdout="",
                            stderr=exc.read().decode("utf-8", "replace"))
    except urllib.error.URLError as exc:
        return ReviewResult(model=model, command=command, returncode=1, stdout="", stderr=str(exc))
    except (ValueError, OSError) as exc:
        # malformed / non-JSON 2xx body, or a read/decode/timeout failure — surface
        # as a normal backend result, not an uncaught exception. (URLError, a
        # subclass of OSError, is handled above; this catches the rest.)
        return ReviewResult(model=model, command=command, returncode=1, stdout="",
                            stderr=f"claude API: malformed or unreadable response: {exc}")


def _have_claude_cli() -> bool:
    try:
        _which("claude-p")
        return True
    except RuntimeError:
        return False


def review_claude(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    """Dispatch the claude/opus backend between the API and CLI variants.

    REVIEW_CLAUDE_MODE forces it: ``api`` (HTTP, no claude binary needed) or
    ``cli`` (claude-p). With no override the choice is automatic and conservative:
    prefer the CLI when the claude binary is present (subscription, no API cost,
    and reliable now that workspace trust is deterministic), and fall back to the
    API only when there is no claude binary but a key IS configured — i.e. don't
    silently switch a working CLI host to the paid API just because a key happens
    to be in the environment. Set REVIEW_CLAUDE_MODE=api to force the API."""
    mode = os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
    if mode == "api":
        return review_claude_api(model, prompt, diff, cwd, timeout)
    if mode != "cli" and not _have_claude_cli() and _anthropic_api_config() is not None:
        return review_claude_api(model, prompt, diff, cwd, timeout)
    return review_claude_cli(model, prompt, diff, cwd, timeout)


def _ensure_workspace_trusted(cwd: Path) -> None:
    """Pre-accept Claude Code's workspace trust for ``cwd`` in ~/.claude.json.

    The headless claude-p backend drives the interactive claude TUI under a PTY;
    in an untrusted directory (a fresh git worktree, a not-yet-opened repo) it
    blocks on claude's "Quick safety check / Do you trust this folder?" gate.
    claude-p's PTY auto-accept of that gate is non-deterministic (it sometimes
    reaches the answer, sometimes reports ``workspace_trust_blocked``), and the
    bypass routes are worse: ``--permission-mode bypassPermissions`` and
    ``--dangerously-skip-permissions`` each raise their own safety gate the PTY
    layer cannot dismiss. Seeding the trust flag directly makes the gate never
    appear, so the deterministic ``dontAsk`` path just works.

    This is a DELIBERATE, persistent edit to the user's global config (review is
    configured to auto-trust the directory it reviews): a later interactive
    claude session in the same dir inherits the trust. It is scoped to the trust
    flag (+ onboarding) only — no permissions or tools are granted.

    Best-effort and conservative: skips the write when already trusted (the
    common case → no write at all), only touches the one project entry, writes
    via a same-dir temp + atomic ``os.replace`` (a concurrent reader never sees a
    half-write), and serialises review's own writers with an flock. Any error
    (missing / locked / non-JSON config) silently degrades to the old behaviour.
    """
    cfg = Path.home() / ".claude.json"
    if not cfg.exists():
        return  # best-effort: never fabricate a config the user didn't have
    # claude keys workspace trust by the RESOLVED real path (it canonicalises
    # cwd — e.g. /tmp -> /private/tmp, symlinked worktrees), so a raw-path key
    # would silently miss and the prompt would still fire.
    key = os.path.realpath(str(cwd))
    # Serialise the read-modify-write against other review processes with an
    # advisory flock on a sidecar lock file (the data file itself is swapped by
    # os.replace, so a lock held on it wouldn't survive the swap). This can't
    # synchronise against `claude` — it doesn't lock — but the skip-when-already-
    # trusted guard means we only ever write on the FIRST review in a fresh dir,
    # keeping that residual window tiny.
    lock = None
    flock_mod = None
    try:
        try:
            import fcntl as flock_mod
        except ImportError:
            flock_mod = None  # non-POSIX (e.g. Windows) → unsynchronised, best-effort
        if flock_mod is not None:
            try:
                lock = open(cfg.with_name(".claude.json.review-trust.lock"), "w")
                flock_mod.flock(lock.fileno(), flock_mod.LOCK_EX)
            except OSError:
                if lock is not None:
                    lock.close()
                lock = None  # lock unavailable → proceed unsynchronised, best-effort
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return  # already trusted — leave the shared config untouched
        entry = entry if isinstance(entry, dict) else {}
        entry["hasTrustDialogAccepted"] = True
        # Force (not setdefault): a prior blocked/headless attempt can leave a
        # partial entry with hasCompletedProjectOnboarding=false, which would
        # still let claude block on the onboarding gate. Seed both to true.
        entry["hasCompletedProjectOnboarding"] = True
        projects[key] = entry
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(cfg.parent), prefix=".claude.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, cfg)  # atomic: a concurrent reader never sees a half-write
        except Exception:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        return  # malformed / unreadable config → degrade to the old behaviour
    finally:
        if lock is not None:
            try:
                if flock_mod is not None:
                    flock_mod.flock(lock.fileno(), flock_mod.LOCK_UN)
            finally:
                lock.close()


def review_claude_cli(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    claude_model = model.split(":", 1)[1] if ":" in model else None
    # Resolve the binary BEFORE touching trust: a missing claude-p must raise
    # here, not after _ensure_workspace_trusted has already mutated ~/.claude.json.
    claude_p = _which("claude-p")
    # Pre-accept workspace trust for cwd: the headless run cannot answer the
    # interactive "Do you trust this folder?" gate (see _ensure_workspace_trusted).
    _ensure_workspace_trusted(cwd)
    argv = [
        claude_p,
        "--cwd",
        str(cwd),
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--safe-mode",
        "--append-system-prompt",
        _CLAUDE_REVIEW_SYSTEM,
        "--disallowedTools",
        "Edit",
        "MultiEdit",
        "Write",
        "Bash",
        "Read",
        "Grep",
        "Glob",
        "NotebookEdit",
        "SlashCommand",
        "Task",
        "TodoWrite",
        "ExitPlanMode",
        "WebFetch",
        "WebSearch",
        "--timeout-sec",
        str(timeout),
    ]
    if claude_model:
        argv += ["--model", claude_model]
    # Feed the payload over STDIN, not `-p <payload>` argv: a brainstorm round's
    # prompt embeds the whole prior-round transcript (and a review's diff can be
    # huge), which as a command-line argument blows past ARG_MAX (~1 MB) → execve
    # E2BIG → the call dies before producing any output. `-p` stays as the print
    # flag; claude-p reads the prompt from stdin, like the codex backend's `-`.
    argv += ["-p"]
    proc = _run_streamed(
        argv, cwd=cwd, input_text=_payload(prompt, diff),
        timeout=timeout + 30, backend="claude", announce=_ANNOUNCE_LOGS,
    )
    command = "claude-p --permission-mode dontAsk --tools '' --strict-mcp-config --disable-slash-commands --safe-mode --append-system-prompt <read-only-review> --disallowedTools Edit MultiEdit Write Bash Read Grep Glob NotebookEdit SlashCommand Task TodoWrite ExitPlanMode WebFetch WebSearch -p  (prompt via stdin)"
    return ReviewResult(model=model, command=command, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def resolve_backend(model: str) -> Callable[[str, str, str, Path, int], ReviewResult]:
    lowered = model.lower()
    if lowered == "codex" or lowered.startswith("codex:"):
        return review_codex
    if lowered in ("gemini", "gemini-api") or lowered.startswith("gemini:"):
        return review_gemini
    # z.ai (Zhipu / GLM) — OpenAI-compatible keyed HTTP. `zai`/`glm` plus `zai:<model>`
    # (e.g. zai:glm-5.2). `glm:` prefix also routes here.
    if (
        lowered in ("zai", "z.ai", "zhipu", "glm")
        or lowered.startswith(("zai:", "z.ai:", "glm:", "zhipu:"))
    ):
        return review_zai
    # commandcode — Command Code's OpenAI-compatible Provider API (keyed HTTP).
    # The legacy `common-code`/`common_code` spellings still route here as aliases so
    # any pre-rename config keeps working.
    if lowered in (
        "commandcode", "command-code", "command_code",
        "common-code", "commoncode", "common_code",
    ) or lowered.startswith(
        ("commandcode:", "command-code:", "command_code:",
         "common-code:", "commoncode:", "common_code:")
    ):
        return review_commandcode
    # fable IS claude-p. Route any fable form to review_claude defensively, so an
    # unexpanded `fable`/`fable5` can NEVER fall through to the review_opencode
    # default (which would hit fireworks — the wrong provider entirely).
    if (
        lowered in ("claude", "claude-p", "claude-fable-5")
        or lowered.startswith("claude:")
        or lowered.startswith("fable")
    ):
        return review_claude
    if lowered.startswith(("oc:", "opencode:")):
        return review_opencode
    return review_opencode


def backend_available(model: str) -> bool:
    """Cheap availability probe so moderator selection never picks a dead backend."""
    backend = resolve_backend(model)
    try:
        if backend is review_gemini:
            _gemini_key()
            return True
        if backend is review_zai:
            # Honor a forced mode: REVIEW_ZAI_MODE=cli makes review_zai a dead
            # backend, so it must NOT report available (resolve_backend_mode raises
            # on the unsupported mode → caught below → False). Codex P2.
            resolve_backend_mode("zai", ZAI_SUPPORTED_MODES, "api")
            _zai_key()
            return True
        if backend is review_commandcode:
            # Same as z.ai: a forced REVIEW_COMMANDCODE_MODE=cli is unrunnable, so the
            # probe must reflect that instead of selecting a backend that only fails.
            resolve_backend_mode("commandcode", COMMANDCODE_SUPPORTED_MODES, "api")
            _commandcode_key()
            return True
        if backend is review_codex:
            _which("codex")
            return True
        if backend is review_claude:
            # Mirror the dispatcher so availability matches what would actually run:
            # a forced mode is available only via that one variant; otherwise it's
            # available via EITHER the API key or the claude-p CLI.
            mode = os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
            if mode == "api":
                return _anthropic_api_config() is not None
            if mode == "cli":
                _which("claude-p")
                return True
            if _anthropic_api_config() is not None:
                return True
            _which("claude-p")
            return True
        if backend is review_opencode:
            _which("opencode")
            return True
    except RuntimeError:
        return False
    return False
