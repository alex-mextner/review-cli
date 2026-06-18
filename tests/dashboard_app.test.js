/* Unit tests for the dashboard SPA's pure resolution/filter logic (review-cli#45).
 *
 * The asset overhaul (PR #43) added non-trivial pure logic in
 * reviewlib/dashboard/assets/app.js with zero coverage — the fable/claude tie and the
 * `proto3`-must-not-match-`o3` substring guard were caught only by manual Playwright.
 * These tests exercise the EXACT functions the browser runs (required via app.js's guarded
 * `module.exports` footer — no drifting copy).
 *
 * Run: `node --test tests/dashboard_app.test.js` (Node's built-in test runner, no deps).
 * Invoked by tests/smoke.py when `node` is on PATH (skipped loudly otherwise).
 *
 * app.js is a browser script (it calls `window.onImgError = …` and
 * `document.addEventListener` at top level). We stub `window`/`document` BEFORE requiring it
 * so any top-level DOM access is a harmless no-op under Node — using a recursive no-op Proxy
 * so a FUTURE top-level DOM call added to app.js can't crash the whole JS suite at require()
 * (the stub isn't pinned to today's exact two browser calls).
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

// A recursive no-op stub: any property read returns another no-op (callable) Proxy, and any
// call returns one too — so `document.addEventListener(...)`, `window.onImgError = …`, or any
// other top-level browser access during module load is harmless. The pure functions under
// test never touch the DOM when CALLED with our inputs, so this only needs to survive
// module-eval, not emulate a real DOM.
function noopProxy() {
  const fn = () => noopProxy();
  return new Proxy(fn, {
    get: (_t, prop) => (prop === Symbol.toPrimitive ? () => '' : noopProxy()),
    apply: () => noopProxy(),
    set: () => true,
  });
}
global.window = global.window || noopProxy();
global.document = global.document || noopProxy();

const APP = path.join(__dirname, '..', 'reviewlib', 'dashboard', 'assets', 'app.js');
const { resolveModel, filteredRuns, monogram, cap, state } = require(APP);

test('resolveModel: exact family hit resolves logo + label', () => {
  const m = resolveModel('opus');
  assert.equal(m.key, 'opus');
  assert.equal(m.logo, 'claude'); // every Anthropic seat → the Claude mark
  assert.equal(m.label, 'Opus');
});

test('resolveModel: gateway prefix is stripped (commandcode:/zai:/oc:)', () => {
  assert.equal(resolveModel('zai:glm-5.2').label, 'GLM');
  assert.equal(resolveModel('commandcode:Qwen/Qwen3.7-Max').key, 'qwen');
  assert.equal(resolveModel('oc:opencode/deepseek-v4-flash-free').key, 'deepseek');
});

test('resolveModel: vendor path is stripped after the gateway prefix', () => {
  // `commandcode:Qwen/Qwen3.7-Max` → drop `commandcode:` → drop `Qwen/` → `qwen3.7-max`
  // → boundary match on `qwen`.
  const m = resolveModel('commandcode:Qwen/Qwen3.7-Max');
  assert.equal(m.key, 'qwen');
  assert.equal(m.label, 'Qwen');
});

test('resolveModel: specific beats generic — claude-fable-5 → Fable, not Claude', () => {
  // The regression the manual Playwright pass caught: `fable` (specific) must win over the
  // GENERIC `claude` even though both appear in the id.
  const m = resolveModel('claude-fable-5');
  assert.equal(m.key, 'fable');
  assert.equal(m.label, 'Fable');
});

test('resolveModel: token-boundary guard — proto3 must NOT match o3', () => {
  // A bare `includes('o3')` would false-positive on `proto3` → OpenAI/Codex. The boundary
  // rule (family must sit at start or after a non-alphanumeric separator) forbids that.
  const m = resolveModel('proto3');
  assert.notEqual(m.key, 'o3');
  assert.equal(m.logo, null); // unknown → monogram, no brand logo
});

test('resolveModel: boundary match DOES fire on a real separator (gpt-5.5 → GPT)', () => {
  const m = resolveModel('gpt-5.5');
  assert.equal(m.key, 'gpt');
  assert.equal(m.logo, 'codex');
  assert.equal(m.label, 'GPT');
});

test('resolveModel: suffixed ids resolve to the right family', () => {
  assert.equal(resolveModel('opus-4-8').key, 'opus');
  assert.equal(resolveModel('glm-5.2').key, 'glm');
});

test('resolveModel: empty / null input yields the placeholder', () => {
  assert.deepEqual(resolveModel(''), { key: '', logo: null, label: '—' });
  assert.deepEqual(resolveModel(null), { key: '', logo: null, label: '—' });
  assert.deepEqual(resolveModel(undefined), { key: '', logo: null, label: '—' });
});

test('resolveModel: longest match within a tier wins (two boundary hits, longer key wins)', () => {
  // Both `kimi` (4) and `mistral` (7) sit on a token boundary in this id and are the SAME
  // tier (neither is generic). The LONGER family must win — pins the within-tier length rank.
  const m = resolveModel('kimi-mistral-x');
  assert.equal(m.key, 'mistral');
  assert.equal(m.label, 'Mistral');
});

test('resolveModel: o3 DOES match on a real token boundary (positive case)', () => {
  // The mirror of the proto3 guard: `o3` after a `-`/`/` separator MUST resolve (the boundary
  // rule fires, it is not disabled outright). Covers both a bare id and a gateway+vendor path.
  assert.equal(resolveModel('gpt-o3').key, 'gpt'); // gpt wins (longer, also at boundary)
  assert.equal(resolveModel('oc:openai/model-o3').key, 'o3'); // o3 on the `-` boundary
  assert.equal(resolveModel('model-o3').logo, 'codex'); // resolves to the OpenAI mark
});

test('resolveModel: unknown model → no logo, capitalized label (monogram fallback)', () => {
  const m = resolveModel('totallyunknownmodel');
  assert.equal(m.logo, null);
  assert.equal(m.label, 'Totallyunknownmodel');
});

// --- filteredRuns -----------------------------------------------------------
// filteredRuns reads state.filterModel / state.filterRole (the module `state` object),
// so each test sets them on the exported `state` and resets after.
function withFilters(model, role, fn) {
  const prevModel = state.filterModel;
  const prevRole = state.filterRole;
  state.filterModel = model;
  state.filterRole = role;
  try {
    fn();
  } finally {
    state.filterModel = prevModel;
    state.filterRole = prevRole;
  }
}

const RUNS = [
  { models: ['opus-4-8', 'gpt-5.5'], mode: 'diff' },
  { models: ['zai:glm-5.2'], mode: 'quorum' },
  { models: ['oc:opencode/deepseek-v4'], mode: 'brainstorm', topic: 'caching' },
  { models: [], mode: 'just-ask' },
];

test('filteredRuns: no filter returns all runs unchanged', () => {
  withFilters(null, null, () => {
    assert.equal(filteredRuns(RUNS).length, RUNS.length);
  });
});

test('filteredRuns: exact model id match keeps the run', () => {
  withFilters('zai:glm-5.2', null, () => {
    const out = filteredRuns(RUNS);
    assert.equal(out.length, 1);
    assert.deepEqual(out[0].models, ['zai:glm-5.2']);
  });
});

test('filteredRuns: family-rollup match — filtering by a sibling id of the same family', () => {
  // Filtering by `claude-fable-5` (family Claude via fable→… actually fable) — use an
  // Anthropic id that rolls up to the same family-key as `opus-4-8`. `opus` and `fable`
  // are DIFFERENT keys, so pick a same-key case: filter `opus` vs run `opus-4-8`.
  withFilters('opus', null, () => {
    const out = filteredRuns(RUNS);
    assert.equal(out.length, 1);
    assert.ok(out[0].models.includes('opus-4-8'));
  });
});

test('filteredRuns: a model nobody used yields an empty list', () => {
  withFilters('mistral', null, () => {
    assert.equal(filteredRuns(RUNS).length, 0);
  });
});

test('filteredRuns: role filter scopes to brainstorm runs (no per-role data in summary)', () => {
  // The role filter narrows to brainstorm runs (mode === 'brainstorm' OR a topic), since
  // the per-summary payload carries no role list.
  withFilters(null, 'architect', () => {
    const out = filteredRuns(RUNS);
    assert.equal(out.length, 1);
    assert.equal(out[0].mode, 'brainstorm');
  });
});

test('filteredRuns: model AND role filters compose', () => {
  withFilters('deepseek', 'architect', () => {
    const out = filteredRuns(RUNS);
    assert.equal(out.length, 1);
    assert.equal(out[0].mode, 'brainstorm');
  });
});

test('filteredRuns: null/undefined runs list is tolerated', () => {
  withFilters(null, null, () => {
    assert.deepEqual(filteredRuns(null), []);
    assert.deepEqual(filteredRuns(undefined), []);
  });
});

// --- monogram / cap (small helpers resolveModel leans on) --------------------
test('monogram: two words → first letters; one word → first two chars', () => {
  assert.equal(monogram('MiniMax Pro'), 'MP');
  assert.equal(monogram('MiniMax'), 'MI');
  assert.equal(monogram('z.ai'), 'ZA'); // splits on non-alphanumeric, no stray punctuation
  assert.equal(monogram(''), '?');
});

test('cap: capitalizes the first character only', () => {
  assert.equal(cap('qwen'), 'Qwen');
  assert.equal(cap(''), '');
});
