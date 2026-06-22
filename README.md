# review-cli

**multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic.**

Runs your git diff through multiple AI backends **in parallel**, collects their findings,
and prints them side by side. Core review modes let you go from a quick pre-commit
sanity check all the way to a structured expert panel that builds consensus or explores
a design space. Built for use from any shell or AI agent harness (Claude Code, Codex,
opencode).

Beyond the core modes it also does **visual review** — attach a rendered screenshot to any
review with the composable `--visual` flag for a keep / rollback / repair verdict — and ships
**interactive spec-review tooling** so a markdown spec can be reviewed like a PR.

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
The `install-*` commands (`install-skill` / `install-commit-hook` / `register-module`)
are idempotent and report their INSTALLED state: each target shows a green ✓ "already
configured" when nothing changed, or "+ wrote/updated" when it (re)wrote — so a re-run on
a fully-set-up machine prints "already configured — nothing to do". A target that can't be
configured (a foreign pre-commit hook, a wrong/occupied skill symlink, an unwriteable
settings.json) is reported as `! conflict`, left untouched, and the command exits non-zero —
resolve the conflict and re-run.

---

## Quick start

Modes are **subcommands**: `review <mode> …`. The first verb selects the mode. A bare
`review` (no subcommand) prints the **help** — the diff review is `review diff`.

```bash
# Review unstaged diff with your default backends
review diff

# Review staged changes (pre-commit)
review diff --staged

# Add backends to the defaults
review diff -m codex -m fable5 -m gemini

# Ask all backends a quick question (no diff needed)
review just-ask "Is a single-file Python CLI the right idiom for this tool?"

# Settle a contested decision with cited evidence
review quorum "Should we cap brainstorm at 8 rounds?"

# Open-ended design exploration
review brainstorm "How should we design the plugin system?"

# Brainstorm ABOUT the current change (grounded in the working-tree / staged diff)
review brainstorm "Alternatives before I commit?" --diff
review brainstorm "Risks in this design?" --staged

# Save the result to a file — use -o, NOT `> file` (zsh noclobber-safe)
review diff -o review.md
```

> **The diff review is `review diff` now.** A bare `review` (no subcommand) prints the
> help — it does **not** run a diff review (the old "bare review == a diff review" default
> was a mistake). The diff review moved from the stuttering `review review` to
> **`review diff`**. The removed `review review` verb and `review -C <repo>` (flags with
> no verb) print a one-line `review diff` pointer and exit non-zero. The meta flags
> (`--list-defaults` / `--show-board` / `--help`) still work with no subcommand.

> **Modes moved from flags to subcommands.** The old `--brainstorm` / `--quorum` /
> `--just-ask` flags are gone — use the `brainstorm` / `quorum` / `just-ask`
> subcommands. The flags now print a one-line pointer and exit non-zero. `--visual` stays
> a **composable flag** (not a mode): it rides any subcommand (e.g. `review diff --visual`).

