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

    REUSE (§6.4 / CTO D9): vision providers piggyback on the SAME config surface review
    already uses for Gemini — no new per-provider egress config is invented."""
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    env_file = os.environ.get("GEMINI_ENV_FILE")
    paths = (Path(env_file),) if env_file else GEMINI_ENV_FALLBACKS
    for path in paths:
        key = _read_env_key(path, fallback_var)
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
