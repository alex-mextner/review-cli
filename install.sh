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

# ── shadow check ──────────────────────────────────────────────────────────────
# The symlink we just created is the ONLY working install (its bin/review shim
# bootstraps sys.path so `import reviewlib` succeeds from any cwd/PYTHONPATH). But a
# stray `review` EARLIER on PATH silently wins over it. The classic offender: a
# `pip/uv install -e .` run from a worktree leaves a hardcoded console-script in a
# bin dir that precedes "$BIN" (e.g. /opt/homebrew/bin), pointing its editable finder
# at that worktree; once the worktree is deleted, `from reviewlib.cli import main`
# raises ModuleNotFoundError everywhere in that interpreter — `review` is dead in
# every shell. Detect the shadow and WARN LOUDLY with copy-paste remediation. We do
# NOT delete the file (it is not ours to remove; the user may have installed it on
# purpose) — we surface exactly what shadows us and how to undo it.
# CAUTION: a shadow is not necessarily BROKEN. A healthy pipx/pip install (pipx is a
# documented install method) is ALSO a regular file with a `#!`-shebang that mentions
# reviewlib, but its `import reviewlib` SUCCEEDS. We only advise uninstalling when the
# import probe actually FAILS; a healthy shadow gets an explain-only warning, never
# "uninstall review-cli" — that would tell the user to remove a valid install.
# Canonicalize a path (follow every symlink). Try realpath, then python3, else as-is.
# realpath can FAIL (missing path / unreadable intermediate dir) even when present, so we
# fall THROUGH to python3 on failure rather than echoing the raw input — otherwise one side
# canonicalizes and the other doesn't, and the != compare below false-positives a "shadow"
# warning on a perfectly good install.
_canon() {
  if command -v realpath >/dev/null 2>&1 && realpath "$1" 2>/dev/null; then
    return 0
  fi
  if command -v python3 >/dev/null 2>&1 && \
     python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null; then
    return 0
  fi
  echo "$1"
}
# Canonicalize BOTH sides so the comparison is symlink-stable: a different bin dir that
# symlinks to OUR entry must NOT be flagged, and a repo reached via a symlinked parent
# (e.g. macOS /tmp -> /private/tmp) must NOT trip a false-positive shadow warning.
WANT_TARGET="$(_canon "$ENTRY_PATH")"
RESOLVED="$(command -v "$TOOL" 2>/dev/null || true)"
if [[ -n "$RESOLVED" ]]; then
  RESOLVED_REAL="$(_canon "$RESOLVED")"
  if [[ "$RESOLVED_REAL" != "$WANT_TARGET" ]]; then
    echo "" >&2
    echo "  ============================================================================" >&2
    echo "  WARNING: '$TOOL' on your PATH is SHADOWED — the working install will NOT run." >&2
    echo "  ============================================================================" >&2
    echo "  We installed the working shim at:" >&2
    echo "      $BIN/$TOOL -> $WANT_TARGET" >&2
    echo "  but \`command -v $TOOL\` resolves FIRST to a different entry:" >&2
    echo "      $RESOLVED" >&2
    if [[ "$RESOLVED" != "$RESOLVED_REAL" ]]; then
      echo "      (-> $RESOLVED_REAL)" >&2
    fi
    echo "" >&2
    # Diagnose the common case: a stale pip/uv editable console-script. It's a regular
    # FILE (not a symlink into a repo) that does a bare `from reviewlib.cli import main`
    # with no sys.path bootstrap, and/or whose interpreter can't import reviewlib.
    _first_line="$(head -n1 "$RESOLVED" 2>/dev/null || true)"
    # A regular (non-symlink) FILE that both starts with a `#!` shebang AND mentions
    # `reviewlib` is a CANDIDATE for the stale-console-script diagnosis — a plain text file
    # that merely mentions `reviewlib` (a wrapper, a doc) is not, and gets the generic
    # remediation. But "candidate" is NOT "stale": a healthy pipx/pip-installed `review`
    # is ALSO a regular file with a `#!` shebang that mentions reviewlib, and pipx is a
    # documented install method. So we only emit the stale-specific "uninstall review-cli"
    # remediation when the `import reviewlib` probe below actually FAILS (proving the
    # install is broken). When the probe SUCCEEDS — a valid shadowing install — we fall
    # through to the generic shadow warning: explain it shadows the new symlink, DON'T tell
    # the user to uninstall a working install.
    _is_stale_console_script=""
    _healthy_reviewlib_shadow=""
    # Reset interp_bin alongside the flags (not inside the candidate block): the stale-branch
    # echo interpolates "$interp_bin", and resetting it here keeps that safe even if a future
    # refactor adds a second writer or wraps this in a loop. Today it is set ONLY below, and
    # the stale branch is reachable ONLY when it is non-empty — this reset preserves that.
    interp_bin=""
    if [[ -f "$RESOLVED" && ! -L "$RESOLVED" && "${_first_line:0:2}" == "#!" ]] && \
       grep -Iq 'reviewlib' "$RESOLVED" 2>/dev/null; then
      # Extract the REAL interpreter from the shebang. pip emits either an absolute path
      # (`#!/opt/.../python3.14`) OR `#!/usr/bin/env python3` — for the latter the first
      # token is `/usr/bin/env`, so a naive `${shebang%% *}` would run `env -c …` (invalid)
      # and print a `env -m pip …` remediation the user can't run. Walk the tokens: skip a
      # leading `.../env` and any `-flag` it carries (`env -S python3 -O`, `env -C dir`), and
      # take the first bare token as the interpreter. Leave `interp` empty if none qualifies
      # (then the generic remediation prints, not a misleading interpreter-specific one).
      shebang="${_first_line#\#!}"
      # Pre-declare the array: a bare `#!` (empty $shebang) leaves `read` populating nothing,
      # and `"${_sh_toks[@]}"` on an UNSET array aborts under `set -u` on older bash (3.2,
      # macOS default) with "unbound variable". `|| true` likewise stops `read`'s non-zero on
      # empty input from tripping `set -e` mid-warning.
      _sh_toks=()
      read -r -a _sh_toks <<<"$shebang" || true
      interp=""
      # `${_sh_toks[@]:-}`: on bash 3.2 (macOS default) an empty array counts as UNSET, so a
      # bare `"${_sh_toks[@]}"` still trips `set -u`; the `:-` default makes the empty case
      # expand to one empty word (harmless — it hits the `*` arm, sets interp="" then we
      # break) instead of aborting the install.
      for _t in "${_sh_toks[@]:-}"; do
        # Skip the `env` launcher and any flags it carries; the interpreter is the first
        # bare (non-`env`, non-`-flag`) token. NOTE: this does not consume flag ARGUMENTS
        # (`env -C dir python3` would pick `dir`); we don't trust the token blindly — the
        # python-ness check below rejects a non-Python binary like coreutils `dir`.
        case "$_t" in
          */env|env) continue ;;
          -*)        continue ;;
          *)         interp="$_t"; break ;;
        esac
      done
      # Resolve the interpreter to a runnable FILE: an absolute path to an executable file
      # as-is, else look it up on PATH (handles a bare `python3` from an env-shebang).
      interp_bin=""
      if [[ -n "$interp" ]]; then
        if [[ -f "$interp" && -x "$interp" ]]; then
          interp_bin="$interp"
        else
          _cv="$(command -v "$interp" 2>/dev/null || true)"
          [[ -n "$_cv" && -f "$_cv" && -x "$_cv" ]] && interp_bin="$_cv"
        fi
      fi
      # Only TRUST it as an interpreter if it actually IS Python. The token-walk can land on
      # a non-Python binary — e.g. `env -C dir python3` mis-yields `dir`, which on Debian is
      # the real coreutils `/usr/bin/dir`. Running `<that> -c 'import reviewlib'` would
      # mis-report and the `<that> -m pip …` remediation would be garbage. `<py> -I -c
      # 'import sys'` is the cheapest "is this a Python that accepts -I?" probe — it gates out
      # non-Python binaries AND anything older than the isolated-mode flag (3.2 / 2.7.3), so
      # the deciding reviewlib probe below can safely run isolated. It also avoids executing an
      # arbitrary shebang-named binary for the heavier reviewlib check. (Keep the -I: it is
      # what makes the reviewlib probe cwd/PYTHONPATH-independent — see below.)
      if [[ -n "$interp_bin" ]] && ! "$interp_bin" -I -c 'import sys' >/dev/null 2>&1; then
        interp_bin=""
      fi
      # The DECIDING probe: does this shadowing entry's interpreter import reviewlib? Only a
      # FAILED probe proves a genuinely-broken stale console-script worth uninstalling. If
      # the import SUCCEEDS, it is a VALID install (e.g. pipx) that merely shadows us — never
      # advise uninstall. If we couldn't resolve a Python interpreter at all (interp_bin
      # empty), we CANNOT prove breakage, so we conservatively decline the uninstall advice
      # too and fall back to the generic shadow warning — telling a user to remove a possibly
      # -valid install on a guess is exactly the harm we are avoiding.
      #
      # `-I` (ISOLATED mode) is LOAD-BEARING here. Without it the probe inherits install.sh's
      # CWD and PYTHONPATH, and CPython prepends the CWD to sys.path[0]. The documented fresh
      # install runs `./install.sh` FROM the review-cli source checkout, so `./reviewlib/`
      # would be importable from CWD — and a genuinely-broken stale console-script would then
      # probe SUCCESSFULLY and be mislabeled "healthy, just shadows" instead of getting the
      # uninstall remediation. `-I` ignores PYTHONPATH and drops the CWD/script dir from
      # sys.path, so the probe answers the ONLY question that matters: can THIS interpreter
      # import reviewlib from its OWN site-packages (i.e. is it a real install)?
      if [[ -n "$interp_bin" ]]; then
        if "$interp_bin" -I -c 'import reviewlib' >/dev/null 2>&1; then
          _healthy_reviewlib_shadow="yes"
        else
          _is_stale_console_script="yes"
        fi
      fi
    fi
    if [[ -n "$_is_stale_console_script" ]]; then
      echo "  That entry looks like a stale pip/uv console-script (a regular file with a" >&2
      echo "  hardcoded interpreter and a bare \`from reviewlib.cli import main\`, no path" >&2
      echo "  bootstrap). This is consistent with an editable install whose target was" >&2
      echo "  deleted, which raises ModuleNotFoundError: No module named 'reviewlib' in EVERY" >&2
      echo "  shell. (Other broken-install causes fail the same probe; the remediation below" >&2
      echo "  clears all of them.)" >&2
      echo "" >&2
      echo "  Confirmed: its interpreter ($interp_bin) cannot \`import reviewlib\`." >&2
      echo "  REMEDIATION (copy-paste) — uninstall the stale console-script:" >&2
      echo "      $interp_bin -m pip uninstall -y review-cli" >&2
      echo "  or remove just the shadowing script:" >&2
      # Quote for copy-paste safety even if the path contains a single quote: replace each
      # `'` with the classic `'\''` idiom (close-quote, escaped-quote, reopen). Two bash
      # subtleties, both verified: (1) the `${var//.../...}` must go through an intermediate
      # variable — written inline as a printf arg it is re-parsed and over-escaped; (2) the
      # assignment RHS must be UNQUOTED — wrapping it in double-quotes also over-escapes the
      # backslashes. An assignment isn't word-split, so unquoted is safe here.
      _rm_arg=${RESOLVED//\'/\'\\\'\'}
      printf "      rm '%s'\n" "$_rm_arg" >&2
    else
      # Not a (provably) broken stale console-script, so we only EXPLAIN the shadow and how
      # to make our symlink win — we never advise uninstalling it. Two sub-cases:
      #   - a VALID install (e.g. pipx) whose interpreter imports reviewlib fine; or
      #   - anything else (symlink, no shebang, doesn't mention reviewlib, or an interpreter
      #     we couldn't resolve to prove breakage).
      if [[ -n "$_healthy_reviewlib_shadow" ]]; then
        echo "  This earlier '$TOOL' imports reviewlib fine (e.g. a pipx/pip install) — it is" >&2
        echo "  NOT broken, it just shadows the symlink we installed at $BIN/$TOOL." >&2
        echo "  REMEDIATION: ensure $BIN precedes the dir holding '$RESOLVED' on PATH so the" >&2
        echo "  symlink wins. (Both installs work — keep whichever you prefer; only remove the" >&2
        echo "  earlier one if you actually meant to switch off that install.)" >&2
      else
        echo "  REMEDIATION: ensure $BIN precedes the dir holding '$RESOLVED' on PATH," >&2
        echo "  or remove/rename that earlier '$TOOL' so the working symlink wins." >&2
      fi
    fi
    echo "  ============================================================================" >&2
    echo "" >&2
  fi
fi

# ── register skill ────────────────────────────────────────────────────────────
# NOTE: invoke OUR symlink by ABSOLUTE path ("$BIN/$TOOL"), never the bare name —
# a PATH shadow (see the check above) would otherwise run the broken console-script.
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
