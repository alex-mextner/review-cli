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
review diff                  # the diff review (was the stuttering `review review`)
review diff --staged         # review the staged diff (pre-commit)
review brainstorm "TOPIC"    # multi-round persona ideation
review brainstorm "TOPIC" --diff    # …grounded in the working-tree (or --staged) diff
review just-ask "QUESTION"   # single-shot multi-model answer (alias: review ask "Q")
review quorum "QUESTION"     # experts cite evidence + a moderator finds quorum
review diff --visual shot.png …   # COMPOSABLE flag (NOT a mode): rides any subcommand
```

A bare `review` (no subcommand) prints the HELP/usage — it does **NOT** run a diff review
(the old "bare review == a diff review" default was a mistake; never reintroduce it). The
diff review is the `diff` subcommand: `review diff` (renamed from the stuttering
`review review`). The removed verb `review review` and `review -C <repo>` (flags with no
verb) print a one-line "use `review diff`" pointer and exit non-zero. The meta flags
(`--list-defaults` / `--show-board` / `--help`) still work with no subcommand. The OLD
mode flags (`--brainstorm` / `--quorum` / `--just-ask`) were likewise REMOVED — they print
a "use the subcommand" pointer and exit non-zero.

Bare subcommands also handled by the CLI (unchanged): `dashboard`, `sessions`, `spec-web`,
`install-skill`, `install-commit-hook`, `register-module`, `trust-module`. `sessions` is a
MANAGEMENT command (list / resume brainstorm sessions parsed from the discussion logs), NOT
a fan-out mode — it is wired in `cli._dispatch` like `dashboard` and its logic lives in the
lib (`reviewlib/sessions.py`); it deliberately does NOT register a `ModeSpec`, so it never
collides with the mode registry.

### Option scoping — global vs subcommand

Flags are SCOPED so `review --help` (the top-level overview) lists only TRULY-GLOBAL
options (`-m/--model`, `-C`, `-o`, `--timeout`, `--list-defaults`, `--show-board`, `--pool`)
+ the subcommand list. Subcommand- and feature-specific flags live on `review <mode> --help`:
the composable `--visual` group (rides any subcommand), `--prompt` (the diff review),
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

When a test needs to stub a mode handler, patch it **where it is defined** (e.g.
`reviewlib.modes.brainstorm.mode_brainstorm`), NOT as a `cli.<fn>` attribute — dispatch
goes through `modes/registry`, so a `cli.mode_*` rebind has no effect.

## Docs language

`AGENTS.md` and any repo-level `CLAUDE.md` are read by all agents — keep them **English
only**, no other natural language.
