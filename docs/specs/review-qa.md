# `review qa` — agent-as-tester mode for review-cli

> Status: spec (not yet implemented). Source: synthesis of 4 facet designs, grounded
> in verified review-cli / tg-cli / rig-cli / ext-test-projects facts (2026-06-19).
> Lives at `docs/specs/review-qa.md` in the **review-cli** repo (`/Users/ultra/xp/review-cli`).

## 1. Goal

`review qa` adds a TESTER mode to review-cli. It brings up a System-Under-Test (SUT),
exercises it against **human-authored test-case suites**, hunts for ANY bug (crash, wrong
output, broken UI, bad error handling, race, security/permission slip, console error,
network failure, accessibility break, slow path), and reports findings with proof
(screenshots / logs / HTTP status / stack traces). A clean report is only credible if the
agent actually drove the system and shows evidence.

It is the **first review-cli mode that needs a WRITE/EXEC-capable agent**. Every existing
backend is read-only by construction — this is a deliberate, enforced security boundary,
not an accident:

- `reviewlib/backends.py:74` — codex is spawned `codex exec -s read-only -C <cwd> --ephemeral`.
- `reviewlib/backends.py:99` — `_READONLY_AGENT_DENIED_PERMISSIONS` denies
  `bash/edit/write/webfetch/task/todowrite/websearch/lsp/skill`; `_ensure_opencode_readonly_agent`
  (`backends.py:233`) rewrites any non-strict opencode agent and refuses to run in a repo
  shipping its own opencode config.
- `reviewlib/backends.py:1175` — claude is spawned `claude-p --permission-mode dontAsk
  --tools '' … --disallowedTools Edit MultiEdit Write Bash Read …`.

A tester needs the **opposite** capability profile. So **`qa` does NOT ride
`run_panel`/the failover board** the way `diff`/`quorum`/`brainstorm` do — those paths
exist to keep the agent caged. `qa` keeps the *registration/dispatch* identical to other
modes but routes the actual run through a NEW thin **agentic launcher** that spawns ONE
write/exec-capable backend in an **isolated checkout/worktree of the SUT**. This
separation is load-bearing; an implementer must NOT wire `qa` into `mode_review`/`run_panel`
or call `_ensure_opencode_readonly_agent`. A comment next to `_READONLY_AGENT_DENIED_PERMISSIONS`
must record that `qa` is the deliberate write/exec exception, so a future hardening pass
does not clamp it shut.

## 2. Non-negotiable requirements

1. **Suites are mandatory.** `qa` requires a path resolving to ≥1 test-case suite
   (recommended `docs/tests/suites/*.md`, relative to the SUT). If none resolve (or a file
   parses to zero cases), it prints a 3-part WHAT/WHY/HOW message teaching the human to
   author suites and **exits non-zero** (`EXIT_QA_NO_SUITES = 5`). A green run with no
   authored cases would be a lie. This gate runs BEFORE any agent spawn, docker, or browser.
2. **Write/exec executor, isolated blast radius.** The tester runs un-caged, but inside an
   isolated `git worktree add` of the SUT by default (`--in-place` escape hatch). It never
   pushes, never commits to the SUT, never touches the user's other repos.
3. **Stage reuse > local bring-up > recommend.** Use an existing stage if one is declared
   and verified reachable; else bring the SUT up locally per the runbook; if neither stage
   nor bring-up config exists, RECOMMEND how to stand one up and exit non-zero — never
   fabricate a half-up environment.
4. **Distinct exit classes.** "no suites", "env broke / unhealthy", "couldn't bring SUT
   up", and "tests found a bug" are DIFFERENT exit codes so CI can tell infra failure from
   a real finding (§6).

## 3. CLI surface

`review qa [SUT_PATH] [flags]` — a self-describing `ModeSpec` in new module
`reviewlib/modes/qa.py`, exposed as top-level `MODE`, added to `MODES` in
`reviewlib/modes/registry.py:34`. **No `cli.py` dispatch surgery** — `get_mode("qa")`
resolves it, `_build_mode_parser` builds its surface, `_add_mode_options`
(`cli.py:1083`) calls `mode.add_arguments`. Verified: the dispatch is table-driven and
already covers a new mode generically.

`ModeSpec` fields:

```python
MODE = ModeSpec(
    name="qa", subcommand="qa", diff_policy="none", stats_mode="qa",
    summary="agent acts as a TESTER: bring up the SUT, run docs/tests/suites/*.md, hunt bugs",
    handler=_handler, add_arguments=_add_arguments, announce_logs=True, aliases=("test",),
)
```

`diff_policy="none"` — qa is about a running system, not a diff (a piped/`--staged` diff is
at most optional grounding, exactly like just-ask). `--prompt`/`--moderator` are NOT added
for qa (they are scoped to review/quorum/brainstorm in `_add_mode_options`, `cli.py:1102-1109`).

Unique flags added by `_add_arguments(parser)` (the shared `-C`, `-m`, `--pool`,
`--timeout`, `--visual`, `--strict`, `--diff`, `--staged` come from `_add_global_options`/
`_add_mode_options`):

| flag | default | meaning |
|---|---|---|
| `sut_path` (positional, optional) | `-C` value, else cwd | path to the SUT repo/checkout (falls back to the shared `-C` via `_effective_cwd`). |
| `--suites PATH` | `docs/tests/suites/*.md` (glob/dir, relative to `sut_path`) | **MUST resolve to ≥1 file with ≥1 case.** This is the recommended canonical location. |
| `--kind {web,ext,backend,bot,auto}` | `auto` | SUT shape; drives which runbook the prompt activates and which harness loads (§7). `auto` runs cheap stdlib detection (§5). |
| `--stage-url URL` | none | an EXISTING stage/preview env to test against instead of booting locally. |
| `--bring-up {auto,local,stage,none}` | `auto` | `local` = boot here; `stage` = use `--stage-url`; `none` = assume already running; `auto` = stage if `--stage-url` given else local. |
| `--config PATH` | `docs/tests/qa.yaml` | env-harness config for backend/bot SUTs (§7.2). Optional; absent → mode prints the skeleton on a backend/bot run that needs bring-up. |
| `--harness PATH` | auto-discover | path to the Playwright/Telegram harness (the installed skill, else `~/work/ext-test-projects/e2e`). |
| `--report PATH` | `<sut>/docs/tests/reports/qa-<utc>.md` | findings report sink. |
| `--out DIR` | `<sut>/docs/tests/reports/qa-<utc>/` | screenshots + `findings.json` sink. |
| `--max-cases N` | all | cap cases per run (smoke vs full). |
| `--in-place` | off | run the tester in the SUT working tree instead of an isolated worktree (riskier; opt-in). |
| `--keep-env` | off | skip teardown on failure for triage; print the exact `down`/`delete` command. |
| `--scaffold-env` | off | write `docs/tests/{suites/smoke.md, qa.yaml, env/*}` stubs (idempotent, never overwrites) and exit, so the human has a concrete starting point. |

`--strict` reuses the existing semantics (`cli.py:1076`/`1790`): under `--strict` ANY
finding → exit `10`; without it, only infra/no-suite/unhealthy failures are non-zero and
findings still print at exit `0` (usable as an exploratory pass).

