#!/usr/bin/env bash
set -euo pipefail

# Guarded smoke test: no API keys or backends required. Only --help, --list-defaults,
# and a syntax check, so CI stays green without secrets.

# Redirect the run-stats store to a throwaway temp file for the WHOLE suite, so no
# test that invokes the real CLI (e.g. the --visual fan-out) appends to the user's
# real ~/.config/review-cli/run-stats.jsonl. Exported so every child python3 sees it.
export REVIEW_STATS_FILE="$(mktemp -d)/run-stats.jsonl"

bin/review --list-defaults | grep -q codex
bin/review --help >/dev/null

# Modes are now SUBCOMMANDS, not flags. The top-level help advertises the subcommand
# list; each mode has its own `review <mode> --help`. The OLD mode flags are GONE. The
# diff review is the `diff` subcommand (renamed from the stuttering `review review`).
bin/review --help | grep -q "subcommands:"
bin/review --help | grep -q "diff"
bin/review --help | grep -q "brainstorm"
bin/review --help | grep -q "just-ask"
bin/review --help | grep -q "quorum"
# Each mode subcommand parses + advertises its positional / shared options.
bin/review diff --help | grep -q -- "--prompt"
bin/review brainstorm --help | grep -q "topic"
bin/review just-ask --help | grep -q "question"
bin/review quorum --help | grep -q "question"
# Brainstorm-only flags live ONLY on the brainstorm parser (not the shared surface): a
# `review just-ask --rounds 5` must error (exit 2), and --rounds shows on brainstorm.
bin/review brainstorm --help | grep -q -- "--rounds"
! bin/review just-ask "q" --rounds 5 >/dev/null 2>&1
! bin/review --help | grep -q -- "--rounds"   # not on the top-level/shared help
# The removed mode FLAGS now ERROR helpfully (exit 2) and point at the subcommand.
# `review` exits 2 here, so capture its output FIRST (a `review … | grep` would trip
# `set -o pipefail` on review's nonzero exit even when grep matches).
! bin/review --brainstorm "x" >/dev/null 2>&1
! bin/review --quorum "x" >/dev/null 2>&1
! bin/review --just-ask "x" >/dev/null 2>&1
removed_msg="$(bin/review --brainstorm "x" 2>&1 || true)"; echo "$removed_msg" | grep -q "review brainstorm"
removed_msg="$(bin/review --quorum "x" 2>&1 || true)"; echo "$removed_msg" | grep -q "review quorum"
removed_msg="$(bin/review --just-ask "x" 2>&1 || true)"; echo "$removed_msg" | grep -q "review just-ask"
# `--mcp` (the dead review-MCP entrypoint) and `--ln` were removed with NO replacement —
# they must FAIL LOUD with a structured what/why/how-to-fix error (exit 2), not argparse's
# opaque "unrecognized arguments". The --mcp fix names the stale registration to delete
# (so a dead `~/.claude/mcp/mcp.json` `review --mcp` is diagnosable, ROADMAP §9).
! bin/review --mcp >/dev/null 2>&1
mcp_msg="$(bin/review --mcp 2>&1 || true)"
echo "$mcp_msg" | grep -q '`--mcp` was removed'
echo "$mcp_msg" | grep -q "mcp.json"
! echo "$mcp_msg" | grep -q "unrecognized arguments"
ln_msg="$(bin/review --ln 2>&1 || true)"; echo "$ln_msg" | grep -q '`--ln` was removed'
# The diff review is now the `diff` SUBCOMMAND (`review diff`), NOT a bare `review`. A bare
# `review` prints HELP (exit 0) — it does NOT run a diff review. Meta queries still work
# without a subcommand. The removed `review review` verb prints a `review diff` pointer.
bin/review --list-defaults | grep -q codex
bin/review diff --list-defaults | grep -q codex                 # explicit diff subcommand
bin/review | grep -q "subcommands:"                             # bare review = HELP (exit 0)
bin/review | grep -q "review diff"                              # …and points at `review diff`
review_review_msg="$(bin/review review 2>&1 || true)"
echo "$review_review_msg" | grep -q "no longer a subcommand"    # removed stutter verb
echo "$review_review_msg" | grep -q "review diff"               # …points at the new verb
! bin/review review >/dev/null 2>&1                             # exit 2 (usage)

