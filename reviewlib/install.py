"""install-skill / install-commit-hook / review-stamp machinery.

Extracted verbatim from the original single-file `bin/review` (Stage 0
decomposition — zero behaviour change).

install-skill: make agent harnesses aware this tool exists. Writes a SKILL.md
(Agent Skills standard, ~/.agents/skills/) read by Claude Code, Codex, opencode,
Gemini, Cursor; a short always-on blurb into each detected harness's global
instruction file; and an idempotent SessionStart hook that surfaces every
installed agent-CLI at the top of each session. `install-commit-hook` adds the
optional hard review-before-commit gate.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SKILL_NAME = "review"
SKILL_MD = """\
---
name: review
description: >-
  Read-only multi-model code review and AI panel. Modes are SUBCOMMANDS. Use BEFORE
  committing to check a diff across several models at once (`review diff --task CODE`), to get a
  multi-model second opinion on a question (`review just-ask "Q" --task CODE`), to settle a
  contested technical decision with cited evidence (`review quorum "Q" --task CODE`), or to
  brainstorm an open design space across rotating expert personas in a loop
  (`review brainstorm "TOPIC" --task CODE`). e.g. `review diff --task CODE` on the current diff.
metadata:
  author: alex-mextner
  repo: https://github.com/alex-mextner/review-cli
---

# review — multi-model read-only code review + panels

Runs your git diff (or a question/topic) across several model backends in parallel.

## NEVER wrap `review` in a short timeout — it takes MINUTES
`review diff`, `review quorum`, and `review brainstorm` are multi-model and/or
multi-round: they fan out to several model backends in parallel, and the panel
modes run several rounds plus a final moderator synthesis. A plain diff review of a
full board is typically a few minutes; a `brainstorm` is commonly 10–20 minutes
(min 3 / max 8 rounds + a final synthesis pass). Wrapping the command in
`timeout 60` / `timeout 300` (or any short cap) KILLS the run before it finishes —
a brainstorm only emits its synthesis at the very end, so a short timeout produces
NOTHING usable, not a partial result. So:

- Do NOT put `review` behind a short shell `timeout`, and do NOT set a short
  per-call deadline expecting a quick return. Let it run.
- At dispatch, `review` prints a one-line ETA to stderr for YOUR pool size and
  mode, e.g. `[review] pool=4 (brainstorm) — typically ~6m12s based on 12 past
  runs of this size; do NOT timeout.` Read that line and wait at least that long.
  With no history yet it still warns it's multi-round and to expect minutes.
- Use NO external timeout at all — not even a generous one. You do NOT need to bound
  `review` yourself: it carries its own internal <=4h backstop (next section), so just
  let it run (background it and poll if you want progress). Any external cap only risks
  killing a good run before its synthesis and adds nothing the backstop doesn't already
  guarantee.

## No external timeout — `review` carries its OWN internal <=4h backstop
Do NOT put ANY external timeout on `review`. It is designed to run unbounded from
the outside; the ONLY time bound is an INTERNAL last-resort backstop of <=4h that the
binary arms itself (a watchdog that force-terminates a genuinely wedged run with exit
124). So a healthy run never needs an external cap — it finishes in minutes, far under
the ceiling — and a stuck run can't run forever either. An external `timeout` only
ever does harm here: it kills good runs before their synthesis and adds nothing the
internal backstop doesn't already guarantee. `$REVIEW_BACKSTOP_SECONDS` can only LOWER
the internal ceiling, never raise it past 4h.

## Invocation — modes are SUBCOMMANDS
Everything is a subcommand: the diff review is `review diff` (NOT a bare `review`). A bare
`review` prints the help. The other modes: `brainstorm` / `just-ask` / `quorum`.
```
review diff --task CODE -C <repo>                  # review current unstaged diff across the failover pool (top 2 available; --preset default -> 4)
review diff --task CODE -C <repo> --staged         # review the staged diff (pre-commit)
review diff --task CODE -C <repo> --pool 0         # run all available seats in the selected preset/board
review diff --task CODE -C <repo> -m codex -m gemini    # pick backends (repeat or comma-separate); narrows config board metadata if present
review just-ask "Q" --task CODE -C <repo>          # multi-model answer to a question (no diff needed)
review quorum "Q" --task CODE -C <repo>            # experts answer + a moderator finds consensus/disagreement
review brainstorm "TOPIC" --task CODE -C <repo>    # iterative persona ideation in a loop, with a moderator
review brainstorm "TOPIC" --task CODE --diff -C <repo>   # …+ the working-tree (or --staged) diff -> brainstorm ABOUT that change
review diff --task CODE -C <repo> -o out.md        # write the result to a file (still prints to stdout)
review task CODE                                   # show iterations, models, and transcripts for this task
```
A bare `review` (no subcommand) prints HELP — it does NOT run a diff review (use
`review diff`). The removed `review review` verb and `review -C <repo>` (flags with no
verb) print a one-line `review diff` pointer and exit non-zero. The OLD mode flags
(`--brainstorm` / `--quorum` / `--just-ask`) were likewise REMOVED. `--visual <img>` stays
the canonical standalone command is `review visual IMAGE`; `--visual` remains a composable
flag for text modes, e.g. `review brainstorm "Q" --task CODE --visual IMAGE`.
For configuration (config file, model/board selection, keys/auth) run `review help config`
(alias `review --help config`); per-subcommand flags are on `review <mode> --help`.

## Task code is REQUIRED for review iterations
Pass `--task CODE` on every recorded review mode, or set `REVIEW_TASK_CODE=CODE` in the
environment for a whole automation/session. Standalone `review visual IMAGE` is the only
normal exception; `review visual IMAGE --diff` is a diff-review iteration and still needs
the task code. The code is written to run-stats and per-call logs so `review task CODE` and
the dashboard can show how many iterations ran, which models were used, and the detailed
conversations.

## Save the result to a file: `-o FILE`, NOT `> FILE`
Use `review diff --task CODE -C <repo> -o out.md`, NOT `review diff -C <repo> … > out.md`. Under zsh
`noclobber` (a common shell default), `> out.md` REFUSES to overwrite an existing
file and the command dies silently — you get no review and no error. `-o` writes
the result with Python (`open(...,"w")`), which bypasses the shell redirect (and
thus noclobber) entirely: it creates parent dirs, always overwrites, and STILL
prints the result to stdout so you see it live. So whenever you want the review in
a file, reach for `-o file.md`, never `> file.md`.

## Reviewer board, presets, and `--pool` (priority-ordered failover pool)
A plain `review diff` runs the **light preset**: pool 2, medium effort, no Fable/Sol.
Use `--preset default` for a routine change review (pool 4, high effort), and
`--preset heavy` for release/risky changes (Sol, Opus, GLM-cc, Kimi at highest effort,
with the remaining board seats as highest-effort reserve). Fable is excluded from
every preset (a confirmed ~100% dispatch failure rate) and sits last-resort in the raw
board instead. The built-in reviewer board is
a **priority-ordered** panel where each model also gets its own role/lens. The active pool
is chosen by **priority + availability** with two failovers so the run keeps its requested
reviewer count: **startup failover** picks the top N AVAILABLE seats by priority (a
higher-priority but unavailable seat is skipped, the next pulled up); **mid-run failover**
replaces a seat that fails DURING the run (backend error, timeout, empty output, or an
"unavailable" reply such as a paywalled model) with the next-priority **reserve**, until
the requested number of working verdicts is produced or the reserve is exhausted (then it
degrades and says so).
Before promoting a reserve, a failed seat is first **retried on the same model** when the
failure is **transient** (429 rate-limit / 529 or 5xx overload / timeout / "overloaded" /
"service unavailable") with backoff + jitter; a **seat-fatal** failure (auth / bad model /
501 / refusal) is never retried and falls straight to the reserve. `--retry N` (or
`$REVIEW_RETRY_COUNT`; default 2, `0` disables) sizes the in-seat retry budget.
`--pool N` sizes the pool (top-N available, same failover); `--pool 0` runs all available
seats in the selected preset/board (`--preset heavy --pool 0` covers all 9
heavy-preset-built-ins; the raw 10-seat board, incl. last-resort Fable, needs an
explicit `board:`).
The board is **never disabled** — there is **no `--no-board` flag**. An explicit `-m`
always limits the run to exactly those models; with no configured `models:`/`board:` it is
the legacy flat panel unless an explicit preset supplies metadata, and with config present it
narrows the configured board metadata to those requested models. A `models:` list in config.yaml is the priority roster for the
failover board: the pool is selected from that ordered set and the rest are reserve. Command
Code and Fireworks run a cheap payment/entitlement preflight when a key is present; a
provider that is authenticated but not paid/entitled is skipped before any backend
process/API call. You can also list disabled providers in config.yaml `unpaid_providers:`
or set `REVIEW_UNPAID_PROVIDERS=commandcode,fireworks`.
`review --show-board` lists the seats in priority order with their pool/reserve/unavail
tier and availability.

The default repo-capable gateway seats are agentic `oc:` routes, not diff-only REST:
`oc:commandcode/moonshotai/Kimi-K2.7-Code`, `oc:commandcode/Qwen/Qwen3.7-Max`,
`oc:commandcode/deepseek/deepseek-v4-pro`, and `oc:zai/glm-5.2`. The keyed-HTTP
`commandcode:` / `zai:` routes remain for explicit `-m cc` / `-m glm` and custom config.

## `brainstorm` can take a diff
`review brainstorm "<topic>"` is multi-round persona ideation. When there IS a diff
present — the working-tree diff under `-C` (pass `--diff`), a `--staged` diff, or a piped
diff — every persona (and the moderator) sees it as grounding context, so you brainstorm
concretely ABOUT that change. With no diff it stays pure ideation. The diff is optional:
an absent diff / non-repo `-C` degrades silently to ideation.

## ALWAYS pass `-C <project-root>`
`review diff` runs the diff and the claude/opus workspace in `-C` (default: the current
directory). Agents often invoke `review` from a scratch or temp dir, so WITHOUT
`-C` it silently reviews the wrong place (commonly /tmp) and returns an empty or
irrelevant result. Always pass `-C <absolute repo path>`. If `-C` is not inside a
git repo, review resolves to the repo root when it can and otherwise prints a
loud warning — heed it. When piping into review non-interactively, also redirect
stdin (`review just-ask "Q" --task CODE -C <repo> < /dev/null`); review reads stdin for an
optional piped diff and will hang waiting for EOF if stdin is an open pipe.

## claude / opus backend: API or CLI
The `claude:` / opus backend runs two ways, so it works whether or not the
`claude` CLI is installed:
- **API** (no CLI needed): set `ANTHROPIC_API_KEY` (sent as `x-api-key`) or
  `ANTHROPIC_AUTH_TOKEN` (sent as `Authorization: Bearer`), and optionally
  `ANTHROPIC_BASE_URL` for an Anthropic-compatible gateway (default
  `https://api.anthropic.com`). Same vars the Anthropic SDK / claude CLI read.
