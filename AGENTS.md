# AGENTS.md — review-cli

Agent-facing notes for working IN this repo. (User-facing docs live in `README.md`.)

## What this is

multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic — with one explicit, narrow opt-in exception: `review diff --staged --commit` creates a checkpoint commit of the staged diff it just reviewed (see "Fix loops" below).

Operationally, `review` fans a git diff (or a question / topic) out to several model
backends in parallel and prints their findings.

## Invocation — modes are SUBCOMMANDS

The mode is selected by a **subcommand**, not a flag:

```
review                       # bare -> prints HELP (does NOT run a diff review)
review diff --task CODE                  # the diff review (was the stuttering `review review`)
review diff --task CODE --staged         # review the staged diff (pre-commit)
review brainstorm "TOPIC" --task CODE    # multi-round persona ideation
review brainstorm "TOPIC" --task CODE --diff    # …grounded in the working-tree (or --staged) diff
review just-ask "QUESTION" --task CODE   # single-shot multi-model answer (alias: review ask "Q")
review quorum "QUESTION" --task CODE     # experts cite evidence + a moderator finds quorum
review visual shot.png            # standalone screenshot verdict
review visual shot.png --task CODE --diff   # screenshot plus diff-review context
review brainstorm "TOPIC" --task CODE --visual shot.png   # composable screenshot context for text modes
review task CODE              # task-scoped iterations, models, and transcripts
```

A bare `review` (no subcommand) prints the HELP/usage — it does **NOT** run a diff review
(the old "bare review == a diff review" default was a mistake; never reintroduce it). The
diff review is the `diff` subcommand: `review diff` (renamed from the stuttering
`review review`). The removed verb `review review` and `review -C <repo>` (flags with no
verb) print a one-line "use `review diff`" pointer and exit non-zero. The meta flags
(`--list-defaults` / `--show-board` / `--help`) still work with no subcommand. The OLD
mode flags (`--brainstorm` / `--quorum` / `--just-ask`) were likewise REMOVED — they print
a "use the subcommand" pointer and exit non-zero. Every recorded review mode requires
`--task CODE` or `$REVIEW_TASK_CODE`; standalone `review visual IMAGE` is the exception,
while `review visual IMAGE --diff` is a diff-review iteration and must carry a task code.
Task codes are one non-whitespace token, max 120 characters, with no control characters.

Bare subcommands also handled directly by the CLI: `dashboard`, `sessions`, `spec-web`,
`task`, `stat`, `install-skill`, `install-commit-hook`, `install-hook tg`, `register-module`,
`trust-module`.
`sessions` is a
MANAGEMENT command (list / resume brainstorm sessions parsed from the discussion logs), NOT
a fan-out mode — it is wired in `cli._dispatch` like `dashboard` and its logic lives in the
lib (`reviewlib/sessions.py`); it deliberately does NOT register a `ModeSpec`, so it never
collides with the mode registry. `task` is also a MANAGEMENT command: it reads run-stats and
dashboard logs to list task iterations, models used, and detailed transcripts. `stat` is the
same class: a per-harness/per-model usage + health report parsed from the real call logs
(`reviewlib/dashboard/tokenstats.py`), added for the 2026-08 token-burn investigation.

### Fix loops — never `git reset --hard` mid-review-cycle

There is no fix-loop/agent-orchestration code inside review-cli itself: `review diff` is
SINGLE-SHOT (it runs the review once and prints findings), and the review → fix findings →
re-review cycle is something the CALLING agent does manually, outside this tool. That means
the discipline below is on the CALLER, not something this CLI can enforce by itself — but
this tool gives the caller a safe primitive for it.

