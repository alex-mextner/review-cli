#!/usr/bin/env bash
set -euo pipefail

# Guarded smoke test: no API keys or backends required. Only --help, --list-defaults,
# and a syntax check, so CI stays green without secrets.

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

# The single-file CLI must always parse.
python3 -c "import ast; ast.parse(open('bin/review').read()); print('ast.parse OK')"

# Streaming runner: real-time log growth + partial-output-on-timeout (no backends
# needed — drives a fake slow python child).
REVIEW_LOG_DIR="$(mktemp -d)" python3 tests/test_streaming.py
echo "streaming tests OK"

# Workspace-trust auto-seed for the headless claude/opus backend (the test
# isolates HOME to a temp dir internally — never touches the real ~/.claude.json).
python3 tests/test_workspace_trust.py
echo "workspace-trust tests OK"

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
