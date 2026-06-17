#!/usr/bin/env bash
# install.sh — install the `review` CLI (Python 3)
# Works both from a local clone (./install.sh) and piped from curl:
#   curl -fsSL https://raw.githubusercontent.com/alex-mextner/review-cli/main/install.sh | bash
set -euo pipefail

# ── identity ──────────────────────────────────────────────────────────────────
TOOL="review"
REPO="review-cli"
GITHUB_USER="alex-mextner"
ENTRY="bin/review"   # path inside repo root
CLONE_BASE="${XDG_DATA_HOME:-$HOME/.local/share}"

# ── locate source dir ─────────────────────────────────────────────────────────
_script_dir=""
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
  _script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$_script_dir" && -f "$_script_dir/$ENTRY" ]]; then
  SRC="$_script_dir"
  echo "review: using local clone at $SRC"
else
  mkdir -p "$CLONE_BASE"
  CLONE_DIR="$CLONE_BASE/$REPO"
  EXPECT_URL="https://github.com/$GITHUB_USER/$REPO.git"
  if [[ -d "$CLONE_DIR/.git" ]]; then
    actual_url="$(git -C "$CLONE_DIR" remote get-url origin 2>/dev/null || echo "")"
    if [[ "$actual_url" != "$EXPECT_URL" ]]; then
      echo "ERROR: $CLONE_DIR exists but its origin is '$actual_url', not $EXPECT_URL." >&2
      echo "       Remove that directory or fix its remote, then re-run." >&2
      exit 1
    fi
    echo "review: updating existing clone at $CLONE_DIR"
    git -C "$CLONE_DIR" pull --ff-only
  else
    echo "review: cloning $EXPECT_URL into $CLONE_DIR"
    git clone "$EXPECT_URL" "$CLONE_DIR"
  fi
  SRC="$CLONE_DIR"
fi

# ── bin dir ───────────────────────────────────────────────────────────────────
BIN="$HOME/.local/bin"
mkdir -p "$BIN"

if [[ ":$PATH:" != *":$BIN:"* ]]; then
  echo ""
  echo "  NOTE: $BIN is not on your PATH."
  echo "  Add the following line to your ~/.bashrc or ~/.zshrc and restart your shell:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
fi

# ── dependency: pyyaml (optional) ─────────────────────────────────────────────
if ! python3 -c 'import yaml' 2>/dev/null; then
  echo "review: pyyaml not found, attempting: pip install --user pyyaml"
  if ! pip install --user pyyaml 2>/dev/null; then
    echo ""
    echo "  WARNING: could not install pyyaml. Some YAML-based features may not work."
    echo "  To install manually: pip install --user pyyaml"
    echo "  Or via pipx: pipx install review-cli  (handles all deps automatically)"
    echo ""
  fi
fi

# ── symlink entry ─────────────────────────────────────────────────────────────
ENTRY_PATH="$SRC/$ENTRY"
chmod +x "$ENTRY_PATH"
ln -sfn "$ENTRY_PATH" "$BIN/$TOOL"
echo "review: symlinked $BIN/$TOOL -> $ENTRY_PATH"

# ── register skill ────────────────────────────────────────────────────────────
if ! "$BIN/$TOOL" install-skill; then
  echo "  WARNING: '$TOOL install-skill' failed — $TOOL is installed but agents may not"
  echo "           auto-discover it. Re-run '$TOOL install-skill' manually to fix."
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  review is installed."
echo "  Usage: review diff              — review current git diff (all backends)"
echo "         review diff -m codex -m gemini — select specific backends"
echo "         review install-skill      — re-register skill with agent harnesses"
echo "         review install-commit-hook — install pre-commit hook"
echo "         review --help             — full usage (bare 'review' prints this)"
echo ""
