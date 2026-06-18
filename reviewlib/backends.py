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
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .process import _run, _run_streamed, write_sidecar_log

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


def review_codex(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
    codex_model = model.split(":", 1)[1] if ":" in model else None
    argv = [_which("codex"), "exec", "-s", "read-only", "-C", str(cwd), "--ephemeral"]
    if codex_model:
        argv += ["-m", codex_model]
    argv.append("-")
    command = " ".join(argv[:-1]) + " -"
    proc = _run_streamed(
        argv, cwd=cwd, input_text=_payload(prompt, diff), timeout=timeout,
        backend="codex", round_no=round_no, announce=_ANNOUNCE_LOGS,
    )
    return ReviewResult(model=model, command=command, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


# Every capability the read-only reviewer agent MUST deny. This is the security boundary
# the whole agentic-opencode path relies on: opencode may READ project files under this
# agent, but every one of these must be `deny` so it can never mutate the repo, run a
# shell command, or hit the network. This list mirrors opencode's mutating/egress
# permission keys as of the version this CLI targets.
#
# SCOPE / LIMITATION (review-cli#40 review finding): the validator's guarantee is "none of
# THESE keys grants, and nothing PRESENT grants". It canNOT defend against a FUTURE
# opencode capability that (a) is not in this list and (b) defaults to permissive when
# absent — such a key would need to be added here AND to the canonical file. So this set
# must be kept in sync with opencode's permission surface; it is the floor, not a proof
# that opencode exposes nothing else. (opencode has no documented deny-by-default/wildcard
# we can lean on instead, so an explicit, maintained key list is the available mitigation.)
_READONLY_AGENT_DENIED_PERMISSIONS = (
    "bash",
    "edit",
    "write",
    "webfetch",
    "task",
    "todowrite",
    "websearch",
    "lsp",
    "skill",
)

# The canonical, safe read-only-reviewer agent. SINGLE SOURCE OF TRUTH: the permission
# block is GENERATED from `_READONLY_AGENT_DENIED_PERMISSIONS` (not duplicated as a
# literal), so the validator (which reads that tuple) and the writer can never drift — a
# deny key added to the tuple is both enforced AND written. Both the "create when missing"
# and "rewrite a permissive/tampered one" paths write THIS exact content.
def _build_readonly_agent_markdown() -> str:
    perm_lines = "\n".join(f"  {name}: deny" for name in _READONLY_AGENT_DENIED_PERMISSIONS)
    return (
        "---\n"
        "description: Read-only code reviewer for diff inspection.\n"
        "mode: primary\n"
        "permission:\n"
        f"{perm_lines}\n"
        "---\n"
        "You are a read-only code reviewer. Do not edit files, write files, run\n"
        "shell commands, or ask questions. Return only actionable findings.\n"
    )


_READONLY_AGENT_MARKDOWN = _build_readonly_agent_markdown()


def _frontmatter_yaml(text: str) -> str | None:
    """The YAML between the opening ``---`` and the next CLOSING ``---``, each on its OWN
    line (the markdown frontmatter convention), or ``None`` if there is no such block.

    Splitting on a LINE-anchored delimiter (not a bare ``---`` substring) avoids tearing
    the YAML on a ``---`` that appears INSIDE a value (e.g. ``description: a---b``), which a
    naive ``str.split('---')`` would mis-cut — turning a legit file into "unparseable" and
    forcing a needless rewrite."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i])
    return None  # opening --- but no closing --- => not a frontmatter block


def _agent_frontmatter(text: str) -> dict | None:
    """Parse the YAML frontmatter of an opencode agent markdown file into a dict.

    Returns ``None`` when there is no line-delimited ``---`` frontmatter block or it does
    not parse to a mapping — the caller treats that as "not a trusted read-only definition"
    and rewrites the canonical one. PyYAML is a hard dependency (it parses config.yaml),
    but the import stays lazy/guarded so a broken install degrades to "rewrite" rather
    than a traceback on the hot review path."""
    block = _frontmatter_yaml(text)
    if block is None:
        return None
    try:
        import yaml

        data = yaml.safe_load(block)
    except Exception:  # noqa: BLE001 — any parse/import failure => not trusted
        return None
    return data if isinstance(data, dict) else None


def _permission_value_is_fully_denied(value: object) -> bool:
    """True if a single permission VALUE grants nothing.

    opencode accepts both the scalar form (``bash: deny``) and a granular per-pattern map
    (``bash: {"*": deny, "git diff": deny}``). A value denies everything iff it is:
      * the string ``deny``; OR
      * a granular map that has an explicit catch-all ``"*": deny`` AND every other leaf is
        also fully denied.
    The catch-all requirement is the SECURITY-CRITICAL bit (verified against opencode's
    permission docs, review-cli#40): opencode's defaults are PERMISSIVE and rules match by
    "last matching wins", so a granular map WITHOUT a ``"*"`` catch-all leaves every
    UNLISTED command at the default ``allow``. A map like ``bash: {"git status": deny}``
    would otherwise look "all-deny" to a naive scan yet permit arbitrary other shell — the
    exact tampered-agent hole this hardening must close. An empty map, a missing/non-deny
    ``"*"``, a string other than ``deny`` (``allow``/``ask``), or any other type is a GRANT."""
    if isinstance(value, str):
        return value == "deny"
    if isinstance(value, dict):
        if not value:
            return False
        # A granular map must explicitly deny the catch-all (unlisted patterns default to
        # allow otherwise) AND deny every other listed pattern.
        if value.get("*") != "deny":
            return False
        return all(_permission_value_is_fully_denied(v) for v in value.values())
    return False


def _agent_is_strictly_readonly(text: str) -> bool:
    """True only if the agent definition's permission block is at-least-as-strict as the
    canonical deny-all set — i.e. it grants NO write/exec capability.

    Strict on purpose (a stale/tampered global agent is the threat in review-cli#40):
      * the frontmatter must parse to a mapping with a ``permission`` mapping;
      * EVERY key we require (``_READONLY_AGENT_DENIED_PERMISSIONS``) must be present and
        fully denied — a missing key defaults to opencode's permissive behaviour, so a
        partial block is treated as permissive;
      * EVERY key actually present (including extra capabilities we don't enumerate, e.g.
        a future ``network``) must ALSO be fully denied. A single grant on ANY key — known
        or unknown, scalar or nested — means we reject it and rewrite the canonical def.
    "Fully denied" accepts BOTH the scalar ``deny`` and a granular per-action map whose
    every leaf is ``deny`` (so a legitimately hardened granular config is NOT clobbered).
    Extra keys that are themselves fully denied are ACCEPTED (a user who hardened the agent
    with more denies is at least as safe as canonical — we must not downgrade them by
    rewriting). Anything with a grant, a missing required key, or an unparseable frontmatter
    is rejected so the caller rewrites the canonical safe definition.

    NOTE: this enforces "no capability is granted", NOT "exactly the canonical key set".
    The known-safe set is the FLOOR (all must be denied); extra denies only raise it."""
    front = _agent_frontmatter(text)
    if front is None:
        return False
    perms = front.get("permission")
    if not isinstance(perms, dict):
        return False
    # Floor: every capability we know must be present and fully denied (a missing one
    # falls back to opencode's permissive default, so a partial block is permissive).
    if any(name not in perms for name in _READONLY_AGENT_DENIED_PERMISSIONS):
        return False
    # And nothing present (extras included) may grant anything: every value must be denied.
    return all(_permission_value_is_fully_denied(value) for value in perms.values())


def _ensure_opencode_readonly_agent(_project: Path, _oc_model: str) -> None:
    """Guarantee the GLOBAL read-only-reviewer opencode agent is the safe deny-all def.

    SECURITY (review-cli#40): a pre-existing global agent file is NOT trusted blindly. A
    stale or tampered ``read-only-reviewer.md`` that ALLOWS bash/edit/write/etc would
    silently give the "read-only" reviewer write/exec capability on the user's repo,
    defeating the guarantee the whole agentic-opencode path leans on. So we VALIDATE an
    existing file and REWRITE the canonical safe definition (loudly) if it is not strictly
    read-only — never run agentically against a permissive agent. Idempotent: a file that
    already matches the canonical deny-all set is left untouched (no spurious writes)."""
    agent = Path.home() / ".config" / "opencode" / "agents" / "read-only-reviewer.md"
    if agent.is_file():
        try:
            existing = agent.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if _agent_is_strictly_readonly(existing):
            return  # already the trusted deny-all definition
        print(
            f"[review-cli] opencode: the global read-only-reviewer agent at {agent} is "
            "not strictly read-only (its permissions grant or omit a deny for a write/exec "
            "capability); rewriting it to the canonical deny-all definition for safety.",
            file=sys.stderr, flush=True,
        )
    agent.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(agent, _READONLY_AGENT_MARKDOWN)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` ATOMICALLY: a concurrent reader sees either the old file or
    the complete new one, never a truncated mid-write file.

    The default board runs SEVERAL agentic opencode seats in parallel, each calling
    `_ensure_opencode_readonly_agent`. A plain `write_text` (truncate-then-write) lets one
    seat's opencode read this agent while another seat is rewriting it — the reader could
    get an empty/partial markdown, lose the frontmatter, and fall back to opencode's
    PERMISSIVE defaults (bash/edit/write). Writing to a temp file in the SAME directory and
    `os.replace`-ing it in is atomic on POSIX, so the read-only guarantee holds under
    concurrency. The temp file is cleaned up on any write failure."""
    fd, tmp_name = tempfile.mkstemp(prefix=".read-only-reviewer.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)  # atomic on POSIX: reader never sees a partial file
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# Project-local opencode config paths that opencode merges when run with `--dir <cwd>`.
# A reviewed repo that ships ANY of these can REDEFINE the `read-only-reviewer` agent
# (or the global permission rules) and FLIP its deny→allow — verified: a repo-local
# `.opencode/agent/read-only-reviewer.md` with `permission: { write: allow, … }` (or an
# `agent:` block in a root opencode.json/jsonc) wins over our global agent, and neither
# OPENCODE_DISABLE_PROJECT_CONFIG nor OPENCODE_PERMISSION nor an inline OPENCODE_CONFIG
# agent suppresses it. So the read-only guarantee would depend on the repo under review.
# We refuse to run agentically in such a repo and fall back to the isolated temp-dir
# (diff-only) posture instead — the safe default for a potentially adversarial repo.
_OPENCODE_PROJECT_CONFIG_NAMES = (".opencode", "opencode.json", "opencode.jsonc")


def _opencode_has_project_config(cwd: Path) -> bool:
    """True if `cwd` ships its OWN opencode config that could redefine the read-only
    agent / weaken its permissions (see _OPENCODE_PROJECT_CONFIG_NAMES). When True we do
    NOT run opencode agentically there — the sandbox can't be trusted."""
    return any((cwd / name).exists() for name in _OPENCODE_PROJECT_CONFIG_NAMES)


def _opencode_runs_in_repo(cwd: Path) -> bool:
    """True iff `cwd` is a git repo we can let opencode read AGENTICALLY AND SAFELY.

    Mirrors review's own cwd resolution: the panel/agentic backends (codex/claude)
    run in the REAL repo read-only and read the whole tree; opencode now does the
    same so the api-only seats routed through it (deepseek/kimi/qwen/glm via `oc:`)
    are AGENTIC too — they can open any project file, not just the diff in the prompt.

    Two gates, both required:
      * it must be a git work tree (a non-repo `cwd` — e.g. `--just-ask` from /tmp —
        has nothing to read, so we fall back rather than roam a scratch dir);
      * it must NOT ship its own opencode project config (.opencode/ or opencode.json
        /jsonc), which could OVERRIDE our read-only agent and re-enable write/bash —
        a real privilege-escalation path on an untrusted repo. Such a repo falls back
        to the isolated temp-dir posture (read-only is enforced by the global agent in
        a clean dir, where no project config can touch it)."""
    if not cwd.is_dir():
        return False
    # This is reached by `--show-board` (a meta flag that must work anywhere) and the
    # opencode-seat scope label, so a missing git binary (OSError -> FileNotFoundError) or a
    # wedged `git rev-parse` (TimeoutExpired) must degrade to "not a repo" (False), never a
    # raw traceback — same defensive catch as cli._is_git_repo.
    try:
        proc = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not (proc.returncode == 0 and proc.stdout.strip() == "true"):
        return False
    if _opencode_has_project_config(cwd):
        print(
            f"[review-cli] opencode: {cwd} ships its own opencode config "
            "(.opencode/ or opencode.json) which could override the read-only agent; "
            "running diff-only in an isolated dir for safety.",
            file=sys.stderr, flush=True,
        )
        return False
    return True


def review_opencode(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
    oc_model = model.split(":", 1)[1] if ":" in model else model
    _ensure_opencode_readonly_agent(cwd, oc_model)

    if _opencode_runs_in_repo(cwd):
        command = f"opencode run --agent read-only-reviewer --dir {cwd} -m {oc_model} <prompt-with-diff>"
        # AGENTIC, read-only: run opencode in the REAL repo (like review_codex's
        # `codex exec -s read-only -C <cwd>`), so it can READ any project file — not
        # just the diff embedded in the prompt. SAFETY is enforced by the
        # read-only-reviewer agent: it DENIES bash/edit/write/webfetch/etc, so opencode
        # may open files but can NEVER mutate the user's repo, run a command, or hit the
        # network. The diff is still handed in the message as the review FOCUS; the repo
        # is the surrounding context the model can pull in on demand.
        message = (
            f"{prompt}\n\nYou are reviewing a real git repository in READ-ONLY mode: "
            "read any project file you need with your read tools, but do not edit, "
            "write, or run commands. "
            + (
                f"Focus on this diff:\n\n```diff\n{diff}\n```"
                if diff.strip()
                else "Answer based on the repository and the prompt."
            )
        )
        argv = [
            _which("opencode"),
            "run",
            "--agent",
            "read-only-reviewer",
            "--dir",
            str(cwd),
            "-m",
            oc_model,
            message,
        ]
        proc = _run_streamed(argv, cwd=cwd, timeout=timeout, backend="opencode", round_no=round_no,
                             announce=_ANNOUNCE_LOGS, header_argv0=f"opencode -m {oc_model}")
        return ReviewResult(model=model, command=command, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    # FALLBACK: cwd is not a git repo (e.g. a panel `--just-ask` run from a scratch
    # dir) — there is nothing to read, so keep the old isolated empty-temp-dir posture
    # and review the diff/prompt alone.
    command = f"opencode run --agent read-only-reviewer -m {oc_model} <prompt-with-diff>"
    with tempfile.TemporaryDirectory(prefix="review-cli-opencode-") as tmp_raw:
        tmp = Path(tmp_raw)
        _run(["git", "init", "-q"], cwd=tmp, timeout=30)
        if diff.strip():
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
        proc = _run_streamed(argv, cwd=tmp, timeout=timeout, backend="opencode", round_no=round_no,
                             announce=_ANNOUNCE_LOGS, header_argv0=f"opencode -m {oc_model}")
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


def review_gemini(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
    gemini_model = model.split(":", 1)[1] if ":" in model else os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    command = f"Gemini API {gemini_model}"
    # Gemini is a REST backend — it never goes through `_run_streamed`, so it must emit
    # its own per-call sidecar log or the dashboard parser (which reads ONLY `.log`
    # files) would not see it at all: models undercounted, Gemini-only runs invisible
    # (HYP-742 finding 2). The sidecar carries the explicit EXIT status (finding 4) and
    # is stamped with the call's START time (captured now), so a slow call's duration
    # and session clustering stay correct (codex P2).
    #
    # KEY RESOLUTION IS INSIDE the try: a missing GEMINI_API_KEY is the COMMON failure
    # (gemini is in DEFAULT_MODELS), and `_gemini_key()` raising before the logged path
    # would leave that auth failure invisible — `run_panel` would turn it into an
    # internal 127 with no `.log` (codex P2). Now the auth failure emits a sidecar too.
    started = datetime.now(timezone.utc)
    try:
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
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        usage = payload.get("usageMetadata", {})
        stdout = text.strip() + f"\n\nprompt_tokens={usage.get('promptTokenCount', 0)} output_tokens={usage.get('candidatesTokenCount', 0)}\n"
        _emit_rest_log("gemini", command, round_no=round_no, returncode=0, stdout=stdout, stderr="", started=started)
        return ReviewResult(model=model, command=command, returncode=0, stdout=stdout, stderr="")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        rc = exc.code or 1
        _emit_rest_log("gemini", command, round_no=round_no, returncode=rc, stdout="", stderr=body_text, started=started)
        return ReviewResult(model=model, command=command, returncode=rc, stdout="", stderr=body_text)
    except Exception as exc:  # noqa: BLE001
        # Anything other than an HTTPError: a missing API key (RuntimeError from
        # `_gemini_key()`), a network/DNS/socket-timeout error (URLError), or a malformed
        # JSON response (ValueError). Without this the call would raise out of run_panel
        # as an "internal" 127 with NO `.log`, so a failed Gemini run would stay invisible
        # to the dashboard (codex P2). Emit a failure sidecar and return a normal non-zero
        # ReviewResult instead of raising.
        err = f"{type(exc).__name__}: {exc}"
        # A urlopen timeout (socket timeout, or a URLError wrapping one) must be recorded
        # as a TIMEOUT, not a generic error, so the dashboard's timeout metric stays
        # consistent with the subprocess backends (codex P2). rc 124 = the timeout code.
        if _is_timeout_error(exc):
            _emit_rest_log(
                "gemini", command, round_no=round_no, returncode=124, stdout="", stderr=err,
                started=started, timed_out=True, timeout_secs=timeout,
            )
            return ReviewResult(model=model, command=command, returncode=124, stdout="", stderr=err)
        _emit_rest_log("gemini", command, round_no=round_no, returncode=1, stdout="", stderr=err, started=started)
        return ReviewResult(model=model, command=command, returncode=1, stdout="", stderr=err)


def _is_timeout_error(exc: BaseException) -> bool:
    """True if ``exc`` is (or wraps) a socket/network timeout.

    `urlopen(..., timeout=N)` surfaces a timeout as `socket.timeout` (== `TimeoutError`
    on 3.10+) directly, or as a `urllib.error.URLError` whose `.reason` is that timeout."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError))


def _emit_rest_log(
    backend: str, argv0: str, *, round_no: int, returncode: int, stdout: str, stderr: str,
    started: datetime | None = None, timed_out: bool = False, timeout_secs: int | None = None,
) -> None:
    """Best-effort sidecar log for a NON-subprocess (REST) backend run.

    Mirrors what `_run_streamed` writes for subprocess backends so the dashboard parser
    counts the run. ``backend`` is the canonical backend name (gemini, z.ai, commandcode)
    — it becomes the `{stamp}-{backend}-r{n}.log` filename segment the parser reads, so
    EACH REST backend is attributed to itself in the dashboard, not lumped under a single
    hardcoded name (HYP-742: z.ai/commandcode runs were invisible because this hardcoded
    "gemini"). Logging must never take down a review: a read-only log dir or write error
    is swallowed (the backend already produced its result). ``started`` is the call's
    START time so the dashboard reports an honest duration (codex P2); ``timed_out``
    records a TIMEOUT marker so a REST timeout counts as a timeout, not a generic error."""
    try:
        write_sidecar_log(
            backend, round_no=round_no, argv0=argv0, returncode=returncode, stdout=stdout,
            stderr=stderr, started=started, timed_out=timed_out, timeout_secs=timeout_secs,
        )
    except Exception:  # noqa: BLE001 - logging is best-effort; it must NEVER change the
        # review outcome. Beyond OSError (read-only / full log dir), a provider can return
        # text with an unpaired surrogate that makes write_sidecar_log raise
        # UnicodeEncodeError (a ValueError) — and since this is called on the SUCCESS path
        # of the REST backends, an unswallowed error would flip a successful ReviewResult
        # into a failure (codex P3). Swallow everything; a missing sidecar only loses a log.
        pass



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
    prompt: str, diff: str, timeout: int, backend: str, round_no: int = 0,
    extra_body: dict | None = None,
) -> ReviewResult:
    """POST an OpenAI-compatible chat/completions request and return a ReviewResult.

    `model` is the REQUESTED backend string (e.g. `zai`, `commandcode:deepseek/deepseek-v4-flash`)
    and is preserved in ReviewResult.model — mode_review keys results by the requested
    string, so substituting the resolved provider id here would KeyError. `api_model`
    is the resolved provider model id sent on the wire (e.g. glm-4.6, deepseek/deepseek-v4-flash).

    `backend` is the canonical sidecar name (e.g. `z.ai`, `commandcode`) used for the
    dashboard `.log` file, and `round_no` is threaded from the panel. Like review_gemini,
    these REST backends never go through `_run_streamed`, so they MUST emit their own
    sidecar log on EVERY return path or the dashboard parser (which reads only `.log`
    files) never sees the run — z.ai/commandcode runs were invisible and models were
    undercounted (HYP-742). The sidecar is stamped with the call START time so a slow
    call's duration and session clustering stay correct (codex P2); a socket timeout is
    recorded as a TIMEOUT, not a generic error, keeping the timeout metric consistent.

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
    started = datetime.now(timezone.utc)
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
            stderr = f"{label} API returned no assistant content: {raw[:500]}"
            _emit_rest_log(backend, command, round_no=round_no, returncode=1, stdout="", stderr=stderr, started=started)
            return ReviewResult(model=model, command=command, returncode=1, stdout="", stderr=stderr)
        prompt_tokens, output_tokens = _parse_openai_usage(payload)
        stdout = text.strip() + (
            f"\n\nprompt_tokens={prompt_tokens} output_tokens={output_tokens}\n"
        )
        _emit_rest_log(backend, command, round_no=round_no, returncode=0, stdout=stdout, stderr="", started=started)
        return ReviewResult(model=model, command=command, returncode=0, stdout=stdout, stderr="")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        rc = exc.code or 1
        _emit_rest_log(backend, command, round_no=round_no, returncode=rc, stdout="", stderr=body_text, started=started)
        return ReviewResult(model=model, command=command, returncode=rc, stdout="", stderr=body_text)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Connection refused / DNS failure / socket timeout — no HTTP status. URLError
        # and TimeoutError are both OSError subclasses; the wide catch normalises any
        # transport-level failure to a dead-backend result instead of crashing. A socket
        # timeout is logged as a TIMEOUT (rc 124) to match the subprocess + gemini metric.
        err = f"{label} API request failed: {exc}"
        if _is_timeout_error(exc):
            _emit_rest_log(
                backend, command, round_no=round_no, returncode=124, stdout="", stderr=err,
                started=started, timed_out=True, timeout_secs=timeout,
            )
            return ReviewResult(model=model, command=command, returncode=124, stdout="", stderr=err)
        _emit_rest_log(backend, command, round_no=round_no, returncode=1, stdout="", stderr=err, started=started)
        return ReviewResult(model=model, command=command, returncode=1, stdout="", stderr=err)
    except (json.JSONDecodeError, ValueError) as exc:
        # 2xx with a non-JSON / truncated body — the provider returned garbage. Treat
        # it as a failed call rather than letting the decode error escape.
        err = f"{label} API returned a malformed response: {exc}"
        _emit_rest_log(backend, command, round_no=round_no, returncode=1, stdout="", stderr=err, started=started)
        return ReviewResult(model=model, command=command, returncode=1, stdout="", stderr=err)


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


def review_zai(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
    # api-only: a forced REVIEW_ZAI_MODE=cli is a config error, surfaced as a
    # dead-backend result instead of silently running the api path.
    # `round_no` is accepted (and forwarded) so the panel's uniform 6-arg dispatch
    # (panel.py: backend(model, prompt, diff, cwd, timeout, round_no)) does not raise
    # for these post-HYP-741 REST backends and the dashboard attributes the run to the
    # right brainstorm round.
    # A forced-mode config error or a missing key both produce a NON-zero result AND a
    # sidecar log — like review_gemini, these are real (failed) run attempts and must be
    # visible in the dashboard, never raise out of run_panel as an invisible internal 127.
    try:
        resolve_backend_mode("zai", ZAI_SUPPORTED_MODES, "api")
        key = _zai_key()
    except RuntimeError as exc:
        _emit_rest_log("z.ai", "z.ai", round_no=round_no, returncode=1, stdout="", stderr=str(exc))
        return ReviewResult(model=model, command="z.ai", returncode=1, stdout="", stderr=str(exc))
    zai_model = model.split(":", 1)[1] if ":" in model else os.environ.get("ZAI_MODEL", ZAI_DEFAULT_MODEL)
    base_url = os.environ.get("ZAI_BASE_URL", ZAI_DEFAULT_BASE_URL)
    return _openai_compatible_request(
        model=model, api_model=zai_model, label="z.ai", base_url=base_url, key=key,
        prompt=prompt, diff=diff, timeout=timeout, backend="z.ai", round_no=round_no,
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


def review_commandcode(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
    # API-only: a forced REVIEW_COMMANDCODE_MODE=cli is a config error (there is no
    # commandcode CLI), surfaced as a dead-backend result, never a silent api POST.
    # `round_no` is accepted so the panel's uniform 6-arg dispatch does not raise for
    # this post-HYP-741 REST backend (see review_zai for the rationale).
    # A forced-mode config error or a missing key both produce a NON-zero result AND a
    # sidecar log (see review_zai) — a failed run must be visible in the dashboard, never
    # an invisible internal 127 raised out of run_panel.
    try:
        resolve_backend_mode("commandcode", COMMANDCODE_SUPPORTED_MODES, "api")
        key = _commandcode_key()
    except RuntimeError as exc:
        _emit_rest_log("commandcode", "commandcode", round_no=round_no, returncode=1, stdout="", stderr=str(exc))
        return ReviewResult(model=model, command="commandcode", returncode=1, stdout="", stderr=str(exc))
    has_suffix = ":" in model
    env_model = os.environ.get("COMMANDCODE_MODEL")
    cc_model = model.split(":", 1)[1] if has_suffix else (env_model or COMMANDCODE_DEFAULT_MODEL)
    base_url = os.environ.get("COMMANDCODE_BASE_URL") or COMMANDCODE_DEFAULT_BASE_URL
    return _openai_compatible_request(
        model=model, api_model=cc_model, label="commandcode", base_url=base_url, key=key,
        prompt=prompt, diff=diff, timeout=timeout, backend="commandcode", round_no=round_no,
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


def review_claude_api(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
    """Anthropic Messages API backend — works WITHOUT the claude CLI (needs only a
    key). POSTs to ``{ANTHROPIC_BASE_URL}/v1/messages``; the default base is
    Anthropic, but any Anthropic-compatible gateway works (e.g. CommandCode via
    ANTHROPIC_BASE_URL). ``cwd`` is unused — the API has no workspace, which is
    exactly why this variant runs where the CLI cannot.

    Like the other REST backends, this never goes through `_run_streamed`, so it emits
    its own dashboard sidecar log (under the canonical ``claude`` backend name, same as
    the CLI variant) on EVERY return path with ``round_no`` threaded — else a claude
    API-mode run would be invisible to the dashboard and missing from stats / brainstorm
    round attribution (codex P2)."""
    claude_model = model.split(":", 1)[1] if ":" in model else os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
    cfg = _anthropic_api_config()
    command = f"Anthropic API {claude_model}"
    started = datetime.now(timezone.utc)
    if cfg is None:
        stderr = "claude API mode: no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN configured"
        _emit_rest_log("claude", command, round_no=round_no, returncode=1, stdout="", stderr=stderr, started=started)
        return ReviewResult(model=model, command=command, returncode=1, stdout="", stderr=stderr)
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
        rc = 0 if text.strip() else 1
        _emit_rest_log("claude", command, round_no=round_no, returncode=rc, stdout=stdout, stderr="", started=started)
        return ReviewResult(model=model, command=command, returncode=rc, stdout=stdout, stderr="")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        rc = exc.code or 1
        _emit_rest_log("claude", command, round_no=round_no, returncode=rc, stdout="", stderr=body_text, started=started)
        return ReviewResult(model=model, command=command, returncode=rc, stdout="", stderr=body_text)
    except urllib.error.URLError as exc:
        err = str(exc)
        if _is_timeout_error(exc):
            _emit_rest_log("claude", command, round_no=round_no, returncode=124, stdout="", stderr=err,
                           started=started, timed_out=True, timeout_secs=timeout)
            return ReviewResult(model=model, command=command, returncode=124, stdout="", stderr=err)
        _emit_rest_log("claude", command, round_no=round_no, returncode=1, stdout="", stderr=err, started=started)
        return ReviewResult(model=model, command=command, returncode=1, stdout="", stderr=err)
    except (ValueError, OSError) as exc:
        # malformed / non-JSON 2xx body, or a read/decode/timeout failure — surface
        # as a normal backend result, not an uncaught exception. (URLError, a
        # subclass of OSError, is handled above; this catches the rest.)
        if _is_timeout_error(exc):
            err = str(exc)
            _emit_rest_log("claude", command, round_no=round_no, returncode=124, stdout="", stderr=err,
                           started=started, timed_out=True, timeout_secs=timeout)
            return ReviewResult(model=model, command=command, returncode=124, stdout="", stderr=err)
        err = f"claude API: malformed or unreadable response: {exc}"
        _emit_rest_log("claude", command, round_no=round_no, returncode=1, stdout="", stderr=err, started=started)
        return ReviewResult(model=model, command=command, returncode=1, stdout="", stderr=err)


def _have_claude_cli() -> bool:
    try:
        _which("claude-p")
        return True
    except RuntimeError:
        return False


def review_claude(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
    """Dispatch the claude/opus backend between the API and CLI variants.

    REVIEW_CLAUDE_MODE forces it: ``api`` (HTTP, no claude binary needed) or
    ``cli`` (claude-p). With no override the choice is automatic and conservative:
    prefer the CLI when the claude binary is present (subscription, no API cost,
    and reliable now that workspace trust is deterministic), and fall back to the
    API only when there is no claude binary but a key IS configured — i.e. don't
    silently switch a working CLI host to the paid API just because a key happens
    to be in the environment. Set REVIEW_CLAUDE_MODE=api to force the API.

    ``round_no`` is threaded from the panel into BOTH variants so the per-call sidecar
    log lands in the right brainstorm round (the dashboard parser keys on it) — the CLI
    variant via _run_streamed, the API variant via its own _emit_rest_log sidecar."""
    mode = os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
    if mode == "api":
        return review_claude_api(model, prompt, diff, cwd, timeout, round_no)
    if mode != "cli" and not _have_claude_cli() and _anthropic_api_config() is not None:
        return review_claude_api(model, prompt, diff, cwd, timeout, round_no)
    return review_claude_cli(model, prompt, diff, cwd, timeout, round_no)




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


def review_claude_cli(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
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
        timeout=timeout + 30, backend="claude", round_no=round_no, announce=_ANNOUNCE_LOGS,
    )
    command = "claude-p --permission-mode dontAsk --tools '' --strict-mcp-config --disable-slash-commands --safe-mode --append-system-prompt <read-only-review> --disallowedTools Edit MultiEdit Write Bash Read Grep Glob NotebookEdit SlashCommand Task TodoWrite ExitPlanMode WebFetch WebSearch -p  (prompt via stdin)"
    return ReviewResult(model=model, command=command, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


# --- Fake backend (TEST-ONLY, env-gated) -------------------------------------------
# `REVIEW_FAKE_BACKEND=1` replaces EVERY real backend with a deterministic in-process
# responder — NO network, NO CLI subprocess, NO API key. It exists so the e2e tests
# (tests/test_e2e_resume.py) can spawn the REAL `review` CLI as a subprocess, kill it
# mid-run, and resume it, exercising the genuine cli.py -> modes/brainstorm -> panel
# path while ONLY the leaf model call is faked. OFF by default: when the env var is unset
# `resolve_backend` returns the real backends unchanged, so production behaviour is
# untouched (the e2e/round-trip unit tests prove this — they don't set the var).
#
# It is wired at `resolve_backend` (the single chokepoint every run_panel / run_moderator
# call funnels through), so the fake is the ONLY fake in the whole stack — argument
# parsing, mode dispatch, the round loop, the discussion-log writer, the parser, and the
# resume seeding all run for real.
def _fake_backend_enabled() -> bool:
    # Normalize case so `False`/`FALSE`/`No` are treated as OFF (not just lowercase forms) —
    # otherwise a case-variant false value would silently route every backend to the fake.
    return os.environ.get("REVIEW_FAKE_BACKEND", "").strip().lower() not in ("", "0", "false", "no")


def review_fake(model: str, prompt: str, diff: str, cwd: Path, timeout: int, round_no: int = 0) -> ReviewResult:
    """Deterministic, network-free stand-in for a real backend (TEST-ONLY).

    Distinguishes the brainstorm prompt shapes by their fixed lead-in text (imported from the
    prompt SOURCE — `modes.brainstorm` — so a reword can never silently misroute the fake):
      * the final SYNTHESIS prompt (SYNTHESIS_PROMPT_MARKER) -> a synthesis body;
      * a MODERATOR round prompt (MODERATOR_PROMPT_LEADIN) -> a summary ending in
        `DECISION: CONTINUE` (so the loop runs to its max cap, then synthesizes — a
        deterministic, always-terminating run with a guaranteed final synthesis);
      * anything else (a persona prompt) -> a short per-round idea.
    The text embeds `model`/`round_no` so each round's content is distinct and a resumed
    run's new rounds are visibly different from the pre-kill ones. `REVIEW_FAKE_DELAY`
    (float seconds, default 0) sleeps per call so an e2e can make a run slow enough to
    reliably SIGTERM it mid-round; the delay still honours `timeout` as an upper bound."""
    import time

    # Lazy import (not at module top) so this lower backends layer never imports the higher
    # modes layer at import time — only when the env-gated fake is actually exercised.
    from .modes.brainstorm import MODERATOR_PROMPT_LEADIN, SYNTHESIS_PROMPT_MARKER

    delay = 0.0
    try:
        delay = float(os.environ.get("REVIEW_FAKE_DELAY", "0") or "0")
    except ValueError:
        delay = 0.0
    if delay > 0:
        time.sleep(min(delay, max(timeout - 0.1, 0.0)))

    if SYNTHESIS_PROMPT_MARKER in prompt:
        body = (
            f"FINAL SYNTHESIS (fake/{model}). BEST IDEAS (ranked): 1) idea-A 2) idea-B. "
            "TRADEOFFS: A is simpler, B scales better. RECOMMENDATION: ship idea-A first."
        )
    elif MODERATOR_PROMPT_LEADIN in prompt:
        body = (
            f"Moderator summary (fake/{model}, round {round_no}): the panel converged on a "
            "couple of concrete directions.\nDECISION: CONTINUE"
        )
    else:
        body = (
            f"Idea from {model} in round {round_no}: a concrete, deterministic suggestion "
            f"(#{round_no}) the fake backend emits without any network or CLI."
        )
    # Mirror a real REST backend's sidecar live-log so `tail -f` / the dashboard parser behave
    # the same under the fake. A log-dir hiccup must not break the call, but it shouldn't fail
    # SILENTLY either — narrow to OSError (the only thing write_sidecar_log raises on disk) and
    # note it on stderr so a broken log dir is diagnosable.
    try:
        write_sidecar_log(backend="fake", round_no=round_no, argv0=f"fake:{model}",
                          returncode=0, stdout=body, stderr="")
    except OSError as exc:
        print(f"[review-cli] fake sidecar log skipped: {exc}", file=sys.stderr, flush=True)
    return ReviewResult(model=model, command=f"fake:{model}", returncode=0, stdout=body, stderr="")


def _match_named_backend(lowered: str) -> Callable[..., ReviewResult] | None:
    """Match a model string to an EXPLICITLY-recognized backend, or None when nothing
    matches. `lowered` is the already-lowercased model id.

    This is the set of providers the CLI knows by name — every branch here is a deliberate,
    documented route. `resolve_backend` adds a catch-all opencode fallthrough on top (so an
    unknown id still runs *something*). Splitting the match out keeps one source of truth for
    the named routes and keeps `resolve_backend` a one-liner over it."""
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
    return None


# Providers known to be DEAD — a default model id must never route through one. Right now
# that is just `fireworks`: the old `oc:fireworks/.../kimi-k2p6-turbo` default ran on the
# suspended `glide` account (review-cli#25). This is a DENYLIST, not an allowlist, on
# purpose: the #25 guard pairs it with a real-routing check (`_match_named_backend` must
# recognize the id — see `default_routes_live`), so the two halves are "the id actually
# routes to a named backend" AND "its underlying provider is not a known-dead one". A
# denylist can't go stale the way a hand-maintained live-allowlist would (every NEW live
# provider would have to be remembered there); when another provider dies, add it here.
_DEAD_PROVIDERS = frozenset({"fireworks"})


def effective_provider(model: str) -> str:
    """The EFFECTIVE underlying provider of a model id, peeling the `oc:`/`opencode:`
    AGENTIC-transport prefix so the real provider — not "opencode" — is what's checked.

    `oc:commandcode/moonshotai/Kimi-K2.7-Code` -> `commandcode`; `oc:fireworks/x` ->
    `fireworks`; `zai:glm-5.2` -> `zai`; a bare `codex` -> `codex`. Lower-cased. The #25
    guard needs this because an `oc:` seat is just a transport: the dead-provider check must
    look THROUGH the `oc:` prefix at the provider underneath — otherwise a dead
    `oc:fireworks/...` default reads as the opencode transport and the rot slips past."""
    lowered = model.lower()
    for prefix in ("oc:", "opencode:"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break
    # The provider is the first segment before either a `:` (keyed-HTTP `provider:model`)
    # or a `/` (opencode `provider/model`), whichever comes first.
    for sep in (":", "/"):
        if sep in lowered:
            lowered = lowered.split(sep, 1)[0]
    return lowered


def default_routes_live(model: str) -> bool:
    """True iff `model` is safe to ship as a DEFAULT (flat panel or board seat): the FULL id
    takes a real named route AND its effective provider is not known-dead.

    This is the #25 anti-rot guard's single check. It validates the WHOLE id against the
    SAME matcher the dispatcher uses, not just the collapsed provider token (codex review):
      * `_match_named_backend(model)` must be non-None — the full id resolves to a named
        backend, NOT `resolve_backend`'s opencode catch-all. This is the real test: it
        mirrors what `resolve_backend` does (minus the fallthrough), so a default whose full
        id only the catch-all would accept fails. This is why checking the COLLAPSED provider
        is not enough: `gemini-api:gemini-2.5-flash` collapses to the named token
        `gemini-api`, but `resolve_backend` matches gemini only as the bare `gemini-api` or a
        `gemini:` prefix — the `gemini-api:` form falls through to opencode, so the full-id
        check is what catches it (the silent-rot #25 is about).
      * For an `oc:`/`opencode:` AGENTIC id the matcher returns `review_opencode` for ANY
        under-provider, so the full-id check alone can't see a dead/typo'd provider beneath
        the transport. So the provider UNDER the transport (`effective_provider`) must ALSO
        name a backend (rejects `oc:comandcode/...`) — checked only when a transport prefix
        is present, since for a flat id the full-id check already covers it.
      * `effective_provider(model)` must not be in `_DEAD_PROVIDERS` — the forward-looking
        half: when a CURRENTLY named provider (e.g. `commandcode`) is suspended, add it here
        and its defaults trip immediately, without also editing `_match_named_backend`.
    It checks real named routing + a curated dead-provider denylist, NOT live network
    reachability — a probe would need keys and can't run in CI.

    CONTRACT + scope: enforced ONLY by the CI guard test
    (`test_every_default_model_routes_live`), not at runtime — `resolve_backend` still sends
    an unknown id to opencode (back-compat). So the guard is the single line of defense, and
    it constrains DEFAULTS to ids `_match_named_backend` resolves to a named backend: an
    opencode-only provider valid at runtime but not a named branch gets a (false) `False`
    here. That is intentional — every shipped default routes through a named provider today,
    and adding one on a new provider means giving it a named branch."""
    # Lowercase ONCE and use it for every check, so the guard mirrors `resolve_backend`
    # (which dispatches on `model.lower()`) exactly — a mixed-case id can't pass the guard
    # while routing differently at runtime (codex review of #49).
    lowered = model.lower()
    if _match_named_backend(lowered) is None:
        return False
    provider = effective_provider(lowered)
    if lowered.startswith(("oc:", "opencode:")):
        # Agentic transport: the full id matched only the opencode branch, so verify the
        # provider UNDER the transport is itself a named backend (not a dead/typo'd one).
        if _match_named_backend(provider) is None:
            return False
    return provider not in _DEAD_PROVIDERS


def resolve_backend(model: str) -> Callable[..., ReviewResult]:
    # TEST-ONLY: with REVIEW_FAKE_BACKEND set, every model routes to the deterministic
    # in-process fake (no network/CLI). Unset (the default) -> real backends, unchanged.
    if _fake_backend_enabled():
        return review_fake
    # Catch-all: an UNRECOGNIZED id still runs via opencode (back-compat with arbitrary
    # `provider/model` selectors). The #25 default-model guard (`default_routes_live`)
    # instead checks each default's under-transport provider is a named backend and not in
    # the dead-provider denylist, so a stale default can't ride this fallthrough silently.
    return _match_named_backend(model.lower()) or review_opencode


def backend_available(model: str) -> bool:
    """Cheap availability probe so moderator selection never picks a dead backend."""
    # TEST-ONLY: under the fake backend EVERY model is reachable (it is faked in-process,
    # no key/CLI needed). Without this the panel/moderator selection would prune the faked
    # models as "unavailable" on a host lacking the real CLIs (e.g. CI), defeating the e2e.
    if _fake_backend_enabled():
        return True
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
