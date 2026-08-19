"""Model backends: codex / gemini / claude / opencode, and backend resolution.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change). Each backend builds a TEXT payload and
returns a ReviewResult. `_ANNOUNCE_LOGS` is a module-global toggled on by the
panel modes so streamed calls print their live-log path to stderr.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .process import (
    _run,
    _run_streamed,
    _safe_log_header,
    git_repo_env,
    strip_control_sequences,
    write_sidecar_log,
)
from .seat_cooldown import active_cooldown, clear_cooldown, record_cooldown

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
    path = _which_optional(name)
    if not path:
        raise RuntimeError(f"{name} not found on PATH")
    return path


def _which_optional(name: str) -> str | None:
    """`shutil.which`, but as a backends-local indirection. The claude CLI resolution
    (review-cli#76) needs the present-or-absent form (None on miss, not a raise); routing
    it through this module symbol lets tests patch `backends._which_optional` to simulate
    a host's CLI inventory WITHOUT monkeypatching the stdlib `shutil.which` globally."""
    return shutil.which(name)


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
            file=sys.stderr,
            flush=True,
        )
    return out


# --- diff size guard (dispatch-time only) ------------------------------------------
# An uncapped diff is the single biggest token-burn driver the 2026-08 investigation
# found: a real 6.5MB / 583-file diff (a debug harness's screenshot/video capture
# scripts touching hundreds of files) was sent WHOLE to every seat in the board. git
# already collapses each binary file to a one-line "Binary files ... differ" stub, so
# the actual cost is oversized TEXT — a huge generated/vendored file, or (as in that
# case) simply a very large number of changed files.
#
# THIS MUST NEVER TOUCH THE CANONICAL DIFF used for the staged commit-stamp / the
# `--commit` checkpoint's integrity check (reviewlib.modes.review._current_staged_diff
# independently re-derives an UNCAPPED `git diff --cached` and compares it byte-for-
# byte — capping the diff at the SOURCE, in `cli._git_diff`, broke that comparison for
# any staged diff over the cap: codex review finding on this feature's own PR). So the
# cap is applied HERE, only to the copy of the diff handed to backend dispatch — never
# to the `diff` variable a caller threads into stamping/checkpointing.
DIFF_MAX_BYTES_DEFAULT = 300_000
_DIFF_TRUNCATED_NOTE = (
    "\n\n[review-cli] diff truncated at {cap} bytes (the full diff was {total} bytes "
    "across {files} changed files) — the payload is capped so a huge diff isn't sent in "
    "full to every board seat. Scope the review (e.g. `git diff -- <path>`) or raise the "
    "cap with $REVIEW_DIFF_MAX_BYTES to review the full change.\n"
)
_DIFF_GIT_FILE_RE = re.compile(r"^diff --git ", re.MULTILINE)


def _diff_max_bytes() -> int:
    """The configured diff size cap, read at CALL time so an env override applies. A
    missing/blank/non-integer value falls back to the default; <= 0 disables the cap
    entirely (the pre-fix, uncapped behaviour)."""
    raw = os.environ.get("REVIEW_DIFF_MAX_BYTES")
    if raw is None or not raw.strip():
        return DIFF_MAX_BYTES_DEFAULT
    try:
        return int(raw)
    except ValueError:
        return DIFF_MAX_BYTES_DEFAULT


def cap_diff_for_dispatch(diff: str) -> str:
    """Truncate an oversized diff to the configured byte cap, appending a visible
    marker naming the real total so the truncation is honest, not silent. A no-op when
    the diff is already within the cap or the cap is disabled.

    The marker itself is reserved OUT OF `cap`, not appended on top of it (codex review
    finding: the first version of this function truncated the diff TO `cap` bytes and
    then appended the marker after, so the actual dispatched payload always exceeded
    `cap` by the marker's length — for the ~300-byte default marker against the
    300,000-byte default cap that's a ~0.1% overshoot, but a small custom cap (e.g. a
    test, or an operator scoping tightly) could end up with a payload many times the
    requested size). Only when `cap` is smaller than the marker itself does the
    truncated diff text drop to empty — the marker still names the real total even
    then, so the truncation is never silent, but the guarantee "result <= cap" cannot
    hold below that floor.

    Callers: apply this to a LOCAL copy used only for backend dispatch (mode_review's
    flat `-m` path and board path) — never to the diff threaded into the staged-commit
    stamp or the `--commit` checkpoint's integrity check (see the module note above)."""
    cap = _diff_max_bytes()
    encoded = diff.encode("utf-8")
    if cap <= 0 or len(encoded) <= cap:
        return diff
    files = len(_DIFF_GIT_FILE_RE.findall(diff))
    marker = _DIFF_TRUNCATED_NOTE.format(cap=cap, total=len(encoded), files=files)
    budget = max(0, cap - len(marker.encode("utf-8")))
    truncated = encoded[:budget].decode("utf-8", errors="ignore")
    return truncated + marker


def _claude_api_model(model: str) -> str:
    return (
        model.split(":", 1)[1]
        if ":" in model
        else os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
    )


def _claude_api_command(model: str) -> str:
    return f"Anthropic API {_claude_api_model(model)}"


def _prompt_with_effort(prompt: str, effort: str | None) -> str:
    if not effort:
        return prompt
    effort_label = "highest" if effort in {"xhigh", "max"} else effort
    hint = f"Use {effort_label} reasoning effort."
    if hint in prompt:
        return prompt
    return f"{prompt}\n\n{hint}"


def _codex_reasoning_effort(effort: str | None) -> str | None:
    if not effort:
        return None
    if effort == "minimal":
        return "low"
    if effort == "max":
        return "xhigh"
    return effort


def _claude_reasoning_effort(effort: str | None) -> str | None:
    if not effort:
        return None
    if effort == "minimal":
        return "low"
    return effort


def _opencode_variant(effort: str | None) -> str | None:
    """The opencode `--variant` value for a review effort level, or None.

    opencode's `--variant` is "provider-specific reasoning effort" (its help lists
    `high`, `max`, `minimal`) forwarded to whatever provider the seat routes to. review's
    effort vocabulary is a strict superset of those examples, so the level passes straight
    through; opencode + the provider decide how to honour it."""
    if not effort:
        return None
    return effort


def call_backend(
    backend: Callable[..., ReviewResult],
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    *,
    effort: str | None = None,
) -> ReviewResult:
    if effort is None:
        return backend(model, prompt, diff, cwd, timeout, round_no)
    try:
        sig = inspect.signature(backend)
    except (TypeError, ValueError):
        accepts_effort = False
    else:
        effort_param = sig.parameters.get("effort")
        accepts_effort = (
            effort_param is not None
            and effort_param.kind != inspect.Parameter.POSITIONAL_ONLY
        ) or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    prompt = _prompt_with_effort(prompt, effort)
    if accepts_effort:
        return backend(model, prompt, diff, cwd, timeout, round_no, effort=effort)
    return backend(model, prompt, diff, cwd, timeout, round_no)


def review_with_images(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    images: tuple[Path, ...] = (),
    effort: str | None = None,
) -> ReviewResult:
    """Run a backend with raw image attachments when that backend safely supports them.

    Unsupported text backends still receive the grounded visual note in the prompt; the
    raw image path is an additive transport for capable backends, not a new hard
    dependency for every panel seat.
    """
    backend = resolve_backend(model)
    if images and backend is review_claude:
        unpaid = unpaid_provider_result(
            model,
            backend="claude",
            command=_claude_api_command(model),
            round_no=round_no,
            provider=_claude_gateway_provider_from_env(),
        )
        if unpaid is not None:
            return unpaid
        # Mirrors review_claude()'s own cooldown check/record (this branch bypasses
        # review_claude entirely to reach the images-capable CLI transport, so it must
        # opt back into the SAME contract explicitly — kimi review finding: --visual
        # was the one dispatch path that never consulted or recorded seat_cooldown).
        cooldown = active_cooldown(model)
        if cooldown is not None:
            return _cooldown_skip_result(model, round_no, cooldown)
        result = review_claude_cli_with_images(
            model, prompt, diff, cwd, timeout, round_no, images, effort=effort
        )
        reason = _chronic_unavailable_reason(result)
        if reason is not None:
            record_cooldown(model, reason)
        elif result.returncode == 0 and result.stdout.strip():
            # review-cli#221: a genuine success clears any escalated cooldown history
            # (see clear_cooldown's docstring) — `reason is None` alone isn't enough
            # here, since it also covers a non-chronic FAILURE (returncode != 0) that
            # must NOT be treated as recovery evidence. Round-4 review finding (k3):
            # `returncode == 0` alone isn't "genuine success" either — panel.py's own
            # `result_is_usable` treats rc=0 with an EMPTY body as a failure shape too
            # ("a silently-disabled model often returns rc=0 with nothing"). Without
            # this check, a seat oscillating between chronic-quota failures (escalates)
            # and empty-rc0 responses (clears) would never actually escalate — pinned
            # at the 10-minute window forever, the exact chronically-broken shape this
            # feature exists to push out of the pool.
            clear_cooldown(model)
        return result
    return call_backend(
        backend, model, prompt, diff, cwd, timeout, round_no, effort=effort
    )


