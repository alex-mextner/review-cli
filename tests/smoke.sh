#!/usr/bin/env bash
set -euo pipefail

bin/review --list-defaults | grep -q codex
bin/review --help >/dev/null
