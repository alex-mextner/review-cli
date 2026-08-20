# Changelog

All notable changes to `review` are documented here. This project adheres to
semantic versioning.

## Unreleased

- **Cross-process/cross-thread locking for the seat-cooldown store (#188).** A board
  dispatches its seats in parallel, so two concurrent `record_cooldown`/`clear_cooldown`
  calls could race on the same unlocked read-modify-write and silently lose one side's
  update. `_locked()` now serializes the critical section with an in-process
  `threading.Lock` plus a bounded cross-process `fcntl.flock`. The flock retry has its
  OWN, much smaller sub-budget than the in-process lock's total deadline — a stalled
  cross-process peer degrades that one thread to in-process-only quickly instead of
  holding the in-process lock hostage (which would otherwise make every other thread
  time out at once and race each other, reintroducing the exact bug this fixes) — and a
  stalled in-process peer degrades further to fully unlocked, instead of hanging the
  caller or blocking indefinitely. A lock failure only ever narrows the guarantee, it
  never aborts the write. The pure disable checks
  (`ttl_seconds=0`/`$REVIEW_SEAT_COOLDOWN_SECONDS<=0`) run before any lock is even
  acquired, so the "un-stick a seat right now" escape hatch stays instant.

- **Diff-identity binding for `review task CODE --check` — closes a real
  quorum-pollution incident class (v4 run-stats records).** The self-merge-authority
  quorum gate used to key PASSED iterations purely by task-code STRING, with no
  binding to which repo or which diff was actually reviewed. Three real incidents in
  one session (2026-08-11) showed task-code reuse (a typo, a shared parent-ticket
  convention, or an accidental/naive substitution) let one diff's real reviews
  silently count toward a completely different diff's quorum — a wrong-repo review,
  a swapped task code between two unrelated PRs, and years of unrelated cross-repo
  history piled onto one code. Every recorded iteration now carries `repo_id` (the
  normalized `origin` remote — lowercased, default-SSH-port-normalized so https/ssh
  forms of the same remote match — or a local path fallback) and `diff_files` (the
  touched-file set), plus `diff_sha256` (diagnostic-only). `review task CODE --check`
  resolves `-C`'s current repo/diff and EXCLUDES any recorded iteration whose repo
  differs or whose files share nothing with the current diff, instead of trusting a
  task-code match alone — matching is file-SET overlap, not diff-content-hash
  equality, so the normal review-fix-re-review loop (where the diff's exact text
  legitimately changes between iterations) still counts. History predating this
  field is "unverifiable" and still counts, preserving old behavior for old data.
  A stderr warning is now printed whenever verification did NOT run (disabled via
  `--no-verify-identity`, or `-C` not resolving to a real directory), so "verified"
  is never silently indistinguishable from "never checked". `gh ship`'s quorum gate
  needs NO changes to benefit — it already resolves `-C` from its own repo root and
  reads the same `passed_iterations`/`.error` keys, which now reflect the filtered
  count automatically. **Threat-model boundary** (be honest about scope): the store
  is a local, self-reported JSONL a caller with write access can append to directly
  — this closes "wrong string matches real but unrelated history", not a
  cryptographic guarantee against a fully malicious agent fabricating a fresh
  record with spoofed identity. See `reviewlib/stats.py`'s "Diff-identity binding"
  docstring section and `tests/test_diff_identity.py` for the full incident-shaped
  regression coverage. Also fixes `_git_diff` to pin `--src-prefix=a/
  --dst-prefix=b/` regardless of the invoking machine's `diff.noprefix` git config
  (found live: a no-prefix diff silently produced an empty `diff_files` list), and
  the `--check` post-push default-branch-diff fallback now uses `git diff
  --name-only` (one call covering staged+unstaged together, immune to
  `diff.noprefix` entirely, no full patch body transferred) — this closes a
  regression a first cut of this same change shipped with, caught by dogfooding
  `review diff` on this PR's own diff before commit (Codex/GLM/Opus/Fable all
  independently found the same missing-prefix-pin bug on the fallback path). A
  SECOND dogfooded review round (same PR, after the round-1 fixes) caught one
  more real bug, independently found by Opus and Fable: the check-time file-set
  resolution took the FIRST non-empty of "local uncommitted changes" vs "branch
  vs default-branch diff", so a single UNRELATED dirty file at post-push check
  time (`gh ship`'s exact call shape) shadowed the branch's real PR files
  entirely, spuriously excluding every legitimate iteration. Fixed to return the
  UNION of both instead of first-match-wins. That round also hardened
  `_compute_repo_id`'s local (no-remote) path fallback to self-normalize via
  `git rev-parse --show-toplevel` instead of trusting the caller's `cwd`
  verbatim, hoisted the per-check file-set into one `frozenset` instead of
  rebuilding it per iteration (was O(iterations × files)), and capped
  `mismatch_details` in `--check --json` at 50 entries (the count in
  `excluded_mismatched_iterations` stays the uncapped true total) so a
  thousands-of-iterations polluted task code — the exact HYP-858 shape — can't
  balloon the JSON payload. A THIRD dogfooded round then added a
  machine-readable `identity_verification: "ran"|"disabled"|"skipped_unresolvable"`
  JSON field (the stderr warnings above weren't parseable by a `--json`-only
  caller) and a regression test for the record-time `diff.noprefix` path (only
  the check-time path had one). A fourth round found no blocking issues.
  Separately (caught only by actually trying to COMMIT this change on this
  repo's own `diff.noprefix=true` dev machine, not by any of the four review
  rounds — none of them execute a live commit against the pre-commit hook):
  `_git_diff`'s new `--src-prefix`/`--dst-prefix` pin made the REVIEWED diff
  text byte-different from the pre-commit hook's own INDEPENDENT, unprefixed
  `git diff --no-ext-diff --cached` recomputation, so the review-stamp hash
  `_write_review_stamp` writes stopped matching the hook's own hash — silently
  breaking the "you must review this exact diff before commit" gate on any
  `diff.noprefix=true` machine. Fixed by having `_write_review_stamp`
  independently re-derive the diff with the SAME unprefixed invocation the
  hook uses, rather than hashing the (now possibly prefixed) reviewed text.
  A FIFTH dogfooded round then caught a subtler consequence of that same fix
  (k3 + Opus, independently): re-deriving the hash at stamp-WRITE time (right
  after the multi-model panel finishes, potentially minutes after dispatch)
  reopened a narrow TOCTOU window — a concurrent index mutation DURING the
  review (a second agent/session in a shared checkout; AGENTS.md documents
  this has happened in production) would get silently certified as reviewed,
  where the pre-fix behavior (hashing the actually-reviewed `diff` text)
  failed closed instead. Fixed by capturing the hook-compatible hash ONCE, at
  DIFF-DISPATCH time (`cli._stamp_hash_for_staged_diff`, called immediately
  adjacent to the diff capture that feeds the models), and threading it
  through `mode_review`/`_mode_review_board`/`_stamp_if_staged_commit_review`
  into `_write_review_stamp` — so the stamp certifies what the models actually
  saw again, while staying hash-compatible with the hook's own unprefixed
  recomputation. Falls back to the write-time re-derive for any non-CLI caller
  of `mode_review` that doesn't thread the new `stamp_diff_hash` parameter.

- **`quorum` — every seat gets a per-seat role/lens, reusing brainstorm's persona
  pool (review-cli#206).** Each expert answer is now reasoned from an assigned role
  (Pragmatic Staff Engineer, Security-Paranoid Reviewer, Developer-Experience
  Designer, Skeptical SRE, Product-Minded Architect, Cost-Conscious Performance
  Engineer — the same pool `brainstorm` rotates through), shown in the transcript as
  `glm [Security-paranoid reviewer]`. When a distinct-model pool is scarce or some
  models are near their usage limit and `expand_flat_models_with_reuse` fills
  multiple seats with one model, each of that model's seats gets a DIFFERENT lens
  (keyed by per-model occurrence, not raw seat index) instead of a bare `<model>#N`
  disclosure label with an otherwise-identical prompt — up to the size of the
  persona pool (currently 6) per model; the moderator is told to still treat all
  seats sharing one model as a single opinion regardless of lens. One persona was
  also renamed in the shared pool: `"DX / ergonomics designer"` →
  `"Developer-experience designer"` (a user-visible change to any brainstorm
  transcript/log referencing the old name).

- **`review stat` — per-harness/per-model usage + health report, and two concrete
  token-burn fixes (2026-08 investigation).** `review stat` (`--days`, `--since`,
  `--top`, `--harness`, `--json`) parses the real per-call logs into a per-backend
  breakdown (calls/ok/fail, a byte-size proxy for every harness, **real** token counts
  for the REST backends that emit them, SKILL.md/MEMORY.md context-pollution rate), the
  Fable (priority-1 board seat) dispatch/failure pattern, retry/promotion totals, and
  the largest individual calls recorded — see `reviewlib/dashboard/tokenstats.py` and
  the README's `review stat` section. Two concrete causes the investigation evidenced
  are now fixed at the source, not just measured: (1) a dispatch-time diff-size cap
  (`reviewlib.backends.cap_diff_for_dispatch`, default 300,000 bytes,
  `$REVIEW_DIFF_MAX_BYTES`) truncates an oversized diff before it reaches any backend —
  a real 6.5MB/583-file diff was previously sent whole to every board seat every round;
  the canonical diff used by `--commit`'s checkpoint integrity check stays uncapped, and
  a piped diff is never capped. (2) A cross-invocation cooldown cache
  (`reviewlib/seat_cooldown.py`) stops the chronically-unavailable Fable seat from
  paying for one full real dispatch on every single invocation — 4,322 of 6,383
  recorded runs dispatched Fable and it failed, most with an explicit session-limit
  notice; a later invocation within the cooldown window now skips the real dispatch and
  returns the same sentinel shape every downstream consumer already recognizes.
- **New `omp:` backend — Oh My Pi agentic read-only seats (review-cli#174).** A board or
  `-m` seat spelled `omp:<provider>/<model>` (e.g. `omp:kimi-code/k3`) now routes to a
  first-class omp backend instead of falling through to the opencode catch-all. The seat
  runs `omp -p --no-session --no-extensions --no-skills --tools read,grep,glob --add-dir
  `<repo> --config <cage-overlay>` from a NEUTRAL temp cwd with a SANITIZED HOME —
  agentic (it can read any project file) and caged read-only with no egress: omp
  executes project-shipped `.mcp.json`/`.omp/tools` from its launch cwd, mounts
  user-scope MCP servers whose tools run arbitrary code, its read tool fetches URLs,
  and the xd:// device transport carries write/edit/bash around `--tools` (all four
  verified against omp v17), so the launch dir is an empty temp dir, HOME points at an
  empty subdir (PI_CODING_AGENT_DIR keeps auth), the repo is mounted read-only via
  `--add-dir`, and a per-run overlay disables `fetch`, `tools.xdev`, and project MCP.
  All boundaries are covered by permanent live assertions (tests/test_omp_cage_live.py,
  opt-in via REVIEW_OMP_CAGE_LIVE=1). The
  prompt+diff travels as an `@<tempfile>` message arg — omp does not read prompts from
  stdin, and the `@file` transport dodges the ~1 MB ARG_MAX ceiling argv-passing would
  hit. `--effort` maps to omp's `--thinking` flag; `REVIEW_OMP_MODE=api` fails loudly
  (CLI-only). Availability is probed offline (binary on PATH + a non-disabled credential
  row for the seat's provider in omp's auth db, honoring `PI_CODING_AGENT_DIR` /
  `OMP_PROFILE`, memoized per db mtime), the seat is `unpaid_providers:`-gateable as
  provider `omp`, `--show-board` labels it `agentic`, and the dashboard attributes
  `omp -m <sel>` calls to the `omp:<sel>` seat (mirroring the `oc:` mapping,
  review-cli#24).
- **`review qa` now honors the run-scoped `--effort` flag (review-cli#127).** The `--effort`
  flag (#150) lifted every review-panel seat's reasoning effort, but the qa write/exec tester —
  a single seat that rides `claude-p`/`codex`, not the panel — silently ignored it. The resolved
  level (a scoped `codex=high` beats the bare global default) now threads into the tester spawn:
  the codex tester gets `-c model_reasoning_effort=<level>` (same builder as the read-only codex
  seat; `max`->`xhigh`), the claude tester gets `--effort <level>` when its binary advertises the
  flag, and both get the universal prompt-level hint so the level is never a silent no-op. This
  closes the gap #150's reviewers flagged ("don't expose `--effort` to qa unless it is honored").
- **Diff review presets and the Sol seat are now built in.** A plain `review diff` now runs the
  `default` preset: pool 4, high effort, and no Fable/Sol by default. Use `--preset light` for a
  cheaper pool 2 at medium effort, or `--preset heavy` for release/risky changes with Fable, Sol
  (`codex:gpt-5.6-sol`), Opus, and GLM-cc in the live pool at highest effort plus the full reserve.
  `--show-board` follows the same preset selection, explicit `-m` remains exact, and unpaid
  Command Code/Fireworks providers are skipped before dispatch. Programmatic `load_board({})`
  now returns the default preset board; use `load_board(..., preset="heavy")` or `DEFAULT_BOARD`
  when callers need the raw 10-seat board including Fable/Sol.

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
- **`review diff --staged --commit` — a safe checkpoint for multi-round fix loops, and a
  documented warning against `git reset --hard` mid-review.** An agent iterating review →
  fix findings → re-review may need several attempts, and until now the only way to
  discard a bad attempt was `git reset --hard` — which can destroy unrelated uncommitted
  work from a DIFFERENT session/agent sharing the same checkout (this happened in
  production). `--commit` (REQUIRES `--staged`; a usage error otherwise, distinct exit
  code, no silent fallback to `git commit -a`) creates a real `git commit` of the staged
  diff right after the review completes, so a bad next attempt can be undone with the safe
  `git reset --soft HEAD~1` instead — it does not touch untracked/foreign files. The
  checkpoint gate mirrors the existing `--staged` commit-hook stamp's three conditions
  exactly (`ok`, `staged`, not `diff_from_stdin` for a piped diff): it checkpoints the
  *reviewed* diff, not a *clean* one — a review reporting open findings still gets
  committed, since `ok` means the pool produced usable verdicts, not "zero findings". The
  commit runs the repo's own commit-msg/pre-commit hooks (never bypassed with
  `--no-verify`); if a hook rejects it, `--commit` fails loudly with its own distinct exit
  code rather than silently skipping the checkpoint. Because a review is multi-minute,
  `--commit` also re-reads the staged index right before committing and refuses (same
  distinct exit code) if it drifted from the diff that was actually reviewed — a TOCTOU
  guard against silently committing unreviewed/unrelated work another process/session
  staged in the meantime. README/AGENTS.md now explicitly warn against `git reset --hard`
  mid-review-cycle and name the safe alternatives.
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

- **GLM 5.2 via the Command Code gateway remains the priority-4 raw-board seat under
  Sol/Opus.** The raw built-in board is now 10 seats: Fable, Sol, Opus, GLM-cc, Kimi,
  Codex, Qwen, DeepSeek, Gemini, and GLM. `commandcode:zai-org/GLM-5.2` (display `GLM-cc`,
  role `performance`) is still diff-only and read-only by construction, because opencode's
  `commandcode` provider does not register that GLM id. A plain `review diff` no longer runs
  Fable/Sol; it uses the `default` preset (`Opus`, `GLM-cc`, `Kimi`, `Codex` as the top
  available pool at high effort, with reserve failover). Use `--preset heavy` to put Fable,
  Sol, Opus, and GLM-cc in the live pool at `xhigh` effort, with the remaining raw-board
  seats as `max`-effort reserve. (Canonical id: `GLM_COMMANDCODE_SEAT`.)

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
    default preset board (Opus/GLM-cc/Kimi/Codex/Qwen/DeepSeek/Gemini/GLM) renders all real
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
- **Priority-ordered failover reviewer pool (historical baseline; superseded by the presets
  entry above)** — the raw built-in board is now a **priority-ordered** list of 10 models
  (strongest first), while a plain `review diff` runs the `default` preset (pool 4, no
  Fable/Sol) and `--preset heavy` runs the full Fable/Sol-capable board. The same failover
  mechanics still apply:
  - **Startup failover** — the active pool is the **top N AVAILABLE** seats by priority;
    a higher-priority but unavailable seat (no key / not on PATH / unpaid provider) is skipped
    and the next-priority one is pulled up, so you still start with the requested number of
    working models.
  - **Mid-run failover** — a seat that fails **during** the review (backend error,
    timeout, empty output, or an "unavailable" reply like a paywalled model returning
    *"… is currently unavailable"*) is replaced by the next-priority **reserve**,
    repeating until the requested number of working verdicts is produced or the reserve is
    exhausted (then the run degrades gracefully, logs it, and exits non-zero).

  Each seat keeps its own role/lens (priority decides *who* sits; the role decides the
  *lens*) — a promoted reserve brings its own lens. `--pool N` overrides the selected
  preset's default; `--pool 0` runs all available seats in that preset/board. `--show-board`
  lists the selected board in priority order (with a `#` rank), tags each seat
  `pool`/`reserve`/`unavail`, and shows the live pool. Run-stats `pool_size` reflects the
  models that actually produced verdicts (a backfilled reserve under its real model id), not
  the planned ones. Re-rank by selecting a preset, setting `models:`, or defining a config
  `board:` list. The board can **never be disabled** (an explicit `-m` or a config `models:`
  list still bypasses it; the `--no-board` flag stays removed).
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
