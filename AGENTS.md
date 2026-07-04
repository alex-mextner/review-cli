# AGENTS.md — review-cli

Agent-facing notes for working IN this repo. (User-facing docs live in `README.md`.)

## What this is

multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic.

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
`task`, `install-skill`, `install-commit-hook`, `register-module`, `trust-module`.
`sessions` is a
MANAGEMENT command (list / resume brainstorm sessions parsed from the discussion logs), NOT
a fan-out mode — it is wired in `cli._dispatch` like `dashboard` and its logic lives in the
lib (`reviewlib/sessions.py`); it deliberately does NOT register a `ModeSpec`, so it never
collides with the mode registry. `task` is also a MANAGEMENT command: it reads run-stats and
dashboard logs to list task iterations, models used, and detailed transcripts.

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