**Handler shape** (thin, mirrors `quorum.py:58`/`just_ask.py:32`): `_handler(ctx) -> int`
reads `ctx.args` + `ctx.cwd` (resolved SUT) + `ctx.models` (single-seat tester) +
`ctx.timeout`, then:
1. if `--scaffold-env`: write stubs, exit 0.
2. resolve suites → `list[Path]` (the NO-SUITES gate, §4) — BEFORE any spawn.
3. detect `kind` if `auto` (§5).
4. for backend/bot: stage-detect → bring-up → health-gate (§7.2), deterministic Python.
5. build the tester system prompt (§8) from the resolved suites + kind runbook.
6. call the agentic launcher (§9) — NOT `run_panel`.
7. parse the agent's `## QA RESULTS` tail → exit code (§6); always teardown (§7.2).

**Timeout carve-out (verified, one place qa's "panel-ness" leaks):** `cli.py:1579`
computes `panel_mode = mode.name != "review"`, and `cli.py:1594` then defaults a panel
mode's timeout to the short `PANEL_TIMEOUT_DEFAULT` (1200s is the review default). Booting
docker + a Playwright run needs the LONG default. In `_handler`, treat an unset
`--timeout` as the long 1200s default (or add qa to that branch). Document this as the
single carve-out qa needs beyond the registry entry.

## 4. NO-SUITES gate (the hard requirement)

Exit-code convention is stable per-class (`cli.py:78-82`): `0` success, `2` argparse,
`3 = EXIT_NOT_A_REPO`, `4 = EXIT_GIT_DIFF_FAILED`, `124` backstop, `10` strict-findings.
qa's new codes start at the next free value, `5` (§6).

Gate in `_handler`, BEFORE any agent/docker/browser:

```
suites = resolve_suites(ctx.cwd, ctx.args.suites)   # glob/dir → sorted list[Path] of *.md with ≥1 case
if not suites:
    print 3-part WHAT/WHY/HOW to stderr (mirror _fail_not_a_repo, cli.py:104):
      WHAT: "[review-cli] qa: no test-case suites found at <resolved path>."
      WHY:  "qa makes the agent act as a tester; without authored cases there is nothing to
             exercise and nothing to verify — a green run would be a lie."
      HOW:  "author at least one suite, then re-run (or run `review qa --scaffold-env`):
               mkdir -p <sut>/docs/tests/suites
               # each *.md = a suite; one '## Case: <name>' per case with Steps / Expected:
               #   ## Case: login rejects empty password
               #   Steps: 1. open /login  2. submit empty password
               #   Expected: inline error 'password required', no network call
             review qa <sut> --suites docs/tests/suites/*.md"
    return EXIT_QA_NO_SUITES   # 5
```

A file that exists but parses to zero `## Case:` blocks is the SAME class → exit 5 with a
"found <f> but it has no '## Case:' blocks" variant. Non-zero even WITHOUT `--strict` (this
is an infra/contract failure, not a finding).

### Suite format

A `*.md` suite file:

```
# Suite: smoke
## Case: preview renders a component
Preconditions:
- dev server can start
Steps:
- open the Hyper Canvas tab
- start the dev server
- select a component
Expected:
- the preview iframe shows non-zero-size DOM, no 404, no console error
```

v1 parses headings (`## Case:` / `## <title>`) into cases; each becomes a sub-goal the
tester must exercise and verdict PASS/FAIL/BLOCKED with proof. Free-form markdown is
chosen for human authorability; the parser counts `## Case:` blocks for the CASES tally.
(Open question: a stricter YAML-frontmatter schema would make CASES counts machine-reliable
— deferred to v2 unless authorability suffers.)

## 5. `--kind auto` detection (stdlib, cheap, in `qa.py`)

First match wins, deterministic order:

- **ext** — `package.json` with `contributes`/`engines.vscode`, or a `*.vsix`, or
  `.vscode-test`/`extension.ts`.
- **web** — `package.json` with a dev server (`vite`/`next`/`react-scripts`) or a
  `playwright.config.*`, or an HTTP-serving `Dockerfile`, and no bot/vscode markers.
- **bot** — a telegram dep (`telegraf`/`grammy`/`python-telegram-bot`/`aiogram`), a `tg`/bot
  token in config, or a compose service named `*bot*`.
- **backend** — `docker-compose*.yml`/`Dockerfile`/`k3s`/`helm`/`skaffold` with an HTTP/gRPC
  service and none of the above.

`auto` failing to classify is NOT fatal → fall back to `backend` and tell the agent in the
prompt that detection was inconclusive (the agent CAN run commands and is the real
detector; the Python pass just seeds the right runbook).

## 6. Exit codes (single coherent block)

Add next to the existing constants (~`cli.py:78`), exported from `cli.py` (or `qa.py`):

```python
EXIT_QA_NO_SUITES      = 5   # no suites / empty suites (contract failure) — §4
EXIT_QA_NO_ENV         = 6   # no stage AND no bring-up config for a backend/bot SUT — §7.2
EXIT_QA_ENV_UNHEALTHY  = 7   # bring-up succeeded but health gate timed out — infra, NOT a bug
EXIT_QA_SUT_BOOT_FAILED= 8   # could not bring the SUT up at all (VERDICT: BLOCKED)
```

These do not collide with 3/4/10/124. Verdict → exit mapping in `_handler` after the
launcher returns and the report is parsed:

- no/empty suites → `5` (handled in §4 before launch).
- backend/bot env unbootable: no stage + no config → `6`; health gate timeout → `7`.
- agent produced no `VERDICT:` line / launcher crashed → `1`.
- `VERDICT: BLOCKED` (couldn't bring SUT up) → `8`.
- `VERDICT: FAIL` (a P0/P1 finding or any failed case) → `1` (or `10` under `--strict`).
- `VERDICT: PASS` with P2/P3 findings under non-`--strict` → `0` (findings still printed);
  `--strict` flips ANY finding to `10`.
- `VERDICT: PASS`, no findings → `0`.

This keeps "env broke" (6/7/8) distinct from "SUT is buggy" (1/10).

## 7. SUT shapes

Two shipped runbooks. Each runbook lives in the **system prompt** (§8), not in Python; the
Python side only does deterministic, idempotent bring-up/health/teardown (backend/bot) and
harness discovery.

### 7.1 web / ext — Playwright harness (`alex-mextner/vscode-playwright`)

**Provisioning (verified ground truth).** rig is NOT a package manager for external repos:
`riglib/catalog.py:48` scans an agent-tools checkout into exactly five categories
(`skills | agent_hooks | git_hooks | ci | mcp`); `_looks_like_agent_tools` requires
`skills/` + `agent-hooks/` dirs (`catalog.py:96`). There is no "clone repo X" item type. So
**"consume via rig" means: ship the harness as an agent-tools SKILL** (the `<tool>
install-skill` → `~/.agents/skills/` mechanism review/tg/draw use; `review install-skill`
at `cli.py:1407`). The harness repo ships its own `install.sh` (modeled on
`review-cli/install.sh`): clone, `npm i`, `npx playwright install chromium`, symlink
`bin/qa-runner.ts` → `~/.local/bin/vscode-playwright-qa`, then register a discovery skill.
rig's only role is pre-allowing the binary in the harness permission allowlist; `rig doctor`
verifies the harness + a VS Code binary are present and emits the install command if not.
(A literal rig.yaml catalog entry would require a net-new rig `harness_repos:`/`tools:`
provisioning category — schema + scanner + action + tests. That is OUT OF SCOPE for qa and
flagged as a separate CTO decision.)

**Seed source (verified).** The e2e tests are at `~/work/ext-test-projects/e2e` (remote
`git@github.com:hyperide/hyper-ext-e2e.git`, private). `gh` is authed as `alex-mextner`;
`alex-mextner/vscode-playwright` does NOT yet exist → genuine `gh repo create
alex-mextner/vscode-playwright --private`. **Seed by selective copy, NOT a fork** —
`hyper-ext-e2e` is HyperIDE-specific (hard-codes `hypercanvas.*` settings, the Hyper Canvas
preview, mock-AI). Copy the GENERIC core:

- COPY: `e2e/setup/electron-app.ts` (the `launchVSCode` isolation core: isolated
  `--user-data-dir`, fresh `--extensions-dir`, `--disable-workspace-trust`/`--skip-welcome`,
  `closeVSCode`/`forceCloseVSCode` process-group kill + dev-server reaping, stray-process
  guards), `e2e/setup/extension-installer.ts` (`getExtensionPath`/`getVscodePath`),
  `helpers/port-allocator.ts`, `helpers/step-logging.ts`, `helpers/wait-for-dev-server.ts`,
  `helpers/iframe-mouse.ts`, `Dockerfile.e2e`, `run-headless.sh`, `playwright.config.ts`,
  a minimal `package.json` (`@playwright/test`, `playwright`, `typescript`, `@types/node`).
- GENERALIZE: the `User/settings.json` writer in `electron-app.ts` hard-codes `hypercanvas.*`
  keys → make it a `settings` parameter so the SUT extension injects its own; KEEP the
  universal isolation flags.
- DROP: `setup-preview.ts` (HyperIDE preview), `data-testid-map.ts`, `mocks/ai-mock-server.ts`,
  every `capture-*.ts`, all screenshot PNGs. Replace with ONE generic `qa-runner.ts`.
- A human curation pass is required before publishing (decision: what subset is safe).

**Two-language boundary.** Playwright is a Node tool; the proven `launchVSCode` is TS.
`review qa` is Python. So `reviewlib/qa/harness.py` locates the `vscode-playwright-qa`
binary (else `EXIT_QA_SUT_BOOT_FAILED`) and shells to the TS runner with a JSON job
(suites + kind + target + out dir). The TS runner owns Electron/browser lifecycle; the
agent drives it through a high-level action vocabulary.

**Driving (verified hard rules from `~/.claude/CLAUDE.md`).** For an EXTENSION: NEVER
`electron.launch` by hand, NEVER `screencapture` — use the harness `launchVSCode()`
(isolated user-data-dir, trust/welcome dialogs handled) and `window.screenshot({path})`
over CDP (bypasses macOS Screen-Recording grants and Spaces); open the actual feature panel
before asserting (activation alone renders nothing). For a SITE: the `agent-browser` skill
(verified installed at `~/.agents/skills/agent-browser`, Chrome-over-CDP with
accessibility-tree snapshots + `@eN` refs, purpose-built for "exploratory testing,
dogfooding, QA, bug hunts") is the default driver; the runner owns dev-server lifecycle.
Decision rule: Electron/VS-Code ⇒ Playwright; plain web ⇒ agent-browser; `--harness`/an
explicit driver overrides.

### 7.2 backend / bot — env harness (docker / k3s / compose)

A deterministic Python layer in `reviewlib/qa/env.py` (NOT LLM-driven, so it never leaks
containers) the handler runs in three phases, declared in `docs/tests/qa.yaml`
(parsed by `reviewlib/qa/config.py`):

```yaml
sut:
  kind: backend                # backend | bot | web | ext
  stage:
    url: https://stage.example.internal       # if set+reachable → REUSE, skip bring-up + skip teardown
    health: https://stage.example.internal/healthz
  bringup:
    driver: compose                            # compose | k3s
    compose_file: docs/tests/env/docker-compose.qa.yml
    project_name: review-qa                    # compose -p, namespaces the run
    env_file: docs/tests/env/qa.env            # NON-SECRET defaults only
  health:
    - { name: api, url: http://localhost:8080/healthz, expect_status: 200, timeout_s: 90 }
    - { name: db,  compose_service: db, healthcheck: true }
  seed:
    - "docs/tests/env/seed.sh"                 # idempotent, run AFTER health pass
  teardown:
    keep_on_failure: false                     # --keep-env overrides
  bot:                                         # only for kind: bot
    driver: mock                               # mock | mtproto
```

**Phase 1 — STAGE DETECTION (reuse if present).** `--stage-url` > `qa.yaml sut.stage.url`
> env `REVIEW_QA_STAGE_URL`. If a stage URL resolves, probe `stage.health` (2xx within a
short timeout). Success → REUSE (`env.mode = "reused-stage"`, test against it, **never
tear it down** — symmetric ownership). A declared-but-unreachable stage → structured 3-part
error + exit non-zero (`EXIT_QA_ENV_UNHEALTHY`), never silent fall-through to a half-up
stage. No stage declared → Phase 2.

**Phase 1b — RECOMMEND if neither stage nor bring-up config.** Mirror the suites gate: print
the `qa.yaml` + `docker-compose.qa.yml` skeleton and `review qa --scaffold-env`, exit
`EXIT_QA_NO_ENV` (6).

**Phase 2 — BRING-UP (compose default, dogfoods the team's pattern).** Reuse the shape of
`~/work/ext-test-projects/e2e/docker-compose.e2e.yml` + `run-headless.sh` (verified:
`docker compose -f … run --rm [--build]`, named cache volumes, `shm_size`/`mem_limit`/`cpus`
limits, project-namespaced). qa runs `docker compose -p <project_name> -f <compose_file>
--env-file <env_file> up -d --wait` (gates on container healthchecks when `--wait` is
supported; else `up -d` + the explicit Phase-3 loop). `-p` namespaces every run so parallel
runs / leftovers never collide and teardown targets exactly this project. `--build` opt-in,
default cached. k3s (`driver: k3s`) is OPTIONAL (deferred to v2 unless a k8s-native SUT
appears): `kubectl apply -n review-qa-<id> -f …`, gate on `kubectl rollout status`, tear
down with `kubectl delete namespace`. All spawns go through `reviewlib/process.py` with
explicit timeouts (the repo's hard rule).

**Phase 3 — HEALTH GATING (hard gate before any test).** Each `health` entry polls
(bounded exponential backoff) until `expect_status`/compose `healthy` within `timeout_s`.
ALL must pass. On timeout → tear down (unless `--keep-env`), print which check failed with
the last response/log tail, exit `EXIT_QA_ENV_UNHEALTHY` (7). Only after a full green gate
are `seed:` scripts run (idempotent) and the tester agent handed the endpoints + suites.

**Phase 4 — TEARDOWN (guaranteed, every exit path).** try/finally + register with
`reviewlib/backstop.py` so a backstop-killed run still reaps containers. compose:
`docker compose -p <project> down -v --remove-orphans`; k3s: `kubectl delete namespace`.
**Ownership rule:** only tear down what THIS run brought up — a reused stage is never torn
down. `--keep-env`/`teardown.keep_on_failure` skips teardown on failure and prints the exact
manual command. Edge cases: docker daemon down → structured error + fix; fixed-port collision
→ recommend ephemeral publish + `docker compose port`; stale leftover `-p` project →
adopt-if-healthy / recreate-if-unhealthy, `--fresh` forces `down -v` first; teardown itself
hanging → bounded timeout + log leftover project name.

**Env-simulation tooling** lives version-controlled under `docs/tests/env/`
(`docker-compose.qa.yml`, `mocks/` wiremock/Prism/hand-rolled stubs, `fixtures/` + `seed.sh`,
`qa.env` with NON-SECRET config only — secrets stay in host env; the repo's gitleaks
pre-commit + CI gate backstops a leak). When a needed mock is missing, the tool-enabled
tester authors it into `docs/tests/env/` and re-runs bring-up; `--scaffold-env` seeds the
structure.

### 7.3 bot — approximating REAL Telegram (the hard case)

**Governing constraint (verified).** tg-cli is Bot-API-only: `tg` sender builds
`https://api.telegram.org/bot${BOT_TOKEN}` (`tg:610`); `tg-ctl` long-polls `getUpdates`
(`tg-ctl:1996`). A grep for `mtproto|telethon|gramjs|api_id|api_hash|tdlib` finds NOTHING —
there is no user-account capability. **A bot cannot DM another bot and `getUpdates` polls AS
the bot**, so "spin up a second bot to play the human" is impossible. The human side must be
approximated. Two tiers, declared in `qa.yaml sut.bot.driver`:

- **Tier 1 — mock Bot-API server (cheap, hermetic, DEFAULT).** Run a local fake Telegram
  Bot-API server as a compose service; point the SUT bot's daemon at it. The INBOUND path
  already supports this: `tg-ctl` honors `TG_API_BASE` (verified `tg-ctl:2169`,
  `env.TG_API_BASE || "https://api.telegram.org"`), so `TG_API_BASE=http://mock-telegram:8081
  tg-ctl run` makes the bot long-poll the fake. The harness POSTs synthetic `getUpdates`
  messages (simulating a human typing — the update shapes are grounded in
  `features/tg-ctl/types.ts`: `TgUpdate{update_id, message{message_id, from, chat, date,
  text/caption/photo/document/voice, reply_to_message, quote, message_thread_id,
  is_topic_message, forum_topic_*}, callback_query{id, from, message, data}}`) and asserts on
  the bot's outbound `sendMessage` calls captured by the mock. **Verified GAP (load-bearing
  prerequisite):** the OUTBOUND `tg` sender HARDCODES the base URL (`tg:610`) and does NOT
  read `TG_API_BASE`. Tier 1 needs a one-line tg-cli change making `tg`'s `API` honor
  `TG_API_BASE` like `tg-ctl` already does — a tiny, justified change to the SUT's own tooling
  (it also makes `tg` itself testable), filed as a **separate prerequisite PR against
  tg-cli**, not assumed. The harness must DETECT an un-patched sender (mock sees zero
  `sendMessage`) and fail the health gate with the precise `tg:610` pointer rather than
  silently passing on zero captured sends.
- **Tier 2 — real MTProto user account + agent-browser (high fidelity, OPT-IN).** For suites
  tagged `requires_live_telegram` (real delivery semantics, voice notes, inline buttons,
  forum topics): drive a DEDICATED test USER account via MTProto (Telethon — NOT installed,
  a new qa-harness dep, NOT a tg-cli dep) and/or a Telegram Web client in `agent-browser`
  acting as the human. The driver: `send(text, reply_to, thread_id, photo/document/voice)`,
  `tap(message, button)` (callback queries — the only faithful way to exercise tg-ctl's
  q-buttons / plan-approval flows), `expect(predicate, timeout)`. Forum-topics coverage ties
  to in-flight tg-ctl task #31 (`features/tg-ctl/topics.ts` awaiting-path → awaiting-model →
  bound lifecycle; `message_thread_id` IS the topic id). agent-browser screenshots rendered
  bubbles for VISUAL correctness (Rich Messages, syntax-highlighted PDFs, custom-emoji pills,
  HTML tables) that MTProto's raw payload can't verify.

**Bot SAFETY (mandatory — drives REAL Telegram in Tier 2).** Dedicated throwaway test USER
account + test BOT (own token from @BotFather; `botIdFromToken` keys the daemon per bot id,
so test traffic never lands in the real daemon's state). DEDICATED test DM + test
supergroup/forum containing ONLY the test account + test bot; the harness MUST refuse to run
(fail-closed) if the configured `chat_id` matches the real `TG_CHAT_ID`. Exactly ONE poller
per test bot token (stop any stray daemon first — `tg-ctl` returns HTTP 409 on a second
poller). Test secrets (`api_id`/`api_hash`/session-string/bot-token) in a SEPARATE
gitleaks-scanned config (`~/.config/tg-cli-qa/.env`), never committed, never logged, redacted
from artifacts. Prefer Telegram's TEST data center where the SUT can register there.
Cleanup created test messages/topics after a run.

### 7.4 Tier-2 (the LIVE tier) — SCAFFOLDING shipped, CREDS the CTO must provision (#82/#84)

Tier-1 (the deterministic / hermetic harnesses) ships and gates in CI with no creds. Tier-2 is
the LIVE tier: it swaps the in-process fake / local boot for a REAL external system. The
**scaffolding** is built and unit-tested without creds (`reviewlib/qa/live_tier.py`): the
`tier: live` config path (a per-SUT `driver:` value), a per-SUT availability GATE that names the
EXACT missing creds, the live-driver SKELETON wired behind the SAME protocol seam the Tier-1
driver speaks, and the dispatch that SKIPs LOUD (a controlled BLOCKED, never a fake pass) when
creds are absent. **The actual live run is tracked in #82** — it needs the credentials/infra
below. Until then, a `tier: live` block runs to a BLOCKED that names exactly what to provide.

**How a live tier is selected.** Set the SUT block's `driver:` to the live value AND set the
opt-in flag (the flag mirrors `REVIEW_QA_PLAYWRIGHT` / `REVIEW_QA_VSCODE`):

| SUT | `driver:` (qa.yaml) | opt-in flag | gate |
| --- | --- | --- | --- |
| bot | `mtproto` | `REVIEW_QA_BOT_LIVE=1` | `live_tier.bot_live_available()` |
| web | `agent-browser` | `REVIEW_QA_WEB_LIVE=1` | `live_tier.web_live_available()` |
| ext | `vscode-visual` | `REVIEW_QA_EXT_LIVE=1` | `live_tier.ext_live_available()` |

**Credentials / infra the CTO must provision for the live run (per SUT):**

**bot (real Telegram, MTProto):**
- A DEDICATED throwaway test Telegram USER account (its own phone / virtual number) — NEVER the
  real account. MTProto user-account automation risks a Telegram ToS BAN; burn a throwaway.
- A test BOT (its own token from @BotFather) and a DEDICATED test chat containing ONLY the test
  account + test bot (never the real chat).
- Env: `REVIEW_QA_BOT_LIVE=1`, `TG_TEST_API_ID`, `TG_TEST_API_HASH` (my.telegram.org app creds of
  the test account), `TG_TEST_SESSION` (a Telethon StringSession for the test USER account),
  `TG_TEST_CHAT_ID` (the test chat id — MUST NOT equal the real `TG_CHAT_ID`; the gate fails
  CLOSED if it does).
- Dep: `pip install telethon` (a qa-harness dep, NOT a tg-cli dep).

**web (real browser, live site):**
- A deployed test SITE URL to drive (a stage, NOT production).
- Env: `REVIEW_QA_WEB_LIVE=1`, `REVIEW_QA_WEB_BASE_URL` (the test site URL, e.g.
  `https://stage.example.test`).
- Runtime: Playwright + a browser (`pip install playwright && python -m playwright install
  chromium`) OR the `agent-browser` CLI on PATH.

**ext (real VS Code, window-screenshot visual diffing — #82's core ask):**
- Env: `REVIEW_QA_EXT_LIVE=1`, `REVIEW_QA_VSCODE=1` (the underlying Tier-1 ext gate),
  `REVIEW_QA_EXT_BASELINE_DIR` (a writable dir — first run records baselines, later runs
  perceptual-diff against them with a threshold gate).
- Runtime: node/tsx (NOT bun — bun hangs Electron launch on macOS), a VS Code binary
  (`VSCODE_PATH` or `code` on PATH), and ImageMagick v7 (`magick`) for the perceptual diff (the
  same tool the visual-verification suite uses).

**How to run Tier-2 once provisioned.** Set the block's `driver:` to the live value, export the
flag + creds above, then run the usual `review qa --kind <bot|web|ext> --suites …`. The dispatch
routes the block to its live gate; with creds present it drives the real SUT (the live run, #82),
with creds absent it BLOCKS and prints exactly which var is missing.

## 8. The tester-agent SYSTEM PROMPT (core deliverable)

Built in `qa.py` (`_build_tester_prompt(kind, suites_text, sut_path, stage_url, bring_up,
harness, strict) -> str`), fed to the single agentic backend. Style mirrors the existing
mode prompts (role line + hard rules + runbook + output contract) — but GRANTS exec/write
because the launcher runs the backend un-caged.

> **ROLE.** You are a senior QA / SDET acting as a hostile but fair TESTER of the
> System-Under-Test (SUT) at `{sut_path}`. BRING THE SUT UP, EXERCISE it against the suites
> below, and hunt for ANY problem. Assume there ARE bugs; a clean report is only credible if
> you actually drove the system and show proof.
>
> **GROUND RULES.**
> 1. You MAY run shell commands, start services, and write throwaway scratch files — but ONLY
>    inside `{sut_path}` and its scratch/worktree. Never touch the user's other repos, never
>    push, never `git commit` to the SUT, never delete SUT source. The SUT tree is disposable.
> 2. Run EVERY case in EVERY suite below unless a precondition genuinely can't be met — then
>    mark it BLOCKED with the reason, never silently skip.
> 3. Evidence or it didn't happen. Each finding cites the exact case, the command/step, and
>    concrete proof: a screenshot path, console/network log lines, an HTTP status, a stack
>    trace, an expected-vs-actual diff. No proof → say "unverified".
> 4. Don't fix the SUT. Report; do not patch (a tiny disclosed env-sim shim to MAKE a test
>    runnable is allowed).
> 5. If you cannot bring the SUT up at all, that is itself a P0 finding (with the failing
>    command + output), not a reason to stop — record it and continue with whatever you reach.
>
> **BRING-UP (mode = `{bring_up}`).** `stage`: test against `{stage_url}`, verify reachable
> first (`curl -sS -o /dev/null -w '%{http_code}'`); `local`: boot per the runbook, prefer the
> project's own scripts, capture boot logs; `none`: connect to a running instance. If no stage
> exists and one is warranted, RECOMMEND how to stand one up — but still do a local bring-up
> this run.
>
> **RUNBOOK — `{kind}`** (only the matching block injected):
> - *web / ext:* Use the Playwright harness at `{harness}`. EXTENSION: NEVER `electron.launch`
>   by hand, NEVER `screencapture` — use `launchVSCode()` + `window.screenshot({path})` over
>   CDP; open the feature panel before asserting. SITE: `agent-browser` (`open` + `click` +
>   `screenshot` + `get text` + `eval`). Per case: drive the Steps, assert Expected, screenshot
>   the end state, read browser console + network even if the case "passed".
> - *backend:* Stand the service up (compose/k3s/Dockerfile), wait for health, exercise each
>   case over its real protocol (HTTP via curl/httpie, gRPC via grpcurl, queue via the client),
>   assert status + body + side effects, probe error paths (malformed input, missing/expired
>   auth, oversized payload, concurrency). Capture container logs on failure.
> - *bot:* The SUT is a chat bot; you need a HUMAN-like caller. Tier 1 (default): a local mock
>   Bot-API server — the bot polls it via `TG_API_BASE`; you POST synthetic `getUpdates` and
>   assert captured `sendMessage` calls. Tier 2 (`requires_live_telegram`): a real DEDICATED
>   test Telegram account via MTProto and/or Telegram Web in agent-browser. Per case: send the
>   trigger, assert reply text/buttons/media, check timing, probe bad input. NEVER use the real
>   chat/account; fail closed if `chat_id` == the real `TG_CHAT_ID`.
>
> **WRITE TOOLING IF NEEDED.** If the env can't be exercised as-is, write SMALL simulation
> tooling (fake API server, seed script, compose override) under the SUT scratch dir, keep it
> minimal, DISCLOSE every shim, and never let a shim mask the behavior under test.
>
> **TEST SUITES — run these (human-authored):**
> ```
> {suites_text}   # concatenated docs/tests/suites/*.md, each headed with its filename
> ```
>
> **OUTPUT CONTRACT (machine-parsed — emit EXACTLY this at the end):**
> ```
> ## QA RESULTS
> SUT: {sut_path}   KIND: {kind}   BRING-UP: {bring_up or stage_url}
> CASES: <total> run, <p> passed, <f> failed, <b> blocked
>
> ### FINDINGS
> - [P0|P1|P2|P3] <case> — <what's wrong> — proof: <screenshot/log/status> — repro: <steps/cmd>
> ...(one bullet per finding; none → "no findings")
>
> ### BLOCKED
> - <case> — <why it couldn't run>
>
> ### ENV-SIM / TOOLING WRITTEN
> - <path> — <what it fakes> (or "none")
>
> ### STAGE RECOMMENDATION
> - <how to stand up a stage if missing, or "n/a">
>
> VERDICT: PASS | FAIL | BLOCKED
> ```
> VERDICT=FAIL on any P0/P1 finding or any failed case. BLOCKED if the SUT could not be brought
> up at all. PASS only if every case ran and passed.

The handler writes the full transcript to `--report`, the per-case `findings.json` + `*.png`
to `--out`, and parses the `VERDICT:`/`CASES:` tail for the exit code (§6).

## 9. The agentic launcher (new, thin, qa-only)

A small function in `reviewlib/qa/executor.py` (reusable later) that spawns ONE write/exec
backend — the deliberate inverse of the caged `review_codex`/opencode path:

- **claude (recommended DEFAULT):** Claude Code headless in the SUT worktree with tools
  ENABLED (bash + browser), system prompt on stdin, output streamed to the report. Reuse the
  streaming/log-announce plumbing from `panel`/`backends._run_streamed` but with NO read-only
  flags.
- **codex (alternate):** `codex exec -s workspace-write -C <sut_worktree> --full-auto`
  (explicitly the opposite of `backends.py:74`'s `-s read-only`).
- **opencode: OUT OF SCOPE for v1.** review-cli's opencode integration is built to FORCE the
  deny-all read-only agent (`backends.py:233`); a write-capable opencode agent fights that
  single-source-of-truth guard.
- It MUST run in an isolated `git worktree add` of the SUT by default (clean up on
  exit/signal via the worktree-isolation pattern), with `--in-place` as the documented escape
  hatch. It explicitly does NOT call `_ensure_opencode_readonly_agent`/`_repo_has_opencode_config`
  — bypassing them here is correct and MUST be commented so a future reader doesn't "restore"
  the read-only flag and silently neuter qa.
- **Single-seat by default.** A tester run is one agent driving a live system; a parallel
  panel of testers fighting over the same docker/port is nonsense. `-m` selects the backend;
  `--pool`/the board are ignored for qa (qa takes the non-review panel branch with a single
  model). Document this override.

## 10. Files touched / created

review-cli:
- NEW `reviewlib/modes/qa.py` — `MODE`, `_add_arguments`, `_handler`, `_build_tester_prompt`,
  `resolve_suites`, kind-detection.
- NEW `reviewlib/qa/{__init__,executor,harness,env,config,suites,findings}.py` — the agentic
  launcher, harness discovery + TS-runner shell-out, the deterministic env harness, qa.yaml
  parse, suite parse, findings sink.
- EDIT `reviewlib/modes/registry.py` — one import line + one `MODES` tuple entry.
- EDIT `reviewlib/cli.py` — add `EXIT_QA_NO_SUITES/NO_ENV/ENV_UNHEALTHY/SUT_BOOT_FAILED`
  (5/6/7/8) next to the existing exit-code block (~`cli.py:78`); a one-line timeout-default
  carve-out so qa gets the long timeout not `PANEL_TIMEOUT_DEFAULT`.
- EDIT `reviewlib/backends.py` — a comment next to `_READONLY_AGENT_DENIED_PERMISSIONS`
  recording that qa is the deliberate write/exec exception.
- Docs: `docs/mode-qa.*` help page + README blurb so `review --help` and `help-docs-sync`
  stay consistent; document the `docs/tests/{suites,env}/` + `docs/tests/qa.yaml` convention.

Separate prerequisite deliverables (qa depends on them but they are NOT review-cli code):
- NEW repo `alex-mextner/vscode-playwright` seeded by selective copy from
  `hyperide/hyper-ext-e2e` `e2e/`, packaged as an agent-tools skill (§7.1).
- tg-cli: a one-line change so the `tg` outbound sender honors `TG_API_BASE` (§7.3, Tier 1).
- (Optional) a Telethon-based MTProto qa-harness driver for bot Tier 2.

## 11. Open decisions for the CTO

1. **Tester backend:** claude DEFAULT, codex `-s workspace-write --full-auto` ALTERNATE,
   opencode OUT of v1 — confirm.
2. **Isolation default:** worktree (safe, but may need node_modules/build provisioned in the
   new tree) vs `--in-place` (simpler, riskier). Spec defaults to worktree; confirm.
3. **Single-seat:** v1 is single-seat (a panel fighting one docker/port is nonsense).
   N testers against N isolated stages is a v2 question. Confirm.
4. **vscode-playwright seeding:** which subset of the private hyper-ext-e2e is safe to publish
   (likely just `setup/` primitives + a generic harness, not the HyperIDE specs). Needs a
   human curation pass before the repo is created.
5. **rig integration:** Option A (the harness self-installs the real agent-tools way, no rig
   change — RECOMMENDED) vs Option B (net-new rig `harness_repos:` provisioning feature to
   literally get a rig.yaml catalog entry). Which?
6. **Suite format:** free-form `## Case:` markdown (easy to author, harder to count) vs YAML
   frontmatter per case (reliable CASES counts). v1 uses markdown; confirm.
7. **Bot Tier 2 credentials:** is a dedicated test Telegram account + virtual number + a
   second bot token provisioned, or must v1 ship Tier-1-only (hermetic mock)? Telethon is not
   installed (a new qa-harness dep). The `tg:610` `TG_API_BASE` fix is a filed tg-cli
   prerequisite regardless.
8. **k3s:** deferred to v2 (compose covers the team's existing pattern) unless a k8s-native
   SUT appears — confirm.


## Phased implementation plan

Smallest shippable first; each increment is independently mergeable and adds verifiable behavior.

**Increment 1 — Mode skeleton + suites gate (review-cli only, no agent yet).**
- NEW `reviewlib/modes/qa.py`: `MODE = ModeSpec(name="qa", subcommand="qa", diff_policy="none", stats_mode="qa", aliases=("test",), handler=_handler, add_arguments=_add_arguments, announce_logs=True)`; `_add_arguments` adds `sut_path`, `--suites`, `--kind`, `--report`, `--out`, `--scaffold-env`, `--max-cases`.
- EDIT `registry.py:34`: one import + one MODES entry. EDIT `cli.py:~78`: `EXIT_QA_NO_SUITES=5`.
- `resolve_suites()` + the no-suites/empty-suites 3-part gate (exit 5) and `--scaffold-env` (writes `docs/tests/{suites/smoke.md,qa.yaml,env/*}` stubs, idempotent).
- Handler stops after the gate with a "not yet implemented: launcher" notice (or runs a dry-run that prints the built prompt). Ships the verb, the gate, the convention. Tests: `review qa --help`, no-suites exit 5, scaffold idempotency, a parsed suite's case count.

**Increment 2 — Write/exec executor + claude tester (the core).**
- NEW `reviewlib/qa/executor.py`: spawn ONE write/exec backend (claude default; codex `-s workspace-write --full-auto` alternate) in an isolated `git worktree add` of the SUT (`--in-place` escape hatch), single-seat, streaming to `--report`; cleanup on exit/signal. Explicitly NOT `run_panel`, NOT `_ensure_opencode_readonly_agent`; comment the exception by `_READONLY_AGENT_DENIED_PERMISSIONS`.
- `_build_tester_prompt` + `## QA RESULTS` tail parser → exit codes. Timeout carve-out (long default for qa). `--kind auto` detection.
- End-to-end on a trivial local SUT with one suite. Tests: prompt contains the runbook + suites; verdict→exit mapping (PASS=0, FAIL=1, FAIL+strict=10, BLOCKED=8, no-VERDICT=1).

**Increment 3 — Backend SUT env harness (compose, against a stage first).**
- NEW `reviewlib/qa/{config.py,env.py}`: parse `docs/tests/qa.yaml`; stage-detect (reuse-if-reachable, never tear down a reused stage; `EXIT_QA_NO_ENV=6` if no stage+no config; declared-unreachable → `EXIT_QA_ENV_UNHEALTHY=7`); compose bring-up `-p`-namespaced + `--wait`; health gate (`EXIT_QA_ENV_UNHEALTHY`); seed; guaranteed teardown via `reviewlib/backstop.py`, ownership rule. All spawns via `reviewlib/process.py` with timeouts. `--keep-env`, `--fresh`.
- Tests with a docker-compose fixture: reuse-stage path, bring-up+health-pass, health-timeout exit 7, teardown-on-failure, no-env exit 6.

**Increment 4 (PREREQUISITE, parallel) — `alex-mextner/vscode-playwright` + web/ext harness wiring.**
- `gh repo create alex-mextner/vscode-playwright --private`, seed by SELECTIVE COPY from `hyperide/hyper-ext-e2e` `e2e/` (generalize `launchVSCode` settings, drop HyperIDE specifics — human curation pass), add `qa-runner.ts` + `install.sh` + a discovery skill. (Gated on the CTO curation decision.)
- NEW `reviewlib/qa/harness.py`: discover `vscode-playwright-qa` (else `EXIT_QA_SUT_BOOT_FAILED=8`), shell to the TS runner with a JSON job; web→agent-browser, ext→Playwright/CDP. Activate the web/ext runbook + driver-selection in the prompt. `rig doctor` verifies the harness.

**Increment 5 (PREREQUISITE + harness) — bot, Tier-1 hermetic only. SHIPPED (review-cli #67).**
- SHIPPED: an IN-PROCESS hermetic fake Telegram Bot-API server (`reviewlib/qa/bot_harness.py`,
  stdlib `http.server`, loopback-only) instead of a compose service — it accepts
  `getUpdates`/`sendMessage`/handshake methods, lets the driver inject synthetic updates and
  capture outbound sends, with zero docker/network. The SUT bot is booted (the `sut.bot.command`
  in `qa.yaml`) with `TG_API_BASE` pointed at the fake, so it long-polls the fake; the
  DETERMINISTIC driver (`reviewlib/qa/bot_driver.py`) parses each `## Case:` block's
  `Send:`/`Expect:`/`Expect-no:`/`Expect-silent` grammar, injects the update, captures the reply,
  classifies PASS/FAIL, and emits the SAME `## QA RESULTS` contract the executor parser reads. A
  POSITIVE CAPABILITY PROBE (inject + require any outbound within the window) turns an un-patched
  sender into a LOUD BLOCKED with the `TG_API_BASE` pointer — closing the "zero sends false-pass"
  footgun. Fail-closed on a real-looking `TG_CHAT_ID`. The 2-fixture DoD
  (`tests/fixtures/qa/bot-{good,buggy}`) proves a good bot → PASS and a buggy bot → FAIL with a
  finding, deterministic in normal CI. NOTE: bot Tier-1 runs DETERMINISTICALLY (no un-caged
  agent) — the hermetic "send update → assert reply" assertion needs no write/exec agent, so it
  stays off the agent-cage blast radius entirely.
- The tg-cli `TG_API_BASE` outbound-sender fix is the prerequisite for testing tg-cli ITSELF as a
  bot SUT (a bot whose sender hardcodes `api.telegram.org` is detected by the probe and BLOCKED),
  but the harness ships independent of it — any bot whose sender honors `TG_API_BASE` is testable
  today. Tracked as a separate tg-cli PR.
- Tier 2 (Telethon MTProto + agent-browser Telegram Web, dedicated test account, test DC) DEFERRED behind a `requires_live_telegram` tag until credentials are provisioned.

Dependencies: 2 needs 1; 3 needs 2; 4 and the tg-cli fix in 5 can start in parallel with 1–3 (separate repos). k3s, YAML-frontmatter suite schema, and N-seat parallel testers are explicitly v2.

## Open issues — RESOLVE BEFORE BUILDING (adversarial review, verdict: needs-work)

This spec is a design, not a green light. An adversarial pass flagged the following; the
must-fix items are blocking for an implementer.

### Must fix before building
- Fix the timeout claim: PANEL_TIMEOUT_DEFAULT is 240 (config.py:93), not 1200, and 1200 is still too short for docker-build + Playwright + an LLM-driven suite. Give qa its own long default that leans on the <=4h backstop, and correct the spec's stated constant before anyone implements the 'one-line carve-out'.
- Design REAL teardown independent of backstop subprocess-reaping. backstop.py only SIGKILLs registered subprocess groups; it has no cleanup-callback hook and cannot run `docker compose down`. Add an explicit signal/atexit teardown that tears down by `-p` project name, plus a startup orphan-sweep, plus an UNCONDITIONAL teardown for the web/ext (agent-driven) path which currently has no deterministic owner at all. Until this exists, the 'never leaks containers / guaranteed teardown' guarantee is false.
- Resolve single-seat vs the shared model-resolution path. cli.py:1583+ resolves a non-review mode to a PANEL of DEFAULT_MODELS; the spec asserts qa is single-seat and ignores --pool/the board but gives no mechanism, while also promising 'no cli.py dispatch surgery.' One of those has to give — either qa modifies the model-resolution branch (admit the cli.py edit) or the handler hard-collapses ctx.models to one seat itself. Specify which.
- Hard-gate bot Tier 2 (real Telegram) behind more than a chat_id equality check: require an explicit opt-in flag/env, enforce Telegram TEST DC (not 'prefer'), maintain a test-chat allowlist, and document the account-ban risk of MTProto automation. The current single fail-closed compare is too thin for something that drives a real account.
- Add an OS-level sandbox for the un-caged executor on backend/bot SUTs (container/VM), and forbid --full-auto + --in-place against a repo with unpushed changes. The blast-radius control is currently a prompt sentence; the read-only modes were caged because prose isn't a boundary, and qa must not regress that to nothing. Also address prompt-injection from suite files / SUT README into the write-capable agent.
- Resolve the worktree-provisioning gap (open decision #2) before picking worktree as the default: specify how node_modules/build/.env get into the fresh tree (worktree-via-project-cli, a provision step, or copy), or the safe default is unusable and everyone falls back to the riskier --in-place.
- Pin down the suite parser contract: which exact heading(s) count as a case, validate it in --scaffold-env's emitted stub, and make the empty-file vs no-Case-block distinction not fire the 'no suites' teaching message on a non-empty authored file. Build an explicit verdict→exit truth table that keeps --strict-with-only-P3 distinct from FAIL (today both map to 10), and confirm code 10 isn't overloaded against how CI reads `review --strict`.

### Gaps
- TIMEOUT FACT IS WRONG AND THE FIX IS TOO SHORT. The spec says 'PANEL_TIMEOUT_DEFAULT (1200s is the review default)' and tells the implementer to 'treat an unset --timeout as the long 1200s default.' Verified: reviewlib/config.py:93 PANEL_TIMEOUT_DEFAULT = 240 (4 min), and 1200 is what `review` (NOT panel) gets at cli.py:1594. So (a) the spec misstates the constant, and (b) its own remedy of 1200s is nowhere near enough for `docker compose up --build` + `npx playwright install chromium` + an Electron VS Code boot (30-60s per the user's own CLAUDE.md) + a full suite run driven by an LLM. A real qa run is tens of minutes to hours. qa must get its OWN long default (lean on the <=4h backstop, not a 20-min cap), not '1200s'.
- BACKSTOP CANNOT REAP CONTAINERS — the 'guaranteed teardown' story is broken on the abnormal-exit path. §7.2 Phase 4 says teardown is guaranteed because it registers 'with reviewlib/backstop.py so a backstop-killed run still reaps containers.' Verified: backstop.py only does `process.kill_live_children()` — SIGKILL to registered backend SUBPROCESS groups. It has NO callback/atexit/on-fire hook (grep: zero Callable/callback/atexit/register-cleanup). Containers started with `docker compose up -d` are daemonized by the Docker daemon, in NO child process group of review. SIGKILLing review's children leaves every container running. The whole 'never leaks containers' claim collapses precisely in the case it was designed for (backstop fire / Ctrl-C / crash). Need a real signal/atexit teardown hook that runs `docker compose down`, independent of subprocess reaping, AND a startup orphan-sweep by `-p` project name.
- ESCAPED PROCESSES FROM THE UN-CAGED AGENT ARE UNBOUNDED. The agent itself runs `docker compose up`, dev servers, and (web/ext) Playwright/Electron. _run_streamed's timeout SIGKILLs only the child's process group; daemonized containers and detached dev servers escape it (the spec even acknowledges 'a leaked stdout fd held by an escaped/daemonized descendant'). For web/ext the agent drives Playwright DIRECTLY (not the deterministic Python env layer), so there is NO deterministic teardown owner for that path at all — `closeVSCode`/dev-server reaping depends entirely on the LLM remembering to call it. The spec's 'deterministic Python never leaks containers' guarantee only covers backend/bot; web/ext leak management is hand-waved to 'the harness handles process-group kill', which is only true if the agent actually invokes it.
- ModeContext HAS NO SEAM FOR sut_path / harness config. Verified contract.py ModeContext fields: args, models, diff, cwd, timeout, with_visual, visual_ctx, moderators, extra. The spec's handler signature ('reads ctx.args + ctx.cwd (resolved SUT) + ctx.models (single-seat tester) + ctx.timeout') assumes cwd is the resolved SUT and models is a single seat — but cwd is resolved by the SHARED _effective_cwd against -C, and `models` is resolved by the shared board/pool logic at cli.py:1583+ which for a non-review mode produces a PANEL of DEFAULT_MODELS, not a single seat. The spec asserts '--pool/the board are ignored for qa' and 'single-seat' but provides NO mechanism for that in the shared resolution path — qa will get the multi-model panel list unless cli.py's model-resolution branch is actually modified, which contradicts the 'no cli.py dispatch surgery' promise.
- THE WORKTREE-ISOLATION DEFAULT IS LIKELY UNUSABLE FOR THE REAL SUTS. Open decision #2 admits 'worktree ... may need node_modules/build provisioned in the new tree.' This isn't a minor caveat — a fresh `git worktree add` of a web/ext/node SUT has NO node_modules, NO build output, NO .env. The agent then has to `npm i` + build inside the worktree before it can boot anything, multiplying time and flakiness, OR the user falls back to --in-place (the riskier path) every time, making the safe default dead on arrival. The user's own CLAUDE.md flags worktree-via-project-cli for exactly this. The spec picks worktree as default without resolving how the tree gets provisioned.
- SUITE FORMAT IS UNDER-SPECIFIED AND THE PARSER CONTRACT IS AMBIGUOUS. §4 shows TWO heading conventions ('## Case: <title>' and '## <title>') and says the CASES tally counts '## Case:' blocks — but the example suite in §4 uses '## Case:' while the runbook prose says cases are sub-goals. If a human writes '## login flow' (no 'Case:' prefix) it parses to ZERO cases → exit 5 'no Case blocks', even though they authored a real suite. The 'WHAT/WHY/HOW' teaching message will then fire on a non-empty file, confusing the author. Either accept both headings for the count or make the required prefix unambiguous and validated by --scaffold-env output.
- STRICT-MODE EXIT-CODE MAPPING CONTRADICTS ITSELF. §6 says 'VERDICT: FAIL → 1 (or 10 under --strict)' AND 'strict flips ANY finding to 10.' But it also says non-strict P2/P3 findings → 0. So under --strict a PASS-verdict-with-P3-finding → 10, while the FAIL path also → 10 — collapsing 'tests passed but found nits' and 'tests failed' into the same code, defeating the spec's own stated goal (§2.4) of distinct exit classes. And the existing --strict semantics at cli.py:1076/1790 are for DIFF findings; reusing code 10 for qa findings may collide with how CI already interprets a review --strict 10. Needs an explicit truth table and a check that 10 isn't overloaded across modes.

### Footguns (it spawns servers, drives REAL Telegram, runs an un-caged agent on a dev machine)
- DRIVES A REAL TELEGRAM ACCOUNT WITH ONLY VERBAL FAIL-CLOSED. The bot Tier-2 safety rests on 'the harness MUST refuse to run if chat_id == real TG_CHAT_ID.' That's one equality check standing between a test run and spamming/mutating the user's real Telegram. There's no allowlist-of-test-chat-ids, no test-DC enforcement (it's 'preferred', not required), and MTProto user-account automation risks an account BAN from Telegram for ToS-violating automation — a throwaway number can be burned, but the SAME api_id/api_hash app credentials are reused and can be flagged. Tier 2 should be HARD-gated behind an explicit env/flag + a test-DC-only assertion, not just a chat_id compare. Treat 'fail-closed' as the LAST line, not the only one.
- TG_API_BASE TIER-1 PREREQUISITE IS UNMERGED AND THE DETECTION IS WEAK. Verified: tg:610 hardcodes `https://api.telegram.org/bot${BOT_TOKEN}` and grep shows tg reads TG_API_BASE NOWHERE; tg-ctl honors it (2194). So Tier 1 literally cannot capture outbound sends until a tg-cli PR lands. The spec's 'detect an un-patched sender (mock sees zero sendMessage) and fail' is fragile: zero captured sends is ALSO what a genuinely-silent bug looks like, so the harness can't distinguish 'tg unpatched' from 'bot correctly sent nothing' — it will either false-fail real silent-by-design cases or false-pass when the patch is missing. Need a positive capability probe (call tg once against the mock at setup and assert capture) before any case runs.
- THE COMPOSE FIXTURE IT WANTS TO 'REUSE THE SHAPE OF' IS RIDDLED WITH HARDCODED HOST PATHS. Verified docker-compose.e2e.yml bind-mounts `/Users/ultra/work/ext-test-projects`, `/Users/ultra/work/hyper-canvas-draft`, and an EXTENSION_PATH pointing at a specific `.claude/worktrees/HYP-342-preview-routing/...` worktree. None of this generalizes to an arbitrary SUT on another dev's machine. 'Reuse the shape' will tempt an implementer to copy these absolute paths. The generalization work (parameterize every mount/path) is real engineering the spec under-scopes as a one-liner.
- RUNS UN-CAGED claude/codex WITH BASH+BROWSER ON A DEV MACHINE, default in a worktree of the user's own repo. `codex exec -s workspace-write --full-auto` and Claude Code with tools enabled can do ANYTHING the user can: the only blast-radius control is 'stay inside {sut_path}', enforced by a PROMPT SENTENCE ('Never touch the user's other repos'), not by a sandbox. An LLM that misreads a path, follows a malicious instruction embedded in SUT test data/README (prompt injection from the suite files or the repo under test), or runs `rm`/`git push`/`docker` with a wrong arg has no OS-level containment. The read-only modes were caged precisely because prompts aren't a security boundary; qa removes the cage and replaces it with prose. At minimum: run inside a container/VM for backend SUTs, and never --full-auto on --in-place against a repo with unpushed work.
- --scaffold-env AND THE AGENT BOTH WRITE INTO THE SUT REPO. The agent 'authors missing mocks into docs/tests/env/ and re-runs', and --scaffold-env writes stubs. Even though qa 'never commits', it DOES write files into the user's working tree (or a worktree without their other deps). If run --in-place, the agent's scratch files + any half-written mock land in the user's actual checkout, polluting `git status` and risking accidental commit by a later human/agent. The 'never overwrites, idempotent' claim covers scaffold but NOT the free-form shims the tester writes mid-run.
- DOCKER/k3s STATE ON A SHARED DEV MACHINE: named volumes (`down -v` destroys data), port collisions with the dev's own running services, and `-p review-qa` project adoption ('adopt-if-healthy') could attach to a STALE project from a previous crashed run with corrupt state and run tests against it, reporting phantom bugs. The 'adopt-if-healthy / recreate-if-unhealthy' heuristic trusts a healthcheck to mean 'this is MY clean env,' which it doesn't.

---
*Generated by the `review-qa-design` agent workflow (6 agents: 4 facet designs → synthesis → adversarial critique), 2026-06-19. Grounded in verified review-cli / tg-cli / rig-cli / ext-test-projects facts.*