- **CLI**: the `claude-p` wrapper (needs the `claude` binary; subscription).
- **Selection**: `REVIEW_CLAUDE_MODE=api|cli` forces it; otherwise it auto-picks
  the CLI when the `claude` binary is present (subscription, no API cost) and the
  API only when there's no binary but a key is set. Set `REVIEW_CLAUDE_MODE=api`
  to force the API even on a host that has the CLI.

## Transport mode (api | cli) per backend
Every backend declares which transports it supports and reads the SAME selector
shape `REVIEW_<BACKEND>_MODE` (the one PR #8 introduced for claude):
- **claude** — both `api` and `cli` (auto-picks; see above).
- **codex / opencode** — `cli` only (agent CLIs that carry their own auth). Both
  are AGENTIC: they run in the real `-C` repo READ-ONLY and can open any project
  file, not just the diff — so an `oc:<provider>/<model>` seat reads the whole repo
  (opencode's read-only-reviewer agent denies edit/write/bash, so it never mutates).
- **z.ai, commandcode** — `api` only (no such CLI exists). Forcing `cli` on an
  api-only backend (`REVIEW_COMMANDCODE_MODE=cli`) is a hard error, not a silent
  fall-through to the API.

## Keyed HTTP backends: z.ai (GLM) and commandcode
OpenAI-compatible `POST /chat/completions` REST backends — **diff-only** (no workspace),
no CLI, just a key. NOTE: the DEFAULT board's Kimi/GLM/Qwen/DeepSeek seats are now the
**agentic** `oc:commandcode/...` / `oc:zai/glm-5.2` opencode routes (they read the repo and
authenticate via opencode, not these keys). These keyed-HTTP backends back the **explicit**
`-m cc` / `-m glm` invocations and `commandcode:`/`zai:` config-board seats:
- **z.ai (Zhipu / GLM)**: `-m zai` / `-m glm` (newest, glm-5.2) — or a pinned id
  `-m glm52`/`-m glm51`/`-m glm47`/`-m glm46`; `-m zai:<model>` for an explicit one.
  Key: `ZAI_API_KEY` (or `ZHIPU_API_KEY`). Base/model override: `ZAI_BASE_URL` /
  `ZAI_MODEL`. DEFAULT base is the GLM Coding-Plan endpoint
  `https://api.z.ai/api/coding/paas/v4` (the only one that serves glm-5.2; the
  standard `…/api/paas/v4` tops out at glm-5.1), default model `glm-5.2`. Standard-plan
  users set `ZAI_BASE_URL=https://api.z.ai/api/paas/v4` and a model their plan serves.
- **commandcode**: `-m commandcode` (alias `-m cc`; `-m commandcode:<model>` for an
  explicit model). Hits Command Code's Provider API
  (`https://api.commandcode.ai/provider/v1/chat/completions`). Key:
  `COMMANDCODE_API_KEY` ONLY — a `user_...` token. (No alias key names: a DeepSeek key
  is NOT a commandcode key, so accepting it would leak that credential to the wrong
  host.) Base/model override: `COMMANDCODE_BASE_URL` / `COMMANDCODE_MODEL`
  (default model `deepseek/deepseek-v4-flash`). NOTE: that endpoint serves OpenAI/OSS
  models; Anthropic (Claude) models on Command Code go through the claude backend
  (`REVIEW_CLAUDE_MODE=api`, `ANTHROPIC_BASE_URL=https://api.commandcode.ai/provider`).
All three resolve their key from the env first, then the shared
`~/.config/review-cli/.env` (the same file the gemini key uses).

## When to use
- Before committing — stage the change and sanity-check it across multiple models in
  parallel (`review diff --staged --task CODE`). The STAGED run is the one that satisfies
  agent-tools' `require-review-before-commit` gate: on passing it writes that gate's marker
  itself, so never `touch` the marker file by hand (an unstaged `review diff` reviews the
  working tree and deliberately leaves the marker alone — it says so on stderr).
- For a hard decision — `review quorum "Q" --task CODE` (settle with cited evidence) or
  `review brainstorm "TOPIC" --task CODE` (explore an open design space across rotating expert roles,
  in a loop). The moderator defaults to opus and falls back to codex/gemini automatically.
- For a quick multi-model second opinion — `review just-ask "Q" --task CODE`.

Pair with `tg` to post the chosen options / pros-cons to Telegram.
"""
SKILL_BLURB = (
    "`review` — multi-model read-only code review + AI panels "
    "(codex/claude/gemini/opencode). Modes are SUBCOMMANDS (the verb leads; -C follows): "
    "`review diff --task CODE -C <repo>` (diff review), "
    '`review quorum "Q" --task CODE -C <repo>`, '
    '`review brainstorm "topic" --task CODE -C <repo>`, '
    '`review just-ask "Q" --task CODE -C <repo>`. '
    "A bare `review` prints HELP — the diff review is `review diff` (NOT a bare "
    "`review`); the old --quorum/--brainstorm/--just-ask flags were removed. "
    "Always pass -C <project-root>. Always pass --task CODE (or set REVIEW_TASK_CODE) for "
    "review iterations; `review task CODE` shows iterations, models, and transcripts. "
    "Use before commits and for hard decisions. Before a COMMIT use `review diff --staged --task CODE`: only a PASSING staged review satisfies agent-tools' require-review-before-commit gate (it writes that gate's marker itself — never `touch` the marker by hand). "
    "NEVER wrap it in a short timeout — it is multi-model / multi-round and takes "
    "MINUTES (brainstorm 10–20m); it prints the expected duration for your pool "
    "size at startup, so wait for that, don't short-timeout it. Use NO external "
    "timeout at all — review carries its own internal <=4h backstop."
)

_HOOK_MARKER = "# agent-tools-awareness"
_HOOK_COMMAND = (
    'sh -c \'d="$HOME/.agents/skills/.blurbs"; ls "$d"/*.md >/dev/null 2>&1 && '
    '{ printf "Agent CLI tools installed on this machine (prefer them):\\n"; '
    'cat "$d"/*.md; }\' ' + _HOOK_MARKER
)


def _detected(cmd: str, *dirs: str) -> bool:
    import shutil

    if shutil.which(cmd):
        return True
    return any(os.path.isdir(os.path.expanduser(d)) for d in dirs)


def _append_marked(path, tool: str, blurb: str) -> bool:
    """Insert/refresh the marked skill blurb block in `path`. Returns True if the file was
    CHANGED (newly added or the block content differs), False if it was already up to date —
    so the caller can report "already configured" vs "updated" (install-* INSTALLED state)."""
    import re

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    start, end = f"<!-- skill:{tool} -->", f"<!-- /skill:{tool} -->"
    # NOTE: read as UTF-8 WITHOUT swallowing a decode error. These are user-authored harness
    # files (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md). If one held non-UTF-8 bytes we must NOT
    # treat it as empty and overwrite it — that would destroy the user's content. We instead
    # let the (rare) decode error propagate; the caller (`install_agent_skill`) catches it and
    # records a `! conflict` (file left as-is, non-zero exit), so there's neither data loss nor
    # a crash (glm review). OUR-generated files go through `_write_if_changed`, which safely
    # rewrites an undecodable file because we own its content.
    before = p.read_text(encoding="utf-8") if p.exists() else ""
    existing = re.sub(
        re.escape(start) + r".*?" + re.escape(end) + r"\n?", "", before, flags=re.S
    )
    block = f"{start}\n{blurb}\n{end}\n"
    after = (existing.rstrip() + "\n\n" + block) if existing.strip() else block
    if after == before:
        return False
    p.write_text(after, encoding="utf-8")
    return True


def _sessionstart_hook_present(home) -> bool:
    """True if our marked SessionStart hook is already in ~/.claude/settings.json — so
    install-skill can report it as "already configured" (vs added). Read-only; any
    read/parse failure -> False (treat as not-present)."""
    settings = Path(home) / ".claude" / "settings.json"
    if not settings.exists():
        return False
    try:
        # UnicodeDecodeError is a ValueError (so is JSONDecodeError) — a non-UTF-8
        # settings.json must degrade to "not present", never crash the install (glm review;
        # matches the ValueError handling in `_write_if_changed` / `_append_marked`).
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    # Tolerate a malformed-but-valid-JSON settings shape: a non-dict `hooks` (e.g.
    # `{"hooks": "bad"}`) or a non-list `SessionStart` must degrade to "not present", not
    # crash on `.get()` / iteration (codex review — `_ensure_sessionstart_hook` guards the
    # same way).
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    sessionstart = hooks.get("SessionStart")
    if not isinstance(sessionstart, list):
        return False
    for group in sessionstart:
        for h in (group or {}).get("hooks", []) if isinstance(group, dict) else []:
            if isinstance(h, dict) and _HOOK_MARKER in str(h.get("command", "")):
                return True
    return False


def _ensure_sessionstart_hook(home) -> bool:
    """Idempotently add a SessionStart hook to ~/.claude/settings.json that
    surfaces installed agent CLIs. Conservative: never removes unrelated config.

    Return contract (load-bearing for the install-* INSTALLED-state reporting):
    True IFF a write occurred (the hook was just ADDED); False if it was already present OR
    could not be written (unparseable / malformed / unwritable settings). Callers
    distinguish "already present" from "could not write" by re-probing with
    `_sessionstart_hook_present`. Do NOT change this to return True on "updated" without
    updating `install_agent_skill`, or every idempotent rerun would flip to "wrote/updated"."""
    settings = Path(home) / ".claude" / "settings.json"
    if not settings.parent.is_dir():
        return False
    try:
        # (OSError, ValueError) also covers UnicodeDecodeError (a non-UTF-8 settings.json) and
        # JSONDecodeError — degrade to "could not write" rather than crash (glm review).
        data = (
            json.loads(settings.read_text(encoding="utf-8"))
            if settings.exists()
            else {}
        )
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return False
    sessionstart = hooks.setdefault("SessionStart", [])
    if not isinstance(sessionstart, list):
        return False
    for group in sessionstart:
        for h in (group or {}).get("hooks", []) if isinstance(group, dict) else []:
            if isinstance(h, dict) and _HOOK_MARKER in str(h.get("command", "")):
                return False
    sessionstart.append({"hooks": [{"type": "command", "command": _HOOK_COMMAND}]})
    if settings.exists():
        settings.with_suffix(".json.bak").write_text(
            settings.read_text(encoding="utf-8"), encoding="utf-8"
        )
    settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _write_if_changed(path: Path, content: str) -> bool:
    """Write `content` to `path` only if it differs from what's there. Returns True if it
    CHANGED (file absent or different), False if already up to date — so install-skill can
    report "already configured" vs "updated" (install-* INSTALLED state)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Read in one shot; if we can't prove the existing content matches, fall through and
        # (re)write rather than crash the whole install (glm review — no exists()+read TOCTOU).
        # OSError = vanished/unreadable; UnicodeDecodeError (a ValueError, NOT an OSError) =
        # the target holds non-UTF-8 bytes (binary blob / foreign installer) — treat as "needs
        # write", don't propagate.
        if path.read_text(encoding="utf-8") == content:
            return False
    except (OSError, ValueError):
        pass
    path.write_text(content, encoding="utf-8")
    return True


# review-cli#180: the codex backend's ONLY safety mechanism was `-s read-only`, a
# filesystem/network sandbox that does NOT restrict codex's shell/exec tool (codex's
# core capability IS running shell commands — there is no `--tools ""`-style built-in
# tool-disable, unlike the claude/opencode backends). That let a codex reviewer
# re-invoke `review diff` on the same worktree as a plain shell command, which spawns
# another codex, forming an unbounded self-reinvocation loop (live-confirmed
# 2026-08-11: 40+ live `codex exec` processes and 11 `review diff` processes across 4
# worktrees, swap at 88.5%, load average 60+).
#
# codex DOES have a real command-level restriction: execpolicy `.rules` files
# (Starlark `prefix_rule(pattern=[...], decision="forbidden")`), loaded by default
# from `$CODEX_HOME/rules/` (user layer) for every `codex exec` unless
# `--ignore-rules` is passed. A `forbidden` decision hard-blocks the command before it
# runs (verified via `codex execpolicy check -r <file> review diff` ->
# `"decision": "forbidden"`) — this is the closest available equivalent to the claude
# backend's `--tools ""` / opencode's `bash: deny`. It is written to the USER-level
# rules dir, not the reviewed repo's own `.codex/rules/`, because `review_codex()`
# runs with `-C <cwd>` pointed at an arbitrary (often untrusted — project-local rules
# require trust) target repo — writing config into someone else's working tree would
# also violate the read-only-reviewer contract.
#
# LIMITATION (be honest about it, VERIFIED not assumed — review-cli#180 review
# finding, glm-5.2): `prefix_rule` matches literal argv TOKENS, so it blocks the
# demonstrated attack vector — codex invoking these binaries BY NAME as the direct
# first token — but NOT every conceivable indirection. Confirmed live via
# `codex execpolicy check` (each below resolves to `"matchedRules": []`, i.e. ALLOWED,
# not forbidden):
#   * a wrapped shell script:      bash -c "review diff"
#   * an absolute path:            /usr/local/bin/review diff
#   * the module entry point:      python3 -m reviewlib diff
# codex's core capability is arbitrary shell exec, so no purely name-pattern-based
# policy can be a complete proof — widening the pattern set to cover these (e.g. a
# blanket rule on `bash`/`sh`/`python3`) would false-positive-block ordinary reviewer
# shell usage, so it is not attempted here. The REVIEW_CLI_ACTIVE reentrancy guard
# (`reviewlib.cli._reject_if_reentrant`) is the mechanism that still holds against ALL
# three of these, because it checks $REVIEW_CLI_ACTIVE inside `review`'s own process at
# startup — it does not matter HOW that process was invoked.
#
# DELIBERATE, PERMANENT, MACHINE-WIDE (not scoped to review-cli's own calls or cleaned
# up after): this rules file lives in codex's USER-level `$CODEX_HOME/rules/`, so it
# affects every `codex exec`/interactive codex session on the machine, not just ones
# spawned by review-cli, and is never removed. That is intentional, not an oversight —
# review-cli#180 was a real machine-hanging incident (see below), and "codex shells
# out to bare `review`/`codex`/`claude`/`opencode`/`omp`" is not a legitimate pattern
# for ordinary interactive codex usage either. There is deliberately NO opt-out flag:
# an opt-out that a compromised/confused agent could set would reopen exactly this
# hole (same class of gap as the REVIEW_CLI_ACTIVE `env -u` bypass documented in
# `cli._reject_if_reentrant`). The file is plainly commented (see the generated
# header below) and lives at a well-known, single path if a human wants to remove it.
_CODEX_RECURSION_GUARD_FILENAME = "review-cli-recursion-guard.rules"
_CODEX_RECURSION_GUARD_MARKER = "review-cli#180"
# The exact binaries review-cli itself shells out to as a backend (reviewlib/backends.py).
# A codex agent invoking any of these AS A COMMAND is, by definition, either the
# review-cli#180 loop or an equally unwanted nested-agent spawn — never something a
# read-only code review legitimately needs to do.
CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS = ("review", "codex", "claude", "opencode", "omp")


def _codex_recursion_guard_rules() -> str:
    """Starlark execpolicy `.rules` content forbidding codex from re-invoking `review`
    or any review-cli backend CLI as a shell command. GENERATED from
    `CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS` (single source of truth) so the
    installed file and the constant can never drift. Every entry is asserted to be a
    plain identifier (no quotes/backslashes) before interpolation, so a future
    addition to the tuple can never emit invalid/injected Starlark (review-cli#180
    review finding, glm-5.2) — the current hardcoded values are all safe already, this
    is future-proofing, not a fix for an observed break."""
    for cmd in CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS:
        # A raise, not `assert` — `assert` is compiled out entirely under `python -O`/
        # `-OO`, which would silently remove this guarantee (review-cli#180 review
        # round 4, Opus).
        if not (cmd.isidentifier() or cmd.replace("-", "_").isidentifier()):
            raise ValueError(f"unsafe command name for Starlark interpolation: {cmd!r}")
    rules = (
        "prefix_rule(\n"
        f'    pattern = ["{cmd}"],\n'
        '    decision = "forbidden",\n'
        f'    justification = "{_CODEX_RECURSION_GUARD_MARKER}: codex must not '
        f're-invoke {cmd} as a shell command — unbounded self-reinvocation loop",\n'
        ")\n"
        for cmd in CODEX_RECURSION_GUARD_FORBIDDEN_COMMANDS
    )
    return (
        f"# {_CODEX_RECURSION_GUARD_MARKER}: installed by review-cli — DO NOT hand-edit,\n"
        "# it is checked (and rewritten if stale/missing) on the FIRST codex backend call\n"
        "# of each review-cli process — not on every call (review-cli#180 review finding,\n"
        "# glm-5.2: this file is safe to delete by hand, it is simply recreated the next\n"
        "# time a codex backend call happens in a NEW review-cli process). See\n"
        "# reviewlib/install.py install_codex_recursion_guard() for the rationale.\n\n"
        + "\n".join(rules)
    )


def codex_home() -> Path:
    """`$CODEX_HOME`, or `~/.codex` when unset/empty — matching codex's own resolution
    for its execpolicy rules-discovery user layer in the common (unset) case. An
    explicitly-EMPTY `CODEX_HOME=""` is treated the same as unset (falls back to
    `~/.codex`) rather than resolved literally — this is an untested edge case (nobody
    sets `CODEX_HOME=""` deliberately); if codex itself ever resolves an empty value
    differently, the guard file could land in a directory codex doesn't read from,
    silently defeating it for that one case (review-cli#180 review finding, Opus)."""
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def install_codex_recursion_guard() -> bool:
    """Idempotently write the review-cli#180 execpolicy guard into codex's user-level
    rules directory. Returns True if it CHANGED anything (created or rewrote a stale
    copy), False if already up to date OR the write failed.

    Best-effort and NEVER raises: a locked-down/unwritable `$CODEX_HOME` must not break
    a review run. The `_write_if_changed` failure modes (permissions, read-only FS,
    ENOSPC) collapse to `False` here rather than propagating, and so does a
    `codex_home()` resolution failure (`Path.home()` raises `RuntimeError` when $HOME
    is unset AND the pwd-database fallback also fails — review-cli#180 review finding,
    glm-5.2: a bare `except (OSError, ValueError)` did not cover that case, silently
    contradicting this docstring). The caller (`backends._ensure_codex_recursion_guard`)
    treats "did nothing" and "wrote it" the same way (fire-and-forget) either way, so
    there is nothing for a caller to branch on besides the boolean."""
    try:
        path = codex_home() / "rules" / _CODEX_RECURSION_GUARD_FILENAME
        return _write_if_changed(path, _codex_recursion_guard_rules())
    except Exception:  # noqa: BLE001 — must never break a review call, see docstring
        return False


def install_agent_skill(name: str, skill_md: str, blurb: str) -> int:
    """Idempotently install the agent skill across detected harnesses. Reports each target's
    STATE (ROADMAP "install-* commands must show INSTALLED state"): a green check + "already
    configured" when nothing changed, "+ wrote/updated" when it (re)wrote. A re-run on a
    fully-installed machine shows all ✓ and "already configured — nothing to do"."""
    home = Path.home()
    # (label, changed?) per target — `changed` False == already-configured.
    results: list[tuple[str, bool]] = []
    conflicts: list[
        str
    ] = []  # targets we could NOT configure (left as-is) — block "nothing to do"

    def _write_target(path: Path, content: str) -> None:
        # A write that fails (read-only FS, ENOSPC, EPERM, immutable flag) must become a
        # `! conflict` (non-zero exit), NOT a mid-loop crash that strands later targets and
        # prints a traceback instead of the documented conflict output (glm review). Mirrors
        # the per-harness-file handling below.
        try:
            results.append((str(path), _write_if_changed(path, content)))
        except (OSError, ValueError) as exc:
            conflicts.append(
                f"{path} could not be written ({exc}) — fix permissions and re-run"
            )

    skill_dir = home / ".agents" / "skills" / name
    _write_target(skill_dir / "SKILL.md", skill_md)
    blurbs = home / ".agents" / "skills" / ".blurbs"
    _write_target(blurbs / f"{name}.md", f"- {blurb}\n")

    claude_skills = home / ".claude" / "skills"
    if claude_skills.is_dir():
        link = claude_skills / name
        want = Path("..") / ".." / ".agents" / "skills" / name
        # `exists()` follows symlinks (False for a broken/dangling one), so probe the link
        # itself with is_symlink(): report "already configured" ONLY when it is a symlink
        # pointing at the EXPECTED target. A regular file/dir, or a symlink to the WRONG
        # target, is a CONFLICT — never a silent "nothing to do" (codex review).
        if link.is_symlink():
            try:
                points_at = link.readlink()
            except OSError:
                points_at = None
            # Compare by RESOLVED target, not the raw stored string: a symlink written with an
            # absolute target (older installer / packaging script / user) that lands on the
            # SAME directory as our relative `want` is already-configured, not a CONFLICT
            # (glm review). Resolve both relative to the link's parent.
            if points_at is not None and (
                points_at == want
                or (link.parent / points_at).resolve() == (link.parent / want).resolve()
            ):
                results.append((str(link), False))  # correct symlink already present
            else:
                target_desc = (
                    "an unreadable target" if points_at is None else f"{points_at}"
                )
                conflicts.append(
                    f"{link} is a symlink to {target_desc} (expected {want})"
                )
        elif link.exists():
            # A regular file/dir occupies the path (is_symlink already handled all symlinks,
            # incl. dangling ones, above).
            conflicts.append(f"{link} exists but is not our skill symlink")
        else:
            try:
                link.symlink_to(want)
                results.append((str(link), True))
            except OSError as exc:
                # A FAILED symlink creation is a conflict, not a silent skip — otherwise a
                # rerun with everything else unchanged would falsely say "nothing to do"
                # while the Claude skill link was never installed (codex review).
                conflicts.append(f"{link} could not be created ({exc})")

    harness_files = [
        ("claude", home / ".claude" / "CLAUDE.md", ("~/.claude",)),
        ("codex", home / ".codex" / "AGENTS.md", ("~/.codex",)),
        (
            "opencode",
            home / ".config" / "opencode" / "AGENTS.md",
            ("~/.config/opencode",),
        ),
        ("gemini", home / ".gemini" / "GEMINI.md", ("~/.gemini",)),
    ]
    for cmd, path, dirs in harness_files:
        if _detected(cmd, *dirs):
            try:
                results.append((str(path), _append_marked(path, name, blurb)))
            except (OSError, ValueError) as exc:
                # A user harness file (CLAUDE.md / AGENTS.md / GEMINI.md) we can't read as
                # UTF-8 (non-UTF-8 bytes -> UnicodeDecodeError, a ValueError) is left UNTOUCHED
                # — never overwritten (data loss) and never a mid-loop crash that strands later
                # targets. Record a conflict so the run exits non-zero and tells the user to
                # fix the file (glm review: honor the `! conflict` contract for this case too).
                conflicts.append(
                    f"{path} is not readable as UTF-8 ({exc}) — left as-is, fix it manually"
                )

    if (home / ".claude").is_dir():
        # _ensure_sessionstart_hook returns True if it ADDED the hook, False if already there
        # (or it could not write). Distinguish "already present" from "couldn't write" by
        # re-probing: if the marker is in settings now, it is configured either way. A WRITE
        # failure (locked/read-only settings.json or a failed .bak write) raises OSError from
        # its write path — catch it here so install-skill reports a `! conflict` and exits
        # non-zero instead of aborting with a traceback (codex review).
        write_error: OSError | None = None
        try:
            added = _ensure_sessionstart_hook(home)
        except OSError as exc:
            added = False
            write_error = exc
        if added or _sessionstart_hook_present(home):
            results.append(("SessionStart hook -> ~/.claude/settings.json", added))
        elif write_error is not None:
            conflicts.append(
                f"SessionStart hook -> ~/.claude/settings.json could not be written "
                f"({write_error}) — fix the file/permissions and re-run"
            )
        else:
            # Could neither write the hook nor find it present -> the target is genuinely
            # UNCONFIGURED. Surface it as a conflict (non-zero exit, blocks "nothing to do")
            # instead of silently dropping it and falsely claiming the install is complete
            # (glm review).
            conflicts.append(
                "SessionStart hook -> ~/.claude/settings.json could not be written "
                "(unparseable or unwritable settings.json) — add it manually or fix the file"
            )

    changed = sum(1 for _label, c in results if c)
    for label, c in results:
        print(f"  {'+ wrote/updated' if c else '✓ already configured'}  {label}")
    for c in conflicts:
        print(f"  ! conflict  {c} — left as-is; fix it manually.")
    if conflicts:
        # A conflict means a target is NOT configured — never say "nothing to do" / done.
        # Return non-zero so a caller/script sees the install is incomplete (codex review).
        print(
            f"{name}: install-skill — {changed} updated, "
            f"{len(results) - changed} already configured, "
            f"{len(conflicts)} CONFLICT(S) left unconfigured. Resolve the conflict(s) "
            "above and re-run."
        )
        return 1
    if changed == 0:
        print(
            f"{name}: install-skill — already configured, nothing to do "
            f"({len(results)} target(s) ✓). Idempotent; re-run anytime."
        )
    else:
        print(
            f"{name}: install-skill done — {changed} updated, "
            f"{len(results) - changed} already configured ({len(results)} target(s)). "
            "Idempotent; re-run anytime."
        )
    return 0


def install_skill() -> int:
    return install_agent_skill(SKILL_NAME, SKILL_MD, SKILL_BLURB)


_PRECOMMIT_MARKER = "# review-before-commit-gate"
# Hash must match exactly what `review diff --staged --task CODE` reviews (`git diff --no-ext-diff
# --cached`) and the stamp path must resolve via `git rev-parse --git-path` so it
# works in worktrees and repos whose `.git` is a pointer file.
# `command git` bypasses shell aliases/functions (e.g. an rtk-style wrapper that rewrites
# `git diff` output) so the hash matches the one written by review-cli, which calls the
# real git binary via subprocess directly.
#
# review-cli#208: the exact-hash check above has zero tolerance -- restaging after even a
# one-line follow-up produces a different hash and used to force a brand-new full
# multi-model review round every time. This block runs ONLY on an exact-hash MISS and
# tolerates a SMALL trailing delta against the last diff that was actually reviewed,
# instead of requiring a byte-for-byte match. `review diff --staged`
# (`_write_review_stamp` below) writes that reviewed diff's raw TEXT to a companion
# `review-stamp-diff` file alongside the hash stamp; this block re-diffs the CURRENT
# staged diff against that stored text (`diff -U0`, symmetric add+remove line count) and
# allows the commit without dispatching a fresh review when the count is within
# `$REVIEW_TRIVIAL_DELTA_LINES` (default 10).
#
# The outer `diff -U0` always emits EXACTLY two header lines (`--- old` / `+++ new`)
# before its first `@@` hunk, so those two lines are dropped positionally (`tail -n +3`),
# NOT by matching their `+`/`-` prefix textually -- the diff TEXT being compared is itself
# a unified diff whose every real content line already starts with `+`/`-`, so a
# second-character content match (an earlier, broken version of this block used
# `grep -c '^[+-][^+-]'`) would misfire: it happens to also exclude a genuinely-changed
# outer line whenever the underlying reviewed line itself starts with `+`/`-` -- which is
# effectively ALWAYS true for diff content, undercounting real drift to near zero
# (caught by this feature's own `test_substantive_change_after_same_baseline_is_still_blocked`
# test, which a naive second-character filter passed through as "trivial"). Skipping a
# fixed line COUNT instead of pattern-matching content sidesteps that collision entirely.
#
# Two more counting pitfalls, both caught by genuine review of this feature's own PR
# (review task REVIEW-208):
#   * GLM-cc-last [Medium-High]: without excluding it, the `index <old>..<new>` metadata
#     line changes for EVERY touched file (always +2 to the outer delta) and every
#     `@@ -a,b +c,d @@` hunk header downstream of a length-changing edit shifts too (+2 per
#     shifted hunk) -- so a genuine one-line insertion in a file that already has several
#     hunks could exceed the threshold and force a full review anyway, defeating the whole
#     point. `index `/`@@ ` lines are pure diff-generation artifacts, not real content
#     drift, so they are excluded from the count (`grep -Ecv '^[+-](@@ |index )'`).
#   * k3 [Security]: binary/gitlink content is invisible to a line-count ruler -- a swapped
#     binary shows only a 2-line `index` change (or zero, if the reviewed diff already had
#     an identical "Binary files ... differ" line) and a submodule bump shows 2
#     `Subproject commit` lines, regardless of how large or opaque the real change is. This
#     block fails CLOSED (skips the fast path entirely, falls through to the block message)
#     whenever either the reviewed baseline or the current diff contains `Binary files ...
#     differ` or a `[+-]Subproject commit <sha>` line -- an unmeasurable delta is never
#     "trivial". (The sha is required in the pattern, not a bare `Subproject commit` prefix
#     -- round-5, below -- so ordinary prose that happens to start with those two words
#     can't force a spurious fail-closed on an unrelated file.)
#   * round-2 finding: a MODIFIED line always appears in the outer diff-of-diffs as a
#     `-oldcontent`/`+newcontent` PAIR (the old text is removed, the new text is added), so a
#     raw `+`/`-` line count doubles every genuinely-edited line -- `REVIEW_TRIVIAL_DELTA_LINES`
#     documented (README/CHANGELOG/--help) and was meant to mean "N edited lines" but the
#     code actually measured "N raw diff lines", roughly 2x the edited-line count for the
#     common in-place-edit case. Fixed by counting ADDED and REMOVED lines SEPARATELY and
#     taking `changed = max(added, removed)`, not a naive `raw / 2`: for a balanced
#     modification (a lines removed, a lines added) `max(a, a) == a`, correctly reporting `a`
#     edited lines instead of `2a` raw ones -- but for a PURE insertion or deletion (only one
#     side present, e.g. inserting k brand-new lines with nothing removed) `max(0, k) == k`
#     stays exact, whereas `raw / 2 == k / 2` would silently DOUBLE the effective tolerance
#     for pure insertions/deletions (a k-line unreviewed addition would only cost k/2 against
#     the threshold) -- a real loosening a naive halving would introduce, verified empirically
#     (a k-line pure insertion after a reviewed baseline raw-counts as exactly k, not 2k, so
#     halving it would undercount). `max()` matches the documented "N edited lines" meaning
#     in the common case and never undercounts the size of an unreviewed pure add/remove.
#   * round-3 finding (Opus + Fable, independently, same PR): the `index `/`@@ ` exclusion
#     covers a length-changing edit inside an ALREADY-TOUCHED file, but not a follow-up that
#     adds, deletes, or renames a WHOLE file -- that introduces MORE pure diff-generation
#     lines that were not excluded: `diff --git a/x b/x`, `--- `/`+++ ` (esp. `--- /dev/null`
#     for a new file or `+++ /dev/null` for a deletion), `new file mode `/`deleted file mode
#     `/`old mode `/`new mode `, and `rename from/to `/`copy from/to `/`similarity index
#     `/`dissimilarity index ` for renames/copies. Left unexcluded, a small brand-new file
#     (e.g. 3 real lines) could raw-count as ~7 (the metadata lines plus the content),
#     tipping past the default threshold and forcing a full review for exactly the trivial
#     case this feature exists to accept. Direction was always SAFE (over-counts, never
#     under -- no unreviewed change could slip through undercounted), so this was a
#     correctness-vs-intent gap, not a security hole; fixed by extending the exclusion list
#     to cover all of the above, same treatment as `index `/`@@ `.
#   * round-4 finding (Opus, next round, on the round-3 fix itself) [Security -- undercount]:
#     a bare `--- `/`+++ ` exclusion (round-3's fix, above) is UNSAFE, unlike every other
#     entry in this list. `index `, `diff --git `, `old mode `, etc. can only ever match a
#     genuine header line, because a real CONTENT line's full outer-diff text is always
#     <outer +/-><inner +/-/space><source text>, and the inner marker can never spell out
#     those words' first letters. But `--- `/`+++ ` are each 3 repeats of a character that
#     IS itself a valid inner marker (`-`/`+`) -- so a REMOVED source line whose own text
#     starts with `-- ` (SQL/Lua/Haskell line-comment syntax, e.g. `-- explanation`) renders
#     in git-diff as `--- explanation` (git's own `-` marker + the source's leading `-- `),
#     and if that removal is new in the current diff, the OUTER diff-of-diffs shows it as
#     `+--- explanation` -- which the bare `--- ` pattern wrongly excludes as "just a file
#     header", undercounting a REAL unreviewed deletion. (Symmetrically for `++ `-prefixed
#     added content colliding with `+++ `.) This is the one direction this whole mechanism
#     must never take: a small enough series of such lines makes `changed` read 0 for a
#     real, unreviewed change, and the gate `exit 0`s it through unreviewed. Fixed by
#     anchoring both patterns to the ACTUAL header shapes git emits -- `--- a/`, `---
#     /dev/null`, `+++ b/`, `+++ /dev/null` -- which a plain `-- `/`++ `-prefixed content
#     line cannot spell (it would need to literally start with `a/` or `/dev/null` right
#     after the two extra dashes/pluses, astronomically narrower than the bare-prefix
#     collision, and in the same accepted-risk class as the pre-existing `index `/`@@ `
#     collision risk this codebase already tolerates). A header that fails to match the
#     tightened pattern (e.g. a git-quoted path with spaces) just falls through to being
#     COUNTED -- safe/conservative, not a new bypass.
#   * round-5 finding (Opus, on this feature's own PR, reviewing rounds 1-4 together)
#     [Security -- unbounded undercount]: `old mode `/`new mode `/`rename from `/`rename
#     to `/`copy from `/`copy to `/`similarity index ` are pure diff-generation metadata,
#     same class as `index `/`@@ ` (round-1's own exclusion) -- but unlike a metadata line
#     attached to a real content edit, a PURE mode change or a 100%-similarity rename has
#     NO content hunk at all: `git diff` emits ONLY `diff --git `+ one or two of those
#     excluded lines, nothing else. So `changed` reads exactly 0 no matter how many files
#     are touched this way -- `chmod +x` on an arbitrary number of scripts, or renaming an
#     arbitrary number of files, is UNBOUNDED by the threshold and sails through unreviewed
#     every time, not just when small (verified empirically: 5 unreviewed `chmod +x`
#     follow-ups on a reviewed baseline pass at `REVIEW_TRIVIAL_DELTA_LINES=2`). This breaks
#     the block's own core premise (a SIZE heuristic bounded by `threshold`) for this one
#     line-type family, the same "unmeasurable by line count" failure class the k3 binary/
#     gitlink fail-closed pre-check (above) already exists to reject -- so fixed the same
#     way, not by trying to count mode/rename lines (they have no natural size unit): the
#     fail-closed pre-check now ALSO fires whenever either diff contains an `old mode `/`new
#     mode `/`rename from `/`rename to `/`copy from `/`copy to ` line, forcing a full review
#     instead of silently passing. A rename or mode change that also touches real content
#     (an actual hunk) now costs one full review it might not strictly have needed --
#     safe/conservative, same tradeoff already accepted for `Subproject commit`/binary.
#
# This is a SIZE heuristic, not a semantic one -- a small but security-critical one-line
# edit gets the same pass as a typo fix. That trade-off is deliberate and matches
# review-cli#208's own filed acceptance criteria (a configurable line-count threshold).
# The baseline is NEVER advanced by this block itself (it only reads review-stamp-diff,
# never writes it) -- only a real `review diff --staged` pass moves the baseline forward,
# so drift is always measured from the last GENUINE review, not a sliding window where
# many small unreviewed commits could add up to something large. Sizing the threshold to
# 0 (`REVIEW_TRIVIAL_DELTA_LINES=0`) disables this block entirely and restores today's
# exact-hash-only behavior -- the default for any stamp that predates this feature (no
# review-stamp-diff file -> the `-f "$stamp_diff"` check below is false -> falls straight
# through to the block message, unchanged from before this feature existed).
_TRIVIAL_DELTA_BLOCK = """\
threshold="${REVIEW_TRIVIAL_DELTA_LINES:-10}"
case "$threshold" in ''|*[!0-9]*) threshold=10 ;; esac
if [ "$threshold" -gt 0 ]; then
  stamp_diff=$(command git rev-parse --git-path review-stamp-diff)
  if [ -f "$stamp_diff" ]; then
    # A stale review-stamp-diff left on disk after the reviewed change is committed
    # must not anchor the comparison for a later unrelated change, so only proceed
    # if the HEAD it was reviewed against (this file's first line, written by
    # _write_review_stamp_diff) still matches the current HEAD; otherwise fall
    # through to the exact-hash gate below. Empty/empty is a legitimate match (the
    # documented pre-first-commit review-then-followup case), not a bypass.
    recorded_head=$(head -n 1 "$stamp_diff" 2>/dev/null)
    # On a repo with no commits, `git rev-parse HEAD` prints the unresolved arg
    # ("HEAD") to stdout and exits non-zero -- check the exit status, not just
    # stdout, or a pre-first-commit review's legitimate empty HEAD never matches.
    if current_head=$(command git rev-parse HEAD 2>/dev/null); then
      :
    else
      current_head=""
    fi
    if [ "$recorded_head" = "$current_head" ]; then
      baseline_tmp=$(mktemp 2>/dev/null) && trap 'rm -f "$baseline_tmp"' EXIT
      cur_tmp=$(mktemp 2>/dev/null) && trap 'rm -f "$baseline_tmp" "$cur_tmp"' EXIT
      if [ -n "$baseline_tmp" ] && [ -n "$cur_tmp" ] \\
        && tail -n +2 "$stamp_diff" > "$baseline_tmp" \\
        && command git diff --no-ext-diff --cached > "$cur_tmp"; then
        # git, diff, and grep each decide "binary" over a different byte-window of
        # a different input, so trusting any one tool's own heuristic leaves a gap
        # a NUL can fall into (silently truncating the `content` pipeline below to
        # nothing, undercounting an arbitrarily large unreviewed change as trivial).
        # Scan both whole files for a NUL up front instead of any partial window.
        # `tr -d '\\000' | cmp -s -` is POSIX-portable (unlike `wc -c`, which BSD
        # pads) and reports a real difference (a NUL was present) via exit status.
        contains_nul() { ! (LC_ALL=C tr -d '\\000' < "$1" | cmp -s - "$1"); }
        if ! contains_nul "$baseline_tmp" && ! contains_nul "$cur_tmp" \\
          && ! LC_ALL=C grep -qE '^(Binary files |[-+]Subproject commit [0-9a-f]{40}|old mode |new mode |rename from |rename to |copy from |copy to )' "$baseline_tmp" "$cur_tmp" 2>/dev/null; then
          diff_out=$(diff -U0 "$baseline_tmp" "$cur_tmp" 2>/dev/null)
          diff_rc=$?
          # diff exits 0 (no diff) or 1 (differences found) on success, 2 on error --
          # only those two mean diff_out is a usable unified diff of two text files.
          # The "Binary files" case is defense-in-depth: the NUL scan above should make
          # it unreachable on GNU/BSD diff (both binary-detect solely on NUL), but a
          # diff implementation with any other heuristic would print that single line
          # with exit 1, and `tail -n +3` would then count it as an empty, trivial diff.
          if [ "$diff_rc" -le 1 ]; then
            case "$diff_out" in
              "Binary files "*) : ;;
              *)
                content=$(printf '%s\\n' "$diff_out" | tail -n +3 | LC_ALL=C grep -Ev '^[+-]( |@@ |index |diff --git |--- (a/|/dev/null)|\\+\\+\\+ (b/|/dev/null)|old mode |new mode |new file mode |deleted file mode |similarity index |dissimilarity index |rename from |rename to |copy from |copy to )')
                added=$(printf '%s\\n' "$content" | LC_ALL=C grep -c '^+')
                removed=$(printf '%s\\n' "$content" | LC_ALL=C grep -c '^-')
                changed=$added
                [ "$removed" -gt "$changed" ] && changed=$removed
                if [ "$changed" -le "$threshold" ] 2>/dev/null; then
                  exit 0
                fi
                ;;
            esac
          fi
        fi
      fi
    fi
  fi
fi"""
_PRECOMMIT = (
    """\
#!/bin/sh
"""
    + _PRECOMMIT_MARKER
    + """ (installed by `review install-commit-hook`)
# Blocks a commit whose staged diff has not been reviewed. Bypass with
# REVIEW_SKIP=1 git commit ...   or   git commit --no-verify
root=$(command git rev-parse --show-toplevel 2>/dev/null) || exit 0

# A global core.hooksPath shadows each repo's own pre-commit, so run it
# explicitly first — tests/formatters/secret-scanners must still gate. Resolve
# via git-path so worktrees/submodules (where .git is a file) work too.
local_hook="$(command git rev-parse --git-path hooks/pre-commit)"
case "$local_hook" in /*) : ;; *) local_hook="$root/$local_hook" ;; esac
if [ -x "$local_hook" ] && [ "$local_hook" != "$0" ]; then
  "$local_hook" "$@" || exit $?
fi

[ -n "$REVIEW_SKIP" ] && exit 0
[ -z "$(command git diff --cached --name-only)" ] && exit 0
h=$(command git diff --no-ext-diff --cached | shasum -a 256 | cut -d' ' -f1)
stamp=$(command git rev-parse --git-path review-stamp)
if [ -f "$stamp" ] && grep -q "$h" "$stamp"; then exit 0; fi

"""
    + _TRIVIAL_DELTA_BLOCK
    + """
echo "review-before-commit: staged changes have not been reviewed." >&2
echo "  run:  review diff --staged --task TASK-CODE      (then commit)" >&2
echo "  skip: REVIEW_SKIP=1 git commit ...   |   git commit --no-verify" >&2
exit 1
"""
)


def _write_review_stamp(
    cwd: Path, diff: str, *, stamp_diff_hash: str | None = None
) -> str | None:
    """Record that this exact diff was reviewed, so the optional pre-commit gate
    can verify it. Uses `git rev-parse --git-path` so worktrees / pointer-file
    .git resolve correctly. Best-effort: never breaks a review on failure.

    Returns None on success, or the reason it did not write, for the same reason
    `_touch_review_marker` does (codex finding, review-cli#350 iteration 6): the LOCAL
    git pre-commit gate keys on this stamp, not on the session marker, so a failed stamp
    write with a healthy marker produces the identical trap one gate over — a green
    staged review, a rejected commit, and no explanation anywhere. Not-a-repo is NOT a
    failure and returns None: there is no gate to satisfy outside a git repo, and saying
    so on every non-repo review would be noise.

    The rev-parse is anchored to `cwd` (`git -C`) AND runs with the repo-pinning git env
    stripped (`git_repo_env`), matching the diff probe in cli._git_diff: a leaked
    GIT_DIR/GIT_WORK_TREE must not write the stamp into an UNRELATED repo while the diff was
    read from `cwd`. The stamp and the diff it stamps stay anchored to the SAME `-C` repo
    (the #18 stamp/tool alignment, kept under the #71 env-leak fix).

    IMPORTANT: the hash is NOT computed from the `diff` parameter. Both the local
    `_PRECOMMIT` template below and the separate agent-tools global hook verify by
    independently running `git diff --no-ext-diff --cached` with NO explicit
    `--src-prefix`/`--dst-prefix` -- so their hash reflects whatever the invoking
    machine's ambient `diff.noprefix`/prefix git config happens to produce.
    `cli._git_diff` pins `--src-prefix=a/ --dst-prefix=b/` (reviewlib.stats
    "Diff-identity binding" -- needed so `extract_diff_files` can parse the
    header), so on a `diff.noprefix=true` machine the REVIEWED `diff` string and
    the hook's own unprefixed recomputation are BYTE-DIFFERENT texts with
    DIFFERENT hashes -- hashing `diff` directly would then never match the stamp,
    permanently failing the pre-commit gate on any such machine (live-caught:
    this repo's own dev machine has `diff.noprefix=true`, and this exact
    divergence blocked committing this fix's own commit).

    `stamp_diff_hash` (round-5 review finding, k3+Opus): PREFER this over any
    fresh recompute -- it is the hook-compatible hash the CALLER captured at
    DIFF-DISPATCH time (`cli._stamp_hash_for_staged_diff`, called immediately
    adjacent to the `diff` this function receives), so the stamp certifies what
    the models actually reviewed. A first cut of the noprefix fix instead
    re-derived the hash HERE, independently, at stamp-WRITE time -- potentially
    MINUTES after dispatch for a real multi-model panel -- reopening a TOCTOU
    window where a concurrent index mutation during the review (a second
    agent/session in a shared checkout; AGENTS.md documents this has happened
    in production) would get silently certified as reviewed. When
    `stamp_diff_hash` is None (any caller that doesn't thread it -- a
    non-CLI/library caller of `mode_review`), falls back to that same
    independent re-derive so the stamp still writes something hook-compatible,
    just without the tightened timing guarantee.

    Also writes the `review-stamp-diff` companion (review-cli#208) so the pre-commit
    gate's trivial-follow-up tolerance (`_TRIVIAL_DELTA_BLOCK`) has a reviewed-diff
    baseline to measure drift against."""
    import hashlib

    from .process import git_repo_env

    try:
        # LC_ALL=C pins git's own messages to English so the not-a-repo classification
        # below can match on their text. Without it a localized shell or container
        # (`LC_ALL=de_DE.UTF-8` → `fatal: kein Git-Repository`) turns every review run
        # outside a repo into a spurious "the stamp could not be written" warning — a
        # false alarm in exactly the tool whose job here is to stop false silence (Fable
        # finding, iteration 10).
        probe_env = {**git_repo_env(cwd), "LC_ALL": "C"}
        p = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-path", "review-stamp"],
            cwd=cwd,
            env=probe_env,
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            # "Not a repo" and "git could not answer" are DIFFERENT outcomes and used to
            # share this one silent return (codex P1 + Fable, iteration 9). Outside a repo
            # there is no stamp-keyed gate to satisfy, so silence is right. But a real
            # failure inside one — `fatal: detected dubious ownership` in a container, git
            # metadata briefly unreadable in a shared worktree — leaves the gate installed
            # and the stamp missing, which is the "green review, rejected commit, no reason
            # anywhere" trap this return value exists to close.
            detail = (p.stderr or "").strip()
            if "not a git repository" in detail.lower():
                return None
            return f"`git rev-parse --git-path review-stamp` failed: {detail or 'no output'}"
        rel = p.stdout.strip()
        stamp = Path(rel) if os.path.isabs(rel) else Path(cwd) / rel
        if stamp_diff_hash is not None:
            digest = stamp_diff_hash
        else:
            hook_diff = subprocess.run(
                ["git", "-C", str(cwd), "diff", "--no-ext-diff", "--cached"],
                cwd=cwd,
                env=git_repo_env(cwd),
                capture_output=True,
                text=True,
                timeout=30,
            )
            # Fall back to the reviewed `diff` text if the independent recompute
            # itself fails (best-effort: never break a review over the stamp) --
            # matches the hook's own tolerance for a diff-less/non-repo state.
            stamped_text = hook_diff.stdout if hook_diff.returncode == 0 else diff
            digest = hashlib.sha256(stamped_text.encode("utf-8")).hexdigest()
        stamp.write_text(f"{digest}\n", encoding="utf-8")
    except Exception as exc:
        # Deliberately broad, as before — the stamp is never worth breaking a review
        # over — but no longer INVISIBLE: the caller turns this into one stderr line.
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    _write_review_stamp_diff(cwd, diff)
    return None


def _write_review_stamp_diff(cwd: Path, diff: str) -> str | None:
    """Companion to `_write_review_stamp` (review-cli#208): persist the RAW reviewed diff
    TEXT (not just its hash) next to the hash stamp, so the pre-commit gate can tolerate a
    small trailing follow-up instead of requiring an exact byte-for-byte restage match on
    every commit. Best-effort, like `_write_review_stamp` itself -- a failure here must
    never break a review or the exact-hash stamp it accompanies; it only means the gate's
    delta-tolerance fast path stays unavailable (falls back to exact-hash-only, same as
    before this feature existed). Unlike `_write_review_stamp`'s marker/stamp, a failure
    here is deliberately NOT surfaced as a stderr line (Opus review finding on this
    feature's own PR corrected an earlier draft's docstring that falsely claimed it was):
    the caller (`_write_review_stamp`) invokes this as a bare statement and discards the
    return, exactly matching the "never break a review" contract above -- losing the
    tolerance fast path costs at most one extra full review round, not a blocked commit,
    so it doesn't warrant the same "green review, blocked commit, no reason anywhere"
    alarm the marker/stamp notices exist to prevent. The return value exists only so a
    future caller CAN surface it if that judgment call ever changes.

    Opus round-3 finding (gate bypass): the file's FIRST LINE is the reviewed HEAD sha
    (`git rev-parse HEAD` at write time), with the raw diff text following. Without this
    binding, a stale `review-stamp-diff` left on disk after the reviewed change is
    COMMITTED becomes a fixed, small, permanent anchor: `git diff --cached` after that
    commit is measured against the OLD diff regardless of which file changed or how many
    commits have landed since, so an unrelated, unreviewed small change to a DIFFERENT
    file compares favorably against it and passes the gate -- repeatably, unboundedly,
    one small unreviewed commit at a time, directly contradicting this feature's own
    "drift is always measured from the last genuine review" safety claim. The hook
    (`_TRIVIAL_DELTA_BLOCK`) rejects the file outright when the recorded HEAD doesn't
    match the current one, falling back to the pre-existing exact-hash gate."""
    from .process import git_repo_env

    try:
        head = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            cwd=cwd,
            env=git_repo_env(cwd),
            capture_output=True,
            text=True,
        )
        # A repo with no commits yet has no HEAD to record -- writes an empty first
        # line rather than skipping the write entirely. This is a real, matchable
        # state (not a sentinel): the hook's HEAD check compares this string against
        # `git rev-parse HEAD` verbatim, and BOTH sides read empty for "no commits
        # yet" -- the documented review-then-uncommitted-followup use case this
        # feature exists for. What the check actually catches is empty-vs-non-empty
        # (reviewed pre-first-commit, but a commit landed since) or two different
        # non-empty shas (reviewed against one commit, HEAD has since moved) --
        # either way, the reviewed content and the current HEAD have diverged.
        head_line = head.stdout.strip() if head.returncode == 0 else ""
        p = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-path", "review-stamp-diff"],
            cwd=cwd,
            env=git_repo_env(cwd),
            capture_output=True,
            text=True,
        )
        if p.returncode != 0:
            return None
        rel = p.stdout.strip()
        stamp_diff = Path(rel) if os.path.isabs(rel) else Path(cwd) / rel
        stamp_diff.write_text(f"{head_line}\n{diff}", encoding="utf-8")
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return None


