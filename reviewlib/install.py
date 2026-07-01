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
  committing to check a diff across several models at once (`review diff`), to get a
  multi-model second opinion on a question (`review just-ask "Q"`), to settle a
  contested technical decision with cited evidence (`review quorum "Q"`), or to
  brainstorm an open design space across rotating expert personas in a loop
  (`review brainstorm "TOPIC"`). e.g. `review diff` on the current diff.
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
(min 5 / max 8 rounds + a final synthesis pass). Wrapping the command in
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
review diff -C <repo>                  # review current unstaged diff across the failover pool (top 4 available)
review diff -C <repo> --staged         # review the staged diff (pre-commit)
review diff -C <repo> --pool 8         # run all 8 available board seats (--pool 0 also = all); default pool is 4
review diff -C <repo> -m codex -m gemini    # pick backends (repeat or comma-separate); bypasses the board
review just-ask "Q" -C <repo>          # multi-model answer to a question (no diff needed)
review quorum "Q" -C <repo>            # experts answer + a moderator finds consensus/disagreement
review brainstorm "TOPIC" -C <repo>    # iterative persona ideation in a loop, with a moderator
review brainstorm "TOPIC" --diff -C <repo>   # …+ the working-tree (or --staged) diff -> brainstorm ABOUT that change
review diff -C <repo> -o out.md        # write the result to a file (still prints to stdout)
```
A bare `review` (no subcommand) prints HELP — it does NOT run a diff review (use
`review diff`). The removed `review review` verb and `review -C <repo>` (flags with no
verb) print a one-line `review diff` pointer and exit non-zero. The OLD mode flags
(`--brainstorm` / `--quorum` / `--just-ask`) were likewise REMOVED. `--visual <img>` stays
a COMPOSABLE flag that rides any subcommand (it is not a mode), e.g. `review diff --visual`.
For configuration (config file, model/board selection, keys/auth) run `review help config`
(alias `review --help config`); per-subcommand flags are on `review <mode> --help`.

## Save the result to a file: `-o FILE`, NOT `> FILE`
Use `review diff -C <repo> -o out.md`, NOT `review diff -C <repo> … > out.md`. Under zsh
`noclobber` (a common shell default), `> out.md` REFUSES to overwrite an existing
file and the command dies silently — you get no review and no error. `-o` writes
the result with Python (`open(...,"w")`), which bypasses the shell redirect (and
thus noclobber) entirely: it creates parent dirs, always overwrites, and STILL
prints the result to stdout so you see it live. So whenever you want the review in
a file, reach for `-o file.md`, never `> file.md`.

## Reviewer board + `--pool` (priority-ordered failover pool)
A plain `review diff` runs the built-in **reviewer board** — a **priority-ordered** panel of 8
models (strongest first) where each model also gets its own role/lens (architecture,
correctness, consistency, performance, quality, security, tests, contracts). The active
**pool is 4**, chosen by **priority + availability** with two failovers so the run keeps
4 working reviewers: **startup failover** picks the top 4 AVAILABLE seats by priority (a
higher-priority but unavailable seat is skipped, the next pulled up); **mid-run failover**
replaces a seat that fails DURING the run (backend error, timeout, empty output, or an
"unavailable" reply such as a paywalled model) with the next-priority **reserve**, until 4
working verdicts are produced or the reserve is exhausted (then it degrades and says so).
Before promoting a reserve, a failed seat is first **retried on the same model** when the
failure is **transient** (429 rate-limit / 529 or 5xx overload / timeout / "overloaded" /
"service unavailable") with backoff + jitter; a **seat-fatal** failure (auth / bad model /
501 / refusal) is never retried and falls straight to the reserve. `--retry N` (or
`$REVIEW_RETRY_COUNT`; default 2, `0` disables) sizes the in-seat retry budget.
`--pool N` sizes the pool (top-N available, same failover); `--pool 0`/`--pool 8` runs all
available. The board is **never disabled** — there is **no `--no-board` flag**. An explicit
`-m` (or a `models:` list in config.yaml) bypasses the board and runs exactly those models.
`review --show-board` lists the seats in priority order with their pool/reserve/unavail
tier and availability.

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
stdin (`review just-ask "Q" -C <repo> < /dev/null`); review reads stdin for an
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
- Before committing — sanity-check a diff across multiple models in parallel (`review diff`).
- For a hard decision — `review quorum "Q"` (settle with cited evidence) or
  `review brainstorm "TOPIC"` (explore an open design space across rotating expert roles,
  in a loop). The moderator defaults to opus and falls back to codex/gemini automatically.
- For a quick multi-model second opinion — `review just-ask "Q"`.

Pair with `tg` to post the chosen options / pros-cons to Telegram.
"""
SKILL_BLURB = (
    "`review` — multi-model read-only code review + AI panels "
    "(codex/claude/gemini/opencode). Modes are SUBCOMMANDS (the verb leads; -C follows): "
    "`review diff -C <repo>` (diff review), `review quorum \"Q\" -C <repo>`, "
    "`review brainstorm \"topic\" -C <repo>`, `review just-ask \"Q\" -C <repo>`. "
    "A bare `review` prints HELP — the diff review is `review diff` (NOT a bare "
    "`review`); the old --quorum/--brainstorm/--just-ask flags were removed. "
    "Always pass -C <project-root>. Use before commits and for hard decisions. "
    "NEVER wrap it in a short timeout — it is multi-model / multi-round and takes "
    "MINUTES (brainstorm 10–20m); it prints the expected duration for your pool "
    "size at startup, so wait for that, don't short-timeout it. Use NO external "
    "timeout at all — review carries its own internal <=4h backstop."
)