# Outside a git repo, the diff-review path must fail GRACEFULLY (no traceback): a clear
# 3-part message + the STABLE EXIT_NOT_A_REPO=3 code. Meta + no-git modes still work
# anywhere. `review diff -C <non-git-dir>` from inside this repo is the non-repo case (a
# fresh mktemp dir is never a git work tree). Capture output FIRST: `review diff` exits 3
# here, so a `review … | grep` would trip `set -o pipefail` on the nonzero exit even when
# grep matches.
NONGIT_SMOKE="$(mktemp -d)"
set +e
nogit_out="$(bin/review diff -C "$NONGIT_SMOKE" </dev/null 2>&1)"; nogit_rc=$?
set -e
[ "$nogit_rc" -eq 3 ]                                            # stable not-a-repo exit code
echo "$nogit_out" | grep -q "not in a git repository"           # WHAT
echo "$nogit_out" | grep -q "diff review needs a repo"          # WHY
echo "$nogit_out" | grep -q "just-ask"                          # HOW (a no-git mode)
echo "$nogit_out" | grep -q "cd into a repo"                    # HOW (or cd into one)
! echo "$nogit_out" | grep -q "Traceback (most recent call last)"   # NO raw traceback
! echo "$nogit_out" | grep -q "RuntimeError"                        # NO raw exception
# A no-git mode + the meta flags must still work from a non-git dir (exit 0, no git error).
# NOTE: the subcommand must come FIRST — `_dispatch` only recognizes a mode as argv[0], so
# `review -C DIR just-ask …` is a TOP-LEVEL review parse (the documented order is
# `review just-ask -C DIR …`). Assert on the SUBCOMMAND-specific help string ("the question
# to ask") so this can't pass spuriously off the top-level overview text.
bin/review just-ask -C "$NONGIT_SMOKE" --help | grep -q "the question to ask"
bin/review -C "$NONGIT_SMOKE" --list-defaults | grep -q codex

# Help must show ACTUAL defaults (ROADMAP "Help must show ACTUAL defaults — esp. --model"):
# --model names the effective default (the active board when no -m/config) on the GLOBAL
# help; --moderator (scoped to quorum/brainstorm) names the auto-pick chain on THAT
# subcommand's help; the numeric flags show their concrete defaults on the right surface.
bin/review --help | grep -q "active reviewer board"            # --model default (global)
bin/review quorum --help | grep -q "claude:claude-opus-4-8"    # --moderator auto-pick chain (panel mode)
bin/review --help | grep -q "default 4"                        # --pool (global)
bin/review diff --help | grep -q "default 60"                  # --vision-timeout (visual, on the subcommand)
python3 tests/test_help_defaults.py
echo "help-defaults tests OK"

# Topic-based deep help (ROADMAP "Topic-based help across the ecosystem"): the main help
# LISTS the topics + points at `review help config`; `review help config` (and the
# `review --help config` alias) prints the config reference; an unknown topic is exit 2.
bin/review --help | grep -q "help topics"
bin/review --help | grep -q "review help config"
bin/review help | grep -q "config"
bin/review help config | grep -q "configuration reference"
bin/review help config | grep -q "SELECTION CASCADE"
bin/review --help config | grep -q "configuration reference"     # the alias form
! bin/review help totally-bogus-topic >/dev/null 2>&1            # unknown topic -> exit 2
python3 tests/test_topic_help.py
echo "topic-help tests OK"

# Subcommand-only options belong in the SUBCOMMAND help, not the global list (ROADMAP).
# The composable --visual feature flags and their companions live on `review <mode> --help`
# (they ride any subcommand), NOT on the top-level `review --help`.
bin/review diff --help | grep -q -- "--visual"
bin/review diff --help | grep -q -- "--no-ai"
bin/review diff --help | grep -q -- "--strict"
bin/review diff --help | grep -q -- "--no-local-model"        # Stage-2a local pre-classifier toggle
bin/review diff --help | grep -q -- "--vision-timeout"
# …and they must NOT appear in the GLOBAL top-level help (option list scoped to truly-global).
# Strip the subcommand-summary epilog first (`brainstorm  …(composable with --diff/--staged…)`
# legitimately names those flags in prose), then assert no visual/mode-only flag is an OPTION.
top_opts="$(bin/review --help | sed -n '/^options:/,/^subcommands:/p')"
! echo "$top_opts" | grep -qE -- "--visual|--no-ai|--strict|--no-local-model|--vision-timeout|--before|--intent|--expect|--check|--json|--project"
! echo "$top_opts" | grep -qE -- "--prompt|--moderator"        # mode-only opts off the global list too
# --prompt is the diff review's; --moderator steers quorum/brainstorm — each on its own help.
bin/review diff --help | grep -q -- "--prompt"
! bin/review just-ask --help | grep -q -- "--prompt"
! bin/review just-ask --help | grep -q -- "--moderator"
bin/review quorum --help | grep -q -- "--moderator"
bin/review brainstorm --help | grep -q -- "--moderator"

