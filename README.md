# review-cli

**Multi-model read-only code review from a single command.**

Runs your git diff through multiple AI backends **in parallel**, collects their findings,
and prints them side by side. Four modes let you go from a quick pre-commit sanity check
all the way to a structured expert panel that builds consensus or explores a design space.
Built for use from any shell or AI agent harness (Claude Code, Codex, opencode).

---

## Install

**One-liner** (installs deps, links `review` into PATH, registers the skill):

```bash
curl -fsSL https://raw.githubusercontent.com/alex-mextner/review-cli/main/install.sh | bash
```

**pipx alternative:**

```bash
pipx install git+https://github.com/alex-mextner/review-cli
```

After install, run `review install-skill` to register the tool into agent harnesses
(`~/.agents/skills/review/`) so that Claude Code, Codex, opencode, and Gemini agents
know `review` exists and can call it. The one-liner above runs this automatically.
`install-skill` is idempotent — safe to re-run.

---

## Quick start

```bash
# Review unstaged diff with your default backends
review

# Review staged changes
review --staged

# Add backends to the defaults
review -m codex -m fable5 -m gemini

# Ask all backends a quick question (no diff needed)
review --just-ask "Is a single-file Python CLI the right idiom for this tool?"

# Settle a contested decision with cited evidence
review --quorum "Should we cap brainstorm at 8 rounds?"

# Open-ended design exploration
review --brainstorm "How should we design the plugin system?"
```

---

## Modes

### Review (default)

![review mode](docs/mode-review.svg)

N backends review your diff in parallel — one pass, no moderator. Best for pre-commit
checks where you want fast, independent perspectives without ceremony.

```bash
review
review --staged
git show --format= --no-ext-diff HEAD | review -m gemini,codex
```

---

### Just Ask

![just-ask mode](docs/mode-just-ask.svg)

Send a plain question to all selected backends in parallel. Diff is optional — pipe
one in or add `--staged` to attach it as context. One pass, no moderator, results
printed side by side.

```bash
review --just-ask "Does this change need a migration?"
git diff | review --just-ask "Is this safe to merge?"
```

---

### Quorum

![quorum mode](docs/mode-quorum.svg)

Two-phase structured panel. **Phase 1:** every expert answers in parallel and must cite
concrete evidence (file/line/fact); if they lack an evidence base they must say
`INSUFFICIENT EVIDENCE` rather than guess. **Phase 2:** a moderator runs sequentially,
reads all expert answers, and emits a structured summary with three sections — QUORUM
(points of majority agreement with evidence), DISAGREEMENT / NO QUORUM, and ABSTAINED.

Use when a question has real stakes and you want cited consensus, not vibes.

**Live logs & partial output.** Each backend call streams its output in real time
to a per-call log in the OS-standard per-user log dir — **macOS** `~/Library/Logs/review-cli/`,
**Linux** `$XDG_STATE_HOME/review-cli/logs/` (default `~/.local/state/review-cli/logs/`);
override with `$REVIEW_LOG_DIR`; files are private, mode 0600. Panel modes print the
log path to stderr at the start of each call, so you can `tail -f` it to watch a long
run progress instead of staring at a frozen terminal. If a call hits its `--timeout`,
the partial output captured so far is still returned (with a `[review-cli] TIMEOUT
after Ns]` marker and exit 124) rather than being thrown away.

```bash
review --quorum "Should we cap brainstorm at 8 rounds?"
git diff | review --quorum "Is this diff safe to merge?" -m codex,gemini,fable5
review --quorum "Should we switch to a plugin architecture?" --moderator gemini
```

---

### Brainstorm

![brainstorm mode](docs/mode-brainstorm.svg)

Iterative ideation loop. Each round assigns at least three distinct **rotating personas**
(Pragmatic Staff Engineer, Security-Paranoid Reviewer, DX Designer, Skeptical SRE,
Product-Minded Architect, Cost-Conscious Perf Engineer) to your panel backends in
parallel. After each round a moderator summarizes and decides STOP/CONTINUE — but
**cannot stop before `--rounds`** (minimum and default: 5). `--max-rounds` (default 8)
is a hard cap. Ends with a full moderator synthesis: best ideas, tradeoffs, and a
concrete recommendation.

Use for genuinely open design questions where you want the discussion to build across
rounds rather than converge in one shot.

The whole conversation is also written **incrementally** to a single discussion log
(`<logdir>/<stamp>-brainstorm.md`, path printed to stderr at the start) — each round
and moderator decision is flushed as it lands, so a timeout or interruption leaves the
discussion-so-far on disk instead of losing everything that was only being held in
memory for the final print.

