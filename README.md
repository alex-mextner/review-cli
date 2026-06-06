# review-cli

Small read-only multi-model code review runner.

## Install

```bash
ln -sfn ~/xp/review-cli/bin/review ~/.files/bin/review
```

## Usage

Review the current git diff with the default reviewers:

```bash
review
```

Run several reviewers in parallel:

```bash
review -m codex -m gemini -m oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo
```

Comma-separated models also work:

```bash
review -m codex,gemini,claude-p
```

Review a supplied diff instead of calling `git diff`:

```bash
git show --format= --no-ext-diff HEAD | review -m gemini
```

## Model Backends

- `codex` or `codex:<model>` uses `codex exec -s read-only`.
- `gemini` or `gemini:<model>` calls the Gemini API directly.
- `claude-p` or `claude:<model>` uses `claude-p` in plan/read-only mode with mutating tools denied.
- `oc:<model>` / `opencode:<model>` uses `opencode run`.

Any unknown `-m` value is treated as an opencode model id.

## Gemini Auth

The Gemini backend reads `GEMINI_API_KEY` or `GOOGLE_API_KEY` from the environment.
It also supports `GEMINI_ENV_FILE=/path/to/.env` with a `GEMINI_API_KEY=...` line.
On this machine it additionally checks `/Users/ultra/xp/ExpenseSyncBot/.env`.

The Gemini CLI OAuth file (`~/.gemini/oauth_creds.json`) is not enough for the public
Generative Language API; a direct bearer call returned `ACCESS_TOKEN_SCOPE_INSUFFICIENT`.

## opencode Read-Only Mode

Current opencode versions can create a project agent with:

```bash
opencode agent create \
  --path . \
  --description "Read-only code reviewer. May inspect files and diffs but must never edit, write, run shell commands, or ask questions." \
  --mode primary \
  --permissions read,grep,glob \
  --model fireworks/accounts/fireworks/routers/kimi-k2p6-turbo
```

At the time this CLI was written, `opencode run --agent read-only-reviewer` did not
discover that local agent reliably. To keep source repositories safe, the opencode backend
runs from a temporary git repository and attaches the source diff as `review.diff`.
That gives the model review context without giving it the source worktree as an edit target.

## Public Defaults

Default reviewers:

```text
codex
gemini
oc:fireworks/accounts/fireworks/routers/kimi-k2p6-turbo
```
