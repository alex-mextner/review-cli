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
  Read-only multi-model code review and AI panel. Use BEFORE committing to check a
  diff across several models at once, to get a multi-model second opinion on a
  question (--just-ask), to settle a contested technical decision with cited
  evidence (--quorum), or to brainstorm an open design space across rotating expert
  personas in a loop (--brainstorm). e.g. `review` on the current diff.
metadata:
  author: alex-mextner
  repo: https://github.com/alex-mextner/review-cli
---

# review — multi-model read-only code review + panels

Runs your git diff (or a question/topic) across several model backends in parallel.

## NEVER wrap `review` in a short timeout — it takes MINUTES
`review`, `review --quorum`, and `review --brainstorm` are multi-model and/or
multi-round: they fan out to several model backends in parallel, and the panel
modes run several rounds plus a final moderator synthesis. A plain diff review of a
full board is typically a few minutes; a `--brainstorm` is commonly 10–20 minutes
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
- If you must bound it, give it MINUTES, not seconds (e.g. background it and poll),
  and never a cap below the printed ETA.

## Invocation
```
review -C <repo>             # review current unstaged diff across default models
review -C <repo> --staged    # review the staged diff (pre-commit)
review -C <repo> -m codex -m gemini    # pick backends (repeat or comma-separate)
review -C <repo> --just-ask "Q"        # multi-model answer to a question (no diff needed)
review -C <repo> --quorum "Q"          # experts answer + a moderator finds consensus/disagreement
review -C <repo> --brainstorm "TOPIC"  # iterative persona ideation in a loop, with a moderator
```

## ALWAYS pass `-C <project-root>`
`review` runs the diff and the claude/opus workspace in `-C` (default: the current
directory). Agents often invoke `review` from a scratch or temp dir, so WITHOUT
`-C` it silently reviews the wrong place (commonly /tmp) and returns an empty or
irrelevant result. Always pass `-C <absolute repo path>`. If `-C` is not inside a
git repo, review resolves to the repo root when it can and otherwise prints a
loud warning — heed it. When piping into review non-interactively, also redirect
stdin (`review -C <repo> --just-ask "Q" < /dev/null`); review reads stdin for an
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
- **codex / opencode** — `cli` only (agent CLIs that carry their own auth).
- **z.ai, commandcode** — `api` only (no such CLI exists). Forcing `cli` on an
  api-only backend (`REVIEW_COMMANDCODE_MODE=cli`) is a hard error, not a silent
  fall-through to the API.

## Keyed HTTP backends: z.ai (GLM) and commandcode
OpenAI-compatible `POST /chat/completions` REST backends — no CLI, just a key:
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
- Before committing — sanity-check a diff across multiple models in parallel.
- For a hard decision — `--quorum` (settle with cited evidence) or `--brainstorm`
  (explore an open design space across rotating expert roles, in a loop). The
  moderator defaults to opus and falls back to codex/gemini automatically.
- For a quick multi-model second opinion — `--just-ask`.