**Never use `git reset --hard` to discard a bad fix attempt mid-loop** — it can destroy
unrelated uncommitted work belonging to a DIFFERENT session/agent sharing the same checkout
(this has happened in production: a fix-loop agent reset hard mid-cycle and wiped another
session's uncommitted changes). Safe alternatives: `git checkout -- <file>` to discard
specific files, or `review diff --staged --commit` to checkpoint each round with a real
commit — undo a bad checkpoint with `git reset --soft HEAD~1`, which does NOT touch
untracked/foreign files (unlike `reset --hard`, which wipes everything in the working tree
regardless of who it belongs to).

`--commit` (requires `--staged`) is the recommended default for any multi-round fix loop:
it checkpoints the *reviewed* staged diff, not a *clean* one — a review with open findings
still gets committed (the checkpoint gates on the pool producing usable verdicts, the same
`ok` that gates the existing `--staged` commit-hook stamp, NOT on "zero findings"). The
commit runs the repo's own commit-msg/pre-commit hooks; a hook rejection fails `--commit`
loudly with its own exit code rather than silently skipping the checkpoint. See
`reviewlib/modes/review.py` (`EXIT_COMMIT_REQUIRES_STAGED` / `EXIT_COMMIT_FAILED` /
`EXIT_COMMIT_DIFF_TRUNCATED`, `_checkpoint_if_requested`) and the README's "Diff review"
section for the full contract. A staged diff big enough to hit the dispatch cap
(`$REVIEW_DIFF_MAX_BYTES`) also refuses `--commit` — a checkpoint must certify the FULL
reviewed diff, and a truncated dispatch never did; the plain `--staged` stamp is skipped
the same way (see `reviewlib.backends.cap_diff_for_dispatch`'s docstring).

### Option scoping — global vs subcommand

Flags are SCOPED so `review --help` (the top-level overview) lists only TRULY-GLOBAL
options (`-m/--model`, `-C`, `--task`, `-o`, `--timeout`, `--list-defaults`, `--show-board`, `--pool`)
+ the subcommand list. Subcommand- and feature-specific flags live on `review <mode> --help`:
the `review visual IMAGE` options, the composable `--visual` group for text modes,
`--prompt` (the diff review),
`--moderator` (quorum/brainstorm), `--rounds`/`--max-rounds` (brainstorm). In `cli.py`:
`_add_global_options` (top-level + every mode) vs `_add_mode_options` (= global + diff-source
+ the mode-relevant flags + the `_add_visual_options` group + the mode's own positional).
When you add a flag, put it where it belongs — do NOT pile it onto the global list.

### Deep help topics

`review help <topic>` (alias `review --help <topic>`) serves DEEP help topics; the main
`review --help` lists them. Topics live in `HELP_TOPICS` in `cli.py` (topic -> (summary,
renderer)); add a topic = add an entry, and the main-help listing + dispatch pick it up.
Keep a topic in sync with behavior (help-docs-sync): a flag/behavior change updates its
topic renderer in the same commit. `review help config` is the config reference (file +
cascade + keys/auth + board).

## Architecture: `lib | cli | mcp`

- **lib** — `reviewlib/` is the engine (`panel.py`, `backends.py`, `config.py`,
  `stats.py`, `features/visual/`). No argparse dependency; callable directly.
- **cli** — `reviewlib/cli.py` is a **thin** argparse front-end: it resolves the diff,
  models, and `--visual` context, then dispatches to a mode handler. It owns no review
  logic of its own.
- **mcp** — not built yet. The seam is kept clean so an MCP wrapper (or another CLI's
  `just-ask`) can call the lib + a mode handler directly without the argparse surface.
  Keep mode handlers thin over the lib.

## Modes are plugin-directory modules

Modes mirror the per-project `features/visual` MODULE registry, generalized to the core
review modes:

```
reviewlib/modes/
  contract.py     # ModeSpec descriptor + ModeContext
  registry.py     # MODES list + get_mode / known_subcommands / diff_mode / iter_modes
  review.py       # MODE = ModeSpec(subcommand="diff",       diff_policy="require",  handler=…)
  visual.py       # MODE = ModeSpec(subcommand="visual",     diff_policy="optional", handler=…)
  brainstorm.py   # MODE = ModeSpec(subcommand="brainstorm", diff_policy="optional", handler=…)
  just_ask.py     # MODE = ModeSpec(subcommand="just-ask",   diff_policy="none",     handler=…)
  quorum.py       # MODE = ModeSpec(subcommand="quorum",     diff_policy="none",     handler=…)
```

Each mode module exposes a top-level `MODE = ModeSpec(...)` (exactly how a visual module
exposes a top-level `MODULE`) declaring: the subcommand verb it registers, its default
diff policy (`require` / `optional` / `none`), the CLI arguments it adds (`add_arguments`,
e.g. its positional question/topic), and its thin handler.

### Adding a mode

1. Create `reviewlib/modes/<name>.py` exposing `MODE = ModeSpec(...)` with a handler that
   is thin over the lib (call `panel.run_panel` / `run_moderator` etc.).
2. Add `from .<name> import MODE as _<NAME>_MODE` and list it in `MODES` in
   `registry.py`.
3. No `cli.py` surgery — dispatch is registry-driven. (`cli.py` only special-cases the
   diff-review mode's failover-board wiring, which is genuinely CLI-side.)

## install-* commands report INSTALLED state

`install-skill` / `install-commit-hook` (`reviewlib/install.py`) and `register-module`
(`reviewlib/features/visual/registry.py`) are idempotent AND report state: each target prints a green ✓
"already configured" when unchanged, "+ wrote/updated" when it (re)wrote; a fully-set-up
re-run says "already configured — nothing to do". A target that could NOT be configured (a
foreign pre-commit hook, a wrong/occupied skill-symlink target, an unwriteable
settings.json) is reported as `! conflict`, left as-is, and the command exits non-zero —
"nothing to do" is never printed when a conflict exists. The change-detection helpers
(`_write_if_changed`, `_append_marked` returning a changed bool, `_sessionstart_hook_present`)
keep that honest — when you add an install target, return whether it changed so the summary
stays accurate.

### `install-commit-hook` delegates to `rig apply` when rig is present

`install_commit_hook` writes a GLOBAL git pre-commit hook (`core.hooksPath`). When
[rig](https://github.com/alex-mextner/rig-cli) is also on the machine, its
`git_hooks.dispatcher` provisions the SAME mechanism as one stage of its composed pre-commit
(`agent-tools/git-hooks/global-dispatcher/hooks/review-gate` — ported verbatim from this
command) — two tools writing `core.hooksPath` is the exact double-write the shared
`agenttools_rig_delegate` lib (agent-tools#282, stdlib-only) exists to remove. `install_commit_hook`
guard-imports it (`_rig_delegate_helper`) and, when rig is present, delegates **scoped**:
`delegate(["apply", "--only", "git_hooks"])` — never reconciling unrelated areas (permissions /
GitHub / tools) as a side effect of installing a commit hook. Three outcomes:

- rig **fails** (non-zero) -> surface that exit code as-is (a real rig failure is never
  swallowed into the fallback — that would recreate the double-write).
- rig **succeeds and the REVIEW gate is in place** (`_commit_gate_active()`: `core.hooksPath`
  resolves to a dir with an executable `pre-commit` that is either the direct marker gate OR is
  rig's composer that BOTH references `review-gate` AND is accompanied by an executable
  `review-gate` sibling stage file — an UNRELATED pre-existing global hook, and a bare orphan
  `review-gate` file next to an ordinary hook that never invokes it, both do NOT count) -> rig
  owns it,
  return 0.
- rig **succeeds but provisions no gate here** (the repo declares no `git_hooks:` block, so the
  scoped apply is a no-op for hooks) -> fall back to `_install_commit_hook_direct`, which installs
  the gate when `core.hooksPath`'s pre-commit slot is free, or reports a `NOT ours` conflict
  (rc 1) WITHOUT clobbering when a foreign hook already occupies it. This is distinct from a rig
  failure.

rig absent, or the helper not installed (`pip install -e <agent-tools>/lib/agenttools_rig_delegate`,
the `rig-delegate` extra) -> `_install_commit_hook_direct` runs exactly as before.

**`install-skill`'s SessionStart hook does NOT delegate.** rig's own `tools:` provisioning
(`riglib/tools.py` in rig-cli) runs THIS repo's `install.sh`, which itself calls `review
install-skill` — rig is a CONSUMER of `install-skill`, not an independent provider of the same
hook. Delegating `install-skill` to `rig apply` would risk a `review install-skill` -> `rig
apply` -> (`tools:` block) -> `install.sh` -> `review install-skill` cycle on a machine that
lists `review` under its `tools.items`. `install-hook tg` is unrelated to rig entirely (a
tg-cli descriptor; rig has no equivalent) and likewise does not delegate.

## Visual review proof

`review visual` / `review <text-mode> --visual` is not proven by a normal text-mode answer. Before reporting that a
screenshot was reviewed, verify that the companion vision fan-out produced a usable
structured verdict from the actual image (`*-vision` log, `available=true`, verdict in the
visual schema). An empty, unparseable, unavailable, or timed-out vision result must fail
closed and block the companion mode; do not substitute cvGate notes, DOM facts, browser
inspection, or a non-vision panel response for screenshot delivery to the vision backend.
When debugging visual failures, inspect the `*-vision` log first, not only the final text
seat log.

## CLI shape before hook workarounds

If a hook or downstream consumer needs cleaner semantics, add the right `review` command
surface first. Do not hide repo context, change cwd, or otherwise coerce behavior in the
caller to paper over a wrong CLI shape; that leaves help/docs lying and makes behavior
depend on the launch environment.

## Unresolved problems need owners

Do not report a failed gate or "unrelated/local environment" problem as a loose caveat.
If a problem is real enough to mention in a final report, it needs a durable follow-up in
the same turn: create or link the task, state the owner/next command, and start any
independent investigation that can run in parallel. A caveat without a ticket or first
action is not a status report; it is dropped work.

## Tests

CI runs **`python tests/smoke.py`** (this IS the test runner — the ecosystem is Python-only:
it drives the real `bin/review` CLI via subprocess for the smoke assertions, then runs every
`tests/test_*.py`). The same file is pytest-collectable (`pytest tests/smoke.py`). Replicate
it locally:

```
pip install -e '.[test]'      # pyyaml runtime + pillow + pytest (test); ImageMagick `magick` is a system dep
python tests/smoke.py         # standalone (what CI runs)
pytest tests/smoke.py         # or under pytest
```

The visual-verification suite needs ImageMagick v7 (`magick`) + Pillow; without them it
self-skips loudly and the core suite still runs. Mode-subcommand + registry coverage is
in `tests/test_mode_subcommands.py`.

The dashboard SPA's pure JS logic (`resolveModel` / `filteredRuns` in
`reviewlib/dashboard/assets/app.js`) has node-based unit tests in
`tests/dashboard_app.test.js`, run by `smoke.py`'s `test_dashboard_js_unit` via Node's
built-in runner (`node --test`). Node is present on the GitHub runner, so they execute in
CI; where `node` is absent the check self-skips loudly. Run them directly with
`node --test tests/dashboard_app.test.js`. The functions are exposed to Node via a guarded
`module.exports` footer in `app.js` (a no-op in the browser), so the tests exercise the
exact code the SPA runs — no drifting copy.

When a test needs to stub a mode handler, patch it **where it is defined** (e.g.
`reviewlib.modes.brainstorm.mode_brainstorm`), NOT as a `cli.<fn>` attribute — dispatch
goes through `modes/registry`, so a `cli.mode_*` rebind has no effect.

## Docs language

`AGENTS.md` and any repo-level `CLAUDE.md` are read by all agents — keep them **English
only**, no other natural language.
