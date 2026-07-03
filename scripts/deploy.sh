#!/usr/bin/env bash
# deploy.sh — update an installed `review` checkout to the latest committed code.
#
# WHY THIS EXISTS
#   The documented install (install.sh) symlinks `~/.local/bin/review` ->
#   <checkout>/bin/review, a thin shim that imports `reviewlib` from the checkout
#   it lives in (single-file-live-symlink-cli: the checked-out TREE *is* the
#   running tool, pure Python, no build step). "Deploying" a merged change is
#   therefore just a fast-forward `git pull` in the checkout the symlink points
#   at. This script makes that one-step deploy safe and idempotent, and it is the
#   hook `rig apply` (rig-cli 0.8.0+) runs on every apply to keep the live
#   checkout fresh instead of silently drifting stale (review-cli#105).
#
#   `review` itself needs NO restart — the next invocation imports the new code.
#   The one resident piece is the managed `review dashboard` / spec-web daemon:
#   a running daemon loaded its reviewlib code at start and keeps the OLD code
#   until restarted, so when a deploy touches reviewlib/ this script prints a
#   restart note (warn-only — it never blind-restarts a daemon it didn't start).
#
# USAGE
#   scripts/deploy.sh [--checkout DIR] [--dry-run]
#
#   --checkout DIR   The git checkout to update. Default: the checkout this
#                    script lives in (what rig's no-arg freshness run needs);
#                    if the script is outside any checkout, fall back to
#                    resolving the `review` on PATH through its symlink chain.
#   --dry-run        Show what would land (fetch + report divergence) without
#                    pulling. (The environment refusals below still apply.)
#
# EXIT CODES
#   0  up to date (including ahead-only: local commits the upstream lacks but
#      nothing new to pull), or deployed successfully
#   1  usage / environment error (no checkout, not a git repo, dirty tree,
#      detached HEAD, no upstream, fetch failure)
#   2  cannot fast-forward (checkout diverged from its upstream) — needs a human
set -euo pipefail

usage() {
  cat <<'EOF'
deploy.sh — update an installed `review` checkout to the latest committed code.

`review` is installed as a symlink to <checkout>/bin/review (a shim importing
reviewlib from the checkout), so "deploying" a merged change is a guarded
fast-forward `git pull` in that checkout. No build step, no reinstall.

Usage:
  scripts/deploy.sh [--checkout DIR] [--dry-run]

  --checkout DIR   The git checkout to update. Default: the checkout this script
                   lives in; outside any checkout, fall back to resolving the
                   `review` on PATH through its symlink chain.
  --dry-run        Show what would land (fetch + report) without pulling.
                   (Environment refusals — dirty/detached/non-repo — still apply.)

Exit codes: 0 up-to-date/deployed · 1 usage/env error · 2 non-fast-forward.
EOF
}

CHECKOUT=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --checkout)
      if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
        echo "deploy: --checkout requires a directory argument." >&2; exit 1
      fi
      CHECKOUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "deploy: unknown argument '$1' (try --help)" >&2; exit 1 ;;
  esac
done

# Scrub repo-pinning GIT_* vars from every git invocation: when this script runs
# from inside a git hook (rig apply triggered by a hook, a hook-spawned shell),
# the environment carries GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE which OVERRIDE
# `git -C` and would silently pin every command to a FOREIGN repo (the exact bug
# class fixed in reviewlib/process.py, review-cli#72). `env -u` of an absent var
# is a no-op, so this is safe everywhere.
git_clean() {
  env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY \
      -u GIT_COMMON_DIR git "$@"
}

