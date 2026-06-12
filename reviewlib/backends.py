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


def _payload(prompt: str, diff: str = "") -> str:
    if not diff.strip():
        return prompt
    return f"{prompt}\n\n```diff\n{diff}\n```"


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


def _read_env_key(env_file: Path) -> str | None:
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("GEMINI_API_KEY="):
                value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except OSError:
        return None
    return None


def _gemini_key() -> str:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    env_file = os.environ.get("GEMINI_ENV_FILE")
    paths = (Path(env_file),) if env_file else GEMINI_ENV_FALLBACKS
    for path in paths:
        key = _read_env_key(path)
        if key:
            return key
    raise RuntimeError("GEMINI_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env")


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


def review_claude(model: str, prompt: str, diff: str, cwd: Path, timeout: int) -> ReviewResult:
    claude_model = model.split(":", 1)[1] if ":" in model else None
    argv = [
        _which("claude-p"),
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
        "You are running inside review-cli in a headless read-only diff review. "
        "Do not use tools, inspect files, ask for permissions, or plan tool work. "
        "Answer only from the prompt and diff supplied by the user.",
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
    argv += ["-p", _payload(prompt, diff)]
    proc = _run_streamed(argv, cwd=cwd, timeout=timeout + 30, backend="claude", announce=_ANNOUNCE_LOGS)
    command = "claude-p --permission-mode dontAsk --tools '' --strict-mcp-config --disable-slash-commands --safe-mode --append-system-prompt <read-only-review> --disallowedTools Edit MultiEdit Write Bash Read Grep Glob NotebookEdit SlashCommand Task TodoWrite ExitPlanMode WebFetch WebSearch -p <prompt>"
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
            _which("claude-p")
            return True
        if backend is review_opencode:
            _which("opencode")
            return True
    except RuntimeError:
        return False
    return False