The growing transcript is fed to the **claude and codex** backends over **stdin**
(not a `-p`/argv argument), which removes review-cli's own argv overhead. Note the
ceiling isn't fully gone: `claude-p`'s inner `claude` exec re-argv's the prompt, and
the **opencode** backend's CLI only takes the message as argv — so a very large
transcript (~1 MB+) can still hit `ARG_MAX` on those paths. `_payload` prints a size
WARNING as it approaches the limit; keep `--max-rounds` and diffs reasonable.

```bash
review --brainstorm "How should we design the plugin system?"
review --brainstorm "API shape for the cache layer" \
  --rounds 5 --max-rounds 10 \
  -m codex,gemini --moderator gemini
```

---

### When to use which

| Mode | Reach for it when... |
|------|----------------------|
| `review` | Pre-commit diff check — fast, parallel, no overhead |
| `--just-ask` | Quick multi-model second opinion on any question |
| `--quorum` | A contested decision that needs cited evidence to settle |
| `--brainstorm` | An open design space you want to explore across multiple rounds |

---

## Agent workflows

`review` earns its keep when an agent hits a hard call:

1. The agent runs `review --brainstorm "<the decision>"` — many models in rotating
   expert roles, looping across several rounds — to surface candidate approaches a
   single model wouldn't reach.
2. It picks the top one or two and posts them to Telegram via
   [`tg`](https://github.com/alex-mextner/tg-cli) as simplified options with pros/cons,
   so you decide from your phone.
3. For the closest calls, it builds the rival approaches in parallel **git worktrees**
   and compares them for real before committing.

And before every commit, `review --staged` is a multi-model gate — optionally *enforced*
with `review install-commit-hook` (a global pre-commit hook that blocks unreviewed
staged changes; bypass with `REVIEW_SKIP=1 git commit` or `git commit --no-verify`).

---

## Model backends

| Specifier | What runs under the hood |
|-----------|--------------------------|
| `codex` / `codex:<model>` | `codex exec -s read-only --ephemeral` |
| `claude` / `claude:<model>` | `claude-p --permission-mode plan --disallowedTools Edit Write Bash` |
| `fable` / `fable5` | Alias for `claude:claude-fable-5` |
| `gemini` / `gemini:<model>` | Gemini REST API (`gemini-2.5-flash` by default) |
| `oc:<model>` / `opencode:<model>` | `opencode run --agent read-only-reviewer` in a temp repo |
| anything else | Treated as an opencode model id |

The opencode backend runs in a **temporary git repository** with the diff attached as
`review.diff`. This keeps the source worktree out of reach — the model gets review
context without getting an edit target.

---

## Flags

```
-m / --model        Backend to include; repeat or comma-separate. Stacks with defaults.
--staged            Review staged diff (git diff --cached) instead of unstaged.
--timeout N         Per-call timeout in seconds (default 1200 for review, 240 for panel modes).
--moderator M       Override the auto-selected moderator for --quorum / --brainstorm.
--rounds N          Minimum brainstorm rounds before STOP is allowed (default 5).
--max-rounds N      Hard cap on brainstorm rounds (default 8).
--list-defaults     Print effective default backends and exit.
--prompt TEXT       Override the default review prompt.
-C / --cwd DIR      Run against a different repository directory.
```

---

## Configuration

Personal defaults live in `~/.config/review-cli/config.yaml`:

```yaml
# Backends used by plain `review` and panel modes
models:
  - codex
  - fable5

# Brainstorm can use a wider panel (falls back to `models` if absent)
brainstorm_models:
  - codex
  - gemini
  - fable5
```

Run `review --list-defaults` to see the effective defaults after config is applied.

Code defaults (when no config file exists): `codex`, `gemini`,
`oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo`.

---

## Auth

**Gemini:** set `GEMINI_API_KEY` or `GOOGLE_API_KEY` in the environment, or put
`GEMINI_API_KEY=...` in `~/.config/review-cli/.env`. The env var
`GEMINI_ENV_FILE=/path/to/.env` overrides the search path.

**Codex / Claude / opencode:** must be on PATH and authenticated per their own setup.

---

## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain:

- **[tg-cli](https://github.com/alex-mextner/tg-cli)** — Telegram bridge for agents: push reports, two-way control, Q→buttons
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.

Each CLI registers a skill into your agent harnesses (`<tool> install-skill`) so agents know it exists — see Install.
