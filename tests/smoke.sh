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

# The single-file CLI must always parse.
python3 -c "import ast; ast.parse(open('bin/review').read()); print('ast.parse OK')"

# Streaming runner: real-time log growth + partial-output-on-timeout (no backends
# needed — drives a fake slow python child).
REVIEW_LOG_DIR="$(mktemp -d)" python3 tests/test_streaming.py
echo "streaming tests OK"