_HOOK_MARKER = "# agent-tools-awareness"
_HOOK_COMMAND = (
    "sh -c 'd=\"$HOME/.agents/skills/.blurbs\"; ls \"$d\"/*.md >/dev/null 2>&1 && "
    '{ printf \"Agent CLI tools installed on this machine (prefer them):\\n\"; '
    "cat \"$d\"/*.md; }' " + _HOOK_MARKER
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
    existing = re.sub(re.escape(start) + r".*?" + re.escape(end) + r"\n?", "", before, flags=re.S)
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
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
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
        settings.with_suffix(".json.bak").write_text(settings.read_text(encoding="utf-8"), encoding="utf-8")
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


def install_agent_skill(name: str, skill_md: str, blurb: str) -> int:
    """Idempotently install the agent skill across detected harnesses. Reports each target's
    STATE (ROADMAP "install-* commands must show INSTALLED state"): a green check + "already
    configured" when nothing changed, "+ wrote/updated" when it (re)wrote. A re-run on a
    fully-installed machine shows all ✓ and "already configured — nothing to do"."""
    home = Path.home()
    # (label, changed?) per target — `changed` False == already-configured.
    results: list[tuple[str, bool]] = []
    conflicts: list[str] = []  # targets we could NOT configure (left as-is) — block "nothing to do"

    def _write_target(path: Path, content: str) -> None:
        # A write that fails (read-only FS, ENOSPC, EPERM, immutable flag) must become a
        # `! conflict` (non-zero exit), NOT a mid-loop crash that strands later targets and
        # prints a traceback instead of the documented conflict output (glm review). Mirrors
        # the per-harness-file handling below.
        try:
            results.append((str(path), _write_if_changed(path, content)))
        except (OSError, ValueError) as exc:
            conflicts.append(f"{path} could not be written ({exc}) — fix permissions and re-run")

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
                target_desc = "an unreadable target" if points_at is None else f"{points_at}"
                conflicts.append(f"{link} is a symlink to {target_desc} (expected {want})")
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
        ("opencode", home / ".config" / "opencode" / "AGENTS.md", ("~/.config/opencode",)),
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
                conflicts.append(f"{path} is not readable as UTF-8 ({exc}) — left as-is, fix it manually")

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
        print(f"{name}: install-skill — {changed} updated, "
              f"{len(results) - changed} already configured, "
              f"{len(conflicts)} CONFLICT(S) left unconfigured. Resolve the conflict(s) "
              "above and re-run.")
        return 1
    if changed == 0:
        print(f"{name}: install-skill — already configured, nothing to do "
              f"({len(results)} target(s) ✓). Idempotent; re-run anytime.")
    else:
        print(f"{name}: install-skill done — {changed} updated, "
              f"{len(results) - changed} already configured ({len(results)} target(s)). "
              "Idempotent; re-run anytime.")
    return 0


def install_skill() -> int:
    return install_agent_skill(SKILL_NAME, SKILL_MD, SKILL_BLURB)


_PRECOMMIT_MARKER = "# review-before-commit-gate"
# Hash must match exactly what `review diff --staged` reviews (`git diff --no-ext-diff
# --cached`) and the stamp path must resolve via `git rev-parse --git-path` so it
# works in worktrees and repos whose `.git` is a pointer file.
# `command git` bypasses shell aliases/functions (e.g. an rtk-style wrapper that rewrites
# `git diff` output) so the hash matches the one written by review-cli, which calls the
# real git binary via subprocess directly.
_PRECOMMIT = """\
#!/bin/sh
""" + _PRECOMMIT_MARKER + """ (installed by `review install-commit-hook`)
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
echo "review-before-commit: staged changes have not been reviewed." >&2
echo "  run:  review diff --staged      (then commit)" >&2
echo "  skip: REVIEW_SKIP=1 git commit ...   |   git commit --no-verify" >&2
exit 1
"""


def _write_review_stamp(cwd: Path, diff: str) -> None:
    """Record that this exact diff was reviewed, so the optional pre-commit gate
    can verify it. Uses `git rev-parse --git-path` so worktrees / pointer-file
    .git resolve correctly. Best-effort: never breaks a review on failure.

    The rev-parse is anchored to `cwd` (`git -C`) AND runs with the repo-pinning git env
    stripped (`git_repo_env`), matching the diff probe in cli._git_diff: a leaked
    GIT_DIR/GIT_WORK_TREE must not write the stamp into an UNRELATED repo while the diff was
    read from `cwd`. The stamp and the diff it stamps stay anchored to the SAME `-C` repo
    (the #18 stamp/tool alignment, kept under the #71 env-leak fix)."""
    import hashlib

    from .process import git_repo_env
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-path", "review-stamp"],
            cwd=cwd, env=git_repo_env(cwd), capture_output=True, text=True,
        )
        if p.returncode != 0:
            return
        rel = p.stdout.strip()
        stamp = Path(rel) if os.path.isabs(rel) else Path(cwd) / rel
        digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        stamp.write_text(f"{digest}\n", encoding="utf-8")
    except Exception:
        pass