def review_codex(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
    codex_model = model.split(":", 1)[1] if ":" in model else None
    unpaid = unpaid_provider_result(
        model, backend="codex", command="codex", round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    argv = [_which("codex"), "exec", "-s", "read-only", "-C", str(cwd), "--ephemeral"]
    if codex_model:
        argv += ["-m", codex_model]
    codex_effort = _codex_reasoning_effort(effort)
    if codex_effort:
        argv += ["-c", f'model_reasoning_effort="{codex_effort}"']
    argv.append("-")
    command = " ".join(argv[:-1]) + " -"
    proc = _run_streamed(
        argv,
        cwd=cwd,
        input_text=_payload(prompt, diff),
        timeout=timeout,
        backend="codex",
        round_no=round_no,
        announce=_ANNOUNCE_LOGS,
        header_argv0=f"codex -m {_safe_log_header(codex_model)}"
        if codex_model
        else None,
    )
    return ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


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
#
# DELIBERATE WRITE/EXEC EXCEPTION — DO NOT clamp qa to this boundary. The `review qa` mode
# (the agent-as-tester, `reviewlib/qa/executor.py`) is review-cli's ONE intentionally
# UN-CAGED agent: a tester MUST run bash + write to bring a SUT up and drive it. It does
# NOT ride this read-only path, does NOT call `_ensure_opencode_readonly_agent`, and spawns
# claude/codex with WRITE/EXEC enabled — by design (review-qa.md §1/§9). Its SUT-MUTATION
# blast radius is fenced by running inside a throwaway `git worktree` of the SUT (see
# `IsolatedSut`) — that bounds writes to a disposable tree, but it is NOT an OS sandbox (an
# un-caged shell can still read elsewhere / hit the network). If a future hardening pass
# tightens the read-only boundary, leave qa's write/exec spawn alone — "restoring" a
# read-only flag there silently neuters the tester.
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
    perm_lines = "\n".join(
        f"  {name}: deny" for name in _READONLY_AGENT_DENIED_PERMISSIONS
    )
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
            file=sys.stderr,
            flush=True,
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
    fd, tmp_name = tempfile.mkstemp(
        prefix=".read-only-reviewer.", suffix=".tmp", dir=str(path.parent)
    )
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
        proc = _run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=git_repo_env(cwd),
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not (proc.returncode == 0 and proc.stdout.strip() == "true"):
        return False
    if _opencode_has_project_config(cwd):
        print(
            f"[review-cli] opencode: {cwd} ships its own opencode config "
            "(.opencode/ or opencode.json) which could override the read-only agent; "
            "running diff-only in an isolated dir for safety.",
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def review_opencode(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
    prompt = _prompt_with_effort(prompt, effort)
    oc_model = model.split(":", 1)[1] if ":" in model else model
    unpaid = unpaid_provider_result(
        model, backend="opencode", command=f"opencode -m {oc_model}", round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    _which("opencode")
    preflight = provider_preflight_result(
        model, backend="opencode", command=f"opencode -m {oc_model}", round_no=round_no
    )
    if preflight is not None:
        return preflight
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
        ]
        variant = _opencode_variant(effort)
        if variant:
            argv += ["--variant", variant]
        argv.append(message)
        proc = _run_streamed(
            argv,
            cwd=cwd,
            timeout=timeout,
            backend="opencode",
            round_no=round_no,
            announce=_ANNOUNCE_LOGS,
            header_argv0=f"opencode -m {_safe_log_header(oc_model)}",
        )
        return ReviewResult(
            model=model,
            command=command,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # FALLBACK: cwd is not a git repo (e.g. a panel `--just-ask` run from a scratch
    # dir) — there is nothing to read, so keep the old isolated empty-temp-dir posture
    # and review the diff/prompt alone.
    command = (
        f"opencode run --agent read-only-reviewer -m {oc_model} <prompt-with-diff>"
    )
    with tempfile.TemporaryDirectory(prefix="review-cli-opencode-") as tmp_raw:
        tmp = Path(tmp_raw)
        # Strip the repo-pinning git env: a leaked GIT_DIR/GIT_WORK_TREE would make `git init`
        # operate on the LEAKED repo instead of this isolated temp dir, defeating the read-only
        # sandbox (review-cli#71). `tmp` is a fresh empty dir (not yet a repo), so git_repo_env
        # resolves no target git dir and drops every set var — exactly the isolation wanted.
        _run(["git", "init", "-q"], cwd=tmp, env=git_repo_env(tmp), timeout=30)
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
        ]
        variant = _opencode_variant(effort)
        if variant:
            argv += ["--variant", variant]
        argv.append(message)
        proc = _run_streamed(
            argv,
            cwd=tmp,
            timeout=timeout,
            backend="opencode",
            round_no=round_no,
            announce=_ANNOUNCE_LOGS,
            header_argv0=f"opencode -m {_safe_log_header(oc_model)}",
        )
    return ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


# Oh My Pi (omp) — agentic read-only seat (review-cli#174).
#
# TRANSPORT: omp does NOT read the prompt from stdin (an empty-message error), and passing
# a large prompt+diff as an argv message would hit the same ~1 MB ARG_MAX ceiling opencode
# has. omp message args support `@file` prefixing ("prefix files with @"), so the payload
# is written to a private temp dir (NEVER inside the reviewed repo) and passed as
# `@<path>`, which dodges ARG_MAX entirely (verified live against omp v17).
#
# READ-ONLY CAGE — three layers, each VERIFIED live against omp v17.1.8 (review of #174):
#   1. `--tools read,grep,glob` restricts the built-in tool set to the read-only subset
#      (no bash/edit/write — asked to run a shell command, the caged seat answers it has
#      no bash tool). `--no-extensions` + `--no-skills` + `--no-session` keep the run
#      free of discovered extensions/skills and ephemeral.
#   2. NEUTRAL CWD + SANITIZED HOME + `--add-dir <repo>`: omp discovers and EXECUTES
#      project-shipped code from its launch cwd — a hostile repo's `.mcp.json` spawns its
#      MCP server command and `.omp/tools/*.js` is imported at startup, BOTH despite
#      layer 1 (verified: marker files were created). And user-scope MCP servers
#      (~/.claude.json, cursor/vscode mcp.json, …) stay mounted regardless of cwd — their
#      tools run arbitrary code (verified: node_repl executed JS inside the caged seat).
#      So omp is launched from a fresh empty temp dir (no project discovery) with HOME
#      pointed at an empty subdir of it (no user-scope MCP discovery), while
#      PI_CODING_AGENT_DIR pins omp's real agent dir so provider auth still resolves
#      (verified). The reviewed repo is mounted read-only as a workspace via `--add-dir`
#      — the read/grep/glob tools still open any project file, but nothing in the repo
#      and no user MCP server is ever executed.
#   3. A `--config` overlay disables `fetch` (omp's read tool accepts https URLs, an
#      outbound exfiltration channel for a prompt-injected seat — verified; with
#      `fetch.enabled: false` the same URL read fails), `tools.xdev` (the `xd://` device
#      transport CARRIES the write/edit/bash device tools around `--tools` — verified:
#      without this a "caged" seat created files; with it the seat reports no write
#      tool), and `mcp.enableProjectConfig` (belt-and-braces behind layer 2).
#
# SCOPE / LIMITATION: the cage bounds what the seat can DO (read-only local tools, no
# egress, no project/user code execution) — it does NOT scope READS: the read tool opens
# any absolute path (e.g. ~/.ssh), and file contents can carry prompt injection, same as
# any agentic reviewer; review transcripts are the containment for that (read-only data
# flow), not the sandbox. The model string after `omp:` is passed to omp's `--model`
# fuzzy selector verbatim (`omp:kimi-code/k3` -> `--model kimi-code/k3`).
_OMP_READONLY_TOOLS = "read,grep,glob"

# The per-run `--config` overlay carrying cage layer 3 (see the comment block above).
_OMP_CAGE_OVERLAY = (
    "# review-cli omp read-only seat (review-cli#174): no outbound network via the read\n"
    "# tool's URL path (fetch backs it), no xd:// device transport (it carries\n"
    "# write/edit/bash around --tools), and never execute project-shipped MCP config.\n"
    "fetch:\n"
    "  enabled: false\n"
    "tools:\n"
    "  xdev: false\n"
    "mcp:\n"
    "  enableProjectConfig: false\n"
)

OMP_SUPPORTED_MODES = ("cli",)  # CLI-only — no omp REST transport exists.


def review_omp(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
    omp_model = model.split(":", 1)[1] if ":" in model else None
    command = (
        f"omp -p --no-session --tools {_OMP_READONLY_TOOLS}"
        + (f" --model {omp_model}" if omp_model else "")
        + " @<payloadfile>"
    )
    unpaid = unpaid_provider_result(
        model, backend="omp", command=command, round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    # CLI-only: a forced REVIEW_OMP_MODE=api is a config error — surface it as a
    # dead-backend result (mirrors review_zai's forced-mode path), never silently run.
    try:
        resolve_backend_mode("omp", OMP_SUPPORTED_MODES, "cli")
    except RuntimeError as exc:
        _emit_rest_log(
            "omp", command, round_no=round_no, returncode=1, stdout="", stderr=str(exc)
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=str(exc)
        )
    # AGENTIC, read-only: omp can READ any project file (mounted via `--add-dir`) — not
    # just the diff embedded in the prompt — while the three-layer cage (see the module
    # comment above) makes mutation, exec, and egress impossible.
    message = (
        f"{prompt}\n\nYou are reviewing a real git repository mounted at `{cwd}` in "
        "READ-ONLY mode: read any project file under that directory with your read "
        "tools, but do not edit, write, run commands, or access the network. "
        + (
            f"Focus on this diff:\n\n```diff\n{diff}\n```"
            # `not diff.isspace()` tests emptiness WITHOUT the full-size copy
            # `.strip()` would allocate just for truthiness.
            if diff and not diff.isspace()
            else "Answer based on the repository and the prompt."
        )
    )
    argv = [
        _which("omp"),
        "-p",
        "--no-session",
        "--no-extensions",
        "--no-skills",
        "--tools",
        _OMP_READONLY_TOOLS,
        "--add-dir",
        str(cwd),
    ]
    if omp_model:
        argv += ["--model", omp_model]
    if effort:
        # review's effort vocabulary (minimal/low/medium/high/xhigh/max) is a strict
        # subset of omp's `--thinking` levels (those plus off/auto) — pass through.
        argv += ["--thinking", effort]
    # The sandbox dir is the NEUTRAL launch cwd AND the seat's HOME (cage layer 2): a
    # fresh empty temp dir with no repo root above it, so omp's project discovery
    # (`.mcp.json`, `.omp/tools`, `.claude/tools`) finds nothing, and its user-scope MCP
    # discovery (~/.claude.json etc.) sees an empty home. PI_CODING_AGENT_DIR pins the
    # REAL agent dir so provider auth still resolves; OMP_PROFILE is dropped so nothing
    # re-derives profile paths from the fake HOME (its profile-scoped settings are user
    # workflow, not reviewer context, and the auth db is pinned explicitly). NOTE: this
    # layer assumes omp resolves `~` via the HOME env var (not getpwuid) — verified
    # live: with HOME redirected, the seat has no user-scope MCP tools
    # (tests/test_omp_cage_live.py). The dir also holds payload + overlay.
    with tempfile.TemporaryDirectory(prefix="review-cli-omp-") as sandbox:
        box = Path(sandbox)
        home = box / "home"
        home.mkdir()
        payload = box / "payload.md"
        payload.write_text(message, encoding="utf-8")
        cage = box / "cage.yml"
        cage.write_text(_OMP_CAGE_OVERLAY, encoding="utf-8")
        argv += ["--config", str(cage), f"@{payload}"]
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in ("HOME", "XDG_CONFIG_HOME", "PI_CODING_AGENT_DIR", "OMP_PROFILE")
        }
        # Sanitized XDG_CONFIG_HOME too — user-scope discovery that keys off XDG
        # instead of HOME must not escape the cage either (review of #174).
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["PI_CODING_AGENT_DIR"] = str(_omp_agent_dir())
        proc = _run_streamed(
            argv,
            cwd=box,
            env=env,
            timeout=timeout,
            backend="omp",
            round_no=round_no,
            announce=_ANNOUNCE_LOGS,
            header_argv0=f"omp -m {_safe_log_header(omp_model)}" if omp_model else None,
        )
    return ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


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
    raise RuntimeError(
        "GEMINI_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env"
    )


# NOTE: `_anthropic_key` / `_openai_key` were added in 30163c5 ONLY for the REST vision
# path. That path is gone (vision now invokes the agent CLIs — claude/codex — which carry
# their own auth, exactly like review's TEXT backends). Gemini stays the one key-based
# vision exception via `_gemini_key`. The two orphaned helpers are removed with the REST
# adapters; `_resolve_key` stays — `_gemini_key` still uses it.


def review_gemini(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
    prompt = _prompt_with_effort(prompt, effort)
    gemini_model = (
        model.split(":", 1)[1]
        if ":" in model
        else os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
    )
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
    unpaid = unpaid_provider_result(
        model, backend="gemini", command=command, round_no=round_no, started=started
    )
    if unpaid is not None:
        return unpaid
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
        stdout = (
            text.strip()
            + f"\n\nprompt_tokens={usage.get('promptTokenCount', 0)} output_tokens={usage.get('candidatesTokenCount', 0)}\n"
        )
        _emit_rest_log(
            "gemini",
            command,
            round_no=round_no,
            returncode=0,
            stdout=stdout,
            stderr="",
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=0, stdout=stdout, stderr=""
        )
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        rc = exc.code or 1
        _emit_rest_log(
            "gemini",
            command,
            round_no=round_no,
            returncode=rc,
            stdout="",
            stderr=body_text,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=rc, stdout="", stderr=body_text
        )
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
                "gemini",
                command,
                round_no=round_no,
                returncode=124,
                stdout="",
                stderr=err,
                started=started,
                timed_out=True,
                timeout_secs=timeout,
            )
            return ReviewResult(
                model=model, command=command, returncode=124, stdout="", stderr=err
            )
        _emit_rest_log(
            "gemini",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=err,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=err
        )


def _is_timeout_error(exc: BaseException) -> bool:
    """True if ``exc`` is (or wraps) a socket/network timeout.

    `urlopen(..., timeout=N)` surfaces a timeout as `socket.timeout` (== `TimeoutError`
    on 3.10+) directly, or as a `urllib.error.URLError` whose `.reason` is that timeout."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError))


def _emit_rest_log(
    backend: str,
    argv0: str,
    *,
    round_no: int,
    returncode: int,
    stdout: str,
    stderr: str,
    started: datetime | None = None,
    timed_out: bool = False,
    timeout_secs: int | None = None,
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
            backend,
            round_no=round_no,
            argv0=argv0,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            started=started,
            timed_out=timed_out,
            timeout_secs=timeout_secs,
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
    return (
        prompt if isinstance(prompt, int) else 0,
        output if isinstance(output, int) else 0,
    )


def _openai_compatible_request(
    *,
    model: str,
    api_model: str,
    label: str,
    base_url: str,
    key: str,
    prompt: str,
    diff: str,
    timeout: int,
    backend: str,
    round_no: int = 0,
    extra_body: dict | None = None,
    extra_headers: dict | None = None,
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
    shared OpenAI wire shape generic. z.ai/commandcode/openrouter all pass None today; the
    hook stays for any future provider that needs a non-standard field.

    `extra_headers` merges provider-specific HTTP headers onto the shared
    Content-Type/Authorization pair (used by OpenRouter for its OPTIONAL leaderboard
    attribution headers HTTP-Referer / X-Title). It can never override the credential:
    any case-insensitive `authorization`/`content-type` key in `extra_headers` is dropped
    BEFORE the canonical pair is written, so a stray (even lower-cased) Authorization can't
    shadow the real bearer key — the defense is explicit, not a reliance on urllib's header
    capitalization. (Note: urllib normalizes header NAMES on the wire, e.g. `HTTP-Referer`
    → `Http-referer`; HTTP header names are case-insensitive so this is cosmetic.)
    This helper does NOT sanitize `extra_headers` VALUES — a value with a CR/LF or a
    non-latin-1 char would make http.client.putheader raise mid-send (caught here as a
    malformed-request ValueError, but a wasted call). The caller is responsible for passing
    safe values; OpenRouter's wrapper validates via `_header_value_is_safe` and drops bad ones.

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
    # Drop any caller-supplied credential/content-type header (case-insensitively) BEFORE
    # writing the canonical pair, so a stray `extra_headers` entry — even a lower-cased
    # `authorization` that would be a DISTINCT dict key — can never shadow the real bearer
    # key or content type. The canonical pair is then the only authority for those names.
    _reserved = {"authorization", "content-type"}
    headers = {
        str(k): str(v)
        for k, v in (extra_headers or {}).items()
        if str(k).lower() not in _reserved
    }
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = f"Bearer {key}"
    headers.setdefault("Accept", "application/json")
    headers.setdefault("User-Agent", "review-cli/0.1")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
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
            _emit_rest_log(
                backend,
                command,
                round_no=round_no,
                returncode=1,
                stdout="",
                stderr=stderr,
                started=started,
            )
            return ReviewResult(
                model=model, command=command, returncode=1, stdout="", stderr=stderr
            )
        prompt_tokens, output_tokens = _parse_openai_usage(payload)
        stdout = text.strip() + (
            f"\n\nprompt_tokens={prompt_tokens} output_tokens={output_tokens}\n"
        )
        _emit_rest_log(
            backend,
            command,
            round_no=round_no,
            returncode=0,
            stdout=stdout,
            stderr="",
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=0, stdout=stdout, stderr=""
        )
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        rc = exc.code or 1
        if _is_payment_preflight_denial(rc, body_text):
            _cache_payment_preflight_denial(model, rc, body_text)
        _emit_rest_log(
            backend,
            command,
            round_no=round_no,
            returncode=rc,
            stdout="",
            stderr=body_text,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=rc, stdout="", stderr=body_text
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Connection refused / DNS failure / socket timeout — no HTTP status. URLError
        # and TimeoutError are both OSError subclasses; the wide catch normalises any
        # transport-level failure to a dead-backend result instead of crashing. A socket
        # timeout is logged as a TIMEOUT (rc 124) to match the subprocess + gemini metric.
        err = f"{label} API request failed: {exc}"
        if _is_timeout_error(exc):
            _emit_rest_log(
                backend,
                command,
                round_no=round_no,
                returncode=124,
                stdout="",
                stderr=err,
                started=started,
                timed_out=True,
                timeout_secs=timeout,
            )
            return ReviewResult(
                model=model, command=command, returncode=124, stdout="", stderr=err
            )
        _emit_rest_log(
            backend,
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=err,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=err
        )
    except (json.JSONDecodeError, ValueError) as exc:
        # 2xx with a non-JSON / truncated body — the provider returned garbage. Treat
        # it as a failed call rather than letting the decode error escape.
        err = f"{label} API returned a malformed response: {exc}"
        _emit_rest_log(
            backend,
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=err,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=err
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
    raise RuntimeError(
        "ZAI_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env"
    )


ZAI_SUPPORTED_MODES = ("api",)  # z.ai is REST-only; no z.ai CLI exists.


def review_zai(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
    prompt = _prompt_with_effort(prompt, effort)
    # api-only: a forced REVIEW_ZAI_MODE=cli is a config error, surfaced as a
    # dead-backend result instead of silently running the api path.
    # `round_no` is accepted (and forwarded) so the panel's uniform 6-arg dispatch
    # (panel.py: backend(model, prompt, diff, cwd, timeout, round_no)) does not raise
    # for these post-HYP-741 REST backends and the dashboard attributes the run to the
    # right brainstorm round.
    # A forced-mode config error or a missing key both produce a NON-zero result AND a
    # sidecar log — like review_gemini, these are real (failed) run attempts and must be
    # visible in the dashboard, never raise out of run_panel as an invisible internal 127.
    zai_model = (
        model.split(":", 1)[1]
        if ":" in model
        else os.environ.get("ZAI_MODEL", ZAI_DEFAULT_MODEL)
    )
    command = f"z.ai API {zai_model}"
    unpaid = unpaid_provider_result(
        model, backend="z.ai", command=command, round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    try:
        resolve_backend_mode("zai", ZAI_SUPPORTED_MODES, "api")
        key = _zai_key()
    except RuntimeError as exc:
        _emit_rest_log(
            "z.ai", command, round_no=round_no, returncode=1, stdout="", stderr=str(exc)
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=str(exc)
        )
    base_url = os.environ.get("ZAI_BASE_URL", ZAI_DEFAULT_BASE_URL)
    return _openai_compatible_request(
        model=model,
        api_model=zai_model,
        label="z.ai",
        base_url=base_url,
        key=key,
        prompt=prompt,
        diff=diff,
        timeout=timeout,
        backend="z.ai",
        round_no=round_no,
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


def review_commandcode(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
    prompt = _prompt_with_effort(prompt, effort)
    # API-only: a forced REVIEW_COMMANDCODE_MODE=cli is a config error (there is no
    # commandcode CLI), surfaced as a dead-backend result, never a silent api POST.
    # `round_no` is accepted so the panel's uniform 6-arg dispatch does not raise for
    # this post-HYP-741 REST backend (see review_zai for the rationale).
    # A forced-mode config error or a missing key both produce a NON-zero result AND a
    # sidecar log (see review_zai) — a failed run must be visible in the dashboard, never
    # an invisible internal 127 raised out of run_panel.
    has_suffix = ":" in model
    env_model = os.environ.get("COMMANDCODE_MODEL")
    cc_model = (
        model.split(":", 1)[1]
        if has_suffix
        else (env_model or COMMANDCODE_DEFAULT_MODEL)
    )
    command = f"commandcode API {cc_model}"
    unpaid = unpaid_provider_result(
        model, backend="commandcode", command=command, round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    try:
        resolve_backend_mode("commandcode", COMMANDCODE_SUPPORTED_MODES, "api")
        key = _commandcode_key()
    except RuntimeError as exc:
        _emit_rest_log(
            "commandcode",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=str(exc),
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=str(exc)
        )
    preflight = provider_preflight_result(
        model, backend="commandcode", command=command, round_no=round_no
    )
    if preflight is not None:
        return preflight
    base_url = os.environ.get("COMMANDCODE_BASE_URL") or COMMANDCODE_DEFAULT_BASE_URL
    return _openai_compatible_request(
        model=model,
        api_model=cc_model,
        label="commandcode",
        base_url=base_url,
        key=key,
        prompt=prompt,
        diff=diff,
        timeout=timeout,
        backend="commandcode",
        round_no=round_no,
    )


# OpenRouter — an OpenAI-compatible API AGGREGATOR (https://openrouter.ai) that fronts 400+
# models across providers (anthropic/openai/google/meta/...) behind ONE key and ONE
# OpenAI-shaped /chat/completions endpoint. So it is a keyed-HTTP backend exactly like z.ai
# and commandcode, NOT a subprocess (no OpenRouter CLI exists — API-only).
#   base   https://openrouter.ai/api/v1
#   POST   /chat/completions      (OpenAI request/response shape)
#   auth   Authorization: Bearer $OPENROUTER_API_KEY   (keys look like sk-or-v1-...)
# The wire model id is the OpenRouter slug `<provider>/<model>` (e.g. anthropic/claude-3.5-
# sonnet, openai/gpt-4o), optionally with a `:variant` suffix (`:free`, `:beta`, `:nitro`).
# A bare `openrouter` seat defaults to OpenRouter's OWN auto-router (`openrouter/auto`),
# which picks a suitable model server-side — a never-stale default, since the concrete pick
# is OpenRouter's job, not a literal we'd have to bump. Override the model with an
# `openrouter:<slug>` seat suffix or OPENROUTER_MODEL, and the base with OPENROUTER_BASE_URL.
OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"
OPENROUTER_SUPPORTED_MODES = ("api",)  # API-only — no OpenRouter CLI exists.


def _openrouter_key() -> str:
    # ONLY OPENROUTER_API_KEY. Like commandcode's `user_...` token, an OpenRouter key is a
    # provider-specific `sk-or-v1-...` credential; accepting a foreign alias (e.g. a raw
    # OpenAI/Anthropic key) would silently POST that credential to openrouter.ai — a wrong
    # host and a cross-provider key leak. So the canonical name is the only one that resolves.
    key = _resolve_key(("OPENROUTER_API_KEY",), "OPENROUTER_API_KEY")
    if key:
        return key
    raise RuntimeError(
        "OPENROUTER_API_KEY not found in env, GEMINI_ENV_FILE, or ~/.config/review-cli/.env"
    )


def _openrouter_extra_headers() -> dict:
    """OpenRouter's OPTIONAL leaderboard-attribution headers, built from env. Empty when
    unset, so the backend works with nothing configured (the headers only affect how the
    app appears on openrouter.ai rankings — never the review result). HTTP-Referer / X-Title
    are the documented header names; the env vars mirror them for discoverability.

    A value containing a control character (CR/LF/etc.) is DROPPED, not sent: http.client's
    putheader would raise a ValueError on a newline mid-send, turning a stray env value into
    a crash AND it would be a header-injection vector. Dropping the bad value keeps the
    backend on its "any failure is a graceful dead-backend, never a raw exception" contract
    and refuses to smuggle attacker/typo CRLF into the request."""
    headers: dict[str, str] = {}
    referer = os.environ.get("OPENROUTER_HTTP_REFERER", "").strip()
    if referer and _header_value_is_safe(referer):
        headers["HTTP-Referer"] = referer
    title = os.environ.get("OPENROUTER_X_TITLE", "").strip()
    if title and _header_value_is_safe(title):
        headers["X-Title"] = title
    return headers


def _header_value_is_safe(value: str) -> bool:
    """True iff `value` is safe to send as an HTTP header value, on two counts:

    * it is encodable as **latin-1** — http.client encodes header values as latin-1, so a
      non-encodable char (emoji, CJK, …) would raise UnicodeEncodeError mid-send; and
    * it carries **no control character** — C0 (0x00-0x1F, except tab), DEL (0x7F), or C1
      (0x80-0x9F) — which could break HTTP header framing (CR/LF injection) or be mangled.

    A value failing either is DROPPED by the caller, keeping an optional attribution header
    from ever crashing or corrupting the request. Printable ASCII + printable latin-1
    (e.g. accented letters, 0xA0-0xFF) + tab are allowed."""
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return not any(
        (ord(ch) < 0x20 and ch != "\t") or 0x7F <= ord(ch) <= 0x9F for ch in value
    )


def review_openrouter(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
    prompt = _prompt_with_effort(prompt, effort)
    # API-only: a forced REVIEW_OPENROUTER_MODE=cli is a config error (no OpenRouter CLI),
    # surfaced as a dead-backend result, never a silent api POST. `round_no` is accepted so
    # the panel's uniform 6-arg dispatch does not raise (see review_zai for the rationale).
    # A forced-mode config error or a missing key both produce a NON-zero result AND a
    # sidecar log so a failed run is visible in the dashboard, not an invisible internal 127.
    # `split(":", 1)[1]` strips ONLY the `openrouter:` prefix, preserving both the slug's
    # `/` AND any trailing `:variant` colon (e.g. openrouter:anthropic/claude-3.5-sonnet:beta
    # → anthropic/claude-3.5-sonnet:beta). The suffix is `.strip()`ed and used ONLY when
    # non-empty: a bare `openrouter`, an EMPTY suffix (`openrouter:` / `openrouter: `) all fall
    # back to OPENROUTER_MODEL, then the auto-router default — never POST `"model": ""` (a
    # guaranteed 400) just because a stray colon was present.
    # Both env overrides are `.strip()`ed and used only when non-empty, symmetric with the
    # suffix discipline above: a whitespace-only OPENROUTER_MODEL / OPENROUTER_BASE_URL must
    # NOT win (it would POST `"model": "   "` / build a broken URL — the same 400 the empty
    # suffix guards against), so it falls through to the next source / the default.
    suffix = model.split(":", 1)[1].strip() if ":" in model else ""
    env_model = os.environ.get("OPENROUTER_MODEL", "").strip()
    or_model = suffix or env_model or OPENROUTER_DEFAULT_MODEL
    command = f"openrouter API {or_model}"
    unpaid = unpaid_provider_result(
        model, backend="openrouter", command=command, round_no=round_no
    )
    if unpaid is not None:
        return unpaid
    try:
        resolve_backend_mode("openrouter", OPENROUTER_SUPPORTED_MODES, "api")
        key = _openrouter_key()
    except RuntimeError as exc:
        _emit_rest_log(
            "openrouter",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=str(exc),
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=str(exc)
        )
    base_url = (
        os.environ.get("OPENROUTER_BASE_URL", "").strip() or OPENROUTER_DEFAULT_BASE_URL
    )
    return _openai_compatible_request(
        model=model,
        api_model=or_model,
        label="openrouter",
        base_url=base_url,
        key=key,
        prompt=prompt,
        diff=diff,
        timeout=timeout,
        backend="openrouter",
        round_no=round_no,
        extra_headers=_openrouter_extra_headers(),
    )


# A non-default User-Agent: some Anthropic-compatible gateways (e.g. CommandCode,
# behind Cloudflare) 403 the bare urllib UA with "error code: 1010".
_ANTHROPIC_UA = "review-cli (anthropic-compatible client)"
_CLAUDE_REVIEW_SYSTEM = (
    "You are running inside review-cli in a headless read-only diff review. "
    "Do not use tools, inspect files, ask for permissions, or plan tool work. "
    "Answer only from the prompt and diff supplied by the user."
)
_CLAUDE_IMAGE_REVIEW_SYSTEM = (
    "You are running inside review-cli in a headless read-only visual diff review. "
    "Use the Read tool only for image file paths explicitly listed in the RAW VISUAL "
    "ATTACHMENT section. Do not inspect the source repository or any other local files. "
    "Answer from the prompt, diff, and those attached images only."
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
    base = _anthropic_base_url_from_env() or "https://api.anthropic.com"
    return {"base": base, "auth": auth}


def _anthropic_base_url_from_env() -> str | None:
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip().rstrip("/")
    return base or None


def _base_url_hostname(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.hostname:
        return parsed.hostname.lower()
    parsed = urllib.parse.urlparse(f"//{base_url}")
    return (parsed.hostname or "").lower()


def _anthropic_gateway_provider(base_url: str) -> str | None:
    host = _base_url_hostname(base_url)
    if host == "api.commandcode.ai":
        return "commandcode"
    return None


def review_claude_api(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
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
    prompt = _prompt_with_effort(prompt, effort)
    claude_model = _claude_api_model(model)
    cfg = _anthropic_api_config()
    command = f"Anthropic API {claude_model}"
    started = datetime.now(timezone.utc)
    gateway_provider = (
        _anthropic_gateway_provider(cfg["base"]) if cfg is not None else None
    )
    unpaid = unpaid_provider_result(
        model,
        backend="claude",
        command=command,
        round_no=round_no,
        started=started,
        provider=gateway_provider,
    )
    if unpaid is not None:
        return unpaid
    if cfg is None:
        stderr = (
            "claude API mode: no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN configured"
        )
        _emit_rest_log(
            "claude",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=stderr,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=stderr
        )
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
            p.get("text", "")
            for p in parts
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
        _emit_rest_log(
            "claude",
            command,
            round_no=round_no,
            returncode=rc,
            stdout=stdout,
            stderr="",
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=rc, stdout=stdout, stderr=""
        )
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", "replace")
        rc = exc.code or 1
        _emit_rest_log(
            "claude",
            command,
            round_no=round_no,
            returncode=rc,
            stdout="",
            stderr=body_text,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=rc, stdout="", stderr=body_text
        )
    except urllib.error.URLError as exc:
        err = str(exc)
        if _is_timeout_error(exc):
            _emit_rest_log(
                "claude",
                command,
                round_no=round_no,
                returncode=124,
                stdout="",
                stderr=err,
                started=started,
                timed_out=True,
                timeout_secs=timeout,
            )
            return ReviewResult(
                model=model, command=command, returncode=124, stdout="", stderr=err
            )
        _emit_rest_log(
            "claude",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=err,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=err
        )
    except (ValueError, OSError) as exc:
        # malformed / non-JSON 2xx body, or a read/decode/timeout failure — surface
        # as a normal backend result, not an uncaught exception. (URLError, a
        # subclass of OSError, is handled above; this catches the rest.)
        if _is_timeout_error(exc):
            err = str(exc)
            _emit_rest_log(
                "claude",
                command,
                round_no=round_no,
                returncode=124,
                stdout="",
                stderr=err,
                started=started,
                timed_out=True,
                timeout_secs=timeout,
            )
            return ReviewResult(
                model=model, command=command, returncode=124, stdout="", stderr=err
            )
        err = f"claude API: malformed or unreadable response: {exc}"
        _emit_rest_log(
            "claude",
            command,
            round_no=round_no,
            returncode=1,
            stdout="",
            stderr=err,
            started=started,
        )
        return ReviewResult(
            model=model, command=command, returncode=1, stdout="", stderr=err
        )


def _have_claude_cli() -> bool:
    # Either binary drives the CLI path: `claude` (preferred, genuine print mode) or the
    # legacy `claude-p` TUI-scraper fallback (review-cli#76). A host with only one still
    # has a working CLI seat, so the dispatcher must not route it to the paid API.
    return bool(_which_optional("claude") or _which_optional("claude-p"))


def _claude_api_available_for_model(model: str) -> bool:
    cfg = _anthropic_api_config()
    if cfg is None:
        return False
    provider = _anthropic_gateway_provider(cfg["base"])
    return _matched_unpaid_provider(model, provider) is None


def _claude_gateway_provider_from_env() -> str | None:
    base = _anthropic_base_url_from_env()
    return _anthropic_gateway_provider(base) if base is not None else None


def _claude_runtime_gateway_provider() -> str | None:
    # The Claude CLI child intentionally inherits ANTHROPIC_* vars, so an
    # Anthropic-compatible gateway can be the runtime provider for both API and CLI paths.
    return _claude_gateway_provider_from_env()


def review_claude(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
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
    variant via _run_streamed, the API variant via its own _emit_rest_log sidecar.

    Before dispatching, checks ``reviewlib.seat_cooldown`` for a CACHED chronic-
    unavailable verdict from an earlier process (Fable's session-limit/paywall pattern —
    see that module's docstring) and, if cooling down, returns a synthetic sentinel
    WITHOUT spawning the real CLI/API call. After a genuine dispatch, a result matching
    that same chronic shape records a fresh cooldown for the NEXT invocation."""
    unpaid = unpaid_provider_result(
        model,
        backend="claude",
        command=_claude_api_command(model),
        round_no=round_no,
        provider=_claude_runtime_gateway_provider(),
    )
    if unpaid is not None:
        return unpaid
    cooldown = active_cooldown(model)
    if cooldown is not None:
        return _cooldown_skip_result(model, round_no, cooldown)
    mode = os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
    if mode == "api":
        result = review_claude_api(
            model, prompt, diff, cwd, timeout, round_no, effort=effort
        )
    elif (
        mode != "cli" and not _have_claude_cli() and _anthropic_api_config() is not None
    ):
        result = review_claude_api(
            model, prompt, diff, cwd, timeout, round_no, effort=effort
        )
    else:
        result = review_claude_cli(
            model, prompt, diff, cwd, timeout, round_no, effort=effort
        )
    reason = _chronic_unavailable_reason(result)
    if reason is not None:
        record_cooldown(model, reason)
    elif result.returncode == 0 and result.stdout.strip():
        # review-cli#221: see the identical comment (incl. the round-4 empty-body
        # finding) on the --visual call site above.
        clear_cooldown(model)
    return result


# The rc=0 administrative sentinel shapes ("Claude Fable 5 is currently unavailable.
# Learn more: ..."). This is now the SINGLE canonical source — panel.py imports these
# two names FROM backends (this direction only; backends never imports panel, so no
# circularity), instead of each module keeping its own copy.
#
# glm/Opus review finding (2026-08 seat-cooldown feature, round 2): a PRIOR version of
# this module kept its own private one-marker literal ("is currently unavailable") that
# the comment claimed "must stay in sync with panel.py's _UNAVAILABLE_MARKERS" — but it
# recognised only 1 of panel's 4 marker phrases ("is temporarily unavailable", "model is
# unavailable", "currently not available" were invisible here). Every OTHER consumer of
# those 4 markers (`panel.result_is_usable`, `retry.classify_failure` via
# `_is_rc0_sentinel`, the dashboard's HEALTH_PAYWALL) already treats all four as
# equally chronic — a Fable response using any of the other three wordings was
# therefore correctly failover-replaced and classified paywall EVERYWHERE except here,
# so `record_cooldown` silently never fired for it and the seat kept paying for a real
# dispatch on every single invocation for exactly the failure shape this feature exists
# to stop. Defining the tuple once, here, and having every consumer import it makes a
# future wording drift impossible instead of merely commented-against.
_UNAVAILABLE_MARKERS = (
    "is currently unavailable",
    "is temporarily unavailable",
    "model is unavailable",
    "currently not available",
)
# A session/usage-limit notice ("You've hit your session limit ... resets 7:30pm") is the
# OTHER chronic shape the token-burn investigation found dominates Fable's real failures
# (1,836 of 4,322 recorded failures) — it is NOT rc=0/short-body shaped like the sentinel
# above (the CLI exits non-zero), so it needs its own marker check.
_CHRONIC_QUOTA_MARKERS = ("session limit", "usage-credits", "usage credits")
# The administrative sentinel is only trustworthy when the WHOLE body is short (a
# one-liner notice) — a real, long review that happens to mention availability must
# never be cached as a chronic failure. Shared by panel.result_is_usable and
# retry._is_rc0_sentinel too (both import this name), so all three agree on the bound.
_UNAVAILABLE_MAX_LEN = 400


def _chronic_unavailable_reason(result: ReviewResult) -> str | None:
    """A short reason string if ``result`` looks like a CHRONIC (cooldown-worthy)
    unavailability review-cli already knows how to recognise elsewhere, else ``None``.

    Deliberately narrow — see seat_cooldown.py's docstring for why only these two
    shapes (not every seat-fatal failure) start a cooldown. BOTH branches require a
    NON-usable result (rc=0 short sentinel, or rc!=0), mirroring retry.py's
    `_error_channel` error-channel discipline: a long, genuinely SUCCESSFUL review
    (rc=0, real content) is NEVER scanned for the quota markers, even though its prose
    could legitimately mention "session limit" (this repo's own README/code does) —
    codex/kimi review finding: the quota check originally scanned ANY body regardless
    of exit code, so a real review of review-cli itself could self-starve the seat."""
    body = (result.stdout or "").strip()
    if body and result.returncode == 0 and len(body) <= _UNAVAILABLE_MAX_LEN:
        if any(marker in body.lower() for marker in _UNAVAILABLE_MARKERS):
            return "unavailable sentinel"
        return None  # a short rc=0 body that is NOT the sentinel is a real (if terse) answer
    if result.returncode == 0:
        return None  # a long rc=0 body is a genuine successful review — never scanned
    # From here: a non-zero exit. Mirrors retry.py's _error_channel — stderr always,
    # plus a SHORT stdout (a failed CLI often writes its error to stdout, not stderr).
    haystack = (result.stderr or "").lower()
    if len(body) <= _UNAVAILABLE_MAX_LEN:
        haystack += "\n" + body.lower()
    if any(marker in haystack for marker in _CHRONIC_QUOTA_MARKERS):
        return "session limit / usage credits"
    return None


def _bounded_cooldown_skip_body(model: str, reason: str, remaining: int) -> str:
    """Build the cooldown-skip stdout, guaranteed <= `_UNAVAILABLE_MAX_LEN` chars so
    `panel.result_is_usable`'s length-gated marker scan (and `retry._is_rc0_sentinel`'s
    identical bound) always recognises it as the sentinel it deliberately mirrors.

    glm review finding: the previous, unbounded f-string could exceed the bound — the
    `reason` seat_cooldown persists can be up to `_REASON_MAX_LEN=200` chars, and
    `model` is the raw, unvalidated seat string from `-m`/a board config entry, with no
    length limit of its own. Past the bound, `result_is_usable` stops scanning for
    markers at all and returns `True` — silently demoting a cached skip (that never ran
    a real review) to a "successful" rc=0 verdict that satisfies the flat path's `ok`
    and the `--commit`/`--staged` gate, exactly the certification failure this whole
    sentinel-mirroring design exists to prevent.

    `reason` (diagnostic only) is truncated first; `model` (the seat identifier a human
    actually needs to read in run-stats) only as a last resort. The marker phrase itself
    (`_UNAVAILABLE_MARKERS[0]`) is NEVER truncated — it's the one substring every
    downstream consumer actually keys on, so truncating it would defeat recognition
    entirely rather than just look slightly odd.

    Opus review finding, round 4: `remaining` used to be embedded as-is, treated as
    part of the FIXED template overhead the `model`/`reason` budgets are computed
    against — but it is not actually bounded anywhere. `_ttl_seconds()` rejects a
    non-finite `$REVIEW_SEAT_COOLDOWN_SECONDS` (NaN/inf), but a pathologically large yet
    still-FINITE value (e.g. approaching float64's ~1.8e308 ceiling) survives that guard
    and renders as a many-hundred-digit `remaining`, which alone can exceed
    `_UNAVAILABLE_MAX_LEN` — at which point even truncating `model` to nothing (the
    function's own last resort) cannot bring the body back under the bound, silently
    breaking the "guaranteed <= max_len" promise this docstring makes and letting
    `result_is_usable` treat the skip as a real result. Clamping `remaining` to a sane
    ceiling up front (a cooldown lasting >31 years is meaningless to display precisely
    anyway) keeps it a true fixed-width part of the overhead, restoring the guarantee
    unconditionally rather than only for realistic TTLs."""
    marker = _UNAVAILABLE_MARKERS[0]
    # 999,999,999s (~31.7 years) — always <= 9 digits, comfortably below any TTL a real
    # operator would configure; a display value beyond this is meaningless precision,
    # not information worth spending the length budget on.
    remaining = min(remaining, 999_999_999)

    def _build(m: str, r: str) -> str:
        return (
            f"{m} {marker} (cached: {r}; skip expires in {remaining}s — "
            "reviewlib.seat_cooldown).\n"
        )

    body = _build(model, reason)
    if len(body) <= _UNAVAILABLE_MAX_LEN:
        return body
    overhead = len(_build(model, ""))
    reason_budget = max(0, _UNAVAILABLE_MAX_LEN - overhead)
    body = _build(model, reason[:reason_budget])
    if len(body) <= _UNAVAILABLE_MAX_LEN:
        return body
    overhead_no_model = len(_build("", ""))
    model_budget = max(0, _UNAVAILABLE_MAX_LEN - overhead_no_model)
    return _build(model[:model_budget], "")


def _cooldown_skip_result(model: str, round_no: int, cooldown: dict) -> ReviewResult:
    """Build the synthetic ReviewResult for a cached-cooldown skip, and log it via the
    same REST sidecar path REST backends use — so the dashboard still sees the call (as
    a skipped/paywall-shaped one) instead of the seat silently vanishing from a session.

    The command label deliberately does NOT say "Anthropic API" (kimi review finding:
    that string is `_claude_api_command`'s REST-transport label and would mislabel a
    CLI-mode seat's skip as an API call in the sidecar log/dashboard, misleading a
    post-mortem) — the skip never chose a transport at all, it short-circuited before
    either."""
    command = "seat-cooldown skip (claude)"
    remaining = int(cooldown["remaining_seconds"])
    stdout = _bounded_cooldown_skip_body(model, cooldown["reason"], remaining)
    _emit_rest_log(
        "claude", command, round_no=round_no, returncode=0, stdout=stdout, stderr=""
    )
    return ReviewResult(
        model=model, command=command, returncode=0, stdout=stdout, stderr=""
    )


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
            fd, tmp = tempfile.mkstemp(
                dir=str(cfg.parent), prefix=".claude.", suffix=".tmp"
            )
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


# The exact flag set `_ensure_workspace_trusted` seeds. An entry holding ONLY these (a subset is
# fine) is one review created and nothing else enriched — safe to reap. Any EXTRA key means a
# real claude session touched it, so we leave it.
_REVIEW_SEEDED_TRUST_KEYS = frozenset(
    {"hasTrustDialogAccepted", "hasCompletedProjectOnboarding"}
)


def _is_review_seeded_trust_entry(entry: object) -> bool:
    """True iff ``entry`` looks like an entry review SEEDED and nothing else enriched: a dict
    whose keys are a subset of the trust/onboarding flags review writes. A larger entry (extra
    keys from a real interactive session) returns False so we never delete a user's real data."""
    return isinstance(entry, dict) and set(entry).issubset(_REVIEW_SEEDED_TRUST_KEYS)


def _remove_workspace_trust(cwd: Path) -> None:
    """Remove a previously-seeded workspace-trust entry for ``cwd`` from ~/.claude.json.

    The inverse of ``_ensure_workspace_trusted``: qa seeds a trust entry for each EPHEMERAL
    throwaway worktree it spawns the claude tester in (a fresh worktree is untrusted and the
    headless gate blocks on it). The worktree is deleted on exit, but its persisted
    ``projects[<realpath>]`` trust entry is NOT — so over many default qa runs ~/.claude.json
    accumulates trusted ``/tmp/review-qa-wt-*`` paths that no longer exist (review-cli#60). Call
    this after the run to drop the one entry, keyed by the SAME realpath the seed used.

    Best-effort and conservative (mirrors the seed): only ever removes the ONE project key, uses
    the same flock + atomic os.replace, and silently degrades on any error. It removes the entry
    ONLY when it still holds just the flags review seeds — if a later real interactive claude
    session enriched it, the entry is LEFT intact so we reap only what we created."""
    cfg = Path.home() / ".claude.json"
    if not cfg.exists():
        return
    key = os.path.realpath(str(cwd))
    lock = None
    flock_mod = None
    try:
        try:
            import fcntl as flock_mod
        except ImportError:
            flock_mod = None
        if flock_mod is not None:
            try:
                lock = open(cfg.with_name(".claude.json.review-trust.lock"), "w")
                flock_mod.flock(lock.fileno(), flock_mod.LOCK_EX)
            except OSError:
                if lock is not None:
                    lock.close()
                lock = None
        data = json.loads(cfg.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        projects = data.get("projects")
        if not isinstance(projects, dict) or key not in projects:
            return  # nothing seeded for this path — nothing to reap
        if not _is_review_seeded_trust_entry(projects.get(key)):
            return  # the entry grew beyond what review seeds — leave it untouched
        del projects[key]
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(cfg.parent), prefix=".claude.", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, cfg)
        except Exception:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except Exception:
        return
    finally:
        if lock is not None:
            try:
                if flock_mod is not None:
                    flock_mod.flock(lock.fileno(), flock_mod.LOCK_UN)
            finally:
                lock.close()


# Tools the read-only review seat must never invoke. Shared by the direct `claude`
# argv and the legacy `claude-p` argv so the two can't drift.
_CLAUDE_DISALLOWED_TOOLS = (
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
)


def _claude_cli_binary() -> tuple[str, bool]:
    """Resolve the claude CLI binary, preferring `claude` in genuine print mode.

    Returns (path, direct): `direct=True` for the `claude --print` headless path,
    `False` for the legacy `claude-p` TUI-scraper fallback.

    `claude --print` writes a clean result straight to stdout — no PTY, no fullscreen
    TUI, no screen-scrape. `claude-p` instead spawns the interactive `claude` TUI under
    a PTY and scrapes the screen, which is a lossy redraw surface: spinner frames and
    cursor redraws smear into the captured output, and the scrape frequently fails
    (`assistant_output_timeout` → empty stdout), corrupting / blanking the opus review
    seat (review-cli#76). So we use `claude` directly whenever it is present and only
    fall back to `claude-p` when it is not (a host that ships only the wrapper)."""
    direct = _which_optional("claude")
    if direct:
        return direct, True
    return _which("claude-p"), False  # raises a clear error if neither is present


@lru_cache(maxsize=8)
def _claude_cli_supports_effort(binary: str) -> bool:
    try:
        proc = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    help_text = f"{proc.stdout}\n{proc.stderr}"
    return "--effort" in help_text


def _claude_cli_argv(
    binary: str,
    direct: bool,
    model: str | None,
    cwd: Path,
    timeout: int,
    *,
    image_dir: Path | None = None,
    effort: str | None = None,
) -> list[str]:
    """Build the argv for the resolved claude CLI binary.

    Both forms run headless, read the prompt from STDIN (never `-p <payload>` argv: a
    brainstorm round embeds the whole prior transcript and a diff can be huge, which as
    a command-line argument blows past ARG_MAX → execve E2BIG). Read-only is enforced by
    an EMPTY tool allowlist (`--tools ""` = all built-in tools off) unless a visual
    panel job needs scoped image reads, in which case direct `claude` gets `--tools Read`
    plus `--add-dir <staged-image-dir>`. That keeps normal text review locked down while
    allowing the explicit screenshot attachment path to read only its temp image files."""
    if direct:
        # `claude --print --output-format text` is the genuine print mode. cwd is the
        # Popen cwd (claude has no --cwd); there is no --timeout-sec (review-cli's own
        # _run_streamed timeout governs the call). Normal text review uses `--tools ""`;
        # visual panel jobs use `--tools Read` scoped to the temp image dir. No
        # --disallowedTools denylist is needed (and its claude-p vocabulary, e.g.
        # MultiEdit/SlashCommand, isn't a real `claude` tool name — review-cli#76).
        tools = "Read" if image_dir is not None else ""
        system_prompt = (
            _CLAUDE_IMAGE_REVIEW_SYSTEM
            if image_dir is not None
            else _CLAUDE_REVIEW_SYSTEM
        )
        argv = [
            binary,
            "--print",
            "--output-format",
            "text",
            "--permission-mode",
            "dontAsk",
            "--tools",
            tools,
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--safe-mode",
            "--append-system-prompt",
            system_prompt,
        ]
        if image_dir is not None:
            argv += ["--add-dir", str(image_dir)]
        claude_effort = _claude_reasoning_effort(effort)
        if claude_effort and _claude_cli_supports_effort(binary):
            argv += ["--effort", claude_effort]
        if model:
            argv += ["--model", model]
        return argv
    # Legacy claude-p fallback (TUI-scraper): keep its --cwd / --tools '' / --timeout-sec
    # / -p surface and its denylist so a claude-less host still works. Disable the wrapper's
    # wall-clock cap; review-cli's own streamed runner owns the idle timeout and can keep a
    # chatty/known-alive Fable run going without a hidden 20m wall kill.
    argv = [
        binary,
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
        *_CLAUDE_DISALLOWED_TOOLS,
        "--timeout-sec",
        "0",
    ]
    if model:
        argv += ["--model", model]
    claude_effort = _claude_reasoning_effort(effort)
    if claude_effort and _claude_cli_supports_effort(binary):
        argv += ["--effort", claude_effort]
    argv += ["-p"]
    return argv


def _stage_panel_images(images: tuple[Path, ...], tmp: Path) -> list[Path]:
    paths: list[Path] = []
    for idx, image in enumerate(images):
        src = Path(image)
        if not src.is_file():
            continue
        suffix = src.suffix if src.suffix else ".png"
        dst = tmp / f"{idx}-panel-image{suffix}"
        try:
            dst.write_bytes(src.read_bytes())
        except OSError:
            continue
        paths.append(dst)
    return paths


def _prompt_with_panel_images(prompt: str, images: list[Path]) -> str:
    if not images:
        return prompt
    refs = "\n".join(f"- @{path}" for path in images)
    return (
        f"{prompt}\n\n"
        "=== RAW VISUAL ATTACHMENT ===\n"
        "Inspect these screenshot image files directly before answering; do not rely only "
        "on any textual visual summary already in the prompt.\n"
        f"{refs}"
    )


def _claude_cli_env() -> dict[str, str]:
    """Child env that keeps the claude CLI from emitting decorative terminal control
    sequences into the captured pipe. `TERM=dumb` + `NO_COLOR=1` disable colour/cursor
    rendering; `CI=1` nudges any progress UI off. Inherits the rest of the environment
    (auth, PATH, ANTHROPIC_* gateway vars) so the subscription / gateway path is intact."""
    env = dict(os.environ)
    env["TERM"] = "dumb"
    env["NO_COLOR"] = "1"
    env["CI"] = "1"
    return env


def review_claude_cli(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
    prompt = _prompt_with_effort(prompt, effort)
    claude_model = model.split(":", 1)[1] if ":" in model else None
    # Resolve the binary BEFORE touching trust so a missing CLI raises here, not after
    # _ensure_workspace_trusted has already mutated ~/.claude.json.
    binary, direct = _claude_cli_binary()
    # Pre-accept workspace trust for cwd: the headless run cannot answer the
    # interactive "Do you trust this folder?" gate (see _ensure_workspace_trusted).
    _ensure_workspace_trusted(cwd)
    argv = _claude_cli_argv(binary, direct, claude_model, cwd, timeout, effort=effort)
    proc = _run_streamed(
        argv,
        cwd=cwd,
        input_text=_payload(prompt, diff),
        env=_claude_cli_env(),
        timeout=timeout,
        backend="claude",
        round_no=round_no,
        announce=_ANNOUNCE_LOGS,
    )
    # Belt-and-suspenders: strip any terminal control noise the CLI still leaked into the
    # pipe (a stray spinner frame from claude-p, an escape sequence) so it can never
    # corrupt the parsed `## <model> [ok]/[needs-changes]` verdict (review-cli#76).
    stdout = strip_control_sequences(proc.stdout)
    stderr = strip_control_sequences(proc.stderr)
    # A redacted, human-readable command line for the result header (no prompt/diff).
    if direct:
        command = (
            "claude --print --output-format text --permission-mode dontAsk --tools '' "
            "--strict-mcp-config --disable-slash-commands --safe-mode "
            "--append-system-prompt <read-only-review>  (prompt via stdin)"
        )
    else:
        command = (
            "claude-p --permission-mode dontAsk --tools '' --strict-mcp-config "
            "--disable-slash-commands --safe-mode --append-system-prompt <read-only-review> "
            "--disallowedTools "
            + " ".join(_CLAUDE_DISALLOWED_TOOLS)
            + " -p  (prompt via stdin)"
        )
    return ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def review_claude_cli_with_images(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    images: tuple[Path, ...] = (),
    effort: str | None = None,
) -> ReviewResult:
    prompt = _prompt_with_effort(prompt, effort)
    claude_model = model.split(":", 1)[1] if ":" in model else None
    if os.environ.get(_mode_env_var("claude"), "").strip().lower() == "api":
        return review_claude(model, prompt, diff, cwd, timeout, round_no, effort=effort)
    binary = _which_optional("claude")
    if not binary:
        return review_claude(model, prompt, diff, cwd, timeout, round_no, effort=effort)
    with tempfile.TemporaryDirectory(prefix="review-cli-claude-images-") as tmp_raw:
        tmp = Path(tmp_raw)
        staged = _stage_panel_images(images, tmp)
        if not staged:
            return review_claude_cli(
                model, prompt, diff, cwd, timeout, round_no, effort=effort
            )
        _ensure_workspace_trusted(tmp)
        argv = _claude_cli_argv(
            binary, True, claude_model, tmp, timeout, image_dir=tmp, effort=effort
        )
        proc = _run_streamed(
            argv,
            cwd=tmp,
            input_text=_payload(_prompt_with_panel_images(prompt, staged), diff),
            env=_claude_cli_env(),
            timeout=timeout,
            backend="claude",
            round_no=round_no,
            announce=_ANNOUNCE_LOGS,
        )
    # The TemporaryDirectory context deleted tmp; remove the trust entry that
    # _ensure_workspace_trusted seeded so dead /tmp/review-cli-claude-images-* paths
    # don't accumulate in ~/.claude.json. (P2 fix — review-cli#98 thread.)
    _remove_workspace_trust(tmp)
    stdout = strip_control_sequences(proc.stdout)
    stderr = strip_control_sequences(proc.stderr)
    command = (
        "claude --print --output-format text --permission-mode dontAsk --tools Read "
        "--strict-mcp-config --disable-slash-commands --safe-mode --add-dir <image-dir> "
        "--append-system-prompt <read-only-review>  (prompt via stdin, image @refs)"
    )
    return ReviewResult(
        model=model,
        command=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


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
    return os.environ.get("REVIEW_FAKE_BACKEND", "").strip().lower() not in (
        "",
        "0",
        "false",
        "no",
    )


def review_fake(
    model: str,
    prompt: str,
    diff: str,
    cwd: Path,
    timeout: int,
    round_no: int = 0,
    effort: str | None = None,
) -> ReviewResult:
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
    prompt = _prompt_with_effort(prompt, effort)
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
        write_sidecar_log(
            backend="fake",
            round_no=round_no,
            argv0=f"fake:{model}",
            returncode=0,
            stdout=body,
            stderr="",
        )
    except OSError as exc:
        print(
            f"[review-cli] fake sidecar log skipped: {exc}", file=sys.stderr, flush=True
        )
    return ReviewResult(
        model=model, command=f"fake:{model}", returncode=0, stdout=body, stderr=""
    )


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
    if lowered in ("zai", "z.ai", "zhipu", "glm") or lowered.startswith(
        ("zai:", "z.ai:", "glm:", "zhipu:")
    ):
        return review_zai
    # commandcode — Command Code's OpenAI-compatible Provider API (keyed HTTP).
    # The legacy `common-code`/`common_code` spellings still route here as aliases so
    # any pre-rename config keeps working.
    if lowered in (
        "commandcode",
        "command-code",
        "command_code",
        "common-code",
        "commoncode",
        "common_code",
    ) or lowered.startswith(
        (
            "commandcode:",
            "command-code:",
            "command_code:",
            "common-code:",
            "commoncode:",
            "common_code:",
        )
    ):
        return review_commandcode
    # OpenRouter — OpenAI-compatible API aggregator (keyed HTTP). `openrouter` plus
    # `openrouter:<provider>/<model>` (e.g. openrouter:anthropic/claude-3.5-sonnet). Does NOT
    # collide with opencode's `oc:`/`opencode:` prefixes — `openrouter:` shares no prefix with
    # either, so the order relative to the opencode catch-all below is irrelevant.
    if lowered == "openrouter" or lowered.startswith("openrouter:"):
        return review_openrouter
    # Oh My Pi (omp) — agentic read-only CLI seat (review-cli#174). `omp` plus
    # `omp:<provider>/<model>` (e.g. omp:kimi-code/k3); the selector after `omp:` goes
    # to omp's `--model` fuzzy matcher verbatim. `omp:` shares no prefix with `oc:`/
    # `opencode:`, but an explicit branch keeps the seat off the catch-all fallthrough.
    if lowered == "omp" or lowered.startswith("omp:"):
        return review_omp
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
            lowered = lowered[len(prefix) :]
            break
    if lowered.startswith("fable"):
        return "claude"
    # The provider is the first segment before either a `:` (keyed-HTTP `provider:model`)
    # or a `/` (opencode `provider/model`), whichever comes first.
    for sep in (":", "/"):
        if sep in lowered:
            lowered = lowered.split(sep, 1)[0]
    return _canonical_provider(lowered)


# ---------------------------------------------------------------------------
# opencode per-provider auth probe helpers (review-cli#94)
# ---------------------------------------------------------------------------

# Env var names that override the default opencode file paths in tests.
_OC_AUTH_FILE_ENV = "OC_AUTH_FILE"
_OC_CONFIG_FILE_ENV = "OC_CONFIG_FILE"

# Providers that run locally and need no API key.
_OC_LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio"})

# Env vars for KNOWN providers.  Kept deliberately minimal — only providers
# whose env var name is well-established and unlikely to change.  Unknown
# providers fall through to a conservative True (opencode may handle them).
_OC_PROVIDER_ENV_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    # commandcode is an opencode custom provider in this ecosystem. Deliberately do NOT
    # treat review-cli's COMMANDCODE_API_KEY as opencode auth: that key powers the direct
    # diff-only `commandcode:` REST backend, while `oc:commandcode/...` must be configured in
    # opencode itself. Listing it here with no env aliases makes missing opencode provider
    # auth a startup skip instead of a long opencode failure.
    "commandcode": (),
    "fireworks": ("FIREWORKS_API_KEY",),
    # z.ai is likewise an opencode provider for `oc:zai/...`; REVIEW/ZAI_API_KEY powers
    # only the direct diff-only `zai:` REST backend and must not make opencode seats live.
    "zai": (),
    "google": ("GOOGLE_AI_API_KEY", "GOOGLE_GENERATIVE_AI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
}

_UNPAID_PROVIDERS_ENV = "REVIEW_UNPAID_PROVIDERS"
_CONFIG_UNPAID_PROVIDERS: frozenset[str] = frozenset()
_PROVIDER_ALIASES = {
    "command-code": "commandcode",
    "command_code": "commandcode",
    "common-code": "commandcode",
    "commoncode": "commandcode",
    "common_code": "commandcode",
    "cc": "commandcode",
    "gemini-api": "gemini",
    "claude-p": "claude",
    "claude-fable-5": "claude",
    "z.ai": "zai",
    "zhipu": "zai",
    "glm": "zai",
}


def _canonical_provider(provider: str) -> str:
    return _PROVIDER_ALIASES.get(provider.strip().lower(), provider.strip().lower())


def _parse_provider_names(raw: object) -> frozenset[str]:
    """Normalize config/env provider lists (`["commandcode"]` or "a,b")."""
    if raw is None:
        return frozenset()
    parts: list[str] = []
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        for item in raw:
            if isinstance(item, str):
                parts.extend(item.split(","))
    return frozenset(_canonical_provider(part) for part in parts if part.strip())


def configure_unpaid_providers(raw: object) -> None:
    """Set per-process providers whose billing/entitlement is currently unavailable.

    `reviewlib.backends` intentionally does not import the YAML config loader. The CLI calls
    this after `load_config()`; tests and non-CLI callers can use REVIEW_UNPAID_PROVIDERS.
    """
    global _CONFIG_UNPAID_PROVIDERS
    _CONFIG_UNPAID_PROVIDERS = _parse_provider_names(raw)


def unpaid_providers() -> frozenset[str]:
    """Providers skipped before dispatch because their subscription/billing is unavailable."""
    return _CONFIG_UNPAID_PROVIDERS | _parse_provider_names(
        os.environ.get(_UNPAID_PROVIDERS_ENV)
    )


def provider_marked_unpaid(model: str) -> bool:
    """True when `model` routes through a provider disabled by the payment/entitlement list."""
    return effective_provider(model) in unpaid_providers()


def _runtime_unpaid_provider(model: str) -> str | None:
    provider = (
        _claude_runtime_gateway_provider()
        if resolve_backend(model) is review_claude
        else None
    )
    return _matched_unpaid_provider(model, provider)


def runtime_provider_marked_unpaid(model: str) -> bool:
    """True when a backend invocation for `model` would use a disabled provider."""
    return _runtime_unpaid_provider(model) is not None


def runtime_unpaid_provider_error(model: str) -> str:
    return unpaid_provider_error(model, _runtime_unpaid_provider(model))


def _matched_unpaid_provider(model: str, provider: str | None = None) -> str | None:
    providers = []
    if provider:
        providers.append(_canonical_provider(provider))
    providers.append(effective_provider(model))
    disabled = unpaid_providers()
    for candidate in providers:
        if candidate in disabled:
            return candidate
    return None


def unpaid_provider_error(model: str, provider: str | None = None) -> str:
    provider = _matched_unpaid_provider(model, provider) or effective_provider(model)
    return (
        f"provider '{provider}' is marked unpaid/disabled "
        f"({_UNPAID_PROVIDERS_ENV} or config.yaml unpaid_providers); skipping {model}"
    )


def unpaid_provider_result(
    model: str,
    *,
    backend: str,
    command: str,
    round_no: int = 0,
    started: datetime | None = None,
    provider: str | None = None,
) -> ReviewResult | None:
    """Return a skipped result for an unpaid provider, emitting the usual live-log sidecar."""
    provider = _matched_unpaid_provider(model, provider)
    if provider is None:
        return None
    stderr = unpaid_provider_error(model, provider)
    try:
        _emit_rest_log(
            backend,
            command,
            stdout="",
            stderr=stderr,
            returncode=1,
            round_no=round_no,
            started=started,
        )
    except OSError as exc:
        print(
            f"[review-cli] unpaid-provider sidecar log skipped: {exc}",
            file=sys.stderr,
            flush=True,
        )
    return ReviewResult(
        model=model, command=command, returncode=1, stdout="", stderr=stderr
    )


_PAYMENT_PREFLIGHT_PROVIDERS = frozenset({"commandcode", "fireworks"})
_PAYMENT_PREFLIGHT_MARKER_CODES = frozenset({400, 401, 403})
_PAYMENT_PREFLIGHT_MARKERS = (
    "account disabled",
    "account suspended",
    "insufficient balance",
    "insufficient credit",
    "insufficient credits",
    "payment required",
    "subscription required",
    "unpaid",
)
_PAYMENT_PREFLIGHT_PROVIDER_WIDE_MARKERS = (
    "account disabled",
    "account suspended",
    "insufficient balance",
    "insufficient credit",
    "insufficient credits",
    "payment required",
    "unpaid",
)
_FIREWORKS_DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"
_PROVIDER_PREFLIGHT_TIMEOUT_SECONDS = 2.0
_PAYMENT_PREFLIGHT_PROVIDER_SCOPE = "*"
_PAYMENT_PREFLIGHT_CACHE: dict[tuple[str, str, str, str], tuple[bool, int | None]] = {}
_PAYMENT_PREFLIGHT_CACHE_LOCK = threading.Lock()


def _provider_preflight_url(provider: str) -> str | None:
    if provider == "commandcode":
        base = os.environ.get("COMMANDCODE_BASE_URL") or COMMANDCODE_DEFAULT_BASE_URL
    elif provider == "fireworks":
        base = os.environ.get("FIREWORKS_BASE_URL") or _FIREWORKS_DEFAULT_BASE_URL
    else:
        return None
    return base.rstrip("/") + "/models"


def _oc_auth_provider_key(provider: str) -> str | None:
    try:
        data = json.loads(_oc_auth_file().read_text())
        value = data.get(provider, {}).get("key", "")
        return value if isinstance(value, str) and value.strip() else None
    except Exception:  # noqa: BLE001
        return None


def _oc_config_provider_key(provider: str) -> str | None:
    key, _base = _oc_config_provider_credentials(provider)
    return key


def _oc_config_provider_credentials(provider: str) -> tuple[str | None, str | None]:
    try:
        data = json.loads(_oc_config_file().read_text())
        opts = data.get("provider", {}).get(provider, {}).get("options", {})
        value = opts.get("apiKey", "")
        key = value if isinstance(value, str) and value.strip() else None
        base_value = (
            opts.get("baseURL") or opts.get("baseUrl") or opts.get("base_url") or ""
        )
        base = base_value.strip() if isinstance(base_value, str) else ""
        return key, (base or None)
    except Exception:  # noqa: BLE001
        return None, None


def _oc_env_provider_key(provider: str) -> str | None:
    for var in _OC_PROVIDER_ENV_VARS.get(provider, ()):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return None


def _oc_provider_key(provider: str) -> str | None:
    return (
        _oc_auth_provider_key(provider)
        or _oc_config_provider_key(provider)
        or _oc_env_provider_key(provider)
    )


def _models_url_from_base(base_url: str | None, provider: str) -> str | None:
    if not base_url:
        return _provider_preflight_url(provider)
    return base_url.rstrip("/") + "/models"


def _payment_preflight_credentials(
    model: str, provider: str
) -> tuple[str | None, str | None]:
    if provider == "commandcode":
        if model.lower().startswith(("oc:", "opencode:")):
            config_key, config_base = _oc_config_provider_credentials(provider)
            auth_key = _oc_auth_provider_key(provider)
            if auth_key:
                return auth_key, _models_url_from_base(config_base, provider)
            if config_key:
                return config_key, _models_url_from_base(config_base, provider)
            env_key = _oc_env_provider_key(provider)
            return env_key, _models_url_from_base(config_base, provider)
        try:
            return _commandcode_key(), _provider_preflight_url(provider)
        except RuntimeError:
            return None, None
    if provider == "fireworks":
        config_key, config_base = _oc_config_provider_credentials(provider)
        auth_key = _oc_auth_provider_key(provider)
        if auth_key:
            return auth_key, _models_url_from_base(config_base, provider)
        if config_key:
            return config_key, _models_url_from_base(config_base, provider)
        env_key = _oc_env_provider_key(provider)
        return env_key, _models_url_from_base(config_base, provider)
    return None, None


def _payment_preflight_cache_key(
    provider: str,
    url: str,
    key: str,
    scope: str = _PAYMENT_PREFLIGHT_PROVIDER_SCOPE,
) -> tuple[str, str, str, str]:
    # Keep the cache key credential-sensitive without storing the credential itself.
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return (provider, url, key_hash, scope)


def _is_payment_preflight_denial(code: int | None, body: str) -> bool:
    if code == 402:
        return True
    lowered = body.lower()
    return code in _PAYMENT_PREFLIGHT_MARKER_CODES and any(
        marker in lowered for marker in _PAYMENT_PREFLIGHT_MARKERS
    )


def _payment_denial_cache_scope(model: str, code: int | None, body: str) -> str:
    if code == 402:
        return _PAYMENT_PREFLIGHT_PROVIDER_SCOPE
    lowered = body.lower()
    if any(marker in lowered for marker in _PAYMENT_PREFLIGHT_PROVIDER_WIDE_MARKERS):
        return _PAYMENT_PREFLIGHT_PROVIDER_SCOPE
    return model


def _payment_preflight_reason(provider: str, model: str, code: int | None) -> str:
    suffix = f"HTTP {code}" if code is not None else "payment/availability denial"
    return f"provider '{provider}' failed payment/availability preflight ({suffix}); skipping {model}"


def _cache_payment_preflight_denial(
    model: str, code: int | None, body: str = ""
) -> None:
    provider = effective_provider(model)
    if provider not in _PAYMENT_PREFLIGHT_PROVIDERS:
        return
    key, url = _payment_preflight_credentials(model, provider)
    if not key or not url:
        return
    cache_key = _payment_preflight_cache_key(
        provider, url, key, _payment_denial_cache_scope(model, code, body)
    )
    with _PAYMENT_PREFLIGHT_CACHE_LOCK:
        _PAYMENT_PREFLIGHT_CACHE[cache_key] = (True, code)


def _cached_payment_preflight_result(
    provider: str,
    model: str,
    url: str,
    key: str,
) -> tuple[bool, int | None] | None:
    with _PAYMENT_PREFLIGHT_CACHE_LOCK:
        return _PAYMENT_PREFLIGHT_CACHE.get(
            _payment_preflight_cache_key(provider, url, key, model)
        ) or _PAYMENT_PREFLIGHT_CACHE.get(
            _payment_preflight_cache_key(provider, url, key)
        )


def cached_payment_preflight_unavailable_reason(model: str) -> str | None:
    """Return a cached payment/entitlement denial without performing network I/O."""
    provider = effective_provider(model)
    if provider not in _PAYMENT_PREFLIGHT_PROVIDERS:
        return None
    key, url = _payment_preflight_credentials(model, provider)
    if not key or not url:
        return None
    cached = _cached_payment_preflight_result(provider, model, url, key)
    if cached is None:
        return None
    denied, code = cached
    return _payment_preflight_reason(provider, model, code) if denied else None


def _provider_payment_preflight_unavailable_reason(model: str) -> str | None:
    """Return a skip reason when a provider key exists but payment/entitlement is unavailable.

    This is deliberately narrow: only providers with known cheap `/models` entitlement probes
    are checked. Network/DNS/transient failures are conservative-allow so an offline probe
    does not make a working provider disappear; explicit payment/auth/disabled responses are
    conservative-deny so they do not launch chat/opencode calls.
    """
    provider = effective_provider(model)
    if provider not in _PAYMENT_PREFLIGHT_PROVIDERS:
        return None
    key, url = _payment_preflight_credentials(model, provider)
    if not key or not url:
        return None
    cached = _cached_payment_preflight_result(provider, model, url, key)
    if cached is not None:
        denied, code = cached
        return _payment_preflight_reason(provider, model, code) if denied else None
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "review-cli/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROVIDER_PREFLIGHT_TIMEOUT_SECONDS):
            with _PAYMENT_PREFLIGHT_CACHE_LOCK:
                provider_cache_key = _payment_preflight_cache_key(provider, url, key)
                cached = _PAYMENT_PREFLIGHT_CACHE.get(
                    _payment_preflight_cache_key(provider, url, key, model)
                ) or _PAYMENT_PREFLIGHT_CACHE.get(provider_cache_key)
                if cached is not None and cached[0] is True:
                    return _payment_preflight_reason(provider, model, cached[1])
                if cached is None or cached[0] is False:
                    _PAYMENT_PREFLIGHT_CACHE[provider_cache_key] = (False, None)
            return None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if _is_payment_preflight_denial(exc.code, body):
            with _PAYMENT_PREFLIGHT_CACHE_LOCK:
                _PAYMENT_PREFLIGHT_CACHE[
                    _payment_preflight_cache_key(provider, url, key)
                ] = (True, exc.code)
            return _payment_preflight_reason(provider, model, exc.code)
        return None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    except Exception:  # noqa: BLE001
        # Test fakes and unusual urllib failures that are not explicit entitlement
        # denials should not make a provider disappear. Only the HTTP payment/auth
        # shapes above are authoritative enough to skip a seat.
        return None


def provider_preflight_result(
    model: str,
    *,
    backend: str,
    command: str,
    round_no: int = 0,
    started: datetime | None = None,
) -> ReviewResult | None:
    reason = _provider_payment_preflight_unavailable_reason(model)
    if reason is None:
        return None
    try:
        _emit_rest_log(
            backend,
            command,
            stdout="",
            stderr=reason,
            returncode=1,
            round_no=round_no,
            started=started,
        )
    except OSError as exc:
        print(
            f"[review-cli] provider-preflight sidecar log skipped: {exc}",
            file=sys.stderr,
            flush=True,
        )
    return ReviewResult(
        model=model, command=command, returncode=1, stdout="", stderr=reason
    )


def _oc_auth_file() -> Path:
    """Path to opencode's auth.json.  Injectable via OC_AUTH_FILE for tests."""
    env = os.environ.get(_OC_AUTH_FILE_ENV, "").strip()
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode" / "auth.json"


def _oc_config_file() -> Path:
    """Path to opencode's opencode.json.  Injectable via OC_CONFIG_FILE for tests."""
    env = os.environ.get(_OC_CONFIG_FILE_ENV, "").strip()
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "opencode" / "opencode.json"


def _oc_auth_has_provider(provider: str) -> bool:
    """True iff opencode's auth.json carries a non-empty key for *provider*."""
    try:
        data = json.loads(_oc_auth_file().read_text())
        return bool(data.get(provider, {}).get("key", ""))
    except Exception:  # noqa: BLE001
        return False


def _oc_config_has_provider_key(provider: str) -> bool:
    """True iff opencode.json carries an inline options.apiKey for *provider*."""
    try:
        data = json.loads(_oc_config_file().read_text())
        opts = data.get("provider", {}).get(provider, {}).get("options", {})
        return bool(opts.get("apiKey", ""))
    except Exception:  # noqa: BLE001
        return False


def _oc_env_has_provider_key(provider: str) -> bool:
    """True iff a known env var for *provider* is set and non-empty."""
    return any(
        os.environ.get(var, "").strip()
        for var in _OC_PROVIDER_ENV_VARS.get(provider, ())
    )


def _oc_provider_auth_available(provider: str) -> bool:
    """True iff opencode has credentials for *provider* from any source.

    Checked in order: local (no-key) → auth.json → opencode.json → env vars.
    For unknown providers (not in _OC_PROVIDER_ENV_VARS and not local) we fall
    back to True — opencode may carry them through its own mechanism and we have
    no signal to reject them.
    """
    if provider in _OC_LOCAL_PROVIDERS:
        return True
    if _oc_auth_has_provider(provider):
        return True
    if _oc_config_has_provider_key(provider):
        return True
    if _oc_env_has_provider_key(provider):
        return True
    # For a KNOWN provider where every credential source came up empty → unavailable.
    # For an UNKNOWN provider → conservative True (no false negatives).
    return provider not in _OC_PROVIDER_ENV_VARS


# Env var that overrides the omp auth db path in tests (mirrors OC_AUTH_FILE for the
# opencode probe), so the availability tests never touch the real ~/.omp/agent/agent.db.
_OMP_AUTH_DB_ENV = "OMP_AUTH_DB"


def _omp_agent_dir() -> Path:
    """omp's agent storage dir for THIS process (verified against omp v17):
    `PI_CODING_AGENT_DIR` replaces it outright; else `OMP_PROFILE` (= `--profile`)
    isolates state under ~/.omp/profiles/<name>/agent; else the default ~/.omp/agent.
    Shared by the auth probe and the seat launch — the seat runs with a SANITIZED HOME
    and pins this explicitly so auth still resolves there."""
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    if agent_dir:
        # Absolutize (after expanding any `~`): a RELATIVE override would resolve
        # against a different cwd in the seat's sandbox than in the probe's process
        # (review of #174).
        return Path(os.path.abspath(os.path.expanduser(agent_dir)))
    profile = os.environ.get("OMP_PROFILE", "").strip()
    if profile:
        return Path.home() / ".omp" / "profiles" / profile / "agent"
    return Path.home() / ".omp" / "agent"


def _omp_auth_db() -> Path:
    """The auth db omp ITSELF would use for this process (`_omp_agent_dir` + agent.db;
    codex review of #174 — probing any OTHER db would misreport a runnable
    authenticated CLI as unauthenticated). The OMP_AUTH_DB test override wins first."""
    override = os.environ.get(_OMP_AUTH_DB_ENV, "").strip()
    if override:
        return Path(override)
    return _omp_agent_dir() / "agent.db"


def _omp_provider_from_model(model: str) -> str | None:
    """Extract the provider an `omp:` seat authenticates against.

    Returns None for a bare 'omp' (binary check + any usable credential is enough).
    'omp:kimi-code/k3' → 'kimi-code'; 'omp:openai/gpt-5.5' → 'openai'.
    """
    lowered = model.lower()
    if not lowered.startswith("omp:"):
        return None
    selector = lowered[len("omp:") :]
    return selector.split("/", 1)[0] or None


# In-process memo for the omp auth probe (glm review of #174): `--show-board` and the
# pool guard probe EVERY omp seat, and a naive probe re-opens the same sqlite db per
# seat. The memo is keyed on the db's (main, -wal) mtimes — WAL writes (a mid-process
# `omp` login/logout) may not touch the main db file — so a credential change is still
# picked up on the very next probe, and one stamp miss hydrates ALL live providers in a
# single read, so N omp seats genuinely cost ONE db read per board pass. A FAILED probe
# (locked/corrupt db) is NOT cached — the next probe retries instead of serving a stale
# "unauthenticated" until the db file happens to change.
_OMP_AUTH_CACHE: dict[str, tuple[tuple[int, int], frozenset[str]]] = {}


def _omp_db_stamp(db: Path) -> tuple[int, int] | None:
    """The (main, -wal) mtime pair for the auth db, or None when the db does not exist."""
    try:
        main = db.stat().st_mtime_ns
    except OSError:
        return None
    try:
        wal = db.with_name(db.name + "-wal").stat().st_mtime_ns
    except OSError:
        wal = 0
    return (main, wal)


def _omp_auth_probe(db: Path) -> frozenset[str] | None:
    """One OFFLINE read of omp's auth db: the set of providers with a NON-disabled
    credential row (`auth_credentials.disabled_cause` marks dead ones), or None when the
    probe itself failed (missing/corrupt db, schema drift, a locked file, a python build
    without sqlite3) — callers treat None as 'unauthenticated' but must NOT cache it.
    omp keeps OAuth/API credentials in `auth_credentials` keyed by `provider`. The db is
    opened READ-ONLY via a file: URI so the probe can never create or mutate it. The
    imports stay inside the try (lazy AND failure-proof; repeat imports are a
    sys.modules lookup, so this costs nothing per probe)."""
    try:
        import sqlite3  # noqa: PLC0415
        from urllib.parse import quote  # noqa: PLC0415

        # Read-only offline probe: a long lock wait is pointless here, and a short one
        # bounds the worst case to N seats x 0.5s on a contended db (glm review of #174).
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=0.5)
        try:
            rows = conn.execute(
                "SELECT DISTINCT provider FROM auth_credentials WHERE disabled_cause IS NULL"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — any sqlite/schema failure reads as unauthenticated
        return None
    return frozenset(row[0] for row in rows if isinstance(row[0], str))


def _omp_auth_available(provider: str | None) -> bool:
    """Cheap OFFLINE omp auth probe: True iff the seat's provider has a usable
    credential (or ANY provider does, for a bare `omp` seat). One stamp miss hydrates
    every live provider in a single db read (`_OMP_AUTH_CACHE`), so probing N omp seats
    costs one db read per board pass; see `_omp_auth_probe` for the read itself."""
    db = _omp_auth_db()
    stamp = _omp_db_stamp(db)
    if stamp is None:
        return False
    key = str(db)
    entry = _OMP_AUTH_CACHE.get(key)
    if entry is None or entry[0] != stamp:
        live = _omp_auth_probe(db)
        if live is None:
            return False  # probe failure — deliberately NOT cached (retry next time)
        entry = (stamp, live)
        _OMP_AUTH_CACHE[key] = entry
    providers = entry[1]
    return bool(providers) if provider is None else provider in providers


def _oc_provider_from_model(model: str) -> str | None:
    """Extract the underlying provider from an oc:/opencode: model string.

    Returns None for a bare 'opencode' (no provider-scoped prefix — binary check only).
    'oc:anthropic/claude-3-5-sonnet' → 'anthropic';
    'opencode:deepseek/deepseek-v3' → 'deepseek';
    'opencode' → None.
    """
    lowered = model.lower()
    if not lowered.startswith(("oc:", "opencode:")):
        return None
    return effective_provider(lowered)


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


def provider_route_name(model: str) -> str:
    """Canonical backend-route name for a model id, reusing `resolve_backend`.

    Maps a model string to the name of the backend it actually runs on — `codex`,
    `claude`, `gemini`, `opencode` (incl. every `oc:`/unknown-fallthrough seat),
    `commandcode`, `zai`, `openrouter`. This is the route KEY a run-scoped `--effort`
    override matches on (config.EffortOverride) and the same mapping the visual path uses
    (`features/visual/vision_client._route_name`)."""
    return {
        review_claude: "claude",
        review_gemini: "gemini",
        review_codex: "codex",
        review_opencode: "opencode",
        review_omp: "omp",
        review_commandcode: "commandcode",
        review_zai: "zai",
        review_openrouter: "openrouter",
    }.get(resolve_backend(model), "opencode")


def is_known_backend_token(token: str) -> bool:
    """Whether a bare token names a backend review RECOGNISES — as opposed to falling
    through `resolve_backend`'s opencode catch-all. opencode itself (`opencode`/`oc`, or an
    `opencode:`/`oc:` prefix) counts as recognised; an unknown word does NOT.

    Used to validate a user-supplied `--effort <provider>=<level>` token so a typo
    (`claud=high`) fails loudly instead of silently landing on the opencode route."""
    lowered = token.strip().lower()
    if lowered in ("opencode", "oc") or lowered.startswith(("opencode:", "oc:")):
        return True
    return _match_named_backend(lowered) is not None


def backend_available(model: str) -> bool:
    """Cheap availability probe so moderator selection never picks a dead backend.

    Thin boolean over `backend_unavailable_reason` (the single source of truth for BOTH
    "is it live?" and "why not?") so the two can never drift."""
    return backend_unavailable_reason(model) is None


def backend_unavailable_reason(model: str) -> str | None:
    """Human-readable reason `model`'s backend is NOT live right now, or None if it is.

    Mirrors `backend_available`'s probe exactly but surfaces WHY a seat is dead so the
    pool-selection guard (reviewlib.pool_guard) can annotate every printed model list with
    per-seat health (live / down + reason). Never raises: a probe RuntimeError (missing
    key/CLI) becomes its message; unpaid/paywalled providers get their dedicated reason."""
    # TEST-ONLY: under the fake backend EVERY model is reachable (it is faked in-process,
    # no key/CLI needed). Without this the panel/moderator selection would prune the faked
    # models as "unavailable" on a host lacking the real CLIs (e.g. CI), defeating the e2e.
    if _fake_backend_enabled():
        return None
    if runtime_provider_marked_unpaid(model):
        return runtime_unpaid_provider_error(model)
    preflight = cached_payment_preflight_unavailable_reason(model)
    if preflight is not None:
        return preflight
    backend = resolve_backend(model)
    try:
        if backend is review_gemini:
            _gemini_key()
            return None
        if backend is review_zai:
            # Honor a forced mode: REVIEW_ZAI_MODE=cli makes review_zai a dead
            # backend, so it must NOT report available (resolve_backend_mode raises
            # on the unsupported mode → caught below → reason). Codex P2.
            resolve_backend_mode("zai", ZAI_SUPPORTED_MODES, "api")
            _zai_key()
            return None
        if backend is review_commandcode:
            # Same as z.ai: a forced REVIEW_COMMANDCODE_MODE=cli is unrunnable, so the
            # probe must reflect that instead of selecting a backend that only fails.
            resolve_backend_mode("commandcode", COMMANDCODE_SUPPORTED_MODES, "api")
            _commandcode_key()
            return None
        if backend is review_openrouter:
            # Same api-only contract: a forced REVIEW_OPENROUTER_MODE=cli is unrunnable, so
            # the probe reports unavailable rather than selecting a backend that only fails.
            resolve_backend_mode("openrouter", OPENROUTER_SUPPORTED_MODES, "api")
            _openrouter_key()
            return None
        if backend is review_codex:
            _which("codex")
            return None
        if backend is review_claude:
            # Mirror the dispatcher so availability matches what would actually run:
            # a forced mode is available only via that one variant; otherwise it's
            # available via EITHER the API key or the claude CLI (the `claude` print
            # binary or the legacy `claude-p` wrapper — review-cli#76).
            mode = os.environ.get("REVIEW_CLAUDE_MODE", "").strip().lower()
            if mode == "api":
                if _claude_api_available_for_model(model):
                    return None
                return "claude: no API key for this model (REVIEW_CLAUDE_MODE=api)"
            if mode == "cli":
                if _have_claude_cli():
                    return None
                return "claude: CLI not found on PATH (REVIEW_CLAUDE_MODE=cli)"
            if _have_claude_cli():
                return None
            if _claude_api_available_for_model(model):
                return None
            return "claude: no `claude` CLI on PATH and no API key"
        if backend is review_opencode:
            _which("opencode")
            provider = _oc_provider_from_model(model)
            if provider is None:
                # Bare 'opencode' (no oc: prefix) — binary check is sufficient.
                return None
            if _oc_provider_auth_available(provider):
                return None
            return f"opencode: provider {provider!r} not authenticated (run `opencode auth login`)"
        if backend is review_omp:
            # Same contract as z.ai/commandcode: a forced REVIEW_OMP_MODE=api is
            # unrunnable, so the probe reports unavailable instead of selecting a
            # backend that only fails (the raise is caught below → reason).
            resolve_backend_mode("omp", OMP_SUPPORTED_MODES, "cli")
            _which("omp")
            provider = _omp_provider_from_model(model)
            if _omp_auth_available(provider):
                return None
            if provider is None:
                return "omp: no usable credentials in ~/.omp/agent/agent.db (run `omp setup`)"
            return (
                f"omp: provider {provider!r} not authenticated "
                f"(run `omp setup`; check `omp token {provider}`)"
            )
    except RuntimeError as exc:
        return str(exc)
    return "backend unavailable"
