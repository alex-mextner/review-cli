# AGENTS.md — review-cli

Agent-facing notes for working IN this repo. (User-facing docs live in `README.md`.)

## What this is

multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic.

Operationally, `review` fans a git diff (or a question / topic) out to several model
backends in parallel and prints their findings.

## Invocation — modes are SUBCOMMANDS

The mode is selected by a **subcommand**, not a flag:

```
review                       # bare -> the diff review (the default mode)
review review --staged       # explicit diff-review subcommand (identical)
review brainstorm "TOPIC"    # multi-round persona ideation
review brainstorm "TOPIC" --diff    # …grounded in the working-tree (or --staged) diff
review just-ask "QUESTION"   # single-shot multi-model answer (alias: review ask "Q")
review quorum "QUESTION"     # experts cite evidence + a moderator finds quorum
review --visual shot.png …   # COMPOSABLE flag (NOT a mode): rides any subcommand
```

A bare `review …` with no recognized subcommand defaults to the `review` mode (so
`review -C <repo>` / `review --staged` / `review --visual shot.png` keep working). The
OLD mode flags (`--brainstorm` / `--quorum` / `--just-ask`) were REMOVED — they now print
a one-line "use the subcommand" pointer and exit non-zero.

Bare subcommands also handled by the CLI (unchanged): `dashboard`, `sessions`, `spec-web`,
`install-skill`, `install-commit-hook`, `register-module`, `trust-module`. `sessions` is a
MANAGEMENT command (list / resume brainstorm sessions parsed from the discussion logs), NOT
a fan-out mode — it is wired in `cli._dispatch` like `dashboard` and its logic lives in the
lib (`reviewlib/sessions.py`); it deliberately does NOT register a `ModeSpec`, so it never
collides with the mode registry.

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
  registry.py     # MODES list + get_mode / known_subcommands / default_mode / iter_modes
  review.py       # MODE = ModeSpec(subcommand="review",     diff_policy="require",  handler=…)
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
   `review` mode's failover-board wiring, which is genuinely CLI-side.)

## Tests

CI runs **`bash tests/smoke.sh`** (this IS the test runner — it runs the bash smoke
assertions then every `tests/test_*.py`). Replicate it locally:

```
pip install -e '.[test]'      # pyyaml runtime + pillow (test); ImageMagick `magick` is a system dep
bash tests/smoke.sh
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