# Reviewer board (HYP-741 / failover pool): the board flags must appear in help, and
# --show-board must list the out-of-the-box 8-seat PRIORITY-ordered DEFAULT_BOARD (no
# config file needed) with roles and byte-exact model ids — incl. the z.ai-direct GLM
# seat (zai:glm-5.2) and the top-priority Fable/Opus seats. Availability depends on the
# env, but the LISTING is always complete. The board can NEVER be disabled — there is no
# --no-board flag.
bin/review --help | grep -q -- "--show-board"
bin/review --help | grep -q -- "--pool"
! bin/review --help | grep -q -- "--no-board"

# -o / --output: documented in help, steers away from `> file` (zsh noclobber), and
# actually writes a file (bypassing the shell redirect) while still printing to stdout.
bin/review --help | grep -q -- "-o FILE"
bin/review --help | grep -qi "noclobber"
OUT_SMOKE="$(mktemp -d)/sub/out.txt"   # parent dir does NOT exist yet -> must be created
bin/review -o "$OUT_SMOKE" --list-defaults | grep -q codex   # still prints to stdout
grep -q codex "$OUT_SMOKE"                                    # AND wrote the file (parent dir made)
# Overwrite must work even under noclobber (the bug `-o` fixes); shell `>` would refuse.
set -o noclobber
bin/review -o "$OUT_SMOKE" --show-board >/dev/null
grep -q "architect" "$OUT_SMOKE"
set +o noclobber

# Board scope labels: agentic (codex/opencode/claude-CLI read the repo) vs diff-only.
bin/review --show-board | grep -qi "agentic"
bin/review --show-board | grep -qi "diff-only"
bin/review --show-board | grep -q "architect"
bin/review --show-board | grep -q "claude:claude-fable-5"
bin/review --show-board | grep -q "claude:claude-opus-4-8"
bin/review --show-board | grep -q "commandcode:deepseek/deepseek-v4-pro"
bin/review --show-board | grep -q "zai:glm-5.2"
# Seat 3 is the agentic codex route (GPT-5.5 IS codex) — assert the Codex seat line shows
# the `codex` model AND the `agentic` scope (the diff-only commandcode:gpt-5.5 was retired).
# Two portable substring checks on the seat line (no GNU-only `\b`, order-independent).
bin/review --show-board | grep "Codex" | grep -q "codex"
bin/review --show-board | grep "Codex" | grep -q "agentic"
bin/review --show-board | grep -q "contracts"
bin/review --show-board | grep -q "8 seats"
# Priority order is shown (seat #1 etc.) and the failover pool is described.
bin/review --show-board | grep -qi "priority"
bin/review --show-board | grep -q "#1"

# Failover pool: default reviewer POOL SIZE = 4. --show-board must advertise the live
# pool (top 4 AVAILABLE seats by priority; the rest reserve) and the --pool sizing.
bin/review --show-board | grep -qi "live pool"
bin/review --show-board | grep -q "reserve"
bin/review --show-board | grep -qi "pool 4"
bin/review --show-board | grep -q -- "--pool"
# --show-board honors an explicit --pool N (every seat env-available on dev): --pool 2
# tags only the top 2 priority seats `pool`. (>= 2 tolerates a degraded dev env where a
# higher-priority seat is unavailable and the pool fills from below; on a fully-keyed dev
# box it is exactly 2.)
[ "$(bin/review --show-board --pool 2 | grep -c '\[pool')" -le 2 ]
[ "$(bin/review --show-board --pool 0 | grep -c '\[reserve\]')" -eq 0 ]
# The --no-board flag is GONE: passing it must be a parse error (exit 2), not accepted.
! bin/review --no-board --show-board >/dev/null 2>&1

# Board redesign: the `brainstorm` subcommand COMBINES with a diff. Its help must parse
# and advertise --diff/--staged grounding. (No backend is invoked — only --help is
# exercised here; the behavioural path is covered in pytest.)
bin/review brainstorm --help | grep -q -- "--diff"
bin/review brainstorm --help | grep -q -- "--staged"

