# Changelog

All notable changes to `review` are documented here. This project adheres to
semantic versioning.

## Unreleased

- **Dashboard overhaul — real model brand logos, an interactive Errors recovery view, and a
  Python smoke suite.** Five changes:
  - **Per-model BRAND LOGOS, not emoji.** Every seat / participant / model chip across the
    dashboard now renders the model's REAL brand logo as an `<img>` (the committed
    `assets/icons/mini_<brand>.png` set, shared with tg-cli) — Anthropic starburst, the
    OpenAI/Codex mark, Gemini's spark, the DeepSeek whale, Qwen, Kimi, Meta, Mistral, Grok,
    etc. — instead of the unicode-emoji fallback. Each model resolves to its brand via the
    same exact-then-prefix logic tg-cli uses (`extractBaseModel`): Opus/Fable → Anthropic,
    GPT/o1/o3/Codex → OpenAI, Llama → Meta, GLM/z.ai → the GLM brand tile, and so on — so the
    8-seat default board (Fable/Opus/Codex/Kimi/GLM/Qwen/DeepSeek/Gemini) renders all real
    logos with no odd-one-out. A family with no shipped logo (MiniMax, a bare gateway probe)
    renders a clean two-letter brand monogram — never a generic emoji. The server serves the
    PNGs from an allowlisted `assets/icons/` path.
  - **Errors tab is now a drill-down recovery view.** Each failed call is a clickable card
    showing its failure CLASS (paywall / auth / blocked / timeout / error), a RECOVERY status
    (recovered — a later seat/retry returned a clean verdict — or unrecovered), the
    planned FALLBACK seat the failover pool would promote (next board seat by priority + its
    lens), and — when auto-failover is exhausted — a **take manual control** button that opens
    the run with a manual-control note primed in the overseer feedback box. Clicking a card
    opens the failing session detail scrolled to and expanded on the failing call.
  - **Detail / session views** lead with the run's prompt/topic and now resolve each call's
    chip to its gateway MODEL (e.g. "Qwen", not the bare "gateway" backend), so the brand
    logo + label are accurate per call.
  - **Smoke suite converted to Python** (`tests/smoke.py`, replacing the bash `smoke.sh`):
    it drives the real `bin/review` CLI via subprocess and runs the `tests/test_*.py` files.
    Runnable standalone (`python tests/smoke.py`, what CI now runs) or under pytest.
  - **Fixed the lib-absent dashboard `--help` path** (the CI failure): a `review dashboard
    --help`/`-h` with the optional `agenttools_service` lib absent now prints help and exits 0
    (the bare-HELP contract), instead of mis-routing to the missing-lib error (exit 4).

- **The default board is AGENTIC by default (review-cli#24).** Every board seat that
  *can* read the repo now does. The Kimi/GLM/Qwen/DeepSeek seats route through opencode
  (`oc:provider/model`, e.g. `oc:commandcode/moonshotai/Kimi-K2.7-Code`, `oc:zai/glm-5.2`)
  instead of the diff-only `commandcode:`/`zai:` keyed-HTTP REST calls, so they run
  read-only *inside* the repo (`opencode run --agent read-only-reviewer --dir <cwd>`) and
  can open any project file — not just the diff in the prompt — exactly like the codex and
  claude-CLI seats already did. Only Gemini stays diff-only (it has no agentic transport).
  opencode registers `commandcode` and `zai` as custom OpenAI-compatible providers, so the
  same wire model ids are reachable agentically with no new auth. The diff-only
  `commandcode:`/`zai:` backends stay available for explicit `-m cc`/`-m glm` and config
  boards on hosts without opencode; the board prefers the agentic transport and, because it
  has a reserve, an `oc:` seat that opencode can't reach is backfilled (startup/mid-run
  failover) rather than blocking. The flat `DEFAULT_MODELS` panel deliberately keeps the
  diff-only commandcode Kimi seat (it has no reserve, so it must not silently shrink on an
  opencode-less host); `_agentic()` derives the board's agentic seat from the same constant
  so the model id has one source of truth. The dashboard's per-model health view attributes
  agentic opencode calls to their `oc:` board seat (the sidecar header now records the
  `-m <provider/model>` selector), so Kimi/GLM/Qwen/DeepSeek no longer collapse into a
  single `opencode` row.
