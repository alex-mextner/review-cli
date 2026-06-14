# Changelog

All notable changes to `review` are documented here. This project adheres to
semantic versioning.

## Unreleased

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