# spec-web subcommand: dispatches + advertises its flags (no server started here).
bin/review spec-web --help | grep -q -- "--seed"
bin/review spec-web --help | grep -q -- "--host"
bin/review spec-web --help | grep -q -- "--exit-on-submit"
# the `reply` subcommand (agent answers a reviewer's question) dispatches + advertises args.
bin/review spec-web reply --help | grep -q -- "--spec"
bin/review spec-web reply --help | grep -q -- "comment_id"
# The single-file CLI must always parse.
python3 -c "import ast; ast.parse(open('bin/review').read()); print('ast.parse OK')"

# HYP-742: the local web dashboard subcommand must be wired and its help must parse.
bin/review dashboard --help | grep -q -- "--no-open"
bin/review dashboard --help | grep -q -- "--port"

# Resumable sessions: the `sessions` bare subcommand must be wired and advertise its
# list/resume flags. Listing is exercised against a TEMP log dir ($REVIEW_LOG_DIR) seeded
# with one completed + one interrupted brainstorm log — never the real logs. -a shows the
# dead one; default hides it; -s resolves ids (unknown -> exit 2). No backend is invoked
# (no resume is actually run here — the round-loop/seed path is covered in pytest).
bin/review sessions --help | grep -q -- "--all"
bin/review sessions --help | grep -q -- "--resume"
bin/review sessions --help | grep -q -- "--diff"      # re-attach grounding on resume
bin/review sessions --help | grep -q -- "--force"
SESS_DIR="$(mktemp -d)"
printf '# Brainstorm: smoke-complete\n\npanel=codex moderator=opus rounds>=5 max=8\n# Round 1\n#### codex\nx\n## Moderator (round 1)\nok\nDECISION: STOP\n# Final synthesis\ndone\n' > "$SESS_DIR/20260101T000000_000001Z-brainstorm.md"
printf '# Brainstorm: smoke-dead\n\npanel=codex moderator=opus rounds>=5 max=8\n# Round 1\n#### codex\n(no output)\n' > "$SESS_DIR/20260101T000100_000001Z-brainstorm.md"
REVIEW_LOG_DIR="$SESS_DIR" bin/review sessions    | grep -q "smoke-complete"   # default: completed only
! REVIEW_LOG_DIR="$SESS_DIR" bin/review sessions    | grep -q "smoke-dead"      # default hides interrupted
REVIEW_LOG_DIR="$SESS_DIR" bin/review sessions -a | grep -q "smoke-dead"        # -a includes interrupted
REVIEW_LOG_DIR="$SESS_DIR" bin/review sessions -a | grep -q "interrupted"
! REVIEW_LOG_DIR="$SESS_DIR" bin/review sessions -s NOPE >/dev/null 2>&1        # unknown id -> exit 2

# Dashboard parser / store / JSON endpoints (no backends, no network beyond a local
# 127.0.0.1 socket the test binds itself; log dir + store are redirected to temp dirs).
python3 tests/test_dashboard.py
echo "dashboard tests OK"

# Stale-command guard (codex review): the dashboard UI must not suggest the REMOVED
# invocations — a bare `review` (now prints help) or the `--quorum`/`--brainstorm`/`--just-ask`
# flags (now error). It must point at the current `review diff` / `review quorum` etc.
! grep -qE 'review --(quorum|brainstorm|just-ask)' reviewlib/dashboard/assets/app.js
grep -q "review diff" reviewlib/dashboard/assets/app.js
echo "dashboard-stale-command guard OK"

# Stale-command guard for USER-FACING DOCS (codex review): the README + the visual spec must
# not contain the UNAMBIGUOUSLY-BROKEN executable command shapes the rename retired — the old
# mode FLAGS as a command (`review --quorum`/`--brainstorm`/`--just-ask`, NOT a "was removed"
# note), or a piped diff into a bare `review` (`git diff | review` with NO verb, which now
# exits 2). The composable `--visual` flag (`review diff --visual`) is the current spelling;
# bare `review --visual` as a command is also retired. (Prose that NAMES the `--visual`
# feature is fine; we match only command-leading forms.) Keeps docs from sending users to
# commands that now fail.
for doc in README.md docs/architecture-visual-verification.md; do
  # Removed mode flags used AS a command (line starts with `review --<flag>`), not prose.
  ! grep -nE '^[[:space:]]*review --(quorum|brainstorm|just-ask|visual)\b' "$doc"
  # A diff piped into a bare `review` with no subcommand (now exits 2). Allow `review diff`,
  # `review just-ask`, `review quorum`, `review brainstorm` after the pipe.
  ! grep -nE '\| *review +(-|$)' "$doc"
  ! grep -nE '\| *review +--' "$doc"
