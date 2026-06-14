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

## `review --visual` — visual verification

**Give it a screenshot; it judges keep / rollback / repair.** `--visual` is image-only
visual verification: pixels in → verdict out. There is **no DOM, no page, no capture** —
the image arrives as a CLI argument (or on a hook's stdin) already rendered. Every check,
including "is this render unstyled or broken", is performed from the **image** — pixel-level
CV heuristics plus an AI-vision model looking at the picture — never by reading a stylesheet.

The pipeline is:

```
cvGate → local cache pre-classifier → AI-vision → policy engine
```

The **model is the witness**; the **deterministic policy engine is the judge**. cvGate is a
fast pixel pre-filter that auto-rejects the unambiguously-broken set (blank/FOUC canvas,
unstyled no-CSS render, error overlay) before any paid call. The local pre-classifier is an
on-device, no-VLM cost-saver that clears the confident-clear cases for free; AI-vision is the
primary judge for everything ambiguous; the policy engine decides the final verdict **outside**
the model (schema validation, CV/model contradiction checks, module vetoes). AI-vision never
self-decides and the local model never overrides it.

![review --visual cases — REPORTS-unstyled (top) vs no-report-styled (bottom)](docs/assets/visual-cases.png)

*What `review --visual <image>` reports across real renders. Top row: unstyled / blank / FOUC /
error-overlay renders the detector flags (each one would block a `tg --photo` send). Bottom row:
properly-styled renders it stays quiet on. (The grid's title art is the tool's old standalone
name "styleprobe" in older copies — it is the `review --visual` detector.)*

### Composable flag, not a mode

`--visual` is **orthogonal** to the four review modes — it combines with `--brainstorm`,
`--quorum`, or the default diff-review (the personas / voters / reviewer literally **see** the
image as multimodal context), or runs standalone:

```bash
# Standalone — pure verdict pipeline on one render
review --visual after.png

# The brainstorm personas see the screenshot and reason about it
review --brainstorm "is this layout good?" --visual after.png

# Every quorum voter gets the image as shared context
review --quorum "ship this UI?" --visual after.png

# Default diff-review with the rendered result attached as evidence
review --visual after.png        # (with a diff present)
```

When a companion mode is present the image and the active modules' visual questions are folded
into **that mode's** model call — there is no separate isolated visual run. The standalone
verdict pipeline (and its exit codes below) fires only in the mode-less case.

### Vision backends

Vision runs **through the agent CLIs** — `codex` / `claude` / `opencode` — mirroring exactly
how review's text backends shell out, but with the image attached and the structured verdict
parsed from the CLI output. No provider REST keys for those three. **Gemini is the one
exception**: its CLI is broken, so the Gemini vision call stays on the REST API key
(`GEMINI_API_KEY`), same as review's text Gemini backend. opencode is a router — pick a
vision-capable model via `oc:<provider>/<vision-model>`; a text-only model is never silently
used to "verify" an image. `--no-local-model` disables the local cache pre-classifier (the
cost-saver) and forces every cvGate pass-through to the paid AI-vision call.

### Modules

Each visual check is an independent, self-selecting **module** that declares *when it
activates*. Built-ins: `style-presence`, `blank-frame`, `error-overlay`. A module contributes a
cheap `cv_check` pre-filter and/or `vision_questions` folded into the multimodal call, plus a
`judge` that can veto a keep. Force-activate one with `--check <name>`; otherwise modules
self-select from `--intent` / `--expect`.

**Per-project modules.** A project ships its own modules via a manifest at
`<project>/.review/visual-modules.json` declaring `{name, entry, activates_on}`. review
discovers, loads, and folds them into the same pipeline. The model is **trust-by-default** —
reviewing your *own* repo, a contributed module loads and runs with zero ceremony. For the rare
untrusted-repo case (an external PR, a cloned stranger's repo) set `REVIEW_UNTRUSTED_MODULES=1`
to re-engage a TOFU quarantine + sha-pin (`review trust-module <name>` to pin). Every load is
recorded to an append-only audit log either way.

The worked example is **`selection-highlight`** (shipped by HyperIDE / hyper-canvas-draft):
`activates_on: ["selection"]`. A HyperCanvas selection frame is a 2px `rgb(59,130,246)` outline
so thin a vision model routinely can't tell whether it's there — so "selection works" proofs
slip through with no frame drawn. The module reuses `bin/frames-check`'s deterministic
colour+shape detector to turn "selection expected but no outline present" into a **hard veto**,
short-circuiting before any vision call.

### `tg --photo` hook

`tg` can run `review --visual` as a **pre-send hook** to block an unstyled / broken screenshot
before it reaches Telegram — turning the often-violated "review screenshots before sending" rule
into an enforced mechanism. The hook runs `review --visual <png> --json --strict`; a `rollback`
verdict (exit 10) drops the photo, a `keep` lets it through, and a no-vision `human_review` /
`unverified` fails *open* (warn + allow) so a missing key never bricks sends. See the
`feat-tg-photo-visual-hook` branch and `docs/architecture-visual-verification.md` §7.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | `keep` — the render is acceptable |
| `10` | blocking verdict (`rollback`, or `human_review`/`unverified` when treated as a block) under `--strict` |
| `1` | usage error — no image / unreadable image |
| `124` | vision-call timeout |

Add `--no-ai` to run cvGate only (fast CI smoke / offline), `--json` for the machine verdict
(used by the tg hook), and `--before <img>` for diff-aware judgement.

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
--show-board        Print the active reviewer board (model -> role + availability) and exit.
--no-board          Disable the reviewer board; use the plain models list instead.
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

## Reviewer board

The default `review` (plain diff review) runs a **reviewer board**: a panel where
each model is given its OWN review role/lens, so the panel covers the diff broadly
instead of every model doing the same generic pass. The board is the default panel
out of the box — no config file required. Reviewers whose backend isn't available
(no key / not on PATH) are skipped and logged; the board degrades gracefully.

The built-in board:

| Reviewer | Backend | Role | Lens focus |
|---|---|---|---|
| Opus | `claude:claude-opus-4-8` | `architect` | architecture, design coherence, API shape, abstraction boundaries (also the moderator) |
| Codex | `codex` | `correctness` | logic bugs, regressions, edge cases, null/async/race, off-by-one |
| Gemini | `gemini` | `consistency` | cross-file consistency, dead refs, contract drift, whole-repo coherence |
| DeepSeek | `commandcode:deepseek/deepseek-v4-pro` | `performance` | complexity, hot paths, allocations, async/concurrency, N+1 |
| Kimi | `commandcode:moonshotai/Kimi-K2.7-Code` | `quality` | readability, naming, duplication, code smells, idiom |
| Qwen | `commandcode:Qwen/Qwen3.7-Max` | `security` | injection, authz, secrets, unsafe deserialization, path traversal, SSRF |
| GLM | `zai:glm-5.2` | `tests` | missing tests, untested branches, boundary conditions, error-path coverage |
| GPT-5.5 | `commandcode:gpt-5.5` | `contracts` | public API shape, contracts, types, backward-compat, interface design |

The `tests` seat goes **direct to z.ai** (`zai:glm-5.2`, the newest GLM, reachable on
the GLM Coding-Plan endpoint) via the z.ai backend — not through the commandcode
gateway. It needs a z.ai key (see Auth). All other commandcode seats need
`COMMANDCODE_API_KEY`.

```bash
review --show-board   # list the active board (model -> role) + availability
review --no-board     # disable the board; use the plain `models` list instead
review -m codex -m gemini   # an explicit -m also bypasses the board (exact models)
```

Override the board in `config.yaml` with a `board:` list — each entry is a
`{model, role}` mapping (optional `name:` for the label). An unknown `role` keeps
the reviewer but falls back to the generic prompt (with a warning); a malformed
entry is skipped. With no `board:` configured, the built-in 8-seat board above applies.

```yaml
board:
  - { model: "claude:claude-opus-4-8", role: architect }
  - { model: "codex",                  role: correctness }
  - { model: "commandcode:Qwen/Qwen3.7-Max", role: security, name: Qwen }
  - { model: "zai:glm-5.2",            role: tests }
  - { model: "commandcode:gpt-5.5",    role: contracts, name: GPT-5.5 }
```

**Optional heavyweight seats** (NOT enabled by default — the board stays at 8). Add
either to your `board:` list for an extra 1M-context resilience / holistic-senior
pass; both run through commandcode (need `COMMANDCODE_API_KEY`):

```yaml
board:
  # ... the 8 default seats ...
  - { model: "commandcode:MiniMaxAI/MiniMax-M3", role: performance, name: MiniMax }   # 1M ctx — resilience
  - { model: "commandcode:nvidia/nemotron-3-ultra-550b-a55b", role: architect, name: Nemotron }  # 550B, 1M ctx — holistic senior
```

Known roles: `architect`, `correctness`, `consistency`, `performance`, `quality`,
`security`, `tests`, `contracts`.

---

## Auth

**Gemini:** set `GEMINI_API_KEY` or `GOOGLE_API_KEY` in the environment, or put
`GEMINI_API_KEY=...` in `~/.config/review-cli/.env`. The env var
`GEMINI_ENV_FILE=/path/to/.env` overrides the search path.

**Codex / Claude / opencode:** must be on PATH and authenticated per their own setup.

**commandcode (DeepSeek / Kimi / Qwen / GPT-5.5 board reviewers):** set
`COMMANDCODE_API_KEY` (a Command Code `user_...` token) in the environment or in
`~/.config/review-cli/.env`. Without it, those commandcode board reviewers are
skipped and the board runs with whatever remains. No key is ever written to disk by
review — it is only read.

**z.ai / GLM (the `tests` board seat = `zai:glm-5.2`):** set `ZAI_API_KEY` (or
`ZHIPU_API_KEY`) in the environment or `~/.config/review-cli/.env`. The default base
URL is the **GLM Coding-Plan endpoint** `https://api.z.ai/api/coding/paas/v4` — only
that endpoint serves the flagship `glm-5.2`; the standard `https://api.z.ai/api/paas/v4`
endpoint tops out at `glm-5.1`. A Coding-Plan key gets `glm-5.2` out of the box; a
standard-plan user overrides with `ZAI_BASE_URL=https://api.z.ai/api/paas/v4`. Note
that the default `tests` board seat pins the model explicitly (`zai:glm-5.2`), and an
explicit `zai:<model>` suffix wins over `ZAI_MODEL` — so a standard-plan user must
also override that seat in a `config.yaml` `board:` list (e.g. `{ model: "zai:glm-5.1",
role: tests }`); `ZAI_MODEL` alone only affects a bare `-m zai` invocation, not the
suffix-pinned board seat. `glm-5.2` is a reasoning model: it returns a final
answer plus a `reasoning_content` field; review reads the answer and falls back to the
reasoning text when the answer is empty (e.g. a low output-token budget). Without a
z.ai key the `tests` seat is skipped; the rest of the board still runs. The key is
only read, never written.

---

## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain:

- **[tg-cli](https://github.com/alex-mextner/tg-cli)** — Telegram bridge for agents: push reports, two-way control, Q→buttons
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.

Each CLI registers a skill into your agent harnesses (`<tool> install-skill`) so agents know it exists — see Install.