# Resolve the real file behind a symlink, following the chain hop-by-hop (no
# `readlink -f` — it is absent on stock macOS). Echoes the final target. A depth
# cap breaks a symlink cycle (a -> b -> a) instead of looping forever.
resolve_link() {
  target="$1"
  hops=0
  while [ -L "$target" ]; do
    hops=$((hops + 1))
    if [ "$hops" -gt 40 ]; then
      echo "deploy: symlink chain for '$1' is too deep (cycle?) — aborting." >&2
      exit 1
    fi
    link="$(readlink "$target")"
    case "$link" in
      /*) target="$link" ;;                      # absolute
      *)  target="$(dirname "$target")/$link" ;; # relative to its own dir
    esac
  done
  echo "$target"
}

# ── resolve which checkout to deploy ───────────────────────────────────────────
# Default order (no --checkout):
#   1. The checkout THIS script lives in. rig's freshness pass runs
#      `bash <repo>/scripts/deploy.sh` with NO args (riglib/actions/runner.py
#      `_run_tool_deploy`), and rig considers the tool installed even when its
#      bin dir is not on PATH — so PATH-first resolution would fail (no `review`
#      on PATH) or worse, deploy a FOREIGN checkout (a pipx/older `review`
#      winning PATH) forever (codex P1). The script's own repo is deterministic
#      and is exactly the checkout rig is trying to keep fresh.
#   2. Fall back to resolving `review` on PATH through its symlink chain — for a
#      copy of this script run from outside any checkout.
if [ -z "$CHECKOUT" ]; then
  script_target="$(resolve_link "$0")" || exit 1
  script_dir="$(cd "$(dirname "$script_target")" && pwd)" || exit 1
  if CHECKOUT="$(git_clean -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "deploy: targeting this script's own checkout (default)."
  else
    review_bin="$(command -v review || true)"
    if [ -z "$review_bin" ]; then
      echo "deploy: this script is outside a git checkout, no 'review' on PATH," >&2
      echo "        and no --checkout given. Pass --checkout DIR." >&2
      exit 1
    fi
    # The shim lives at <checkout>/bin/review; ask git for the toplevel rather
    # than hardcoding the ../.. layout. A pipx/pip console-script (a regular file
    # in a site bin dir, not a symlink into a clone) resolves to a non-repo dir
    # and fails the is-a-git-checkout test below with the pip hint.
    # Resolve in its own statement: resolve_link's `exit 1` (cycle) only exits
    # the $(...) subshell, so catch the failure explicitly instead of relying on
    # how set -e treats a nested substitution.
    resolved_target="$(resolve_link "$review_bin")" || exit 1
    resolved_dir="$(cd "$(dirname "$resolved_target")" && pwd)" || exit 1
    if CHECKOUT="$(git_clean -C "$resolved_dir" rev-parse --show-toplevel 2>/dev/null)"; then
      :
    else
      CHECKOUT="$resolved_dir"
    fi
  fi
fi

# Accept any git work tree, including one whose `.git` is a FILE (worktrees,
# submodules) — a `-d .git` check would wrongly reject those.
if ! git_clean -C "$CHECKOUT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "deploy: '$CHECKOUT' is not a git checkout." >&2
  echo "        review may be installed via pipx/pip rather than a clone;" >&2
  echo "        upgrade that kind of install with its own tool (e.g. 'pipx upgrade review-cli')," >&2
  echo "        or re-run install.sh for the symlink-to-clone install." >&2
  exit 1
fi

git_c() { git_clean -C "$CHECKOUT" "$@"; }

echo "deploy: checkout = $CHECKOUT"

# ── refuse to clobber a dirty tree ─────────────────────────────────────────────
# Only TRACKED changes block a fast-forward; untracked files (a stray
# review_cli.egg-info/, editor temp files) do not, so exclude them. Capture into
# a var first so a `git status` FAILURE (locked index, corrupt repo) aborts
# under `set -e` instead of being read as "clean" inside `$(...)`.
dirty="$(git_c status --porcelain --untracked-files=no)"
if [ -n "$dirty" ]; then
  echo "deploy: checkout has local (tracked) changes — refusing to pull over them." >&2
  echo "        Commit, stash, or discard them, then re-run." >&2
  echo "$dirty" >&2
  exit 1
fi

branch="$(git_c rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "HEAD" ]; then
  echo "deploy: checkout is in detached-HEAD state — no branch to pull." >&2
  echo "        Check out a branch (e.g. 'git -C $CHECKOUT switch main') first." >&2
  exit 1
fi
echo "deploy: branch  = $branch"

# ── fetch and measure divergence ───────────────────────────────────────────────
# Honor the branch's CONFIGURED upstream (`@{upstream}`) — a checkout whose branch
# tracks a differently-named remote (a fork tracking `upstream/main`) or a
# differently-named branch must deploy against what it actually tracks, not a
# hardcoded `origin/$branch` (codex P1). Fall back to `origin/$branch` only when
# no upstream is configured (a plain `git clone` always configures one).
if upstream="$(git_c rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)"; then
  remote="${upstream%%/*}"
else
  remote="origin"
  upstream="origin/${branch}"
  echo "deploy: branch '$branch' has no configured upstream — assuming '$upstream'."
fi

# A failed fetch (no such remote, network/auth down) must be the documented
# friendly exit 1, not a raw set -e abort mid-script.
if ! git_c fetch "$remote" --quiet; then
  echo "deploy: 'git fetch $remote' failed — check the remote/network and re-run." >&2
  exit 1
fi
if ! git_c rev-parse --verify --quiet "$upstream" >/dev/null; then
  echo "deploy: no upstream ref '$upstream' — is this branch pushed?" >&2
  exit 1
fi

local_sha="$(git_c rev-parse HEAD)"
remote_sha="$(git_c rev-parse "$upstream")"

if [ "$local_sha" = "$remote_sha" ]; then
  echo "deploy: already up to date ($(git_c rev-parse --short HEAD)) — nothing to do."
  exit 0
fi

# Ahead-only (the upstream has nothing the checkout lacks — only unpushed local
# commits) is NOT divergence: there is nothing to deploy, and hard-failing here
# would turn every unattended rig apply red until the commits are pushed. Report
# and succeed.
if git_c merge-base --is-ancestor "$upstream" HEAD; then
  echo "deploy: checkout is AHEAD of '$upstream' (unpushed local commits) — nothing to deploy."
  git_c log --oneline "$upstream..HEAD" | sed 's/^/  /'
  exit 0
fi

# Fast-forward only: refuse if the checkout and its upstream have truly diverged.
if ! git_c merge-base --is-ancestor HEAD "$upstream"; then
  echo "deploy: cannot fast-forward — '$branch' has diverged from '$upstream'." >&2
  echo "        A human must reconcile (rebase/merge). Aborting." >&2
  exit 2
fi

echo "deploy: $(git_c rev-parse --short HEAD) -> $(git_c rev-parse --short "$upstream"), commits to land:"
git_c log --oneline "HEAD..$upstream" | sed 's/^/  /'

# Does the deploy touch code a resident daemon may hold in memory? The managed
# `review dashboard` / spec-web daemon imports reviewlib at start; any change
# under reviewlib/ may leave a running daemon on stale code. This only drives a
# WARNING (never a restart), so erring toward over-warning is the safe bias.
# Capture the name list first and grep the VARIABLE (no live pipe): under
# `set -o pipefail`, `git diff | grep -q` can return 141 when grep exits on the
# first match and git catches SIGPIPE — silently flipping a real match into
# "no match" and eating the warning (Opus/codex finding).
daemon_changed=0
changed_files="$(git_c diff --name-only "HEAD..$upstream")"
if grep -qE '^reviewlib/' <<<"$changed_files"; then
  daemon_changed=1
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "deploy: --dry-run — not pulling."
  [ "$daemon_changed" = "1" ] && echo "deploy: (dry-run) a running 'review dashboard' daemon would need a restart (reviewlib code changed)."
  exit 0
fi

# ── fast-forward to the already-validated upstream ─────────────────────────────
# Use `merge --ff-only "$upstream"` (not `pull`, which would re-fetch): we already
# fetched and validated `$upstream` is a strict descendant of HEAD, so this updates
# against the SAME object state the daemon_changed decision was computed from — no
# second fetch, no race window where a new push slips in unwarned.
# The one way this still fails after the clean/ancestor checks: an UNTRACKED
# local file colliding with a tracked file the upstream adds (untracked files
# deliberately don't block above, but git refuses to overwrite one). Surface
# that as the documented friendly exit 1 instead of a raw set -e abort.
if ! git_c merge --ff-only --quiet "$upstream"; then
  echo "deploy: fast-forward failed — most likely an untracked local file collides" >&2
  echo "        with a file this deploy adds (git refuses to overwrite it)." >&2
  echo "        Move/remove the file named in the git error above, then re-run." >&2
  exit 1
fi
new_sha="$(git_c rev-parse --short HEAD)"
echo "deploy: pulled — now at $new_sha"

# Bound every post-deploy invocation of the freshly-pulled tool with `timeout`
# when available, so a wedged binary can't hang the deploy (nor a rig apply).
timeout_bin="$(command -v timeout || command -v gtimeout || true)"
[ -z "$timeout_bin" ] && echo "deploy: NOTE — no timeout(1)/gtimeout; post-deploy review calls run unbounded." >&2
bounded() {
  if [ -n "$timeout_bin" ]; then "$timeout_bin" 30 "$@"; else "$@"; fi
}

# Re-register the agent skill (idempotent; keeps the installed skill file in
# lockstep with the deployed code). Run the deployed checkout's OWN shim, not
# whatever is first on PATH — when --checkout names a different tree, the PATH
# binary is unrelated. Non-fatal: the deploy itself already succeeded.
review_checkout="$CHECKOUT/bin/review"
if [ -x "$review_checkout" ]; then
  bounded "$review_checkout" install-skill >/dev/null 2>&1 \
    && echo "deploy: refreshed review skill (install-skill)" \
    || echo "deploy: WARNING — 'review install-skill' failed/timed out; re-run it manually." >&2
fi

if [ "$daemon_changed" = "1" ]; then
  echo "deploy: NOTE — this deploy changed reviewlib/ code. A running 'review dashboard'"
  echo "deploy:        or spec-web daemon still holds the OLD code; restart to apply:"
  echo "deploy:            review dashboard stop && review dashboard start"
  echo "deploy:            review spec-web stop && review spec-web start --agent <name>"
fi

echo "deploy: done — deployed $new_sha."