done
echo "docs-stale-command guard OK"

# Streaming runner: real-time log growth + partial-output-on-timeout (no backends
# needed — drives a fake slow python child).
REVIEW_LOG_DIR="$(mktemp -d)" python3 tests/test_streaming.py
echo "streaming tests OK"

# Workspace-trust auto-seed for the headless claude/opus backend (the test
# isolates HOME to a temp dir internally — never touches the real ~/.claude.json).
python3 tests/test_workspace_trust.py
echo "workspace-trust tests OK"

# Moderator priority selection + runtime fallback (backends stubbed; no API keys).
python3 tests/test_moderator.py
echo "moderator tests OK"

# cwd resolution: git-toplevel detection + non-repo warning (real temp git repos).
python3 tests/test_cwd.py
echo "cwd tests OK"

# No-git-repo graceful failure: the diff-review path outside a repo prints the 3-part
# message + the stable EXIT_NOT_A_REPO code with NO traceback; the no-git modes + meta
# flags work anywhere; a piped diff needs no repo; a real repo is regression-safe. Driven
# through the real bin/review as a subprocess (catches any uncaught traceback). Offline.
python3 tests/test_no_git_repo.py
echo "no-git-repo tests OK"

# -o / --output: argv extraction (all flag forms), tee-to-stdout + file write, overwrite
# (the noclobber fix), parent-dir creation, bad-path error, file written even on a
# non-zero review. All offline (uses --list-defaults; no backends needed).
python3 tests/test_output_flag.py
echo "output-flag tests OK"

# opencode real-repo (read-only agentic): the oc: backend runs in the real -C repo with
# --dir (reads any file), falls back to a temp dir outside a repo, and never writes to
# the repo. _run_streamed is mocked so no live opencode is needed.
python3 tests/test_opencode_realrepo.py
echo "opencode-realrepo tests OK"

# spec-web reviewer: render (slugs/figures), store (CRUD/submit/seed/export, 0600),
# server routes + origin guard (loopback + Tailscale allowed, foreign rejected). All
# offline on a loopback ephemeral port; store isolated to a temp dir.
python3 tests/test_specweb.py
echo "spec-web tests OK"

# claude backend api/cli dispatch + Anthropic Messages API path (urlopen stubbed;
# no network, no claude binary, no real key).
python3 tests/test_claude_api.py
echo "claude-api tests OK"

# Keyed OpenAI-compatible provider backends (z.ai / commandcode, HYP-741): backend
# routing, request shape, key resolution, availability — all offline (urlopen mocked,
# no keys/network needed).
python3 tests/test_provider_keys.py
echo "provider-keys tests OK"

# Reviewer board (HYP-741): default-board shape, config.yaml `board:` parsing,
# role-lens injection, graceful skip of unavailable reviewers, CLI wiring. All
# offline (backends monkeypatched / forced unavailable; no keys, no network).
python3 tests/test_reviewer_board.py
echo "reviewer-board tests OK"

# Agent-tools session review marker (~/.cache/agent-tools/last-review): a successful
# staged review touches the mtime-windowed marker the separate require-review-before-commit
# hook checks; an unstaged or FAILED review does not; the touch is best-effort (an
# unwritable marker never fails the review). Offline (REVIEW_FAKE_BACKEND, marker redirected
# to a temp path via REVIEW_MARKER — never the real cache).
python3 tests/test_review_marker.py
echo "review-marker tests OK"

# Failover pool (priority + availability): startup failover skips an unavailable
# higher-priority seat and pulls the next up; mid-run failover backfills a FAILED seat
# (incl. the rc=0 "unavailable" sentinel body) from the reserve to keep the count;
# --pool N honored; priority order respected; graceful degradation; tally correctness.
# All offline (resolve_backend stubbed — no model call, no network).
python3 tests/test_failover_pool.py
echo "failover-pool tests OK"

# Board redesign: --brainstorm + diff grounding. With a diff present, every persona
# job (and the moderator) sees it as context; with no diff it's pure ideation. All
# offline (run_panel / run_moderator stubbed — no model call, no network).
python3 tests/test_brainstorm_diff.py
echo "brainstorm-diff tests OK"