# The session-scoped, mtime-windowed marker that the separate `agent-tools`
# `require-review-before-commit` agent-hook checks (see that hook's docstring:
# `review … && touch "$REVIEW_MARKER"`). review-cli and agent-tools stay
# decoupled — review-cli does not import agent-tools; it just touches a
# well-known cache path on a successful staged review so a genuine review run
# satisfies that gate without the agent forging the marker. The path is
# overridable via REVIEW_MARKER (the same env the hook reads) for tests / custom
# setups; default matches the hook's DEFAULT_MARKER.
DEFAULT_REVIEW_MARKER = "~/.cache/agent-tools/last-review"


def _touch_review_marker() -> None:
    """Touch the agent-tools review marker so the require-review-before-commit
    hook sees "a review ran this session". Best-effort: a failure here must
    never break a review (the marker is a discipline reminder, not correctness).
    Honors the REVIEW_MARKER env var (same name the hook reads)."""
    try:
        marker = Path(
            os.path.expanduser(os.environ.get("REVIEW_MARKER", DEFAULT_REVIEW_MARKER))
        )
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError:
        # Swallow only I/O failures (unwritable path, a dir in the way, a bad
        # REVIEW_MARKER) — the marker is a discipline reminder, never correctness, so a
        # disk hiccup must not break a review. A non-OSError (e.g. a bad default
        # constant) is a real bug and is intentionally NOT swallowed.
        pass


def install_commit_hook() -> int:
    """Install a GLOBAL git pre-commit hook enforcing review-before-commit.
    Opt-in (not run by install-skill) because it affects every repo."""
    home = Path.home()
    hooks_dir = home / ".config" / "git" / "hooks"
    # Respect an existing global hooksPath rather than hijacking it.
    cur = subprocess.run(
        ["git", "config", "--global", "--get", "core.hooksPath"], capture_output=True, text=True
    )
    existing_path = cur.stdout.strip()
    if existing_path:
        expanded = os.path.expanduser(existing_path)
        if not os.path.isabs(expanded):
            # Git resolves a relative core.hooksPath per-repo, so a single global
            # gate can't live there. Refuse rather than silently misinstall.
            print(f"review: global core.hooksPath is relative ('{existing_path}').")
            print("        Git resolves it per repository, so a global gate can't be placed")
            print("        there. Set an absolute core.hooksPath (or unset it) and re-run.")
            return 1
        hooks_dir = Path(expanded)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    pre_commit = hooks_dir / "pre-commit"

    already = False  # the gate is ALREADY installed with our exact content AND executable
    if pre_commit.exists():
        body = pre_commit.read_text(encoding="utf-8", errors="replace")
        if _PRECOMMIT_MARKER not in body:
            print(f"review: a pre-commit hook already exists at {pre_commit} and is NOT ours.")
            print("        Not overwriting. Merge the gate manually or remove that hook first.")
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
        print("review: commit gate already active — nothing to do. `review diff --staged` "
              "before committing; bypass with REVIEW_SKIP=1 or --no-verify.")
        return 0

    if not already:
        try:
            pre_commit.write_text(_PRECOMMIT, encoding="utf-8")
            pre_commit.chmod(0o755)
        except OSError as exc:
            # A write/chmod that fails (read-only FS, EPERM, ENOSPC) must be a structured
            # conflict + non-zero exit, NOT a traceback — same contract as install-skill's
            # write paths (glm review). Don't print "gate active": it isn't.
            print(f"  ! conflict  {pre_commit} could not be written ({exc}) — fix permissions "
                  "and re-run.")
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
            capture_output=True, text=True,
        )
        if cfg.returncode != 0:
            print(f"  ! conflict  could not set global core.hooksPath -> {hooks_dir} "
                  f"({cfg.stderr.strip() or 'git config failed'}). The hook is written but git "
                  "is not pointed at it; fix your global git config and re-run.")
            return 1
        print(f"  + set global core.hooksPath -> {hooks_dir}")
    elif hookspath_ok:
        print(f"  ✓ already configured  core.hooksPath -> {hooks_dir}")
    print("review: commit gate active. `review diff --staged` before committing; "
          "bypass with REVIEW_SKIP=1 or --no-verify.")
    return 0
