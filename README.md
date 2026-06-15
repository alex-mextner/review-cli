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

# Save the result to a file — use -o, NOT `> file` (zsh noclobber-safe)
review -o review.md
```

> **Write to a file with `-o file.md`, not `review … > file.md`.** Under zsh
> `noclobber` (a common default), `> file.md` refuses to overwrite an existing file
> and the command dies silently — no review, no error. `-o` writes the result with
> Python (`open(...,"w")`), bypassing the shell redirect entirely: it creates parent
> dirs, always overwrites, and still prints to stdout. See [Flags](#flags).

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

**Run stats & a startup ETA — never short-timeout `review`.** `review` is
multi-model and (for the panel modes) multi-round, so it takes **minutes**, and a
short shell `timeout` around it kills the run before its synthesis — a `--brainstorm`
only emits its final answer at the very end, so a short cap yields *nothing* usable.
To make the expected duration visible up front, every run that actually dispatches a
backend appends a structured stat record — mode, pool size (backends actually
dispatched; for a small-panel brainstorm that is the per-round persona slot count),
model **names** (never keys or prompts), the real monotonic wall-clock, and ok/fail
counts — to an append-only
JSONL store at `~/.config/review-cli/run-stats.jsonl` (mode 0600; override with
`$REVIEW_STATS_FILE`). At dispatch it then prints a one-line ETA to **stderr** keyed
on `(mode, pool_size)`:

```
[review] pool=4 (brainstorm) — typically ~6m12s based on 12 past runs of this size; do NOT timeout.
```

With no exact history it falls back to pool-size-only across modes, then to a
`no history yet … expect MINUTES` line. Read that line and wait at least that long.
(This store is separate from the dashboard's per-call log reader, whose mode is
*inferred* and whose duration is an mtime proxy — this one records the run's ground
truth.)

**No external timeout — `review` carries its own internal ≤4h backstop.** Do not
put *any* external `timeout` on `review`: it is designed to run unbounded from the
outside. The only time bound is an **internal**, last-resort backstop of **≤4h** that
the binary arms itself (`reviewlib.backstop`) — a watchdog that force-terminates a
genuinely wedged run (exit `124`). So a healthy run never needs an external cap (it
finishes in minutes, far under the ceiling) and a stuck run can't run forever either.
`$REVIEW_BACKSTOP_SECONDS` can only **lower** that ceiling, never raise it past 4h.

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

**Brainstorm about a specific change (`--brainstorm` + a diff).** When there IS a diff
— an uncommitted working-tree diff in `-C`, a `--staged` diff, or a piped diff — every
persona (and the moderator) sees it as constant **grounding context**, so you can
brainstorm concretely ABOUT a change instead of in the abstract. With **no** diff
present it stays pure ideation, exactly as before. The diff is optional: an absent diff
or a non-repo `-C` degrades silently to ideation.

```bash
# brainstorm grounded in the current uncommitted working-tree diff
review -C <repo> --brainstorm "Is this caching approach sound? What are the risks?"
review -C <repo> --staged --brainstorm "Alternatives to this design before I commit?"
git diff main... | review -C <repo> --brainstorm "How else could we structure this?"
```

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
| `--brainstorm` | An open design space you want to explore across multiple rounds (optionally grounded in a diff — pass `--staged` or have an uncommitted diff to brainstorm about a specific change) |

---

## `review dashboard` — local web dashboard

```bash
review dashboard            # start the dashboard, open a browser at http://127.0.0.1:<port>/
review dashboard --port 8765 --no-open   # fixed port, no auto-open (for remote/tmux use)
```

A single-page web app over every review-cli run, built on the Python **stdlib** HTTP
server (no extra deps) and a **vanilla-JS SPA** (no npm/build step — assets ship in the
package). It binds **127.0.0.1 only**: the logs persist prompts/diffs that may carry
secrets, so the dashboard is never exposed on the network.

**Data sources (read-only):** review-cli does not emit a structured run record, so the
dashboard reads the real on-disk artifacts in `log_dir()` — the per-call streamed logs
(`{stamp}-{backend}-r{n}.log`) and the brainstorm discussion logs (`{stamp}-brainstorm.md`).
Subprocess backends (codex/claude/opencode) write these live; REST backends (gemini, z.ai,
commandcode) emit an equivalent sidecar log on every call — each under its OWN backend name,
so every backend is counted and attributed correctly. Calls are time-clustered into
**sessions** (review-cli emits no run id; a session = a burst of calls separated by a gap),
and the mode (review / panel / brainstorm) is inferred from the call/round shape.

**Panels:** Chat logs (per-run transcripts), Stats (runs over time, by mode/model/role),
Models & roles, Metrics (durations, success/fail rates), Overseer feedback, Modes, Errors,
Tasks (mark a session **conscious**), Prompts, and PR + ticket links.

**The overseer's annotations** — free-text feedback, the per-session **conscious** flag,
and PR/ticket associations — are the only NEW persistence: a small atomic JSON store at
`~/.config/review-cli/dashboard.json` (override with `$REVIEW_DASHBOARD_STORE`), keyed by
the deterministic session id so annotations stay pinned as logs age out. The server exposes
small local-only JSON endpoints (`GET /api/runs|stats|runs/<id>`, `POST .../feedback|conscious|links`).

> Token/cost and an explicit run id are **not recorded** by review-core today; those panels
> show a graceful empty-state noting what review-core would need to log, rather than faking data.

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

## `review spec-web <spec.md>` — interactive spec reviewer

**Render any markdown spec server-side and review it like a GitHub PR.** Select any text in
the rendered spec → ask a question / leave a comment anchored to that selection; comments
accumulate in a **pending batch**; one **Submit review** finalizes them; answers thread
inline under each comment. Reusable for *any* spec markdown file. Serve it over Tailscale to
review from your phone.

```sh
# local, ephemeral port, opens a browser
review spec-web docs/specs/my-spec.md --open