# Brainstorm FAILS LOUD on a dead panel (ROADMAP CTO 2026-06-16): a round whose seats all
# return empty/errored output aborts with a clear error + the stable EXIT_DEAD_PANEL code,
# instead of a hollow DECISION: STOP + empty synthesis. Exercises the real loop /
# _round_is_dead / result_is_usable; only the backend boundary is stubbed (no network).
REVIEW_LOG_DIR="$(mktemp -d)" python3 tests/test_brainstorm_dead_panel.py
echo "brainstorm-dead-panel tests OK"

# Mode SUBCOMMANDS + the mode registry (the modes-subcommands redesign): each
# subcommand dispatches to the right mode, a bare `review` prints HELP (the diff review is
# `review diff`), brainstorm composes with --diff, the removed mode flags error helpfully,
# and the registry contract
# (get_mode/known_subcommands/diff_mode). All offline (mode handlers stubbed).
python3 tests/test_mode_subcommands.py
echo "mode-subcommands tests OK"

# Installed pre-commit hook text: the review-before-commit gate must tell a blocked user to
# run `review diff --staged` (the renamed diff review), not the dead bare `review --staged`.
python3 tests/test_install_hook_text.py
echo "install-hook-text tests OK"

# install-* INSTALLED state (ROADMAP "install-* commands must show INSTALLED state"): a
# re-run of install-skill / install-commit-hook / register-module on an already-set-up target
# reports a green ✓ "already configured — nothing to do" instead of silently rewriting. All
# state (HOME / git config / registry) is isolated to temp dirs INSIDE the test — never the
# real machine — so this is safe to run in CI / smoke.
python3 tests/test_install_state.py
echo "install-state tests OK"

# Resumable brainstorm sessions: id derivation, parsing a completed vs interrupted (incl.
# empty-round) discussion log, list_sessions (default completed-only vs -a all), find_session
# (exact/prefix/unknown/ambiguous), and the RESUME seed (continue from completed_round+1,
# reuse saved topic/panel/moderator, append to the SAME log). All offline (run_panel /
# run_moderator stubbed where defined; log dir redirected to a temp dir — never the real logs).
python3 tests/test_sessions.py
echo "sessions tests OK"

# REAL end-to-end resume: spawn the actual `bin/review` CLI as a SUBPROCESS (not in-process
# stubs), KILL it mid-brainstorm, then RESUME and verify continuation + synthesis. The ONLY
# fake is the leaf model call (REVIEW_FAKE_BACKEND=1 -> the deterministic, network-free
# review_fake at backends.resolve_backend); the whole cli.py->modes->panel path runs for real.
# No network / no real backends, so it is CI-safe. The kill scenario self-skips if its timing
# window can't be hit on a slow box; the deterministic spawn+resume path always runs.
python3 tests/test_e2e_resume.py
echo "e2e resume tests OK"

# Run-stats store + startup ETA: record shape (mode/pool/duration/ok/fail), the
# (mode,pool_size) -> pool-only -> no-history ETA fallbacks, real wall-clock on a
# CLI run, and the no-timeout advertising warning. All offline (backends stubbed;
# store + log dir redirected to temp; no keys, no network).
REVIEW_LOG_DIR="$(mktemp -d)" python3 tests/test_run_stats.py
echo "run-stats tests OK"

# Internal run backstop: the clamped 4h ceiling (env can only LOWER it), the watchdog
# cancelling on a fast block, an ACTUAL fire (exit 124 + loud line) for a wedged run
# in a child process, and main() arming the backstop around dispatch. All offline (no
# backend, no network — the wedged-run children just sleep).
python3 tests/test_backstop.py
echo "backstop tests OK"

# Stage 1 visual-verification suite (cvGate / vision_client / policy / pipeline /
# composability). All offline: cvGate shells to magick, the vision call is mocked,
# fixtures are generated (Pillow) — no API keys, no network. These need two non-core
# deps: ImageMagick (`magick`, system) and Pillow (the `.[test]` extra). On a bare CI
# without them, SKIP loudly rather than fail — they are not a runtime requirement.
if command -v magick >/dev/null 2>&1 && python3 -c "import PIL" >/dev/null 2>&1; then
  python3 tests/test_cv_gate.py
  python3 tests/test_vision_client.py
  python3 tests/test_policy_engine.py
  python3 tests/test_pipeline.py
  python3 tests/test_preclassifier.py
  python3 tests/test_visual_compose.py
  python3 tests/test_visual_registry.py
  python3 tests/test_selection_highlight.py
  python3 tests/test_visual_fanout.py
  echo "visual-verification tests OK"
else
  echo "SKIP visual-verification tests: need ImageMagick (\`magick\`) + Pillow (pip install -e '.[test]')" >&2
fi