# The session-scoped, mtime-windowed marker that the separate `agent-tools`
# `require-review-before-commit` agent-hook checks (see that hook's docstring:
# `review … && touch "$REVIEW_MARKER"`). review-cli and agent-tools stay
# decoupled — review-cli does not import agent-tools; it just touches a
# well-known cache path on a successful staged review so a genuine review run
# satisfies that gate without the agent forging the marker. The path is
# overridable via REVIEW_MARKER (the same env the hook reads) for tests / custom
# setups; default matches the hook's DEFAULT_MARKER.
DEFAULT_REVIEW_MARKER = "~/.cache/agent-tools/last-review"


def _touch_review_marker() -> str | None:
    """Touch the agent-tools review marker so the require-review-before-commit
    hook sees "a review ran this session". Best-effort: a failure here must
    never break a review (the marker is a discipline reminder, not correctness).
    Honors the REVIEW_MARKER env var (same name the hook reads).

    Returns None on success, or the OS error text when the write failed. The return
    value exists because swallowing the failure SILENTLY reproduces the exact incident
    this marker's caller was hardened against (review-cli#350): a passing staged review
    prints nothing, the marker stays stale, the commit is blocked, and the caller — told
    by every doc that a passing staged review writes the marker — concludes the gate is
    broken and forges the marker by hand. The caller decides what to say about it; this
    function still never raises."""
    try:
        # `or DEFAULT_REVIEW_MARKER`: an exported-but-EMPTY `REVIEW_MARKER=` (a shell
        # variable that never got its value, a blank CI matrix entry) would otherwise
        # become `Path("")` — the current directory — and this would "write" a marker by
        # bumping that directory's mtime. The gate reads the same variable and now
        # normalizes it the same way (agent-tools#506); a variable set to nothing has to
        # mean the same thing on both sides or the two watch different files in silence.
        marker = Path(
            os.path.expanduser(os.environ.get("REVIEW_MARKER") or DEFAULT_REVIEW_MARKER)
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        if not marker.is_file():
            # `Path.touch()` on an EXISTING DIRECTORY succeeds — it just bumps the
            # directory's mtime — so a `REVIEW_MARKER` pointing at a directory (a typo, a
            # path built by joining an empty variable, a symlink to one) would otherwise
            # be reported as a marker successfully written while no marker FILE exists at
            # all. A gate that stats for a regular file then blocks the commit anyway,
            # with review-cli having just claimed success: the same silent-failure shape
            # this return value was added to remove (codex finding, iteration 5).
            return f"{marker} is not a regular file"
    except OSError as exc:
        # Swallow only I/O failures (unwritable path, a dir in the way, a bad
        # REVIEW_MARKER) — the marker is a discipline reminder, never correctness, so a
        # disk hiccup must not break a review. A non-OSError (e.g. a bad default
        # constant) is a real bug and is intentionally NOT swallowed.
        return str(exc) or exc.__class__.__name__
    return None


# --- install-hook tg (review-visual pre-send-photo gate) -------------------
#
# The canonical source of `pre_send_photo.py` + its descriptor template is the
# `tg-cli` repo (features/hooks/review-descriptor/), NOT review-cli — review-cli only
# provides the `review visual` verdict the hook shells out to. Before this installer
# existed, the descriptor's own template comment ("`review install-hook tg` substitutes
# the absolute cmd path") described a command that was never actually built: the hook
# was placed by hand-copying both files into ~/.agents/hooks/tg/, and that copy silently
# desynced from tg-cli twice in one day (an unmerged fix landed on the live copy by hand
# and was never carried forward when tg-cli moved on).
#
# Fix, mirrored from rig's own `install_agent_hook` action (riglib/actions/runner.py
# `_do_install_agent_hook`): install ONLY the descriptor, with `cmd` rewritten to the
# absolute path of the script INSIDE the source checkout. There is no local copy of the
# script to go stale — `git pull` (or `git submodule update`) in the tg-cli checkout IS
# the entire resync step, same as it already is for every agent-tools-sourced hook.
_TG_HOOK_REL_DIR = "features/hooks/review-descriptor"
_TG_HOOK_SCRIPT_NAME = "pre_send_photo.py"
_TG_HOOK_DESCRIPTOR_NAME = "review-visual.pre-send-photo.json"
# The exact identity tg's hook dispatcher (features/hooks/run-photo-hooks.ts
# loadDescriptors) matches on: it loads every *.json under ~/.agents/hooks/tg/ and keeps
# only the ones whose `point` equals "pre-send-photo" — a descriptor with a right-shaped
# but WRONG id/point would install "successfully" here and then simply never fire.
_TG_HOOK_EXPECTED_ID = "review-visual"
_TG_HOOK_EXPECTED_POINT = "pre-send-photo"

# Preference order: the `.files` submodule is the checkout the `tg` binary on PATH
# actually runs (a deliberately version-pinned "deployed" copy); the `xp/tg-cli` dev
# workspace is a fallback for a machine that only has the dev checkout.
_TG_CLI_SOURCE_CANDIDATES = ("~/.files/repos/tg-cli", "~/xp/tg-cli")


def _looks_like_tg_cli_checkout(p: Path) -> bool:
    return (p / _TG_HOOK_REL_DIR / _TG_HOOK_SCRIPT_NAME).is_file()


def resolve_tg_cli_source(configured: str | None = None) -> Path:
    """Resolve the tg-cli checkout that owns the pre-send-photo hook's canonical source.

    Same resolution order/shape as rig's `agent_tools_source` (riglib/catalog.py
    `resolve_source`): an explicit arg, then the `REVIEW_TG_CLI_SOURCE` env var, then a
    fixed candidate list — each validated by checking the hook script actually exists
    there, so a stale/wrong path fails loudly instead of installing a broken `cmd`."""
    # (source label, raw value) — pairs so the error message attributes a bad path to
    # where it ACTUALLY came from (an explicit `configured` arg vs. the env var), not
    # always the env var name regardless of origin (review found).
    for label, raw in (
        ("configured", configured),
        ("REVIEW_TG_CLI_SOURCE", os.environ.get("REVIEW_TG_CLI_SOURCE")),
    ):
        if raw:
            p = Path(os.path.expanduser(raw)).resolve()
            if not _looks_like_tg_cli_checkout(p):
                raise ValueError(
                    f"{label} '{raw}' is not a tg-cli checkout (expected "
                    f"{_TG_HOOK_REL_DIR}/{_TG_HOOK_SCRIPT_NAME} under {p})"
                )
            return p
    for cand in _TG_CLI_SOURCE_CANDIDATES:
        p = Path(os.path.expanduser(cand)).resolve()
        if _looks_like_tg_cli_checkout(p):
            return p
    raise ValueError(
        "no tg-cli checkout found for the pre-send-photo hook. Set REVIEW_TG_CLI_SOURCE "
        "to one, or clone tg-cli to one of: " + ", ".join(_TG_CLI_SOURCE_CANDIDATES)
    )


def _load_source_descriptor(src_descriptor: Path) -> tuple[dict | None, str | None]:
    """Load + validate the source descriptor JSON. Returns (spec, None) on success, or
    (None, message) describing the conflict otherwise — never raises.

    Requires the EXACT expected `id`/`point` (`_TG_HOOK_EXPECTED_ID` / `_TG_HOOK_EXPECTED_POINT`
    — "review-visual"/"pre-send-photo"), not merely non-empty strings: tg's own dispatcher
    (features/hooks/run-photo-hooks.ts `loadDescriptors`) only fires a descriptor whose `point`
    matches "pre-send-photo" exactly, so a right-shaped-but-wrong value would install
    "successfully" here and then silently never run — the same "looks fine, does nothing"
    failure mode a missing field would cause (review found both). `on_error`, if present, must
    be `open`/`closed` (it's optional — omitted or absent is fine). `cmd` is NOT checked here;
    the caller always overwrites it."""
    if not src_descriptor.is_file():
        return None, f"descriptor not found: {src_descriptor}"
    try:
        spec = json.loads(src_descriptor.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"bad descriptor json at {src_descriptor}: {exc}"
    if not isinstance(spec, dict):
        # A syntactically valid but non-object descriptor (`[]`, `"..."`, `null`, a bare
        # number) must be a reported conflict, not a `TypeError` crash on the caller's
        # `spec["cmd"] = ...` assignment (review found).
        return None, f"descriptor at {src_descriptor} is not a JSON object: {spec!r}"
    if spec.get("id") != _TG_HOOK_EXPECTED_ID:
        return None, (
            f"descriptor at {src_descriptor} has id={spec.get('id')!r}, expected "
            f"{_TG_HOOK_EXPECTED_ID!r} — a right-shaped but wrong id installs a descriptor "
            "the tg dispatcher will never match"
        )
    if spec.get("point") != _TG_HOOK_EXPECTED_POINT:
        return None, (
            f"descriptor at {src_descriptor} has point={spec.get('point')!r}, expected "
            f"{_TG_HOOK_EXPECTED_POINT!r} — the tg dispatcher only fires a descriptor whose "
            "point matches exactly, so a wrong-but-non-empty value would silently never run"
        )
    on_error = spec.get("on_error")
    if on_error is not None and on_error not in ("open", "closed"):
        return (
            None,
            f"descriptor at {src_descriptor} has on_error={on_error!r} (must be 'open' or 'closed')",
        )
    return spec, None


def _check_source_script(src_script: Path) -> bool:
    """Best-effort executable check, run BEFORE any write. `resolve_tg_cli_source` already
    guarantees `src_script` EXISTS (that is exactly what `_looks_like_tg_cli_checkout`
    checks), so there is nothing left to gate existence on here — only whether it's
    runnable. Returns True iff a non-executable warning was printed (never a conflict:
    on_error stays open, matching the hook's own fail-open philosophy)."""
    if not os.access(src_script, os.X_OK):
        print(f"      (warning: source script not executable: {src_script})")
        return True
    return False


def _clear_stale_local_copy(target_dir: Path, script_name: str) -> tuple[bool, bool]:
    """Remove a leftover local .py copy that predates this no-copy installer (dead weight:
    the descriptor no longer reads it, and a second copy that LOOKS authoritative is
    exactly the trap that let this hook silently desync from tg-cli in the first place).

    A REGULAR FILE or a BROKEN/dangling symlink is removed (neither can be a legitimate
    reference to anything real). A WORKING symlink is left alone — assumed to be an
    intentional convenience link someone already pointed at the canonical file.

    A failed removal is a WARNING, never a conflict: by this point the descriptor is
    already written and `cmd` already points at the source, so the hook IS installed and
    working — the leftover file is inert clutter, not a correctness problem, and must not
    sink an otherwise-successful install (review found: it previously returned exit 1 here,
    reporting a working install as a failure).

    Returns (removed, warned): `warned` is True iff a removal was ATTEMPTED and failed — the
    caller must fold this into whether "nothing to do" is still an honest final summary
    (review found: an unremovable copy previously printed its warning directly above a
    contradicting "nothing to do" line, same class of bug already fixed for the OTHER two
    non-idempotent paths, wrote_descriptor and warned_non_executable)."""
    local_copy = target_dir / script_name
    if local_copy.is_symlink():
        if (
            local_copy.exists()
        ):  # `exists()` follows the link — True here means it resolves
            return False, False  # a working symlink; leave it alone
        label, note = "broken symlink", "dangling; cmd points at the source checkout"
    elif local_copy.exists():
        label, note = "stale local copy", "cmd now points at the source checkout"
    else:
        return False, False
    try:
        local_copy.unlink()
        print(f"  - removed {label}  {local_copy} ({note})")
        return True, False
    except OSError as exc:
        print(
            f"      (warning: could not remove {label} {local_copy}: {exc} — harmless, the hook doesn't read it)"
        )
        return False, True


def install_hook_tg() -> int:
    """`review install-hook tg` — install/refresh the tg `pre-send-photo` review-visual
    gate descriptor. Idempotent; safe to re-run after every tg-cli update (that's the
    whole point: re-running never needs to copy anything new, `cmd` already points at
    the live checkout)."""
    home = Path.home()
    try:
        source = resolve_tg_cli_source()
    except ValueError as exc:
        print(f"  ! conflict  {exc}")
        return 1

    src_dir = source / _TG_HOOK_REL_DIR
    src_descriptor = src_dir / _TG_HOOK_DESCRIPTOR_NAME
    src_script = src_dir / _TG_HOOK_SCRIPT_NAME
    spec, error = _load_source_descriptor(src_descriptor)
    if error is not None:
        print(f"  ! conflict  {error}")
        return 1

    warned_non_executable = _check_source_script(src_script)

    spec["cmd"] = str(src_script.resolve())
    content = json.dumps(spec, indent=2) + "\n"

    target_dir = home / ".agents" / "hooks" / "tg"
    target_descriptor = target_dir / _TG_HOOK_DESCRIPTOR_NAME
    try:
        wrote_descriptor = _write_if_changed(target_descriptor, content)
    except OSError as exc:
        print(f"  ! conflict  {target_descriptor} could not be written ({exc})")
        return 1
    print(
        f"  {'+ wrote/updated' if wrote_descriptor else '✓ already configured'}  {target_descriptor}"
    )
    print(f"      cmd -> {spec['cmd']}")
    print(
        f"      (sourced live from {source}; `git pull` there resyncs the hook — no re-install needed)"
    )

    removed_stale_copy, warned_stale_removal = _clear_stale_local_copy(
        target_dir, _TG_HOOK_SCRIPT_NAME
    )

    # "nothing to do" is only true when NOTHING changed and nothing needs attention — a
    # rewritten descriptor, a removed stale copy, a failed-but-harmless removal attempt, or a
    # live non-executable-script warning must all keep the summary from claiming "nothing to
    # do" (review found: each of these could previously print a real action/warning right
    # above a contradicting "nothing to do" line).
    if wrote_descriptor or removed_stale_copy:
        print(
            f"review: install-hook tg — done. Descriptor points at {source}. Idempotent; re-run anytime."
        )
    elif warned_non_executable or warned_stale_removal:
        print(
            "review: install-hook tg — descriptor already configured, but see the warning above."
        )
    else:
        print(
            "review: install-hook tg — already configured, nothing to do. Idempotent; re-run anytime."
        )
    return 0


def _rig_delegate_helper():
    """Best-effort import of the shared `agenttools_rig_delegate` lib (agent-tools#282). It
    is an in-ecosystem editable install, like `agenttools_service` — not always present.
    Returns the module, or None if it isn't importable, so callers degrade to exactly
    today's direct install rather than crash."""
    try:
        import agenttools_rig_delegate
    except ImportError:
        return None
    return agenttools_rig_delegate


def _commit_gate_active() -> bool:
    """True iff a global commit gate is in place AND it is the REVIEW gate — not merely any
    executable `pre-commit`. `core.hooksPath` must resolve to an absolute dir whose executable
    `pre-commit` enforces review-before-commit, which one of two mechanisms provides:
      * rig's `git_hooks.dispatcher` — a COMPOSING pre-commit that runs an executable
        `review-gate` sibling stage in the same dir; or
      * the direct installer — a self-contained pre-commit carrying `_PRECOMMIT_MARKER`.
    An UNRELATED pre-existing global `pre-commit` (any other executable file) must NOT satisfy
    this postcondition. If it did, `rig apply --only git_hooks` exiting 0 on a repo that
    declares no gate would let a user's unrelated global hook masquerade as the review gate:
    delegation would print "rig owns the hooks" and return 0, silently leaving the user
    without the review gate they explicitly asked for (codex review)."""
    cur = subprocess.run(
        ["git", "config", "--global", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
    )
    raw = cur.stdout.strip()
    if not raw:
        return False
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return False
    hooks_dir = Path(expanded)
    pre_commit = hooks_dir / "pre-commit"
    if not (pre_commit.is_file() and os.access(pre_commit, os.X_OK)):
        return False
    try:
        body = pre_commit.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Unreadable (permissions) or raced-away between the stat and the read — treat as
        # "gate not provably in place" so the caller falls back to the direct installer
        # instead of crashing the whole `install-commit-hook` on a traceback.
        return False
    if _PRECOMMIT_MARKER in body:
        return True  # the direct self-contained gate
    # rig's composing dispatcher (agent-tools `git-hooks/global-dispatcher/hooks/`) is installed
    # INTO `core.hooksPath` as a set of sibling files — verified against rig: its `pre-commit`
    # composer runs `"$HOOK_DIR/review-gate"` (HOOK_DIR == core.hooksPath), and rig-cli's own drift
    # check enumerates `pre-commit/commit-msg/pre-push/review-gate` as the composers living in that
    # one dir. So the review stage IS a same-dir `review-gate` sibling of the composed pre-commit.
    # Require BOTH signals to prove it — each closes one false-positive direction the other leaves
    # open (codex review, two rounds): the composer's pre-commit body must reference `review-gate`
    # (an orphan/leftover `review-gate` file next to an UNRELATED hook that never invokes it does
    # not count) AND an executable `review-gate` file must actually be present (a mere comment-only
    # mention with no stage file does not count). An ordinary unrelated user hook satisfies
    # neither (a contrived hook that both comments the token AND leaves a stray stage file is
    # not worth guarding beyond this).
    review_gate = hooks_dir / "review-gate"
    return (
        "review-gate" in body
        and review_gate.is_file()
        and os.access(review_gate, os.X_OK)
    )


def install_commit_hook() -> int:
    """Install a GLOBAL git pre-commit hook enforcing review-before-commit.
    Opt-in (not run by install-skill) because it affects every repo.

    When rig is present, let rig OWN the git hooks (single source of truth): rig's
    `git_hooks.dispatcher` composes a global pre-commit whose `review-gate` stage is this
    command's mechanism (`agent-tools/git-hooks/global-dispatcher/hooks/review-gate`, ported
    verbatim). Delegation is SCOPED to `rig apply --only git_hooks` so it never reconciles
    unrelated areas (permissions / GitHub / tools) as a side effect of installing a commit
    hook.

    Three outcomes:
      * rig absent, or the shared helper not installed -> the direct installer runs unchanged.
      * rig present and it FAILS (non-zero) -> surface that exit code as-is (a real rig failure
        is never swallowed into a fallback — that would recreate the double-write).
      * rig present, succeeds, BUT the gate is not actually in place afterward (e.g. this repo
        declares no `git_hooks:` block, so `rig apply` is a no-op for hooks) -> fall back to the
        direct installer so the user still gets the hook they explicitly asked for. This is
        distinct from "rig failed": rig simply doesn't manage this gate here."""
    rig_delegate = _rig_delegate_helper()
    if rig_delegate is None or not rig_delegate.rig_available():
        return _install_commit_hook_direct()
    result = rig_delegate.delegate(["apply", "--only", "git_hooks"])
    if result.returncode != 0:
        return result.returncode  # a real rig failure — surfaced, not swallowed
    if _commit_gate_active():
        print(
            "review: rig owns the global git hooks (delegated to `rig apply --only git_hooks`)."
        )
        return 0
    # rig succeeded but provisions no commit gate here (no git_hooks block) — honor the
    # explicit request via the direct installer rather than silently leaving no gate.
    print("review: rig installed no commit gate here — installing directly.")
    return _install_commit_hook_direct()


def _install_commit_hook_direct() -> int:
    """The direct installer (pre-rig-delegation behavior, unchanged). Writes the global git
    pre-commit hook + sets `core.hooksPath` itself. Used as the `install_commit_hook`
    fallback when rig is absent or the delegation helper isn't installed."""
    home = Path.home()
    hooks_dir = home / ".config" / "git" / "hooks"
    # Respect an existing global hooksPath rather than hijacking it.
    cur = subprocess.run(
        ["git", "config", "--global", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
    )
    existing_path = cur.stdout.strip()
    if existing_path:
        expanded = os.path.expanduser(existing_path)
        if not os.path.isabs(expanded):
            # Git resolves a relative core.hooksPath per-repo, so a single global
            # gate can't live there. Refuse rather than silently misinstall.
            print(f"review: global core.hooksPath is relative ('{existing_path}').")
            print(
                "        Git resolves it per repository, so a global gate can't be placed"
            )
            print(
                "        there. Set an absolute core.hooksPath (or unset it) and re-run."
            )
            return 1
        hooks_dir = Path(expanded)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    pre_commit = hooks_dir / "pre-commit"

    already = (
        False  # the gate is ALREADY installed with our exact content AND executable
    )
    if pre_commit.exists():
        body = pre_commit.read_text(encoding="utf-8", errors="replace")
        if _PRECOMMIT_MARKER not in body:
            print(
                f"review: a pre-commit hook already exists at {pre_commit} and is NOT ours."
            )
            print(
                "        Not overwriting. Merge the gate manually or remove that hook first."
            )
            return 1
        # "Already configured" requires the exec bit too: a 0644 hook with our exact content
        # is SKIPPED by git, so reporting "already active" would be a false claim (codex
        # review). Re-chmod below in that case instead of an idempotent no-op.
        already = body == _PRECOMMIT and os.access(pre_commit, os.X_OK)
    # core.hooksPath is "already configured" iff it already points at our hooks_dir. Compare
    # RESOLVED paths so a symlinked HOME (macOS /var -> /private/var, firmlinks) doesn't make
    # an equivalent path look different and needlessly break "nothing to do" (glm review). Both
    # sides already exist here (hooks_dir was just mkdir'd; it equals existing_path's dir).
    hookspath_ok = bool(existing_path) and (
        Path(os.path.expanduser(existing_path)).resolve() == hooks_dir.resolve()
    )

    if already and hookspath_ok:
        # Idempotent no-op: report the INSTALLED state (ROADMAP "install-* commands must show
        # INSTALLED state") instead of silently rewriting identical content.
        print(f"  ✓ already configured  {pre_commit}")
        print(f"  ✓ already configured  core.hooksPath -> {hooks_dir}")
        print(
            "review: commit gate already active — nothing to do. "
            "`review diff --staged --task TASK-CODE` before committing; "
            "bypass with REVIEW_SKIP=1 or --no-verify."
        )
        return 0

    if not already:
        try:
            pre_commit.write_text(_PRECOMMIT, encoding="utf-8")
            pre_commit.chmod(0o755)
        except OSError as exc:
            # A write/chmod that fails (read-only FS, EPERM, ENOSPC) must be a structured
            # conflict + non-zero exit, NOT a traceback — same contract as install-skill's
            # write paths (glm review). Don't print "gate active": it isn't.
            print(
                f"  ! conflict  {pre_commit} could not be written ({exc}) — fix permissions "
                "and re-run."
            )
            return 1
        print(f"  + wrote {pre_commit}")
    else:
        print(f"  ✓ already configured  {pre_commit}")
    if not existing_path:
        # If git can't write the global config (locked / read-only / corrupt $GIT_CONFIG_GLOBAL),
        # the hook file exists but git never points at it — so commits would NOT be gated. Don't
        # claim "+ set" / "gate active" in that case: report a `! conflict` + non-zero exit
        # (codex review).
        cfg = subprocess.run(
            ["git", "config", "--global", "core.hooksPath", str(hooks_dir)],
            capture_output=True,
            text=True,
        )
        if cfg.returncode != 0:
            print(
                f"  ! conflict  could not set global core.hooksPath -> {hooks_dir} "
                f"({cfg.stderr.strip() or 'git config failed'}). The hook is written but git "
                "is not pointed at it; fix your global git config and re-run."
            )
            return 1
        print(f"  + set global core.hooksPath -> {hooks_dir}")
    elif hookspath_ok:
        print(f"  ✓ already configured  core.hooksPath -> {hooks_dir}")
    print(
        "review: commit gate active. `review diff --staged --task TASK-CODE` before "
        "committing; bypass with REVIEW_SKIP=1 or --no-verify."
    )
    return 0