Pair with `tg` to post the chosen options / pros-cons to Telegram.
"""
SKILL_BLURB = (
    "`review` — multi-model read-only code review + AI panels "
    "(codex/claude/gemini/opencode): `review -C <repo>` (diff), "
    "`review -C <repo> --quorum \"Q\"`, `review -C <repo> --brainstorm \"topic\"`. "
    "Always pass -C <project-root>. Use before commits and for hard decisions. "
    "NEVER wrap it in a short timeout — it is multi-model / multi-round and takes "
    "MINUTES (brainstorm 10–20m); it prints the expected duration for your pool "
    "size at startup, so wait for that, don't short-timeout it."
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


def _append_marked(path, tool: str, blurb: str) -> None:
    import re
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    start, end = f"<!-- skill:{tool} -->", f"<!-- /skill:{tool} -->"
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    existing = re.sub(re.escape(start) + r".*?" + re.escape(end) + r"\n?", "", existing, flags=re.S)
    block = f"{start}\n{blurb}\n{end}\n"
    p.write_text((existing.rstrip() + "\n\n" + block) if existing.strip() else block, encoding="utf-8")


def _ensure_sessionstart_hook(home) -> bool:
    """Idempotently add a SessionStart hook to ~/.claude/settings.json that
    surfaces installed agent CLIs. Conservative: never removes unrelated config."""
    settings = Path(home) / ".claude" / "settings.json"
    if not settings.parent.is_dir():
        return False
    try:
        data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    except (json.JSONDecodeError, OSError):
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


def install_agent_skill(name: str, skill_md: str, blurb: str) -> int:
    home = Path.home()
    written = []

    skill_dir = home / ".agents" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    written.append(str(skill_dir / "SKILL.md"))
    blurbs = home / ".agents" / "skills" / ".blurbs"
    blurbs.mkdir(parents=True, exist_ok=True)
    (blurbs / f"{name}.md").write_text(f"- {blurb}\n", encoding="utf-8")

    claude_skills = home / ".claude" / "skills"
    if claude_skills.is_dir():
        link = claude_skills / name
        if not link.exists():
            try:
                link.symlink_to(Path("..") / ".." / ".agents" / "skills" / name)
            except OSError:
                pass

    harness_files = [
        ("claude", home / ".claude" / "CLAUDE.md", ("~/.claude",)),
        ("codex", home / ".codex" / "AGENTS.md", ("~/.codex",)),
        ("opencode", home / ".config" / "opencode" / "AGENTS.md", ("~/.config/opencode",)),
        ("gemini", home / ".gemini" / "GEMINI.md", ("~/.gemini",)),
    ]
    for cmd, path, dirs in harness_files:
        if _detected(cmd, *dirs):
            _append_marked(path, name, blurb)
            written.append(str(path))

    if (home / ".claude").is_dir():
        if _ensure_sessionstart_hook(home):
            written.append("SessionStart hook -> ~/.claude/settings.json")

    for w in written:
        print(f"  ✓ {w}")
    print(f"{name}: install-skill done ({len(written)} target(s)). Re-run anytime; idempotent.")
    return 0


def install_skill() -> int:
    return install_agent_skill(SKILL_NAME, SKILL_MD, SKILL_BLURB)


_PRECOMMIT_MARKER = "# review-before-commit-gate"
# Hash must match exactly what `review --staged` reviews (`git diff --no-ext-diff
# --cached`) and the stamp path must resolve via `git rev-parse --git-path` so it
# works in worktrees and repos whose `.git` is a pointer file.
_PRECOMMIT = """\
#!/bin/sh
""" + _PRECOMMIT_MARKER + """ (installed by `review install-commit-hook`)
# Blocks a commit whose staged diff has not been reviewed. Bypass with
# REVIEW_SKIP=1 git commit ...   or   git commit --no-verify
root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0

# A global core.hooksPath shadows each repo's own pre-commit, so run it
# explicitly first — tests/formatters/secret-scanners must still gate. Resolve
# via git-path so worktrees/submodules (where .git is a file) work too.
local_hook="$(git rev-parse --git-path hooks/pre-commit)"
case "$local_hook" in /*) : ;; *) local_hook="$root/$local_hook" ;; esac
if [ -x "$local_hook" ] && [ "$local_hook" != "$0" ]; then
  "$local_hook" "$@" || exit $?
fi

[ -n "$REVIEW_SKIP" ] && exit 0
[ -z "$(git diff --cached --name-only)" ] && exit 0
h=$(git diff --no-ext-diff --cached | shasum -a 256 | cut -d' ' -f1)
stamp=$(git rev-parse --git-path review-stamp)
if [ -f "$stamp" ] && grep -q "$h" "$stamp"; then exit 0; fi
echo "review-before-commit: staged changes have not been reviewed." >&2
echo "  run:  review --staged      (then commit)" >&2
echo "  skip: REVIEW_SKIP=1 git commit ...   |   git commit --no-verify" >&2
exit 1
"""


def _write_review_stamp(cwd: Path, diff: str) -> None:
    """Record that this exact diff was reviewed, so the optional pre-commit gate
    can verify it. Uses `git rev-parse --git-path` so worktrees / pointer-file
    .git resolve correctly. Best-effort: never breaks a review on failure."""
    import hashlib
    try:
        p = subprocess.run(
            ["git", "rev-parse", "--git-path", "review-stamp"], cwd=cwd, capture_output=True, text=True
        )
        if p.returncode != 0:
            return
        rel = p.stdout.strip()
        stamp = Path(rel) if os.path.isabs(rel) else Path(cwd) / rel
        digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        stamp.write_text(f"{digest}\n", encoding="utf-8")
    except Exception:
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

    if pre_commit.exists():
        body = pre_commit.read_text(encoding="utf-8", errors="replace")
        if _PRECOMMIT_MARKER not in body:
            print(f"review: a pre-commit hook already exists at {pre_commit} and is NOT ours.")
            print("        Not overwriting. Merge the gate manually or remove that hook first.")
            return 1
    pre_commit.write_text(_PRECOMMIT, encoding="utf-8")
    pre_commit.chmod(0o755)
    if not existing_path:
        subprocess.run(["git", "config", "--global", "core.hooksPath", str(hooks_dir)], check=False)
        print(f"  ✓ set global core.hooksPath -> {hooks_dir}")
    print(f"  ✓ wrote {pre_commit}")
    print("review: commit gate active. `review --staged` before committing; "
          "bypass with REVIEW_SKIP=1 or --no-verify.")
    return 0