- **`review spec-web` is now a bidirectional review channel (phase 2).** Three changes:
  - **Submit delivers the review to the launching agent** (no markdown export). The
    primitive "Export review as markdown" button + the `/api/export` endpoint + the
    `--export` flag are **removed**. Instead, clicking **Submit review** marks the batch
    submitted in the store and the blocking `review spec-web` process prints the
    **structured review** (a JSON object with every comment's `id`, `kind`, `status`,
    `quote`, `section_title`, `body`, reply thread, and `counts`) to stdout between the
    markers `<<<REVIEW-SPEC-WEB-SUBMITTED` … `REVIEW-SPEC-WEB-SUBMITTED>>>`, so the agent
    that launched it can parse and act on it. `--exit-on-submit` returns after the first
    submit; by default the server keeps serving so the reviewer can continue and the agent
    can `reply`. The on-disk store stays the single source of truth.
  - **In-progress drafts autosave to disk, reload-safe.** The composer autosaves the
    half-typed note text to the server (debounced ~500ms) under a per-slot key (a new note
    and each edit-in-progress have their own slot), persisted in the same per-spec JSON
    file. On page load the most recent draft is restored into the composer, so a reload
    mid-typing continues where you left off. Saving the note (or emptying the box) clears
    its draft. New routes: `GET /api/drafts`, `POST /api/drafts/<slot>`.
  - **`review spec-web reply <comment-id> <answer> --spec <spec>`** lets the agent answer a
    reviewer's question/remark. The reply threads into the store stamped with the `agent`
    author (the UI styles it distinctly and an open page picks it up by polling the comments
    API), and is **also** delivered to Telegram via the `tg` CLI on `PATH` — best-effort: a
    missing/failing `tg` logs and continues, never failing the reply (`--no-tg` skips it).
- **`-o FILE` / `--output FILE`** — write the review result to a file via Python
  (`open(...,"w")`), which **bypasses the shell redirect** and therefore zsh
  `noclobber`. Agents that ran `review … > out.md` hit a silent failure under
  `noclobber` (the `>` refuses to overwrite an existing file and the command dies
  with no review and no error); `-o out.md` fixes that — it creates parent dirs,
  overwrites, and **still prints to stdout** so the result streams live. The file is
  written on any completed run (including a non-zero one like "No diff to review"), but
  NOT on an early `SystemExit` — an argparse usage error or `--help` never truncates a
  pre-existing `-o` target. Use `review -o out.md`, not `review … > out.md`.
- **opencode backend is now agentic (reads the real repo, read-only)** — the `oc:` /
  `opencode:` backend used to run in an empty temp `git init` dir, so it only ever saw
  the diff in the prompt (the same blindness as the raw-API seats). It now runs in the
  **real `-C` repository** (`opencode run --dir <repo>`), exactly like the codex
  backend, so an `oc:` seat can **read any project file**, not just the diff. Safety is
  enforced by the `read-only-reviewer` agent (denies `edit`/`write`/`bash`/`webfetch`):
  opencode may open files but never mutates the worktree, runs a command, or hits the
  network. It falls back to an isolated temp dir (diff-only) when `-C` is not a git repo
  (e.g. `--just-ask` from a scratch dir) **or when the repo ships its own opencode config**
  (`.opencode/` or `opencode.json`/`.jsonc`) — a repo-local agent definition can override
  the global read-only agent and re-enable write/bash, so review refuses to run agentically
  there and reviews the diff in a clean dir instead (sandbox-trust safety). (At the time of
  this entry the commandcode/z.ai board seats stayed raw diff-only — opencode's
  `@ai-sdk/openai-compatible` adapter did not reliably drive the Command Code gateway, and
  z.ai/GLM was not yet an opencode-native provider. **Superseded by review-cli#24 (see the
  top Unreleased entry):** with `commandcode` and `zai` registered as opencode custom
  providers, those default board seats now run agentically through opencode too.)
