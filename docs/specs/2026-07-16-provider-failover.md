# review-cli provider-failover — consolidated CTO requirements

Source: Alex's Telegram messages (2026-07-15/16), consolidated after he noted it had
been requested several times and kept regressing (opus→opencode infra-fail blocked a
review quorum). tg#8607, 8614, 8619, 8658, 8664, 8674, 8681, 8719, 8879.

## The model↔provider model

1. **NO 1-to-1 aliases.** A model maps to an ORDERED LIST of providers, tried in order.
   `opus` is not "one backend" — it's a model that several providers can serve.
2. **A "provider" = harness + service**, counted together as one unit. Examples:
   `oc:zai/glm-5.2` = opencode-harness + zai-service; a direct `zai:glm-5.2` = direct-zai;
   `claude:...`; `codex`; other `oc:*`. The SAME model (glm-5.2, opus, …) is reachable via
   MULTIPLE (harness+service) providers — list them all as alternatives per model.
3. **opus defaults to the `claude` provider, NOT `oc`/opencode.** The current bug: `-m opus`
   routes to `opencode -m opus`, whose backend infra-fails → the model is unusable and the
   quorum can't form. opus's first/default provider must be `claude` (claude:claude-opus-4-8).

## Dynamic failover (the core)

4. **Live, mid-review, in-flight switchover.** When a model's current provider fails DURING
   a review call — OR that model is (temporarily or fully) unavailable in that provider — OR
   the WHOLE provider/service is down — switch that model to its NEXT provider and the review
   CONTINUES SEAMLESSLY (the seat does not abort, the board does not degrade). If a whole
   provider/service outage hits, EVERY model that was using it reroutes to its alternates.
5. **Cache the last-working provider per model** (persist, e.g. `~/.cache/review-cli/`); try
   it first next time; rotate on failure.

## Retry + skip (the cascade)

6. **Universal retry-with-backoff for TRANSIENT errors — provider-agnostic, one central
   classifier, NOT ad-hoc per provider.** Before failing over, retry the same provider a few
   times with exponential backoff on: network timeouts (z.ai read-timeouts), DNS failures,
   5xx, connection resets, 429. Permanent errors (400 insufficient-credits, 401/403 auth) do
   NOT retry — skip/fail fast.
   Full cascade: **transient → retry-with-backoff (same provider) → FAILOVER (next provider) →
   skip if unpaid → targeted per-provider error when all exhausted.**
7. **Skip dead/unpaid providers up-front** (never dispatched, never failed-over into):
   `commandcode` (insufficient credits), `gemini` (deprecated + went paid; host no longer
   resolves — fully disabled, not just failed-over).

## Foolproofing (pool/model selection UX)

8. When explicit `-m` narrows the set and some are down and the pool can't converge, and when
   `--pool N` > number of live models:
   - Do NOT fail opaquely — PROPOSE not overriding: use the default models (nothing specified)
     or a preset. For EACH proposal SHOW the models that would run, annotated per-model
     LIVE/DEAD + reason (e.g. "commandcode: insufficient credits (400)", "opus-via-oc: backend
     infra error"). Show ONLY proposals that fit the requested pool size.
   - If even the default board can't converge → FAIL with a TARGETED error enumerating
     per-provider what's wrong (which need credit top-up, which are down/auth, etc.).

## Implementation constraints

- Develop BRANCH-based; never break the live installed `review` (concurrent agents run
  quorums with it). Swap in only when its own tests pass green. (WIP already exists on branch
  `feat/foolproof-pool-selection` from an earlier agent that hit its weekly limit.)
- Config-file changes (pool size, unpaid_providers) are safe to apply live; CODE changes are
  branch-gated.
- Tests: provider-A-fails→falls-over-to-B (mid-review, review completes); opus defaults to
  claude; whole-service outage reroutes all its models; transient retries-then-succeeds;
  permanent (400/401) no-retry; last-working cached & tried first; foolproofing proposals show
  per-model live/dead+reason.