# expose over Tailscale (reachable from a phone on the tailnet)
review spec-web docs/specs/my-spec.md --host 0.0.0.0 --port 8787

# pre-load an existing Q&A thread
review spec-web docs/specs/my-spec.md --seed thread.json

# dump the whole review (quotes + questions + threaded answers) as markdown
review spec-web docs/specs/my-spec.md --export > review.md
```

| Flag | Meaning |
|------|---------|
| `--host` | bind host (default `127.0.0.1`; `0.0.0.0` exposes over Tailscale) |
| `--port` | bind port (default: a free ephemeral port) |
| `--seed FILE` | import an initial review thread from a JSON file before serving |
| `--export` | print the persisted review as markdown and exit (no server) |
| `--open` | open the URL in a browser on startup |

**Layout.** Desktop (≥900px) = two panes side-by-side (spec left, comments right, a
draggable divider). Mobile (<900px) = comments as a bottom sheet under the spec. Both panes
collapse/expand from the topbar.

**Rendering.** Markdown → HTML is rendered server-side with the GitHub heading-slug scheme,
so the spec's own internal links (`[§9.4](#94-…)`) resolve. Figures referenced as
`./assets/fig-*.svg|png` are served as real HTTP resources at `/asset/<name>` (never
inlined).

**Comments.** A comment stores the selected quote, the containing section id, char offsets,
the body, author, created-at, a status (`pending`/`submitted`/`answered`/`resolved`), and a
thread of replies. On reload, each comment re-anchors by locating its quote within its
section and highlighting it; a quote that can't be re-found shows in the sidebar as
**unanchored** (never a crash).

**Persistence.** One JSON file per spec at `~/.config/review-cli/spec-web/<sha1-of-abspath>.json`
(mode `0600`), surviving restarts. Override the directory with `$REVIEW_SPECWEB_DIR`.

**Security.** Reads (spec, assets, comments) are open; only figures the markdown
*references* are served (an unrelated file in the assets dir 404s), SVGs are served with a
`sandbox` CSP so a directly-opened one can't run script, and symlinked assets that escape
the assets dir are refused. Writes (post comment / reply / submit / import) are
origin-guarded against both CSRF and DNS rebinding: a write requires (1) the request's
**Host** to be in the allowlist — **loopback + this machine's Tailscale identity
(discovered at runtime via `tailscale status`, never hardcoded) + `$REVIEW_SPECWEB_ALLOWED_HOSTS`**
(comma-separated) — and (2) the **Origin/Referer** to match that Host (classic CSRF check),
plus `Content-Type: application/json` and a body-size cap. The Host allowlist is what stops
a rebound attacker hostname from posting same-origin. So to accept writes from a phone over
Tailscale, the Tailscale name/IP must be discovered or listed in
`$REVIEW_SPECWEB_ALLOWED_HOSTS` (e.g. `REVIEW_SPECWEB_ALLOWED_HOSTS=ultras-mbp.tailbfe8ea.ts.net,100.123.113.82`).

**Seed / import JSON shape** (`comments` is the only required key):

```json
{
  "comments": [
    {
      "quote": "the selected text",
      "body": "the question or comment",
      "section_id": "94-...",
      "section_title": "§9.4 ...",
      "author": "alex",
      "status": "submitted",
      "batch": "2026-06-14T10:00:00+00:00",
      "replies": [ { "author": "claude", "body": "the answer" } ]
    }
  ]
}
```

Missing `id`/`created` are generated; unknown keys are ignored. Add `"replace": true` at the
top level to discard existing comments before importing.

Routes: `GET /` (SPA shell), `GET /static/<app.css|app.js>`, `GET /asset/<name>`,
`GET /api/spec`, `GET /api/comments`, `GET /api/export`, `POST /api/comments`,
`POST /api/comments/<id>/{reply,status,delete}`, `POST /api/submit`, `POST /api/import`.

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

Each backend runs as a **`cli`** subprocess, a **`api`** REST call, or both:

| Specifier | Transport | What runs under the hood |
|-----------|-----------|--------------------------|
| `codex` / `codex:<model>` | cli | `codex exec -s read-only --ephemeral` |
| `claude` / `claude:<model>` | api \| cli | `claude-p` CLI, or the Anthropic-compatible Messages API |
| `fable` / `fable5` | api \| cli | Alias for `claude:claude-fable-5` |
| `gemini` / `gemini:<model>` | api | Gemini REST API (`gemini-2.5-flash` by default) |
| `zai:<model>` / `glm` / `glm52` … | api | z.ai (GLM) OpenAI-compatible REST API — needs `ZAI_API_KEY` |
| `commandcode:<model>` / `cc` | api | Command Code OpenAI-compatible Provider API — needs `COMMANDCODE_API_KEY` |
| `oc:<model>` / `opencode:<model>` | cli | `opencode run --agent read-only-reviewer --dir <repo>` (reads the real repo, read-only) |
| anything else | cli | Treated as an opencode model id |

**Transport split.** codex and opencode are **cli-only** (no REST API). gemini, z.ai,
and commandcode are **api-only** keyed HTTP backends (no CLI on PATH). claude supports
**both** — `REVIEW_CLAUDE_MODE=api|cli` forces one, else it auto-picks (CLI if the
binary is present, API when it isn't and a key is set). Each backend's mode can be
forced with `REVIEW_<NAME>_MODE`; forcing an unsupported mode (e.g.
`REVIEW_COMMANDCODE_MODE=cli`) is a hard error, never a silent fall-through.

The opencode backend is **agentic and read-only**: it runs in the **real `-C`
repository** (via `opencode run --dir <repo>`), exactly like the codex backend, so an
`oc:` seat can **read any project file** — not just the diff in the prompt. Safety is
enforced by the `read-only-reviewer` agent, which **denies** `edit`/`write`/`bash`/
`webfetch`: opencode may open files but can never mutate the worktree, run a command,
or hit the network.

It falls back to an isolated temp dir (diff-only) in two cases:
- `-C` is **not a git repo** (e.g. a `--just-ask` from a scratch dir) — nothing to read;
- the repo **ships its own opencode config** (`.opencode/` or `opencode.json`/`.jsonc`).
  A repo-local agent definition can **override** the global `read-only-reviewer` and
  re-enable `write`/`bash` (verified: project config wins, and no opencode env flag
  suppresses it), so to keep the sandbox trustworthy on a potentially adversarial repo,
  review refuses to run agentically there and reviews the diff in a clean dir instead.

> **Note on the api-only board seats (commandcode, z.ai).** These are kept as raw
> `api` backends, not routed through opencode. opencode's `@ai-sdk/openai-compatible`
> adapter does not reliably drive the Command Code gateway (the request hangs / returns
> empty, while the same models answer correctly over raw HTTP), and z.ai/GLM is not an
> opencode-native provider. So commandcode/z.ai stay on the direct keyed-HTTP path;
> opencode-native models (`oc:deepseek/…`, `oc:fireworks/…`, the free `oc:opencode/…`
> gateway) get the agentic real-repo treatment above.

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
--pool N            How many of the board's seats to run (default 4); the first N seats run,
                    the rest are kept in reserve. The board is never off — --pool only sizes
                    it. N<=0 (e.g. --pool 0) runs all seats.
--prompt TEXT       Override the default review prompt.
-C / --cwd DIR      Run against a different repository directory.
-o / --output FILE  Write the result to FILE via Python (creates parent dirs, always
                    overwrites) while still printing to stdout. Use this instead of
                    `review … > FILE`, which fails silently under zsh noclobber.
```

---

## Configuration

Personal defaults live in `~/.config/review-cli/config.yaml`:

```yaml
# Backends used by plain `review` and panel modes. Setting `models:` OVERRIDES the
# default reviewer board (see "Board vs. models precedence") — you get exactly these.
models:
  - codex
  - fable5

# Brainstorm can use a wider panel (falls back to `models` if absent)
brainstorm_models:
  - codex
  - gemini
  - fable5
```

Run `review --list-defaults` to see the effective (normalized) models after config is
applied.

Code defaults (when no config file exists): `codex`, `gemini`,
`oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo`.

---

## Reviewer board

The default `review` (plain diff review) runs a **reviewer board**: a panel where
each model is given its OWN review role/lens, so the panel covers the diff broadly
instead of every model doing the same generic pass. The board is the default panel
out of the box — no config file required.

### Priority-ordered failover pool

The board is a **priority-ordered** list of 8 models — strongest first — and a plain
`review` runs a **pool of 4**. The pool is chosen by **priority + availability**, with
two layers of failover so the run keeps **4 working reviewers** even when models drop:

- **Startup failover** — the active pool is the **top 4 AVAILABLE** seats by priority.
  A higher-priority seat whose backend isn't reachable (no key / not on PATH) is
  **skipped** and the next-priority seat is pulled up, so you still start with 4 working
  models. (E.g. if Fable 5 is paywalled/unavailable, the pool starts at Opus.)
- **Mid-run failover** — if an active seat **fails during the review** (backend error,
  timeout, empty output, or an "unavailable" reply such as a paywalled model returning
  *"… is currently unavailable"*), the next-priority **reserve** model is promoted and
  run in its place, repeating until **4 working verdicts** are produced or the reserve is
  exhausted (then the run degrades gracefully and says so on stderr, exiting non-zero).

The role/lens **travels with the seat**: priority decides *who* sits in the pool, the
role decides *with what lens* they review. A promoted reserve brings its own lens, so
the panel still covers a broad set of facets.

`--pool N` overrides the default 4 (the top-N available, with the same failover); `--pool
0` runs **all available** seats. The board is **never disabled** — `--pool` only sizes
the pool.

The built-in board, in **priority order** (the `tier` column shows the live split on a
fully-keyed environment):

| # | Tier | Reviewer | Backend | Role | Lens focus |
|---|---|---|---|---|---|
| 1 | pool | Fable | `claude:claude-fable-5` | `architect` | architecture, design coherence, API shape, abstraction boundaries |
| 2 | pool | Opus | `claude:claude-opus-4-8` | `correctness` | logic bugs, regressions, edge cases, null/async/race, off-by-one (also the moderator) |
| 3 | pool | Codex | `codex` | `consistency` | cross-file consistency, dead refs, contract drift, whole-repo coherence |
| 4 | pool | Kimi | `commandcode:moonshotai/Kimi-K2.7-Code` | `performance` | complexity, hot paths, allocations, async/concurrency, N+1 |
| 5 | reserve | GLM | `zai:glm-5.2` | `quality` | readability, naming, duplication, code smells, idiom |
| 6 | reserve | Qwen | `commandcode:Qwen/Qwen3.7-Max` | `security` | injection, authz, secrets, unsafe deserialization, path traversal, SSRF |
| 7 | reserve | DeepSeek | `commandcode:deepseek/deepseek-v4-pro` | `tests` | missing tests, untested branches, boundary conditions, error-path coverage |
| 8 | reserve | Gemini | `gemini` | `contracts` | public API shape, contracts, types, backward-compat, interface design |

**To re-rank** the board, reorder the priority list (`DEFAULT_BOARD` in
`reviewlib/config.py`, or a `board:` list in `config.yaml`) — the top entry is the
highest priority. The role lens you attach to each model is independent of its priority.

The GLM seat goes **direct to z.ai** (`zai:glm-5.2`, the newest GLM, reachable on the
GLM Coding-Plan endpoint) via the z.ai backend — not through the commandcode gateway.
It needs a z.ai key (see Auth). All other commandcode seats need `COMMANDCODE_API_KEY`.

```bash
review --show-board   # priority order + which 4 are the live pool + reserve + availability
review                # default failover pool: the top 4 AVAILABLE seats by priority
review --pool 8       # run all 8 available seats (--pool 0 also means "all available")
review --pool 2       # run the top 2 available seats (with failover)
review -m codex -m gemini   # an explicit -m bypasses the board entirely (exact models)
```

### Board vs. models precedence

The board is the default **only when you have not expressed a model preference**.
Precedence (cost-safety first — the board never runs against your wishes):

```
explicit -m on the CLI   >   `models:` in config.yaml   >   default board (failover pool)
```

- A `models:` list in `config.yaml` **overrides the board**: you configured exact
  models, so `review` runs exactly those (the flat panel, no failover), not the board.
- The board runs whenever there is **no** `-m` and **no** `models:`. It can **never**
  be disabled — there is no `--no-board` flag. Use `--pool N` to size the failover pool
  (default 4; `--pool 0`/`--pool 8` runs all available seats).
- An "effectively empty" `models:` (absent, `[]`, or only blank entries) is **not** a
  preference — the board still applies.

Override the board itself in `config.yaml` with a `board:` list — each entry is a
`{model, role}` mapping (optional `name:` for the label). **List the models in priority
order** (the first entry is the highest priority); the failover pool fills from the top.
An unknown `role` keeps the reviewer but falls back to the generic prompt (with a
warning); a single malformed entry is skipped (the valid ones are kept). With **no**
`board:` configured, the built-in 8-seat priority board above applies. A `board:` that is
**present but has no usable entry at all** is a hard error (non-zero exit) — it never
silently falls back to the paid default board.

```yaml
# Priority order: the first 4 reachable models are the live pool; the rest are the
# reserve that backfills a skipped/failed seat.
board:
  - { model: "claude:claude-fable-5",  role: architect }
  - { model: "claude:claude-opus-4-8", role: correctness }
  - { model: "codex",                  role: consistency, name: Codex }
  - { model: "commandcode:moonshotai/Kimi-K2.7-Code", role: performance, name: Kimi }
  - { model: "zai:glm-5.2",            role: quality }
  - { model: "commandcode:Qwen/Qwen3.7-Max", role: security, name: Qwen }
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
Codex is the #3 board seat (GPT-5.5 IS codex — the agentic CLI route, free).

**commandcode (DeepSeek / Kimi / Qwen board reviewers):** set
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