> **Write to a file with `-o file.md`, not `review … > file.md`.** Under zsh
> `noclobber` (a common default), `> file.md` refuses to overwrite an existing file
> and the command dies silently — no review, no error. `-o` writes the result with
> Python (`open(...,"w")`), bypassing the shell redirect entirely: it creates parent
> dirs, always overwrites, and still prints to stdout. See [Flags](#flags).

> **Deep help: `review help <topic>`.** Beyond `review --help` / `review <mode> --help`,
> `review help config` (alias `review --help config`) prints the configuration reference —
> the config file + cascade, the model/board selection, and keys/auth. The main `--help`
> lists the available topics.

---

## Modes

### Diff review (`review diff`)

![review mode](docs/mode-review.svg)

N backends review your diff in parallel — one pass, no moderator. Best for pre-commit
checks where you want fast, independent perspectives without ceremony. The diff review is
the **`diff`** subcommand (`review diff`); **a diff is required**.

```bash
review diff
review diff --staged
git show --format= --no-ext-diff HEAD | review diff -m gemini,codex
```

---

### Just Ask

![just-ask mode](docs/mode-just-ask.svg)

Send a plain question to all selected backends in parallel. Diff is optional — pipe
one in or add `--staged` to attach it as context. One pass, no moderator, results
printed side by side.

```bash
review just-ask "Does this change need a migration?"
git diff | review just-ask "Is this safe to merge?"
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
short shell `timeout` around it kills the run before its synthesis — a `brainstorm`
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
review quorum "Should we cap brainstorm at 8 rounds?"
git diff | review quorum "Is this diff safe to merge?" -m codex,gemini,fable5
review quorum "Should we switch to a plugin architecture?" --moderator gemini
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

**Brainstorm about a specific change (`brainstorm` + a diff).** brainstorm is composable
with the diff. When there IS a diff — an uncommitted working-tree diff in `-C` (pass
`--diff`), a `--staged` diff, or a piped diff — every persona (and the moderator) sees it
as constant **grounding context**, so you can brainstorm concretely ABOUT a change
instead of in the abstract. With **no** diff present it stays pure ideation, exactly as
before. The diff is optional: an absent diff or a non-repo `-C` degrades silently to
ideation.

```bash
# brainstorm grounded in the current uncommitted working-tree diff
# (the subcommand leads; -C and the other shared options follow it)
review brainstorm "Is this caching approach sound? What are the risks?" --diff -C <repo>
review brainstorm "Alternatives to this design before I commit?" --staged -C <repo>
git diff main... | review brainstorm "How else could we structure this?" -C <repo>
```

The whole conversation is also written **incrementally** to a single discussion log
(`<logdir>/<stamp>-brainstorm.md`, path printed to stderr at the start) — each round
and moderator decision is flushed as it lands, so a timeout or interruption leaves the
discussion-so-far on disk instead of losing everything that was only being held in
memory for the final print. That log is also what makes a crashed brainstorm
**resumable** — see [`review sessions`](#review-sessions--list--resume-brainstorm-sessions).

The growing transcript is fed to the **claude and codex** backends over **stdin**
(not a `-p`/argv argument), which removes review-cli's own argv overhead. Note the
ceiling isn't fully gone: `claude-p`'s inner `claude` exec re-argv's the prompt, and
the **opencode** backend's CLI only takes the message as argv — so a very large
transcript (~1 MB+) can still hit `ARG_MAX` on those paths. `_payload` prints a size
WARNING as it approaches the limit; keep `--max-rounds` and diffs reasonable.

```bash
review brainstorm "How should we design the plugin system?"
review brainstorm "API shape for the cache layer" \
  --rounds 5 --max-rounds 10 \
  -m codex,gemini --moderator gemini
```

---

### When to use which

| Subcommand | Reach for it when... |
|------------|----------------------|
| `review` (default) | Pre-commit diff check — fast, parallel, no overhead |
| `just-ask` | Quick multi-model second opinion on any question |
| `quorum` | A contested decision that needs cited evidence to settle |
| `brainstorm` | An open design space you want to explore across multiple rounds (optionally grounded in a diff — pass `--diff` / `--staged` or have an uncommitted diff to brainstorm about a specific change) |

---

## `review sessions` — list / resume brainstorm sessions

Every `review brainstorm` run is persisted as a round-by-round discussion log
(`<logdir>/<stamp>-brainstorm.md`, written incrementally as each round lands). `review
sessions` reads those logs so you can **list** past brainstorms — including ones that
**crashed, were killed, or timed out** before the final synthesis — and **resume** an
interrupted one instead of starting over.

```bash
review sessions          # recent COMPLETED sessions (those that reached a synthesis)
review sessions -a       # ALL sessions, including dead/interrupted ones (no synthesis)
review sessions -s <id>  # RESUME: reload the transcript, continue the round loop, synthesize
```

**Listing.** Each row shows a short **session id** (derived from the log's UTC
timestamp, e.g. `20260616T013310`), the **status** (`completed` = has a Final synthesis /
`interrupted` = crashed before one), the number of **rounds** captured (`r3`), the
**timestamp**, and the **topic**:

```
Brainstorm all sessions (incl. interrupted) — newest first; resume with `review sessions -s <id>`:

  20260616T020000  [interrupted]  r2  2026-06-16 02:00 UTC  resilient retry policy
  20260616T013310  [completed  ]  r5  2026-06-16 01:33 UTC  how to cache the widget
```

By default (no `-a`) the list shows only **completed** sessions, newest first, capped at
the 20 most recent — the "recent finished work" subset. `-a`/`--all` adds the
dead/interrupted sessions and lifts the cap, so you can find a crashed run to resume.
Listing is **read-only**, so it is safe to run against a brainstorm that is still
in progress (it just parses as a shorter transcript).

**Resuming.** `review sessions -s <id>` does **not** start from scratch: it reloads the
prior transcript, **reuses the saved topic, panel, and moderator**, and continues the
round loop from `completed_round + 1` to the original `--max-rounds` (respecting the
min-rounds / moderator-STOP rules), then produces the final synthesis. The continued
rounds and synthesis are **appended to the same log**, so the resumed run is one
continuous session, not a new file. Override the saved panel/moderator with `-m` /
`--moderator`, point at a repo with `-C`, or cap per-call time with `--timeout`. If the
session crashed *after* the moderator already decided to STOP (but before the synthesis
was written), resume skips straight to the synthesis — it does not run extra rounds.

The original `--diff`/`--staged` grounding is **not** persisted in the log, so a resumed
grounded brainstorm would otherwise continue ungrounded. Pass `--diff` (working-tree) or
`--staged` to **re-attach** the current diff as grounding for the resumed rounds and
synthesis.

The `<id>` can be the short displayed id or any **unambiguous prefix**. Edge cases are
handled explicitly:

| Situation | Behaviour |
|-----------|-----------|
| Unknown id | Error (exit 2), suggests `review sessions -a` to list ids |
| Ambiguous prefix (two runs in the same second) | Error (exit 2), lists the full ids to disambiguate |
| Already-completed session | Refused (exit 2) with a message — pass `--force` to re-synthesize from the saved transcript |
| Zero usable rounds | Degrades to a fresh run over the saved topic (nothing to continue) |

---

## `review dashboard` — local web dashboard (managed service)

The dashboard is a **managed service**: it gets the same lifecycle subcommands every
long-running agent-tools server shares (run / start / status / stop / enable / disable),
from the reusable `agenttools_service` lib — review-cli does not hand-roll pidfiles or
autostart units.

```bash
review dashboard            # bare = HELP (prints the actions, launches NOTHING)
review dashboard run        # run in the FOREGROUND (this shell), blocking — ad-hoc / when disabled
review dashboard start      # start in the BACKGROUND (detached daemon); returns immediately
review dashboard status     # is it running? pid / port / url / autostart-enabled
review dashboard stop       # stop the background instance
review dashboard enable     # install OS autostart (launchd / systemd --user / fallback) AND start now
review dashboard disable    # remove OS autostart AND stop
review dashboard --port 8765 run   # global --host/--port apply to any action
```

A bare `review dashboard` (no action) prints HELP and launches nothing. On `run`/`start`
it hints how to `enable` autostart at login. `run` binds an ephemeral port; the managed
`start`/`enable` bind a stable default port (so `status`'s url and a later restart land on
the same address).

A single-page web app over every review-cli run, built on the Python **stdlib** HTTP
server (no extra deps) and a **vanilla-JS SPA** (no npm/build step — assets ship in the
package). It binds **127.0.0.1 only** by default — the logs persist prompts/diffs that may
carry secrets — so it is not exposed on the network unless you pass `--host 0.0.0.0` (e.g.
to reach it over Tailscale).

**Dependency:** the lifecycle subcommands need the shared `agenttools_service` lib (an
in-ecosystem dependency, the `[dashboard]` extra). It is not yet on PyPI; until then install
it editable from the agent-tools checkout (`pip install -e <agent-tools>/lib/agenttools_daemon
-e <agent-tools>/lib/agenttools_service`). Without it, `run` still works for an ad-hoc
foreground server and a bare `review dashboard` still prints help; only the managed
start/status/stop/enable/disable actions emit an actionable error (exit 4).

**Supported autostart matrix:** macOS → launchd LaunchAgent; Linux → systemd `--user`
unit (with a no-systemd fallback); other OSes → no autostart (`enable` still starts the
service now and warns it will not survive reboot). See `agent-tools/lib/agenttools_service`
for the full matrix.

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

## `review diff --visual` — visual verification

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

*What `review diff --visual <image>` reports across real renders. Top row: unstyled / blank / FOUC /
error-overlay renders the detector flags (each one would block a `tg --photo` send). Bottom row:
properly-styled renders it stays quiet on. (The grid's title art is the tool's old standalone
name "styleprobe" in older copies — it is the `--visual` detector.)*

### Composable flag, not a mode

`--visual` is **orthogonal** to the four review modes — it is a **composable flag** on
any subcommand (`brainstorm` / `quorum` / `just-ask` / `diff`), so the personas /
voters / reviewer literally **see** the image as multimodal context, or it runs
standalone:

```bash
# Standalone — pure verdict pipeline on one render (no diff present)
review diff --visual after.png

# The brainstorm personas see the screenshot and reason about it
review brainstorm "is this layout good?" --visual after.png

# Every quorum voter gets the image as shared context
review quorum "ship this UI?" --visual after.png

# Diff review with the rendered result attached as evidence (a diff present)
review diff --visual after.png
```

When a companion mode is present the image and the active modules' visual questions are folded
into **that mode's** model call — there is no separate isolated visual run. The standalone
verdict pipeline (and its exit codes below) fires only in the mode-less case.

### Vision backends

Vision runs **through the agent CLIs** — `codex` / `claude` / `opencode` — mirroring exactly
how review's text backends shell out, but with the image attached and the structured verdict
parsed from the CLI output. No provider REST keys for those three. **Gemini is the one
exception**: its CLI is broken, so the Gemini vision call stays on the REST API key
(`GEMINI_API_KEY`), same as review's text Gemini backend. Vision requires a vision-capable
model on whatever backend you pick — review never silently uses a text-only model to "verify"
an image. (For router backends like opencode, that means selecting a vision model explicitly,
e.g. `oc:<provider>/<vision-model>`.) `--no-local-model` disables the local cache pre-classifier (the
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

`tg` can run `review diff --visual` as a **pre-send hook** to block an unstyled / broken screenshot
before it reaches Telegram — turning the often-violated "review screenshots before sending" rule
into an enforced mechanism. The hook runs `review diff --visual <png> --json --strict`; a `rollback`
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

**Render any markdown spec server-side and review it like a GitHub PR — as a bidirectional
channel between a human reviewer and the agent that launched it.** Select any text in the
rendered spec → leave a **question** (expects an answer from the spec author) or a **remark**
(feedback that doesn't), anchored to that selection; notes accumulate in a **pending batch**;
one **Submit review** finalizes them **and delivers the structured review back to the
launching agent** (no copy-paste export step). The agent answers a question with
`review spec-web reply <id> <answer>` — the reply shows **in the UI** (distinct agent
styling) and is pushed to **Telegram**. In-progress note text **autosaves to disk** so a page
reload never loses a half-typed comment. Single implicit reviewer (no author field). Reusable
for *any* spec markdown file. Serve it over Tailscale to review from your phone.

```sh
# local, ephemeral port, opens a browser
review spec-web docs/specs/my-spec.md --open

# expose over Tailscale (reachable from a phone on the tailnet)
review spec-web docs/specs/my-spec.md --host 0.0.0.0 --port 8787

# pre-load an existing Q&A thread
review spec-web docs/specs/my-spec.md --seed thread.json

# return to the agent as soon as the reviewer submits (one-shot review handoff)
review spec-web docs/specs/my-spec.md --exit-on-submit

# the agent answers a reviewer's question (shown in the UI + sent to Telegram)
review spec-web reply <comment-id> "the answer" --spec docs/specs/my-spec.md
```

| Flag | Meaning |
|------|---------|
| `--host` | bind host (default `127.0.0.1`; `0.0.0.0` exposes over Tailscale) |
| `--port` | bind port (default: a free ephemeral port) |
| `--seed FILE` | import an initial review thread from a JSON file before serving |
| `--exit-on-submit` | stop the server after the first Submit (the blocking call returns once the review is delivered) |
| `--open` | open the URL in a browser on startup |

**The submit → agent handoff.** `review spec-web` is started by an agent and blocks. When the
reviewer clicks **Submit review**, the server marks the batch submitted in the store and the
launching process prints the **structured review** to stdout framed by a line that is exactly
`<<<REVIEW-SPEC-WEB-SUBMITTED`, then **one line** of compact JSON, then a line that is exactly
`REVIEW-SPEC-WEB-SUBMITTED>>>`. **Parse by taking the single line immediately after the begin
marker** (not by splitting on the end marker) — the JSON is one line and a reviewer's free
text is JSON-escaped on it, so a body that happens to contain the marker substring can never
break the framing. The payload carries every comment's **id** (so the agent can answer a
specific one), `kind`, `status`, `quote`, `section_title`, `body`, and its reply thread, plus
`counts` (questions/remarks/total). By default the server keeps serving after a submit (the
reviewer can keep going and the agent can `reply`), re-emitting a fresh framed payload on each
submit; `--exit-on-submit` returns after the first submit instead. (Like the other persistent
server paths, `--exit-on-submit` ignores `-o FILE` — read the framed payload from stdout.) The
store on disk stays the single source of truth.

**The agent reply → UI + Telegram.** `review spec-web reply <comment-id> <answer> --spec <spec>`
threads the agent's answer under that comment in the store (stamped with the `agent` author so
the UI styles it distinctly), and **also** delivers it to Telegram via the `tg` CLI on `PATH`
(best-effort — a missing/failing `tg` never fails the reply; it logs and continues; `--no-tg`
skips it). An open spec-web page **polls** the comments API, so the agent's reply appears under
the question without a manual refresh.

**Drafts (reload-safe).** While you type a note, the composer **autosaves** the in-progress
text to the server (debounced, persisted to disk) under a per-slot key (the new-note composer
and each edit-in-progress have their own slot). On page load the most recent draft is **restored
into the composer**, so an accidental reload mid-typing continues exactly where you left off.
Saving the note (or clearing the box) drops its draft.

**Layout.** Desktop (≥900px) = two panes side-by-side (spec left, comments right, a
draggable divider). Mobile (<900px) = comments as a bottom sheet under the spec. The
comments panel collapses to just its header bar (which carries a **count badge** so added
notes are visible while collapsed) and re-expands from that same bar. Following an internal
cross-reference link shows a **← Back** control that returns to the prior scroll position.

**Rendering.** Markdown → HTML is rendered server-side with the GitHub heading-slug scheme,
so the spec's own internal links (`[§9.4](#94-…)`) resolve. Figures referenced as
`./assets/fig-*.svg|png` are served as real HTTP resources at `/asset/<name>` (never
inlined). Tables size columns to their content and scroll horizontally on narrow screens
rather than crushing columns.

**Comments.** A note stores the selected quote, the containing section id, char offsets, its
**kind** (`question` | `remark`, default `remark`), the body, created-at, a status
(`pending`/`submitted`/`answered`/`resolved`), and a thread of replies. (An `author` field
is still persisted for import/export round-tripping but is not shown in the UI.) The kind is
surfaced with a coloured chip + icon; each note can be edited (`/api/comments/<id>/edit`). On
reload, each note re-anchors by locating its quote within its section and highlighting it; a
quote that can't be re-found shows in the sidebar as **unanchored** (never a crash).

**Persistence.** One JSON file per spec at `~/.config/review-cli/spec-web/<sha1-of-abspath>.json`
(mode `0600`), surviving restarts. It holds the comments, the in-progress composer **drafts**
(per slot), and the `last_submit` batch the launching agent reads back. Override the directory
with `$REVIEW_SPECWEB_DIR`.

**Security.** Reads (spec, assets, comments, **drafts**) are open — same model as comments
(the in-progress composer text is readable by anyone who can reach the port; there are no
secrets in a spec review); only figures the markdown *references* are served (an unrelated
file in the assets dir 404s), SVGs are served with a `sandbox` CSP so a directly-opened one
can't run script, and symlinked assets that escape the assets dir are refused. Writes (post
comment / reply / submit / import / draft autosave)
are origin-guarded against both CSRF and DNS rebinding: a write requires (1) the request's
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
      "kind": "question",
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
`GET /api/spec`, `GET /api/comments`, `GET /api/drafts`, `POST /api/comments`,
`POST /api/comments/<id>/{reply,edit,status,delete}`, `POST /api/drafts/<slot>`,
`POST /api/submit`, `POST /api/import`. (The old `GET /api/export` is removed — Submit now
delivers the structured review to the launching agent.)

---

## Agent workflows

`review` earns its keep when an agent hits a hard call:

1. The agent runs `review brainstorm "<the decision>"` — many models in rotating
   expert roles, looping across several rounds — to surface candidate approaches a
   single model wouldn't reach.
2. It picks the top one or two and posts them to Telegram via
   [`tg`](https://github.com/alex-mextner/tg-cli) as simplified options with pros/cons,
   so you decide from your phone.
3. For the closest calls, it builds the rival approaches in parallel **git worktrees**
   and compares them for real before committing.

And before every commit, `review diff --staged` is a multi-model gate — optionally *enforced*
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

**Transport split.** Each backend declares which transports it supports — `cli`, `api`,
or both — shown in the *Transport* column above. `REVIEW_<NAME>_MODE` forces one; forcing
a mode a backend doesn't support is a hard error, never a silent fall-through. (Today:
codex/opencode are cli-only, gemini/z.ai/commandcode are api-only, claude does both and
auto-picks — CLI if the binary is present, API when it isn't and a key is set.)

The opencode backend is **agentic and read-only**: it runs in the **real `-C`
repository** (via `opencode run --dir <repo>`), exactly like the codex backend, so an
`oc:` seat can **read any project file** — not just the diff in the prompt. Safety is
enforced by the `read-only-reviewer` agent, which **denies** `edit`/`write`/`bash`/
`webfetch`: opencode may open files but can never mutate the worktree, run a command,
or hit the network.

It falls back to an isolated temp dir (diff-only) in two cases:
- `-C` is **not a git repo** (e.g. a `just-ask` from a scratch dir) — nothing to read;
- the repo **ships its own opencode config** (`.opencode/` or `opencode.json`/`.jsonc`).
  A repo-local agent definition can **override** the global `read-only-reviewer` and
  re-enable `write`/`bash` (verified: project config wins, and no opencode env flag
  suppresses it), so to keep the sandbox trustworthy on a potentially adversarial repo,
  review refuses to run agentically there and reviews the diff in a clean dir instead.

> **Note on commandcode / z.ai (review-cli#24).** These were historically kept as raw
> diff-only `api` backends — opencode's `@ai-sdk/openai-compatible` adapter did not
> reliably drive the Command Code gateway, and z.ai/GLM was not an opencode-native
> provider. That has been **re-investigated and resolved**: with `commandcode` and `zai`
> registered as opencode **custom providers** (`~/.config/opencode/opencode.json`, auth via
> `opencode auth login`), the default board's Kimi/GLM/Qwen/DeepSeek seats now run
> agentically through opencode (`oc:commandcode/…`, `oc:zai/glm-5.2`) like the rest of the
> board. The raw keyed-HTTP `commandcode:`/`zai:` backends remain for explicit `-m cc` /
> `-m glm` and config-board seats on hosts without opencode.

---

## Subcommands & flags

The mode is a **subcommand** (`review <mode> …`); the flags below are shared options
available to the relevant subcommands. A bare `review` (no subcommand) prints this help —
the diff review is `review diff`.

```
SUBCOMMANDS
diff                Diff review across the reviewer board (requires a diff).
brainstorm TOPIC    Multi-round persona ideation; composable with --diff/--staged grounding.
just-ask QUESTION   Single-shot multi-model answer to a question (diff optional).
quorum QUESTION     Experts cite evidence + a moderator finds quorum/disagreement.
dashboard           Local web dashboard over review-cli runs.
sessions            List / resume brainstorm sessions (-a all, -s <id> resume).
spec-web SPEC.md    Interactive web reviewer for a markdown spec.
install-skill | install-commit-hook | register-module

GLOBAL FLAGS (shown by `review --help`; apply to every subcommand)
-m / --model        Backend to run; repeat or comma-separate. Default (no -m) is mode-aware:
                    `review diff` runs the active reviewer board (or your config `models:`);
                    brainstorm uses `brainstorm_models:`, just-ask/quorum the defaults.
                    Each subcommand's `--help` shows its own effective default.
-C / --cwd DIR      Run against a different repository directory.
-o / --output FILE  Write the result to FILE via Python (creates parent dirs, always
                    overwrites) while still printing to stdout. Use this instead of
                    `review … > FILE`, which fails silently under zsh noclobber.
--timeout N         Per-call timeout in seconds (default 1200 for review, 240 for panel modes).
--list-defaults     Print effective default backends and exit.
--show-board        Print the active reviewer board (model -> role + availability) and exit.
--pool N            How many of the board's seats to run (default 4); the first N seats run,
                    the rest are kept in reserve. The board is never off — --pool only sizes
                    it. N<=0 (e.g. --pool 0) runs all seats.

SUBCOMMAND-SCOPED FLAGS (shown by `review <mode> --help`, not the global list)
--diff / --staged   Diff source: working-tree (--diff) or staged (--staged). On the diff
                    review the diff is required; optional grounding for brainstorm.
--prompt TEXT       (review diff) Override the diff-review prompt.
--moderator M       (quorum / brainstorm) Override the auto-picked moderator.
--rounds N          (brainstorm) Minimum rounds before STOP is allowed (default 5).
--max-rounds N      (brainstorm) Hard cap on rounds (default 8).
--visual IMAGE …    Composable visual-verification group (NOT a mode): attach/verify a
                    render; rides any subcommand (e.g. `review diff --visual shot.png`).
                    Companions: --before/--intent/--expect/--check/--json/--strict/--no-ai/
                    --no-local-model/--vision-timeout/--project. See `review <mode> --help`.
```

> **Modes are subcommands, not flags.** `--brainstorm` / `--quorum` / `--just-ask` were
> removed; use `review brainstorm …` / `review quorum …` / `review just-ask …`. The old
> flags print a one-line pointer and exit non-zero. The diff review is `review diff` (a
> bare `review` prints help; the removed `review review` verb points at `review diff`).

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

The plain `review diff` runs a **reviewer board**: a panel where
each model is given its OWN review role/lens, so the panel covers the diff broadly
instead of every model doing the same generic pass. The board is the default panel
out of the box — no config file required.

### Priority-ordered failover pool

The board is a **priority-ordered** list of 8 models — strongest first — and a plain
`review diff` runs a **pool of 4**. The pool is chosen by **priority + availability**, with
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
| 3 | pool | GLM-cc | `commandcode:zai-org/GLM-5.2` | `performance` | complexity, hot paths, allocations, async/concurrency, N+1 (GLM 5.2 via the Command Code gateway; diff-only, read-only by construction) |
| 4 | pool | Codex | `codex` | `consistency` | cross-file consistency, dead refs, contract drift, whole-repo coherence |
| 5 | reserve | Kimi | `oc:commandcode/moonshotai/Kimi-K2.7-Code` | `performance` | complexity, hot paths, allocations, async/concurrency, N+1 (z.ai-less host backfill for the GLM-cc lens) |
| 6 | reserve | GLM | `oc:zai/glm-5.2` | `quality` | readability, naming, duplication, code smells, idiom (z.ai subscription route) |
| 7 | reserve | Qwen | `oc:commandcode/Qwen/Qwen3.7-Max` | `security` | injection, authz, secrets, unsafe deserialization, path traversal, SSRF |
| 8 | reserve | DeepSeek | `oc:commandcode/deepseek/deepseek-v4-pro` | `tests` | missing tests, untested branches, boundary conditions, error-path coverage |
| 9 | reserve | Gemini | `gemini` | `contracts` | public API shape, contracts, types, backward-compat, interface design |

**Agentic by default.** Every board seat that *can* read the repo does. Fable/Opus run via
the agentic claude CLI **when `claude-p` is on PATH** (they fall back to the diff-only
Anthropic API only on a host that lacks the CLI but has an API key), Codex via the codex
CLI, and Kimi/z.ai-GLM/Qwen/DeepSeek through opencode (`oc:provider/model`) — all run
read-only *inside* `-C` and can open any project file, not just the diff. Two seats are
always diff-only stateless HTTP calls: **Gemini** (no agentic transport) and the priority-3
**GLM-cc** seat (`commandcode:zai-org/GLM-5.2` — opencode's `commandcode` provider does not
register this GLM id, so the agentic form errors; the keyed-HTTP route is the one that
reaches it). Both are read-only by construction (they POST only the diff).
`review --show-board` shows each seat's live
`agentic`/`diff-only` scope for the current host. The board has
a reserve, so an `oc:` seat that opencode can't reach is backfilled rather than blocking:
a missing opencode **binary** is detected at startup (the seat probes unavailable and the
pool fills from the next reserve); a missing **provider auth** (opencode present but the
`commandcode`/`zai` provider not logged in) only surfaces at run time and triggers a mid-run
reserve backfill. (The diff-only `commandcode:`/`zai:` keyed-HTTP backends are still there
for explicit `-m cc`/`-m glm` and config boards on hosts without opencode.)

**In-seat retry before reserve-replace.** A seat that fails is first re-tried *on the same
model* when the failure looks **transient** — a `429` rate-limit, a `529`/5xx overload, a
provider timeout, an "overloaded"/"service unavailable" notice — with exponential backoff +
jitter, before any reserve is promoted. The same model usually answers on the next try a
moment later, so a brief throttle spike no longer burns a reserve seat. A **seat-fatal**
failure (auth / bad model / `501` not-implemented / a refusal) is **never** retried — no
retry can fix it — so it falls straight to the reserve. The retry budget is configurable via
`--retry N` (or `$REVIEW_RETRY_COUNT`; default 2, `0` disables it). Every retry and every
reserve promotion is recorded **durably** in the run-log dir (not just stderr), so a
post-mortem or the dashboard can reconstruct exactly how a seat recovered or fell over.

**To re-rank** the board, reorder the priority list (`DEFAULT_BOARD` in
`reviewlib/config.py`, or a `board:` list in `config.yaml`) — the top entry is the
highest priority. The role lens you attach to each model is independent of its priority.

The GLM seat uses **his z.ai subscription** (`glm-5.2`, the newest GLM) through opencode's
`zai` provider, so it reviews agentically — not the diff-only z.ai REST call. It needs that
provider configured in opencode (see Auth); the other `oc:commandcode/…` seats reach the
commandcode gateway the same way. opencode must be installed for the agentic seats; without
it they fall back to the reserve.

```bash
review --show-board        # priority order + which 4 are the live pool + reserve + availability
review diff                # default failover pool: the top 4 AVAILABLE seats by priority
review diff --pool 8       # run all 8 available seats (--pool 0 also means "all available")
review diff --pool 2       # run the top 2 available seats (with failover)
review diff --retry 4      # up to 4 in-seat retries on a transient failure before the reserve
review diff --retry 0      # disable in-seat retry (straight to reserve-replace, legacy)
review diff -m codex -m gemini   # an explicit -m bypasses the board entirely (exact models)
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
`board:` configured, the built-in 9-seat priority board above applies. A `board:` that is
**present but has no usable entry at all** is a hard error (non-zero exit) — it never
silently falls back to the paid default board.

```yaml
# Priority order: the first 4 reachable models are the live pool; the rest are the
# reserve that backfills a skipped/failed seat.
board:
  - { model: "claude:claude-fable-5",  role: architect }
  - { model: "claude:claude-opus-4-8", role: correctness }
  - { model: "codex",                  role: consistency, name: Codex }
  # Agentic via opencode (oc:provider/model) — reads the repo read-only, like the default
  # board (review-cli#24). Use the diff-only `commandcode:`/`zai:` forms only if you want a
  # stateless keyed-HTTP seat that sees just the diff (and needs no opencode install).
  - { model: "oc:commandcode/moonshotai/Kimi-K2.7-Code", role: performance, name: Kimi }
  - { model: "oc:zai/glm-5.2",         role: quality }
  - { model: "oc:commandcode/Qwen/Qwen3.7-Max", role: security, name: Qwen }
```

**Optional heavyweight seats** (NOT enabled by default — the board stays at 9). Add
either to your `board:` list for an extra 1M-context resilience / holistic-senior
pass; both run agentically through opencode's commandcode provider (needs opencode +
`opencode auth login`, like the default `oc:` seats):

```yaml
board:
  # ... the 8 default seats ...
  - { model: "oc:commandcode/MiniMaxAI/MiniMax-M3", role: performance, name: MiniMax }   # 1M ctx — resilience
  - { model: "oc:commandcode/nvidia/nemotron-3-ultra-550b-a55b", role: architect, name: Nemotron }  # 550B, 1M ctx — holistic senior
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

**Kimi / Qwen / DeepSeek / GLM board reviewers (agentic, via opencode):** since
review-cli#24 these default board seats are `oc:commandcode/…` / `oc:zai/glm-5.2` —
they run **agentically through opencode**, so they authenticate via **opencode's own
provider config** (`opencode auth login`, the `commandcode`/`zai` providers in
`~/.config/opencode/opencode.json`), NOT review-cli's `COMMANDCODE_API_KEY`/`ZAI_API_KEY`.
opencode must be installed for these seats. A missing opencode **binary** makes the seat
probe unavailable at startup (the board fills the pool from the next reserve); a missing
**provider auth** (opencode present but the `commandcode`/`zai` provider not logged in) is
NOT caught by the startup probe — it surfaces at run time and triggers a **mid-run reserve
backfill**. Either way the board degrades gracefully rather than blocking. The default GLM
seat pins `oc:zai/glm-5.2` (the
flagship); to run an older GLM, override the seat in a `config.yaml` `board:` list (e.g.
`{ model: "oc:zai/glm-5.1", role: quality }`).

**`COMMANDCODE_API_KEY` / `ZAI_API_KEY` (diff-only `-m cc` / `-m glm` + config boards):**
set `COMMANDCODE_API_KEY` (a Command Code `user_...` token) and/or `ZAI_API_KEY` (or
`ZHIPU_API_KEY`) in the environment or `~/.config/review-cli/.env` to use the **diff-only**
keyed-HTTP backends directly — `-m cc`, `-m glm`, or an explicit `commandcode:`/`zai:` seat
in a `config.yaml` `board:` list. These keys are NOT consulted for the agentic `oc:` board
seats above (opencode carries its own auth). No key is ever written to disk by review — it
is only read. For z.ai the default base URL is the **GLM Coding-Plan endpoint**
`https://api.z.ai/api/coding/paas/v4` — only that endpoint serves the flagship `glm-5.2`;
the standard `https://api.z.ai/api/paas/v4` endpoint tops out at `glm-5.1`. A Coding-Plan
key gets `glm-5.2` out of the box; a standard-plan user overrides with
`ZAI_BASE_URL=https://api.z.ai/api/paas/v4`. An explicit `zai:<model>` suffix wins over
`ZAI_MODEL`; `ZAI_MODEL` alone only affects a bare `-m zai` invocation. `glm-5.2` is a reasoning model: it returns a final
answer plus a `reasoning_content` field; review reads the answer and falls back to the
reasoning text when the answer is empty (e.g. a low output-token budget). This key gates
only the **diff-only** `zai:` path (`-m glm` / an explicit `zai:` config seat) — the
**default** GLM board seat is the agentic `oc:zai/glm-5.2` (role `quality`) and
authenticates via opencode, not `ZAI_API_KEY`. The key is only read, never written.

---

## Architecture — `lib | cli | mcp` + the mode registry

review-cli is layered so the same panel engine is reusable beyond the CLI:

- **lib** — `reviewlib/` is the engine: `panel.py` (parallel fan-out, moderator,
  failover board), `backends.py` (the model transports), `config.py` (board/defaults).
  It has no argparse dependency and is callable directly.
- **cli** — `reviewlib/cli.py` is a **thin** argparse front-end. It resolves the diff,
  models, and `--visual` context, then dispatches to a mode handler. It owns no review
  logic of its own.
- **mcp** — *not built yet*, but the seam is kept clean: an MCP wrapper (or another CLI
  — a future research-cli / task-cli `just-ask`) can call the lib + a mode handler
  directly without dragging the argparse surface along. Each mode handler is thin over
  the lib for exactly this reason.

**Modes are plugin-directory modules** (generalized from the per-project
`features/visual` MODULE registry):

```
reviewlib/modes/
  contract.py     # ModeSpec descriptor + ModeContext (mirrors features/visual/contract.py + module_api.py)
  registry.py     # MODES list, get_mode / known_subcommands / default_mode (mirrors features/visual/registry.py)
  review.py       # MODE = ModeSpec(subcommand="review", diff_policy="require", handler=…)
  brainstorm.py   # MODE = ModeSpec(subcommand="brainstorm", diff_policy="optional", …)
  just_ask.py     # MODE = ModeSpec(subcommand="just-ask", diff_policy="none", …)
  quorum.py       # MODE = ModeSpec(subcommand="quorum", diff_policy="none", …)
```

Each mode is a **self-describing module** that exposes a top-level `MODE = ModeSpec(…)`
declaring the subcommand it registers, its default diff policy, the CLI arguments it
adds, and its thin handler — exactly how a visual module exposes a top-level `MODULE`.
The CLI looks the subcommand up in the registry and dispatches; **adding a mode = drop a
`modes/<name>.py` and list it in `registry.MODES` — no `cli.py` surgery.** A bare
`review …` (no recognized subcommand) routes to the default `review` mode, so the common
diff-review ergonomics are preserved; `--visual` is a composable flag orthogonal to the
mode, so it rides any subcommand.

---

## How review compares

AI code review tools cluster into three camps. **PR-bots** (Qodo PR-Agent, CodeRabbit,
GitHub Copilot code review) run a single model against a *pull request* — they live on
the platform, comment inline, and are great once a PR exists. **In-agent review** (Claude
Code `/review`, Codex review) runs one model on the local diff inside the harness you are
already in. **Autonomous-loop tools** ([ralphex](https://github.com/umputun/ralphex)) fold
review *into* a full code-gen loop — they drive a coding agent through a plan and review its
output as one fused, opinionated pipeline (see [review vs ralphex](#review-vs-ralphex--the-whole-loop-vs-the-review-primitive) below).

`review` is neither: it runs **several models in parallel on the local working-tree diff**
before you ever push, then goes further — a cited **quorum** (consensus with evidence) and
a multi-round **brainstorm** panel for open design questions. It adds **visual review**
(attach a render with `--visual` for a keep / rollback / repair verdict) and **interactive
spec-review tooling** (review a markdown spec like a PR), all from the same binary. It is
**read-only** (never edits your code), **CLI-first** (no PR, no hosted service — it shells
out to model CLIs you already have), and **harness-agnostic** (callable from Claude Code,
Codex, opencode, or a plain shell).

| Tool | Multi-model in parallel | Local pre-PR diff | Consensus / quorum | Design brainstorm | Read-only | No hosted service | Generates code |
|---|---|---|---|---|---|---|---|
| **review** | ✓ | ✓ | ✓ (cited) | ✓ (multi-round) | ✓ | ✓ (your own model CLIs) | — (review only, by design) |
| Qodo PR-Agent | — (1 call) | ~ (CLI, PR-oriented) | — | — | — (suggests edits) | ~ (self-host or hosted) | — |
| CodeRabbit CLI | — (1 service) | ✓ | — | — | — (one-click fixes) | — (hosted) | — |
| GitHub Copilot review | — (1 model) | — (PR / IDE) | — | — | — (suggests edits) | — (hosted) | — |
| Claude Code `/review` | — (1 model) | ✓ | — | — | ~ | — (in-harness) | — |
| Codex review | — (1 model) | ✓ | — | — | ~ | — (in-harness) | — |
| ralphex | ✓ (5 review agents + opt. codex) | ✓ | — | — | — (drives an agent that edits) | ✓ (local Go binary) | ✓ (drives the coding agent) |

`~` = partial. PR-bots shine *after* a PR exists and can apply fixes; `review` is the
pre-commit, multi-perspective second opinion that runs from any shell and decides nothing
for you — it surfaces findings and consensus, you stay in control of the edit.

### review vs ralphex — the whole loop vs the review primitive

[**ralphex**](https://github.com/umputun/ralphex) is the *extended Ralph loop*: a single
local binary that takes a written plan and runs the **entire** autonomous loop — it drives
a coding agent (Claude Code / codex / Copilot CLI) to write code task-by-task in fresh
sessions, then runs its own multi-agent review pipeline (5 parallel agents → optional GPT-5
codex cross-review → a final pass), committing after each step. It genuinely owns the part
`review` does not touch at all: **code generation**. If you want "write a plan, walk away,
come back to reviewed-and-committed code" in one opinionated tool, that is exactly what
ralphex is for, and `review` is `—` on code-gen on purpose.

`review` is the other half of that picture: not a loop, but the **review component an agent
plugs into a loop it controls itself**. You (or your agent) drive the loop and call `review`
only for the critique step — which is the step agents do *worst* on their own. The two shapes:

**ralphex** — one opaque binary that encapsulates *both* code-generation and review in a single
autonomous loop:

![ralphex — all-in-one encapsulated loop: one binary drives a code-gen agent, runs its own built-in review, and commits, repeating until the plan is done](docs/compare-ralphex.svg)

**`review`** — does *only* the review part (the part agents do badly), transparently and
controllably, driven by the agent that keeps code-gen and every decision:

![review — a critique primitive the agent orchestrates: the agent writes code, calls review (multi-model, read-only, never edits) for the critique step, then decides fix / ship / re-loop](docs/compare-review.svg)

In one sentence: **ralphex's strength is that it is all-in-one** — code-gen and review
fused into one walk-away binary; **`review`'s strength is that it is focused and
controllable** — a read-only, multi-model review primitive the agent composes into its own
Ralph loop, so the agent keeps full control of code-gen and of every fix/ship/re-loop
decision instead of handing the loop to a black box.

---

## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain:

- **[tg-cli](https://github.com/alex-mextner/tg-cli)** — simple Telegram CLI to send messages, photos & files, and a two-way agent bridge (reports, Q→buttons, voice/rich)
- **[rig-cli](https://github.com/alex-mextner/rig-cli)** — umbrella dev-env driver: sets up a repo from config — skills, hooks, CI, dep-bootstrap; reconciles drift
- **[agent-tools](https://github.com/alex-mextner/agent-tools)** — the shared catalog `rig` applies: portable agent skills, agent-hooks, the global git-hook dispatcher, CI gates, and MCP servers
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.

Each CLI registers a skill into your agent harnesses (`<tool> install-skill`) so agents know it exists — see Install.
