# Changelog

All notable changes to `review` are documented here. This project adheres to
semantic versioning.

## Unreleased

- **Gemini review/vision seat now defaults to the current GA model `gemini-3.5-flash` (review-cli#139).** The old `gemini-2.5-flash` default was 404ing the pool's gemini seat (it is retired-soon; Google names `gemini-3.5-flash` as its GA replacement with no shutdown date), so a dead seat could silently count toward the self-merge review-quorum's distinct-model bar. Both the text backend (`review_gemini`) and the vision fallback default to `gemini-3.5-flash`; an explicit `$GEMINI_MODEL` or a `gemini:<model>` seat suffix still overrides it. The quorum-count half (a failed/404/degraded seat must not count as a substantive reviewer) is handled by review-cli#138.

- **Model override and unpaid-provider handling are now explicit (review-cli#128).**
  `-m/--model` is honored even when passed before a mode verb (`review -m fable5 diff ...`)
  and remains an exact flat-panel override over config `models:` / `board:`. Providers whose
  billing/subscription is unavailable can be listed in `config.yaml` `unpaid_providers:` or
  `REVIEW_UNPAID_PROVIDERS`; direct seats such as `commandcode:...` and agentic
  `oc:commandcode/...` / `oc:fireworks/...` seats are skipped before any backend process or
  API call. DeepSeek stays in the default board as `oc:commandcode/deepseek/deepseek-v4-pro`;
  the provider availability gate decides whether it can run on the current machine. Use
  `--pool 0` to run every available reviewer seat; fixed counts such as `--pool 8` no
  longer mean "the whole board" after the built-in board grew.
- **Review/panel subprocess backends now time out on silence, not total runtime (review-cli#128).**
  Agent CLI seats such as Fable/Claude/Codex/opencode get at least 20 minutes of quiet
  thinking time on normal review runs; any stdout/stderr progress resets the idle timer, and
  the review-level backstop remains the hard last-resort cap. Bounded QA and vision calls keep
  wall-clock timeout caps. `REVIEW_IDLE_TIMEOUT_SECONDS=N` overrides that subprocess idle
  window for advanced cases; `0` disables idle reap and uses wall-clock `--timeout`.
- **Visible error text is now a built-in visual veto (review-cli#121).** The visual verifier
  asks the existing vision-LLM pass whether any on-screen text reads as a real runtime
  error, exception, or failure diagnostic. Because arbitrary error text has no reliable
  cvGate pixel signature, the new `error-text` module has no CV pre-filter and only blocks
  when the vision answer explicitly sets `error_text_visible=true`.
- **Review iterations are now task-coded (review-cli#108).** All recorded review modes now
  require `--task CODE` (or `$REVIEW_TASK_CODE`) before dispatch, and the code is persisted
  into run-stats plus per-call/brainstorm logs. `review task [CODE]` lists task iterations,
  models, ok/fail counts, linked dashboard sessions, and can print full transcript detail
  by iteration or session id; the dashboard now parses task metadata, filters by task badge,
  and shows task-grouped history. Standalone `review visual IMAGE` remains a verifier-only
  exception, while visual+diff review iterations require the task code.
- **`scripts/deploy.sh` — the rig-apply deploy hook (review-cli#105).** rig-cli 0.8.0+ runs a
  tool's `scripts/deploy.sh` on every `rig apply` to keep the installed tool fresh; this repo
  had none, so the live symlinked checkout silently drifted stale. The script is a guarded
  fast-forward `git pull` of, by default, the checkout the script itself lives in — exactly the
  repo rig's no-arg freshness run targets; a copy outside any checkout falls back to resolving
  the `review` symlink on PATH (the install is a symlink to `bin/review`, pure Python, no build
  step — a pull IS the deploy). It scrubs foreign `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`
  from the environment (a git-hook caller would otherwise pin every command to the wrong repo,
  the review-cli#72 bug class), refuses on a
  dirty (tracked-changes) worktree, detached HEAD, or a checkout that diverged from its
  upstream (exit 2 — never merges/rebases on the user's behalf), exits 0 both when already up
  to date and after a successful deploy, re-registers the agent skill post-pull (bounded,
  non-fatal), and prints a restart note when the deploy touched `reviewlib/` while a resident
  `review dashboard`/spec-web daemon may hold the old code. `--checkout DIR` targets a specific
  clone; `--dry-run` reports what would land without pulling. Tests drive the real script
  against throwaway origin+clone pairs.
- **qa ext harness hardening: a queue-backed stdout reader + an absolute-path isolation
  warning (review-cli#75).** The ext runner's stdout is now drained by a dedicated reader
  THREAD into a thread-safe queue (`_StdoutLineReader`), replacing the `select` + text-mode
  `readline` in `_await_ready` / `_read_reply`. A text-mode pipe buffers ABOVE the OS fd, so
  `select` could report "nothing to read" while a full line sat decoded in the buffer (a false
  timeout) and `readline` could block past the deadline — the new reader always drains complete
  lines and the consumer polls with its own deadline, so a silent-but-alive or mid-reply-dying
  runner is bounded by the timeout, not the buffering. Separately, an ABSOLUTE `sut.ext`
  `extension_path`/`workspace` silently escaped the isolated worktree (`cwd / abs` drops `cwd`)
  while the docstring promised "relative to the run cwd"; `review qa` now WARNS that the ext run
  is not isolated under the default (worktree) run (silent under `--in-place`, where an absolute
  path is expected). Tests cover the reader unit, the `_read_reply` timeout/runner-death/noise-skip
  paths against a fake runner, and the absolute-path warning. (Tier-2 visual screenshot-diffing
  remains deferred — it needs screenshot-capture infra and is tracked in #75.)
- **`review qa` now FORWARDS a `-m claude:<model>` / `-m codex:<model>` suffix to the tester
  spawn, and reaps the ephemeral-worktree trust entry it seeds (review-cli#60).** A model suffix
  used to be REJECTED as "not yet forwarded"; now `resolved_tester_model` extracts it (precedence:
  `REVIEW_QA_TESTER_MODEL` env > a `-m` suffix matching the resolved backend > the backend default)
  and the spawn passes `claude --model <m>` / `codex -m <m>`, so `review qa -m claude:claude-opus-4-8`
  runs the claude tester on opus instead of its default model. Separately, the per-run trust entry
  `_ensure_workspace_trusted` seeds in `~/.claude.json` for each throwaway qa worktree is now
  removed after the run (`_remove_workspace_trust`, in a `finally`) — so default qa runs no longer
  accumulate dead `/tmp/review-qa-wt-*` trusted paths. Reap is doubly conservative: it runs ONLY
  for an ephemeral `review-qa-wt-*` worktree (never the user's real `--in-place` checkout), and it
  removes ONLY an entry still holding just the flags review seeds, leaving an entry a real claude
  session enriched.
- **`review qa --kind auto` now detects a PYTHON Telegram bot, and `review help config`
  documents qa's model selection (review-cli#61).** `_looks_like_bot` previously only read
  `package.json` deps, so a normal Python bot (no `package.json`) fell through to the `backend`
  runbook. It now also parses `requirements.txt` + `pyproject.toml` (PEP 621 `[project]` and
  Poetry `[tool.poetry.dependencies]`) for the Python bot markers (`python-telegram-bot`,
  `aiogram`, `pyrogram`, `telethon`, `pyTelegramBotAPI`) — names matched in PEP 503 canonical
  form so `python_telegram_bot` (underscores) also hits. Best-effort, never crashing detection.
  The pyproject path uses the stdlib `tomllib` (3.11+) and falls back to `tomli` if installed;
  on a bare 3.9/3.10 host with neither, a pyproject-ONLY Python bot is not auto-detected (pass
  `--kind bot`). `requirements.txt` detection works on all supported runtimes. The deep
  `review help config` SELECTION CASCADE now has a `review qa` row noting
  it IGNORES `models:`/`brainstorm_models:`/the defaults and selects one tester via
  `REVIEW_QA_TESTER` / a bare `-m claude|codex`.
- **The reviewer board can now resolve a seat by CAPABILITY or ROLE from the shared
  `agent-tools/lib/contracts/models.yaml` manifest (rig-cli#8 consumer side, review-cli#78).**
  The new `reviewlib/manifest.py` reads the ecosystem's single source of truth for per-model
  capability tags (`vision`/`reasoning`/`code`) + the `roles:` map, and a config `board:` entry
  can name its seat by `capability: vision` (the strongest model carrying that tag) or
  `capability: role:reasoning` (the manifest's symbolic lens) instead of a literal `model:`.
  Manifest provider tokens map to review-cli seat strings (`anthropic`->`claude:`, `openai`->the
  agentic `codex` route, `gemini`, `commandcode:`, `zai:`). **Fully additive + backward-
  compatible:** existing `-m <model>` and literal `model:` board entries are untouched, and a host
  with no manifest reachable degrades gracefully — a `capability:` entry is skipped with a warning
  and the board runs its literal seats / the hardcoded `DEFAULT_BOARD`, never a crash. The manifest
  is located via `$REVIEW_MODELS_MANIFEST` / `$AGENT_TOOLS_DIR` / a few conventional checkout paths.
- **The `claude:claude-opus-4-8` review seat (the PRIMARY review-gate seat) now runs through
  `claude --print` directly instead of the `claude-p` TUI-scraper, fixing a recurring corrupted/
  empty verdict (review-cli#76).** `claude-p` spawns the interactive fullscreen `claude` under a
  PTY and screen-scrapes the result — a lossy redraw surface: spinner frames and cursor redraws
  smear into the captured output, and the scrape frequently fails outright (`assistant_output_
  timeout` → empty stdout), blanking or garbling the opus seat so agents fell back to gemini/codex.
  The CLI path now prefers `claude --print --output-format text` (genuine headless print mode — no
  PTY, no TUI, clean stdout) and only falls back to `claude-p` when the `claude` binary is absent.
  The child is spawned with a decoration-hostile env (`TERM=dumb` / `NO_COLOR=1` / `CI=1`), and the
  captured verdict is run through a shared ANSI/OSC/control-sequence stripper (`strip_control_
  sequences`, now the single source for both this path and the `-o` output file) as
  belt-and-suspenders so a stray escape can never corrupt the parsed `## … [ok]/[needs-changes]`
  line. Other seats and the API path are unchanged.

- **GLM 5.2 via the Command Code gateway added as the priority-3 board seat (directly under
  Opus).** A new default-board seat `commandcode:zai-org/GLM-5.2` (display `GLM-cc`, role
  `performance`) sits immediately after Opus, so a plain `review diff` runs Fable, Opus,
  GLM-5.2-via-commandcode, Codex as its top-4 pool. It carries the `performance` lens (NOT a
  second `correctness` — that would duplicate Opus's lens): inserting it at #3 pushes Kimi,
  the old performance seat, to #5/reserve, so GLM-cc takes over `performance` and the default
  pool keeps its four distinct lenses (architect/correctness/performance/consistency) — a
  pure priority change, no *lens* lost. TRADE-OFF (named explicitly): the default top-4 pool
  used to be fully agentic; now the `performance` lens in a plain `review diff` is served by
  this **diff-only** GLM-cc seat instead of the repo-aware Kimi (pushed to reserve #5), so
  that lens no longer reads the whole repo in a default run — a deliberate consequence of
  ranking GLM-5.2's model strength above transport capability, per the directive, not an
  oversight. When GLM-cc itself is unavailable, the pool backfills with Kimi (#5,
  `performance`) and ends as `[Fable, Opus, Codex, Kimi]` — the SAME four distinct lenses the
  pre-#57 board had, so the new seat costs no lens diversity when it is the one missing (test
  `test_glm_cc_unavailable_keeps_four_distinct_lenses`). Whether that backfill is *agentic*
  depends on the host: review-cli's `COMMANDCODE_API_KEY` gates only the diff-only GLM-cc
  seat, NOT opencode's `commandcode` provider (which carries its own auth from `opencode auth
  login`, independent of that env var — verified: opencode's provider config has no `apiKey`
  bound to it), so a host missing only review-cli's key still has the agentic Kimi reserve.
  The seat is **diff-only** (a stateless
  keyed-HTTP POST through `review_commandcode`, like Gemini) and therefore **read-only by
  construction** — no repo access, tools, or exec, so it needs no `-s read-only` cage. It is
  diff-only on purpose: opencode's `commandcode` provider does not register this GLM id, so
  the agentic `oc:commandcode/zai-org/GLM-5.2` route errors; the keyed-HTTP route is the only
  one that reaches it (verified live). It is **distinct** from the existing lower-priority
  `oc:zai/glm-5.2` seat (display `GLM`): same model family, different provider/transport
  (Command Code gateway vs the z.ai Coding-Plan subscription). It degrades gracefully when
  `COMMANDCODE_API_KEY` is absent (the pool backfills it from the reserve), exactly like every
  other key-gated backend. The board is now 9 seats. (Canonical id: `GLM_COMMANDCODE_SEAT`.)

- **In-seat retry before reserve-replace, with retryable/seat-fatal classification.** The
  failover board now retries a failed seat *on the same model* when the failure is
  **transient** — a `429` rate-limit, a `529`/5xx overload, a provider timeout (exit 124), an
  "overloaded"/"service unavailable"/"too many requests"/quota/throttle notice — with
  **exponential backoff + jitter**, BEFORE promoting a reserve. A **seat-fatal** failure
  (auth/`401`/`403`, an invalid/unknown model, `501` not-implemented, a refusal) is never
  retried and falls straight to the reserve, so the retry budget is spent only where a retry
  can help. The transient/fatal classifier is **mirrored from the agent-tools fallback
  contract** (`lib/contracts/models.yaml` + `model-error-fallback`'s transient regex), reading
  the error CHANNEL only — a long review body that merely mentions "503"/"rate limit" is not
  misread, and the short rc=0 "currently unavailable" sentinel still counts. New `--retry N`
  flag (and `$REVIEW_RETRY_COUNT`; default 2, `0` disables, clamped to a ceiling) — it applies
  to BOTH the failover board and an explicit `-m` panel. Three independent caps bound the cost
  so a slow-failing seat can't pin the run: the retry COUNT, a TIMEOUT sub-cap (a process
  timeout, exit 124, gets one extra attempt — each retry costs a whole per-call timeout), and a
  WALL-CLOCK cap (`$REVIEW_RETRY_MAX_SECONDS`, default 90s) — the backstop for a SLOW transient
  (a 503 returned just shy of the per-call timeout, rc != 124) the timeout sub-cap can't see. An exhausted **quota /
  billing** limit is seat-fatal for in-seat retry (the same key won't replenish in the retry
  window — unlike the cross-harness chain, which can fall to a different provider's quota).
  Every retry, seat-fatal short-circuit, timeout-exhausted cap, and reserve promotion is logged
  **durably** to the run-log dir (a `*-retry.log` event file, each `kind=` distinct), not
  stderr-only. Fail-loud-on-empty is preserved: an unrecovered seat is handed back to the
  reserve/degrade path unchanged, never fabricated into a verdict.
  New `tests/test_inseat_retry.py` (classification matrix, channel discipline, transient-then-
  recovers, fatal-zero-retries, budget respected, durable log, backoff growth + jitter, and the
  panel integration: retry keeps the pool full without touching the reserve; fatal falls
  through; retry-then-reserve on an unrecoverable transient).

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
