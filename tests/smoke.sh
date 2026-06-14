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

# New panel modes must appear in help.
bin/review --help | grep -q -- "--just-ask"
bin/review --help | grep -q -- "--quorum"
bin/review --help | grep -q -- "--brainstorm"

# Stage 1: the composable --visual flag and its core sub-flags must appear in help.
bin/review --help | grep -q -- "--visual"
bin/review --help | grep -q -- "--no-ai"
bin/review --help | grep -q -- "--strict"
# Stage 2a: the local pre-classifier toggle must appear in help.
bin/review --help | grep -q -- "--no-local-model"

# Reviewer board (HYP-741): the board flags must appear in help, and --show-board
# must list the out-of-the-box 8-seat DEFAULT_BOARD (no config file needed) with
# roles and byte-exact model ids — incl. the z.ai-direct tests seat (zai:glm-5.2)
# and the gpt-5.5 contracts seat. Availability depends on the env, but the LISTING
# is always complete.
bin/review --help | grep -q -- "--show-board"
bin/review --help | grep -q -- "--no-board"
bin/review --show-board | grep -q "architect"
bin/review --show-board | grep -q "commandcode:deepseek/deepseek-v4-pro"
bin/review --show-board | grep -q "zai:glm-5.2"
bin/review --show-board | grep -q "commandcode:gpt-5.5"
bin/review --show-board | grep -q "contracts"
bin/review --show-board | grep -q "8 seats"

# spec-web subcommand: dispatches + advertises its flags (no server started here).
bin/review spec-web --help | grep -q -- "--seed"
bin/review spec-web --help | grep -q -- "--host"
bin/review spec-web --help | grep -q -- "--export"
# The single-file CLI must always parse.
python3 -c "import ast; ast.parse(open('bin/review').read()); print('ast.parse OK')"

# HYP-742: the local web dashboard subcommand must be wired and its help must parse.
bin/review dashboard --help | grep -q -- "--no-open"
bin/review dashboard --help | grep -q -- "--port"

# Dashboard parser / store / JSON endpoints (no backends, no network beyond a local
# 127.0.0.1 socket the test binds itself; log dir + store are redirected to temp dirs).
python3 tests/test_dashboard.py
echo "dashboard tests OK"

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