- **Priority-ordered failover reviewer pool** — the reviewer board is now a
  **priority-ordered** list of 8 models (strongest first), and a plain `review` runs a
  **pool of 4** chosen by **priority + availability** with two layers of failover so the
  run keeps **4 working reviewers**:
  - **Startup failover** — the active pool is the **top 4 AVAILABLE** seats by priority;
    a higher-priority but unavailable seat (no key / not on PATH) is skipped and the
    next-priority one is pulled up, so you still start with 4 working models.
  - **Mid-run failover** — a seat that fails **during** the review (backend error,
    timeout, empty output, or an "unavailable" reply like a paywalled model returning
    *"… is currently unavailable"*) is replaced by the next-priority **reserve**,
    repeating until 4 working verdicts are produced or the reserve is exhausted (then the
    run degrades gracefully, logs it, and exits non-zero).

  Each seat keeps its own role/lens (priority decides *who* sits; the role decides the
  *lens*) — a promoted reserve brings its own lens. `--pool N` overrides the default 4
  (top-N available, same failover); `--pool 0` runs all available. `--show-board` now
  lists the board in priority order (with a `#` rank), tags each seat `pool`/`reserve`/
  `unavail`, and shows the live pool. Run-stats `pool_size` reflects the models that
  actually produced verdicts (a backfilled reserve under its real model id), not the
  planned ones. Re-rank by reordering `DEFAULT_BOARD` (or a config `board:` list). The
  default priority order is Fable 5, Opus 4.8, Codex (GPT-5.5), Kimi K2.7, GLM-5.2,
  Qwen3.7-Max, DeepSeek-V4-Pro, Gemini. The board can **never be disabled** (an explicit
  `-m` or a config `models:` list still bypasses it; the `--no-board` flag stays removed).
- **Seat 3 is `codex` (agentic), not `commandcode:gpt-5.5` (diff-only)** — GPT-5.5 IS
  codex (same model, two routes). The board now seats the AGENTIC codex CLI route
  (`codex exec -s read-only -C <cwd>`), which reads the whole repo and is free, instead
  of the diff-only `commandcode:gpt-5.5` HTTP route for the same model. The bare `codex`
  seat string takes no `-m`, so it uses the codex CLI default model (a `codex:<model>`
  spec would pin a version). `--show-board` shows seat 3 as `Codex / codex / agentic`.
  If your `config.yaml` `board:` pins `commandcode:gpt-5.5`, replace it with `codex` to
  get the agentic route (a custom `board:` fully overrides `DEFAULT_BOARD`, so it keeps
  the old diff-only route until you change it).

  **Migration note for existing `config.yaml` `board:` lists:** the ORDER of your
  `board:` entries is now interpreted as **priority** (first = highest), since that order
  drives both the startup pool and the failover backfill. A board you previously ordered
  by role (or arbitrarily) will still work, but to get the failover you want, reorder it
  by model strength. `review --show-board` shows the resulting priority + pool/reserve.
- **`--brainstorm` can take a diff into account** — when there is an uncommitted
  working-tree diff under `-C`, a `--staged` diff, or a piped diff, every persona (and
  the moderator) sees it as grounding context so you can brainstorm ABOUT a specific
  change; with no diff it stays pure ideation.
- **Local web dashboard (`review dashboard`)** — serves logs, per-model stats,
  timeout/error metrics, and a moderator/overseer view over the sidecar `.log`
  files. Every REST backend now emits the same sidecar logs as the subprocess
  backends, each under its OWN backend name (gemini, z.ai, commandcode, and
  claude in API mode) — so those runs are no longer invisible or misattributed —
  with `round_no` threaded from the panel so brainstorm rounds are attributed
  correctly and a REST socket timeout is counted as a timeout. Footerless
  (in-flight / aborted) calls are tracked as running, not faked as successes,
  and a quoted timeout/exit marker in review prose never mis-flags a call.
  CSRF-guarded write endpoints.
