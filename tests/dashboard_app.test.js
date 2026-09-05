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
const { resolveModel, filteredRuns, monogram, cap, state, PANELS, fmtDur, render, STATS_ONLY_PANELS, loadAll } = require(APP);

// MUST run before any other test mutates `state` — pins the true module-load initializer,
// not a value some other test's cleanup happened to restore. render()'s entire loading
// guard (panelDataMissing()) is a strict `=== null` check: if a future edit ever changed the
// `state` literal's `runs`/`stats` fields to `undefined` (or omitted them), the guard would
// silently stop firing at boot — `panelDataMissing()` would return `false` before the first
// load ever resolves, and every panel would render against absent data (the exact
// false-empty bug this whole file exists to prevent), with every OTHER test in this suite
// still green because they all set runs/stats explicitly via withRenderState.
test('state: runs/stats initialize to null (not undefined) — required for panelDataMissing()\'s boot-time guard', () => {
  assert.equal(state.runs, null);
  assert.equal(state.stats, null);
});

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

test('resolveModel: Sol seat resolves specifically, not generic GPT', () => {
  const m = resolveModel('codex:gpt-5.6-sol');
  assert.equal(m.key, 'gpt-5.6-sol');
  assert.equal(m.logo, 'codex');
  assert.equal(m.label, 'Sol');
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
function withFilters(model, role, fn, task = null) {
  const prevModel = state.filterModel;
  const prevRole = state.filterRole;
  const prevTask = state.filterTask;
  state.filterModel = model;
  state.filterRole = role;
  state.filterTask = task;
  try {
    fn();
  } finally {
    state.filterModel = prevModel;
    state.filterRole = prevRole;
    state.filterTask = prevTask;
  }
}

const RUNS = [
  { models: ['opus-4-8', 'gpt-5.5'], mode: 'diff', task_code: 'HYP-742' },
  { models: ['zai:glm-5.2'], mode: 'quorum', task_code: 'HYP-742' },
  { models: ['oc:opencode/deepseek-v4'], mode: 'brainstorm', topic: 'caching', task_code: 'HYP-999' },
  { models: [], mode: 'just-ask', task_code: null },
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

test('filteredRuns: task filter keeps only matching task iterations', () => {
  withFilters(null, null, () => {
    const out = filteredRuns(RUNS);
    assert.equal(out.length, 2);
    assert.deepEqual(out.map((r) => r.task_code), ['HYP-742', 'HYP-742']);
  }, 'HYP-742');
});

test('PANELS.tasks: active task filter scopes task groups and related runs', () => {
  const prevStats = state.stats;
  const prevRuns = state.runs;
  try {
    withFilters(null, null, () => {
      state.runs = [
        { session_id: 's1', started: '2026-06-01T10:00:00Z', models: ['codex'], mode: 'review', task_code: 'HYP-742' },
        { session_id: 's2', started: '2026-06-01T11:00:00Z', models: ['gemini'], mode: 'review', task_code: 'HYP-999' },
      ];
      state.stats = {
        tasks: [
          { task_code: 'HYP-742', iterations: 1, models: ['codex'], modes: ['review'], last_started: '2026-06-01T10:00:00Z' },
          { task_code: 'HYP-999', iterations: 1, models: ['gemini'], modes: ['review'], last_started: '2026-06-01T11:00:00Z' },
        ],
      };
      const html = PANELS.tasks();
      assert.match(html, /HYP-742/);
      assert.doesNotMatch(html, /HYP-999/);
    }, 'HYP-742');
  } finally {
    state.stats = prevStats;
    state.runs = prevRuns;
  }
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

// --- fmtDur (Metrics tab duration formatting) --------------------------------
test('fmtDur: does not round a seconds-remainder up to a bogus "60s" in the minutes branch', () => {
  // 1199.6s = 19m + 59.6s. Rounding the remainder in isolation used to yield
  // `Math.round(59.6) === 60`, rendering "19m 60s" instead of "20m 0s".
  assert.equal(fmtDur(1199.6), '20m 0s');
});

test('fmtDur: does not render a bogus "60.0s" in the sub-minute branch either', () => {
  // Same bug class, one branch over: 59.96s is < 60 so the OLD code took the `toFixed(1)`
  // path unconditionally and printed "60.0s" — a value that reads as a whole minute but
  // isn't formatted as one. Rounding to one decimal BEFORE branching sends it to the
  // minutes branch instead.
  assert.equal(fmtDur(59.96), '1m 0s');
  assert.equal(fmtDur(59.94), '59.9s'); // just under the boundary: stays sub-minute
});

test('fmtDur: ordinary sub-minute and multi-minute values are unaffected', () => {
  assert.equal(fmtDur(null), '—');
  assert.equal(fmtDur(5.25), '5.3s');
  assert.equal(fmtDur(90), '1m 30s');
  assert.equal(fmtDur(3600), '60m 0s');
});

test('fmtDur: does not double-round the minutes-branch total (review bug caught in review)', () => {
  // Rounding `s` to one decimal for the sub-minute check, then rounding THAT again to an
  // integer, can push a value already rounded onto a ".5" boundary the wrong way:
  // 90.47 -> round to one decimal -> 90.5 -> round again -> 91 -> "1m 31s", a second later
  // than the true "1m 30s" a single `Math.round(90.47) === 90` gives. The fix rounds the
  // ORIGINAL value once for the minutes/seconds decomposition.
  assert.equal(fmtDur(90.47), '1m 30s');
  assert.equal(fmtDur(60.47), '1m 0s');
});

// --- render (top-level panel dispatch) ---------------------------------------
// A minimal recording stub for `document` — the file-level noopProxy stub can't tell us WHAT
// got written to `#panel`, only that a write happened, which would let a bare early `return`
// (leaving the PREVIOUS tab's stale HTML on screen) pass a "panel fn not called" assertion
// just as well as showing the loading placeholder does. Recording the actual innerHTML pins
// the real contract: not just "no panel renderer ran", but "the loading placeholder shows".
function withRecordedPanel(fn) {
  const prevDocument = global.document;
  const writes = {};
  global.document = {
    getElementById: (id) => ({
      set innerHTML(v) {
        writes[id] = v;
      },
      get innerHTML() {
        return writes[id] || '';
      },
      setAttribute() {},
    }),
    querySelectorAll: () => [],
  };
  try {
    fn();
  } finally {
    global.document = prevDocument;
  }
  return writes;
}

// Runs `body` with `state.panel`/`state.runs`/`state.detail` set as given, restoring the
// prior values afterwards — shared by the render tests below so a future addition to what
// render() reads from `state` (e.g. `state.manualSeed`) needs updating in one place.
function withRenderState({ panel, runs, detail = null, stats = undefined, loadError = undefined }, body) {
  const prev = { panel: state.panel, runs: state.runs, detail: state.detail, stats: state.stats, loadError: state.loadError };
  state.panel = panel;
  state.runs = runs;
  state.detail = detail;
  // `stats`/`loadError` are optional — most render() tests don't care about them, but the
  // guard also gates stats-only panels on `state.stats` and, when data is missing, on
  // `state.loadError` (a failed load vs. one still in flight), so tests exercising those
  // paths must set them explicitly.
  if (stats !== undefined) state.stats = stats;
  if (loadError !== undefined) state.loadError = loadError;
  try {
    body();
  } finally {
    state.panel = prev.panel;
    state.runs = prev.runs;
    state.detail = prev.detail;
    state.stats = prev.stats;
    state.loadError = prev.loadError;
  }
}

test('render: shows the loading placeholder (not a panel rendered against null data) while a runs reload is in flight', () => {
  const prevErrors = PANELS.errors;
  let called = false;
  PANELS.errors = () => {
    called = true;
    return '<div>should not render while data is reloading</div>';
  };
  try {
    withRenderState({ panel: 'errors', runs: null }, () => {
      const writes = withRecordedPanel(render);
      // The bug: switching tabs mid-reload used to call PANELS.errors() with `state.runs ===
      // null`, and every runs-reading panel falls back to `(state.runs || [])`, silently
      // rendering a false "no errors / no sessions" panel instead of a loading state.
      assert.equal(called, false, 'render() must not invoke a panel renderer while state.runs is null');
      assert.match(writes.panel || '', /class="loading"/, 'render() must show the loading placeholder, not leave stale/empty content');
    });
  } finally {
    PANELS.errors = prevErrors;
  }
});

test('render: shows the actual error, not an eternal "Loading…", after a failed reload (state.loadError set)', () => {
  // Raised independently three times across review rounds: state.runs===null is ALSO true
  // after a failed loadAll() (its catch has nothing else to put there), which the bare
  // guard couldn't tell apart from "a reload is genuinely in flight" — so a single failed
  // fetch pinned every runs-reading (and, before state.stats loaded once, every stats-only)
  // tab on a "Loading…" that was never going to resolve until an unrelated SSE event
  // happened to fire another attempt. loadAll() now records the failure in state.loadError;
  // render() must surface THAT instead of the generic placeholder when it's set.
  const prevErrors = PANELS.errors;
  let called = false;
  PANELS.errors = () => {
    called = true;
    return '<div>should not render — the last load failed</div>';
  };
  try {
    withRenderState({ panel: 'errors', runs: null, loadError: 'HTTP 500' }, () => {
      const writes = withRecordedPanel(render);
      assert.equal(called, false);
      assert.match(writes.panel || '', /Failed to load data/);
      assert.match(writes.panel || '', /HTTP 500/);
      assert.doesNotMatch(writes.panel || '', /class="loading"/, 'a failed load must not render as a bare "Loading…" — that reads as progress, not a dead end');
    });
  } finally {
    PANELS.errors = prevErrors;
  }
});

test('render: a genuinely in-flight reload (no loadError) still shows the plain loading placeholder', () => {
  withRenderState({ panel: 'errors', runs: null, loadError: null }, () => {
    const writes = withRecordedPanel(render);
    assert.match(writes.panel || '', /class="loading"/);
    assert.doesNotMatch(writes.panel || '', /Failed to load data/);
  });
});

// --- loadAll (fetch orchestration + the toast-vs-error failure split) -------
// A `document` stub covering everything loadAll()'s success/failure paths touch: `render()`'s
// `#panel` write (same shape as withRecordedPanel) PLUS `toast()`'s `#toast` element
// (getElementById returns null once so toast() takes its create-and-append branch,
// thereafter returns the created stand-in).
async function withLoadAllHarness(fetchImpl, body) {
  const prevDocument = global.document;
  const prevFetch = global.fetch;
  const writes = { panel: '' };
  let toastEl = null;
  global.document = {
    getElementById: (id) => {
      if (id === 'panel') {
        return {
          set innerHTML(v) {
            writes.panel = v;
          },
          get innerHTML() {
            return writes.panel;
          },
          setAttribute() {},
          querySelectorAll: () => [],
        };
      }
      if (id === 'toast') return toastEl;
      return null;
    },
    createElement: () => {
      toastEl = {
        id: 'toast',
        className: '',
        textContent: '',
        classList: { add() {}, remove() {} },
        _t: null,
      };
      return toastEl;
    },
    body: { appendChild() {} },
    querySelectorAll: () => [],
  };
  global.fetch = fetchImpl;
  try {
    // MUST await here, not just `return body(...)`: body is async (it awaits loadAll()
    // inside), so a bare `return` would let this function's `finally` restore
    // global.document/global.fetch BEFORE loadAll()'s internal fetch/render calls actually
    // run against them — a classic try/finally-around-a-promise footgun that silently swaps
    // the stubs out from under the very call this harness exists to sandbox.
    return await body({ writes, getToastText: () => (toastEl ? toastEl.textContent : null) });
  } finally {
    global.document = prevDocument;
    global.fetch = prevFetch;
  }
}

function jsonResponse(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(data) });
}

test('loadAll: a failed refresh with data ALREADY shown toasts the failure AND records loadError (for a later, different, data-missing tab) without repainting the current panel', async () => {
  // The bug this pins: the refresh button's onclick calls loadAll() WITHOUT nulling
  // state.runs/state.stats first (unlike invalidate()/scheduleReload()), so a failure there
  // used to leave `dataMissing === false` — the guard branch (gated on data being missing)
  // never ran, and NOTHING recorded or surfaced the failure at all. A failed refresh looked
  // identical to a successful no-op one. state.loadError is recorded UNCONDITIONALLY (a
  // later review round caught the narrower version of this fix: if the user switches to a
  // DIFFERENT, data-missing panel while this fetch is in flight, THAT panel's render() must
  // be able to see this failure too — see the cross-tab regression test below) — but the
  // panel actually on screen right now already has valid data, so it is not re-rendered.
  await withLoadAllHarness(
    () => Promise.reject(new Error('network down')),
    async ({ writes, getToastText }) => {
      const prevRuns = state.runs,
        prevStats = state.stats,
        prevError = state.loadError,
        prevPanel = state.panel;
      state.panel = 'errors';
      state.runs = []; // data already loaded once (a prior successful load)
      state.stats = { session_count: 1 };
      state.loadError = null;
      try {
        await loadAll();
        assert.equal(state.loadError, 'network down');
        assert.match(getToastText() || '', /refresh failed/);
        assert.match(getToastText() || '', /network down/);
        assert.equal(writes.panel, '', 'the currently-shown (still-valid) panel must not be repainted just because a background refresh failed');
      } finally {
        state.runs = prevRuns;
        state.stats = prevStats;
        state.loadError = prevError;
        state.panel = prevPanel;
      }
    },
  );
});

test('loadAll: a failed reload on a STATS-ONLY panel toasts too, even though scheduleReload() only nulled state.runs', async () => {
  // The bug this pins (caught in review): scheduleReload()/invalidate() null ONLY
  // state.runs, never state.stats. A blanket `runs !== null && stats !== null` read as
  // "hadDataBefore" would see runs===null here and conclude "no data yet" for a stats-only
  // tab whose OWN data (state.stats) is perfectly valid — landing in the worst of both
  // branches: no toast (hadDataBefore false) AND no visible error either, since render()'s
  // guard judges stats-only panels by state.stats alone and finds it non-null. A failed
  // background reload would go completely unreported while the user stares at a frozen
  // Metrics/Stats/Models tab. hadDataBefore must be judged per-panel, the same way
  // render()'s own guard is.
  for (const panel of STATS_ONLY_PANELS) {
    await withLoadAllHarness(
      () => Promise.reject(new Error('stats fetch failed')),
      async ({ writes, getToastText }) => {
        const prevRuns = state.runs,
          prevStats = state.stats,
          prevError = state.loadError,
          prevPanel = state.panel;
        state.panel = panel;
        state.runs = null; // scheduleReload() already nulled this before calling loadAll()
        state.stats = { session_count: 1 }; // ...but never touches this
        state.loadError = null;
        try {
          await loadAll();
          assert.match(getToastText() || '', /stats fetch failed/, `PANELS.${panel}'s still-valid stats snapshot means this failure must toast, not go silent`);
          assert.equal(state.loadError, 'stats fetch failed');
          assert.equal(writes.panel, '', 'the still-valid stats panel must not be repainted either');
        } finally {
          state.runs = prevRuns;
          state.stats = prevStats;
          state.loadError = prevError;
          state.panel = prevPanel;
        }
      },
    );
  }
});

test('loadAll: a failed reload is visible even after the user switches to a DIFFERENT, data-missing tab mid-fetch', async () => {
  // The bug this pins (caught in review): hadDataBefore snapshots the panel active when
  // loadAll() STARTS. Concrete scenario: user is on Stats (has valid state.stats) when an
  // SSE event nulls state.runs and starts this call — hadDataBefore reads true for Stats.
  // Before the fetch settles, the user switches to Errors (a runs-reading panel); render()
  // immediately shows "Loading…" since state.runs is null. When the fetch then fails, if
  // the catch only toasted (because the STARTING panel had data) and never recorded
  // state.loadError, the Errors tab the user is NOW looking at would be stuck on that
  // "Loading…" forever — a 1.8s toast for a tab they're not even on is the only clue.
  // loadError must be recorded unconditionally so whichever panel is ACTUALLY missing data
  // when this fails can surface it.
  await withLoadAllHarness(
    () => Promise.reject(new Error('server down')),
    async ({ writes }) => {
      const prevRuns = state.runs,
        prevStats = state.stats,
        prevError = state.loadError,
        prevPanel = state.panel;
      // Started on Stats, with valid stats but runs already nulled by the SSE handler.
      state.panel = 'stats';
      state.runs = null;
      state.stats = { session_count: 1 };
      state.loadError = null;
      try {
        // Simulate the mid-fetch tab switch: by the time the (rejected) fetch settles,
        // the user is on a runs-reading panel that has never loaded runs at all.
        const p = loadAll();
        state.panel = 'errors';
        await p;
        assert.equal(state.loadError, 'server down', 'the failure must be recorded regardless of which panel was active when the fetch STARTED');
        assert.match(writes.panel, /Failed to load data/, 'the panel the user is ACTUALLY looking at now must surface the failure, not sit on an eternal Loading…');
        assert.match(writes.panel, /server down/);
      } finally {
        state.runs = prevRuns;
        state.stats = prevStats;
        state.loadError = prevError;
        state.panel = prevPanel;
      }
    },
  );
});

test('loadAll: judges data-presence at CATCH time, not a stale snapshot from before the fetch started', async () => {
  // The exact bug this fix closes (caught in review): if data-presence were captured in a
  // variable BEFORE the fetch started, that snapshot goes stale the moment something else
  // changes state.runs/state.stats while THIS call is still in flight (e.g. an overlapping
  // loadAll() call settling first). This call starts with data ABSENT, but something else
  // populates it WHILE this fetch is still in flight — deterministically simulated here by
  // mutating state directly mid-fetch (a real overlapping loadAll() call landing here would
  // do the exact same mutation, just via its own success path; using a direct mutation
  // removes any dependency on this test's own microtask/promise-settling order matching
  // production's, which isn't something either this diff or this test needs to pin). A
  // start-time snapshot would still say "no data" here and fail SILENTLY — no toast (its
  // stale snapshot said absent) and no render (live state says present). Checking
  // panelDataMissing() live, in the catch, is what makes this toast correctly instead.
  await withLoadAllHarness(
    () =>
      new Promise((_resolve, reject) => {
        // Mutate state WHILE the fetch is in flight (before this call's own catch runs) —
        // simulates "something else" (another completed load, a manual edit) populating
        // the data this call itself started without.
        state.runs = [9, 9, 9];
        state.stats = { session_count: 1 };
        reject(new Error('failed after data was populated mid-flight'));
      }),
    async ({ writes, getToastText }) => {
      const prevRuns = state.runs,
        prevStats = state.stats,
        prevError = state.loadError,
        prevPanel = state.panel;
      state.panel = 'errors';
      state.runs = null; // data ABSENT when this call starts
      state.stats = null;
      state.loadError = null;
      try {
        await loadAll();
        assert.equal(state.loadError, 'failed after data was populated mid-flight');
        assert.match(getToastText() || '', /failed after data was populated mid-flight/, 'must toast — by catch time, live state has data, regardless of what was true when this call STARTED');
        assert.equal(writes.panel, '', 'the now-populated panel must not be repainted just because this particular call failed');
      } finally {
        state.runs = prevRuns;
        state.stats = prevStats;
        state.loadError = prevError;
        state.panel = prevPanel;
      }
    },
  );
});

test('loadAll: a failed load with NO data yet records state.loadError so render() can surface it', () => {
  return withLoadAllHarness(
    () => Promise.reject(new Error('HTTP 500')),
    async ({ writes }) => {
      const prevRuns = state.runs,
        prevStats = state.stats,
        prevError = state.loadError,
        prevPanel = state.panel;
      state.panel = 'errors';
      state.runs = null;
      state.stats = null;
      state.loadError = null;
      try {
        await loadAll();
        assert.equal(state.loadError, 'HTTP 500');
        assert.match(writes.panel, /Failed to load data/);
        assert.match(writes.panel, /HTTP 500/);
      } finally {
        state.runs = prevRuns;
        state.stats = prevStats;
        state.loadError = prevError;
        state.panel = prevPanel;
      }
    },
  );
});

test('loadAll: nulling state.runs and calling loadAll() (the invalidate()/scheduleReload() pattern) clears a stale loadError with no gap — raised in review, verified NOT a real bug', () => {
  // Reviewed concern: could a stale state.loadError from an earlier failed refresh survive
  // into the null-runs window that invalidate()/scheduleReload() open, and get painted by
  // render() for a fetch that is actually freshly in flight? Both call sites null
  // state.runs and call loadAll() back-to-back with NO `await` in between (invalidate():
  // `state.runs = null; loadAll();` — scheduleReload()'s timeout callback: identical
  // shape). loadAll() is an async function, but its body runs SYNCHRONOUSLY up to its
  // first `await` — and `state.loadError = null` is one of the first statements, before
  // the fetch's `await`. So there is no yield point, and therefore no render() call, between
  // "runs goes null" and "loadError goes null" — this test pins that with a fetch that
  // NEVER resolves (so if there WERE a gap, it would show up as loadError still being
  // stale immediately after the synchronous call to loadAll(), before anything settles).
  const prevFetch = global.fetch;
  const prevRuns = state.runs,
    prevStats = state.stats,
    prevError = state.loadError,
    prevPanel = state.panel;
  global.fetch = () => new Promise(() => {}); // never resolves — isolates the SYNCHRONOUS prefix
  try {
    state.panel = 'errors';
    state.runs = [1, 2, 3];
    state.stats = { session_count: 1 };
    state.loadError = 'stale from an earlier failed refresh';
    // Exact shape of invalidate() / scheduleReload()'s timeout callback: null runs, then
    // call loadAll() WITHOUT awaiting it (matches production — neither call site awaits).
    state.runs = null;
    loadAll();
    assert.equal(state.loadError, null, 'loadError must already be cleared synchronously, before the fetch even settles');
  } finally {
    global.fetch = prevFetch;
    state.runs = prevRuns;
    state.stats = prevStats;
    state.loadError = prevError;
    state.panel = prevPanel;
  }
});

test('loadAll: a successful load clears a stale loadError from a previous failed attempt and actually renders — not a swallowed post-success throw', () => {
  // A prior review round caught a subtler bug this test alone couldn't have: if `render()`/
  // `updateTabBadges()` had still lived INSIDE the try (they no longer do), a throw there
  // AFTER a successful fetch would land in the catch, which — reading `hadDataBefore` from
  // the just-assigned runs/stats — would misclassify "fetch succeeded, rendering crashed" as
  // "we already had data" and silently toast instead of surfacing anything, leaving the panel
  // frozen. Asserting the panel actually got written (not just that loadError went null)
  // and that nothing was toasted pins that the success path is genuinely unguarded now.
  return withLoadAllHarness(
    () => jsonResponse([]),
    async ({ writes, getToastText }) => {
      const prevRuns = state.runs,
        prevStats = state.stats,
        prevError = state.loadError,
        prevPanel = state.panel;
      state.panel = 'overview';
      state.runs = null;
      state.stats = null;
      state.loadError = 'stale failure from a previous attempt';
      try {
        await loadAll();
        assert.equal(state.loadError, null);
        assert.deepEqual(state.runs, []);
        assert.notEqual(writes.panel, '', 'a successful load must actually render the panel');
        assert.equal(getToastText(), null, 'a successful load must not toast anything');
      } finally {
        state.runs = prevRuns;
        state.stats = prevStats;
        state.loadError = prevError;
        state.panel = prevPanel;
      }
    },
  );
});

test('loadAll: an exception from render() AFTER a successful fetch is NOT swallowed as "refresh failed, data already shown"', () => {
  // Direct regression test for the finding above: force PANELS.overview (the panel render()
  // dispatches to) to throw, with data already populated before the call — the exact
  // condition that used to make the old (wider) try/catch misclassify a render-time crash as
  // a fetch failure and silently toast it away, freezing the panel on stale/loading content.
  // With the try narrowed to only the fetch, this must propagate as a real rejection instead.
  const prevOverview = PANELS.overview;
  PANELS.overview = () => {
    throw new Error('boom: render-time crash, not a fetch failure');
  };
  return withLoadAllHarness(
    () => jsonResponse([]),
    async ({ getToastText }) => {
      const prevRuns = state.runs,
        prevStats = state.stats,
        prevError = state.loadError,
        prevPanel = state.panel;
      // Data already shown BEFORE this call, matching the scenario that triggered the bug.
      state.panel = 'overview';
      state.runs = [];
      state.stats = { session_count: 1 };
      state.loadError = null;
      try {
        await assert.rejects(() => loadAll(), /boom: render-time crash/);
        assert.equal(getToastText(), null, 'a render-time throw must not be misreported as a "refresh failed" toast');
      } finally {
        state.runs = prevRuns;
        state.stats = prevStats;
        state.loadError = prevError;
        state.panel = prevPanel;
        PANELS.overview = prevOverview;
      }
    },
  );
});

test('render: renders the panel normally once state.runs is populated', () => {
  const prevErrors = PANELS.errors;
  let called = false;
  PANELS.errors = () => {
    called = true;
    return '<div>ok</div>';
  };
  try {
    withRenderState({ panel: 'errors', runs: [] }, () => {
      const writes = withRecordedPanel(render);
      assert.equal(called, true);
      assert.equal(writes.panel, '<div>ok</div>');
    });
  } finally {
    PANELS.errors = prevErrors;
  }
});

// Every test below drives its loop from the exported STATS_ONLY_PANELS itself (not a
// hand-copied ['stats', 'models', 'metrics'] literal) — so the Set, the render() guard, and
// these tests can't drift apart: add/rename a stats-only panel in app.js and every test here
// picks it up automatically instead of silently going stale.

// A non-trivial state.stats fixture: touches the NON-empty branches of all three stats-only
// renderers (a populated, PROBLEMATIC model_health row for Models & roles' "raw board
// health" section; multi-entry by_model/by_day/by_role for Stats' bar charts; real duration
// numbers for Metrics) — not just the "no data" early-return paths a bare `{session_count: 1}`
// would exercise. Named and hoisted so a future renderer needing a new field fails with an
// obvious "fixture is stale" diff instead of a confusing crash buried in a loop body.
const NON_TRIVIAL_STATS = {
  session_count: 3,
  call_count: 10,
  ok_calls: 8,
  error_calls: 2,
  timeout_calls: 0,
  by_day: { '2026-09-01': 2, '2026-09-02': 1 },
  by_mode: { panel: 2, brainstorm: 1 },
  by_task: { 'GH-1': 2 },
  by_model: { claude: 5, 'zai:glm-5.2': 3, opus: 2 },
  by_role: { architect: 2 },
  model_health: [
    { model: 'zai:glm-5.2', display: 'GLM', calls: 3, ok: 0, fail: 3, ok_rate: 0, problematic: true, dominant_class: 'timeout', status: 'active' },
    { model: 'claude', display: 'Claude', calls: 5, ok: 5, fail: 0, ok_rate: 1, problematic: false, status: 'active' },
  ],
  problematic_count: 1,
  duration_seconds: { min: 1, p50: 2, p90: 3, max: 4 },
};

test('render: stats-only panels still render during a runs reload — once stats have loaded at least once', () => {
  // These panels read only `state.stats`, which invalidate()/scheduleReload() never null —
  // so once the first stats fetch has landed, they can (and should) keep showing their
  // still-valid data while `state.runs` is being reloaded, instead of being forced to
  // "Loading…" like the runs-reading panels above.
  for (const panel of STATS_ONLY_PANELS) {
    const prevFn = PANELS[panel];
    let called = false;
    PANELS[panel] = () => {
      called = true;
      return '<div>stats-derived content</div>';
    };
    try {
      withRenderState({ panel, runs: null, stats: { session_count: 1 } }, () => {
        const writes = withRecordedPanel(render);
        assert.equal(called, true, `PANELS.${panel} should still be invoked while state.runs is null (state.stats already loaded)`);
        assert.equal(writes.panel, '<div>stats-derived content</div>');
      });
    } finally {
      PANELS[panel] = prevFn;
    }
  }
});

test('render: stats-only panels still show the loading placeholder on the INITIAL boot fetch (state.stats also null)', () => {
  // The exemption above only holds once state.stats has loaded at least once. At initial
  // boot state.stats starts null too (until the first loadAll() resolves) — a stats-only
  // panel opened before THAT lands would otherwise hit the same false-empty bug one level
  // down (PANELS.stats/models/metrics each render their own "no data yet" on a null
  // state.stats). The guard must fire here even though the panel is in STATS_ONLY_PANELS.
  for (const panel of STATS_ONLY_PANELS) {
    const prevFn = PANELS[panel];
    let called = false;
    PANELS[panel] = () => {
      called = true;
      return '<div>should not render before the first stats fetch lands</div>';
    };
    try {
      withRenderState({ panel, runs: null, stats: null }, () => {
        const writes = withRecordedPanel(render);
        assert.equal(called, false, `PANELS.${panel} must not run before state.stats has loaded at least once`);
        assert.match(writes.panel || '', /class="loading"/);
      });
    } finally {
      PANELS[panel] = prevFn;
    }
  }
});

test('render: the STATS_ONLY_PANELS exemption is honest — the REAL stats/models/metrics renderers never read state.runs', () => {
  // The exemptions above only proved render() still DISPATCHES to these panels while
  // state.runs is null — they stub PANELS[panel], so they can't catch the exemption itself
  // going stale (e.g. a future edit to PANELS.models that starts reading state.runs). This
  // test runs the REAL renderers, against a NON-trivial stats fixture (so it actually
  // reaches the populated/problematic branches, not just the empty early-returns), with
  // state.runs replaced by a Proxy that throws on any property access — so any of them
  // touching state.runs fails loudly instead of quietly reintroducing the false-empty bug.
  const throwingRuns = new Proxy(
    {},
    {
      get(_t, prop) {
        throw new Error(`stats-only panel touched state.runs.${String(prop)} — exemption is stale`);
      },
    },
  );
  for (const panel of STATS_ONLY_PANELS) {
    withRenderState({ panel, runs: throwingRuns, stats: NON_TRIVIAL_STATS }, () => {
      assert.doesNotThrow(() => withRecordedPanel(render));
    });
  }
});

// NOTE: render()'s `state.panel === 'chat' && state.detail` branch must keep taking
// precedence OVER the null-runs guard below it — openDetail() fetches its own data via
// `/api/runs/<id>` and never reads state.runs, so a session detail open during a background
// reload must keep showing the transcript, not the generic loading guard. No automated test
// pins this here: openDetail is an async top-level function (not exported, and — unlike a
// browser classic-script global — not reachable/stubbable from a CommonJS test module, since
// require() wraps the file in its own function scope), and faithfully exercising its
// fetch/DOM-write body would mean stubbing `location`/`history`/`fetch` well beyond what this
// suite's lightweight stubs cover. Verified instead by direct code reading (twice,
// independently, in the review-cli quorum gate for review-cli#362): the chat-detail branch
// is the first statement in render() and returns unconditionally, so the guard below it is
// unreachable whenever a detail is open.