- **Per-run stats + startup ETA** — every run that dispatches a backend appends a
  structured record (mode, pool_size, model names, REAL monotonic wall-clock,
  ok/fail counts) to a new append-only JSONL store at
  `~/.config/review-cli/run-stats.jsonl` (0600,
  `$REVIEW_STATS_FILE`-overridable). At dispatch the tool prints a one-line ETA to
  stderr keyed on (mode, pool_size) — e.g. `[review] pool=4 (brainstorm) —
  typically ~6m12s based on 12 past runs of this size; do NOT timeout.` — falling
  back to pool-size-only, then a "no history yet, expect minutes" line. Distinct
  from the dashboard's per-call log reader (whose mode is inferred and whose
  duration is a mtime proxy); this store records the ground truth the run knows.
- **No external timeout — internal ≤4h backstop** — `review` now advertises and
  behaves as having NO external time bound: agents must not wrap it in a shell
  `timeout`. The only bound is an INTERNAL last-resort backstop of ≤4h
  (`reviewlib.backstop`), a watchdog `main()` arms itself that force-terminates a
  genuinely wedged run (exit 124) so "no external timeout" can never mean "runs
  forever". A healthy run finishes in minutes, far under the ceiling, and the
  watchdog is cancelled cleanly on return. On a fire it KILL-FIRST reaps the live
  backend subprocesses (SIGKILL straight to each one's own session group, no
  blocking SIGTERM grace, so even a SIGTERM-ignoring backend is bound and the reap
  can't be preempted) before the hard exit — without ever signalling the CLI's own
  / the caller's process group — and a deadman timer guarantees the `os._exit` even
  if the stderr announce blocks on a full pipe. The persistent server subcommands
  (`dashboard`, `spec-web`) are exempt (they run until Ctrl-C).
  `$REVIEW_BACKSTOP_SECONDS` can only LOWER the ceiling, never raise it past 4h.
- **Advertising: never short-timeout `review`** — the installed SKILL.md and the
  always-on blurb now state plainly that `review` / `--quorum` / `--brainstorm`
  are multi-model / multi-round and take MINUTES, that a short shell `timeout`
  kills the run before its synthesis, that the startup ETA line is what to wait
  for, and that there is NO external timeout (only the internal ≤4h backstop).
  Re-run `review install-skill` to regenerate.

## 0.2.0

First versioned cut. Everything below is already on `main`.

- **Multi-backend `claude` / opus** — an API variant (Anthropic-compatible
  Messages API, e.g. CommandCode via `ANTHROPIC_BASE_URL`) alongside the
  `claude-p` CLI variant. Selected by `REVIEW_CLAUDE_MODE=api|cli` or auto
  (CLI if the binary is present, API only when it isn't and a key is set).
- **Opus-first moderator with runtime fallback** — `MODERATOR_CANDIDATES`
  (`claude:claude-opus-4-8` → `codex` → `gemini`); a dead top moderator
  auto-falls-back at run time, and brainstorm promotes the winner so a dead top
  is paid once, not every round.
- **`--visual`** — a composable image-review pipeline (cvGate + AI vision +
  policy engine), per-project visual modules, and trust-by-default module
  loading with a TOFU quarantine under `REVIEW_TRUST` guard.
- **Streaming output** + partial-output-on-timeout for the panel modes, plus an
  incremental brainstorm discussion log.
- **git-root cwd resolution** — `-C` / `--cwd` resolves to the git toplevel and
  warns loudly when run off a repo.
- **Headless `claude`/opus auto-trust** — seeds workspace trust so the headless
  backend never blocks on the "Do you trust this folder?" prompt (paired with
  claude-p's deterministic trust).
- Decomposed the monolithic `bin/review` into the `reviewlib` package
  (zero behaviour change).
- **CI** — GitHub Actions runs `tests/smoke.sh` (core suite + guarded visual
  suite) across Python 3.10–3.13.
