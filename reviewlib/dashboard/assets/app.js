/* review-cli dashboard SPA — vanilla JS, no build step, no framework.
 * Talks to the local-only stdlib server's /api/* JSON endpoints. All rendering is
 * string-templated with strict HTML escaping (logs contain arbitrary model output).
 *
 * Design primitive: the SEAT CHIP. A review board is a panel of named models, each with a
 * lens/role. Every model the dashboard shows is rendered as its REAL brand-logo PNG + name
 * (see seatChip / modelIconHtml / MODEL_LOGO) so a board model reads as a recognizable identity,
 * not a `oc:provider/model` log string — and NOT a unicode emoji.
 * Seat chips and role chips are CLICKABLE — they set state.filterModel / state.filterRole and
 * narrow the session lists, so the dashboard is a tool you drill through, not a static dump. */
'use strict';

const state = {
  panel: 'overview',
  runs: null, // [summary]
  stats: null,
  detail: null, // session_id currently open in chat panel
  gap: 90,
  filterModel: null, // when set, session lists show only runs that used this model
  filterRole: null, // when set, session lists show only brainstorm runs with this persona
  filterTask: null, // when set, session lists show only runs for this task code
};

// ---- model identity --------------------------------------------------------
// Model name -> the BRAND-LOGO key whose committed PNG (assets/icons/mini_<brand>.png) the
// chip renders as an <img>. These are the SAME per-vendor brand logos tg-cli ships
// (tg-cli/emoji-icons/mini_<brand>.png), so a board model wears its REAL logo — not a unicode
// emoji — across the whole HyperIDE ecosystem. Keys are lower-cased base families; resolveModel()
// does exact-then-prefix matching (tg-cli's extractBaseModel logic) so suffixed ids
// (`opus-4-8`, `glm-5.2`, `claude-fable-5`, `commandcode:Qwen/Qwen3.7-Max`, `zai:glm-5.2`) all
// resolve to the right brand. A family with no dedicated logo maps to its closest brand: every
// Anthropic seat (Opus/Fable/Sonnet/Haiku/Devin/Aider) → the Claude (Anthropic) logo; every
// OpenAI seat (GPT/o1/o3/Codex) → the Codex (OpenAI) logo; Llama/Meta → Meta; xAI → Grok, etc.
const ICON_DIR = '/assets/icons/';
const MODEL_LOGO = {
  // Anthropic — one brand mark (the starburst). The board's two seats (Fable, Opus) share it;
  // the seat `label` (from the server's board `display`) is what tells them apart.
  claude: 'claude',
  anthropic: 'claude',
  fable: 'claude',
  opus: 'claude',
  sonnet: 'claude',
  haiku: 'claude',
  devin: 'claude',
  cognition: 'claude',
  aider: 'claude',
  // OpenAI family → the Codex (OpenAI) mark.
  codex: 'codex',
  openai: 'codex',
  o3: 'codex',
  o1: 'codex',
  gpt: 'codex',
  'gpt-5.6-sol': 'codex',
  sol: 'codex',
  // Google
  gemini: 'gemini',
  google: 'gemini',
  // DeepSeek
  deepseek: 'deepseek',
  // Alibaba Qwen
  qwen: 'qwen',
  alibaba: 'qwen',
  // Moonshot Kimi
  kimi: 'kimi',
  moonshot: 'kimi',
  // Meta / Llama (+ Ollama runs Llama locally → Ollama's own mark)
  llama: 'meta',
  meta: 'meta',
  ollama: 'ollama',
  // Mistral
  mistral: 'mistral',
  // xAI Grok
  grok: 'grok',
  xai: 'grok',
  // Microsoft Copilot / GitHub
  copilot: 'copilot',
  github: 'copilot',
  // Perplexity
  perplexity: 'perplexity',
  // Editors
  cursor: 'cursor',
  windsurf: 'windsurf',
  // HyperIDE
  hyperide: 'hyperide',
  // z.ai serves Zhipu's GLM — the board's priority-5 `tests` seat (zai:glm-5.2). The shipped
  // GLM tile carries the brand wordmark so the seat reads as a real logo image alongside its
  // board siblings, not a bare monogram.
  glm: 'glm',
  zai: 'glm',
  chatglm: 'glm',
  // NOTE: `minimax` (an OPTIONAL heavyweight, not in the default board) has NO dedicated logo
  // and no close brand — resolveModel() renders it as a clean two-letter brand MONOGRAM (still
  // a branded chip, never a generic 🤖/💬 emoji). `commandcode` is the gateway, shown only for
  // a bare probe with no resolved model: also a monogram.
};
// The (always-present) src for a brand key.
function logoSrc(key) {
  return ICON_DIR + 'mini_' + key + '.png';
}
// Family -> the clean display label used on a seat chip. Lets `opus-4-8` read as "Opus".
const MODEL_LABEL = {
  fable: 'Fable',
  opus: 'Opus',
  sonnet: 'Sonnet',
  haiku: 'Haiku',
  claude: 'Claude',
  codex: 'Codex',
  'gpt-5.6-sol': 'Sol',
  sol: 'Sol',
  openai: 'OpenAI',
  gpt: 'GPT',
  gemini: 'Gemini',
  deepseek: 'DeepSeek',
  qwen: 'Qwen',
  kimi: 'Kimi',
  glm: 'GLM',
  zai: 'GLM',
  'z.ai': 'GLM', // the raw backend name in the call logs (board id is `zai:glm-5.2`)
  chatglm: 'GLM',
  minimax: 'MiniMax',
  llama: 'Llama',
  meta: 'Meta',
  ollama: 'Ollama',
  mistral: 'Mistral',
  grok: 'Grok',
  copilot: 'Copilot',
  perplexity: 'Perplexity',
  cursor: 'Cursor',
  windsurf: 'Windsurf',
  hyperide: 'HyperIDE',
  commandcode: 'gateway',
};
// Every base family we know a label OR a logo for — the list resolveModel() iterates for the
// boundary match, and the Set for the O(1) exact-hit check. (A few keys, e.g. `minimax`, have a
// label but no logo → they render a monogram.)
const KNOWN_FAMILIES = Array.from(new Set([...Object.keys(MODEL_LOGO), ...Object.keys(MODEL_LABEL)]));
const KNOWN_FAMILY_SET = new Set(KNOWN_FAMILIES);
// Resolve a raw model/backend string to {key, logo, label}. `logo` is the brand key whose PNG
// (assets/icons/mini_<logo>.png) the chip renders as an <img>, or null when no brand logo
// exists — then the chip falls back to a clean letter MONOGRAM (never a unicode emoji). Strips a
// gateway prefix (`commandcode:` / `zai:` / `oc:` / `claude:` / `codex:`) and a vendor path,
// then matches the longest known family on a token boundary (tg-cli's extractBaseModel logic).
function resolveModel(raw) {
  const s = String(raw == null ? '' : raw).trim();
  if (!s) return { key: '', logo: null, label: '—' };
  let body = s.toLowerCase();
  const colon = body.indexOf(':');
  if (colon !== -1) body = body.slice(colon + 1); // drop gateway prefix
  const slash = body.lastIndexOf('/');
  if (slash !== -1) body = body.slice(slash + 1); // drop vendor path
  // exact family hit first (O(1) Set lookup)
  if (KNOWN_FAMILY_SET.has(body)) {
    return { key: body, logo: MODEL_LOGO[body] || null, label: MODEL_LABEL[body] || cap(body) };
  }
  // BOUNDARY match. A bare `includes` false-positives on short keys (`o1`/`o3`/`gpt`/`glm`
  // would hit any id that merely contains those letters, e.g. `proto3` → OpenAI). Require the
  // family to sit on a token boundary: at the start, or preceded by a non-alphanumeric
  // separator (`-`, `_`, `.`, space). GENERIC vendor keys (`claude`/`anthropic`) are a
  // fallback only — a SPECIFIC model in the same id (e.g. `fable` in `claude-fable-5`) must
  // win, otherwise the chip mislabels "Fable" as "Claude". So we rank specific over generic
  // first, then longest match within the same tier.
  const GENERIC = new Set(['claude', 'anthropic', 'openai', 'google', 'meta', 'alibaba', 'moonshot', 'chatglm', 'xai', 'github']);
  let best = null;
  let bestGeneric = true;
  for (const fam of KNOWN_FAMILIES) {
    const i = body.indexOf(fam);
    if (i === -1) continue;
    if (i !== 0 && /[a-z0-9]/.test(body[i - 1])) continue; // not on a token boundary
    const generic = GENERIC.has(fam);
    if (best === null || (bestGeneric && !generic) || (bestGeneric === generic && fam.length > best.length)) {
      best = fam;
      bestGeneric = generic;
    }
  }
  if (best) return { key: best, logo: MODEL_LOGO[best] || null, label: MODEL_LABEL[best] || cap(best) };
  return { key: body, logo: null, label: cap(s) };
}
// The brand-logo <img> (or a clean letter monogram when no logo exists), used everywhere a model
// icon appears. A real PNG brand mark — NEVER a unicode emoji. `loading=lazy` + fixed box keeps
// the lists light; `alt` carries the brand name for screen readers; a broken/missing PNG falls
// back to the monogram via onerror (so an unknown brand never shows a broken-image glyph).
function modelIconHtml(m, cls) {
  // `cls` is always a literal from our own call sites, but escape it anyway so this can never
  // become an injection vector if a future caller derives it from data (glm review finding).
  const klass = esc('model-ic' + (cls ? ' ' + cls : ''));
  if (m.logo) {
    // A broken/missing PNG swaps to the monogram via the global onImgError handler. The
    // attribute value is a FIXED literal (`onImgError(this)`) — no data value is interpolated
    // into it; the fallback text rides in data-mono and is read by the handler, so the handler
    // body never executes attacker/data-derived strings.
    return `<img class="${klass}" src="${esc(logoSrc(m.logo))}" alt="${esc(m.label)} logo" loading="lazy" decoding="async" data-mono="${esc(monogram(m.label))}" onerror="onImgError(this)" />`;
  }
  return `<span class="${klass} mono" aria-hidden="true">${esc(monogram(m.label))}</span>`;
}
// Global handler for a broken/missing logo PNG: replace the <img> with the monogram span it
// carries in data-mono. Defined as a function (not an inline onerror body) so no value is ever
// interpolated into an HTML attribute as executable JS. textContent assignment is inherently safe.
window.onImgError = function (img) {
  const span = document.createElement('span');
  span.className = img.className + ' mono';
  span.setAttribute('aria-hidden', 'true');
  span.textContent = img.getAttribute('data-mono') || '?';
  img.replaceWith(span);
};
// A two-letter brand monogram for a model with no shipped logo (MiniMax, gateway probes):
// first letter of each of the first two ALPHANUMERIC words, else the first two alnum chars.
// Splits on any non-alphanumeric run (so `z.ai` → "ZA", not ".A"), and never yields a stray
// punctuation char. Branded, not emoji.
function monogram(label) {
  const words = String(label || '').split(/[^a-z0-9]+/i).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return '?';
}
function cap(s) {
  s = String(s);
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

// ---- tiny helpers ----------------------------------------------------------
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
function fmtDur(s) {
  if (s == null) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60),
    r = Math.round(s % 60);
  return `${m}m ${r}s`;
}
function api(path, opts) {
  return fetch(path, opts).then((r) =>
    r.json().then((j) => {
      if (!r.ok) throw new Error(j && j.error ? j.error : 'HTTP ' + r.status);
      return j;
    }),
  );
}
function toast(msg) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove('show'), 1800);
}
function modeBadge(m) {
  return `<span class="badge mode-${esc(m)}"><span class="badge-dot" aria-hidden="true"></span>${esc(m)}</span>`;
}
function $(id) {
  return document.getElementById(id);
}

// A clickable model seat chip: `icon + label`. `data-model` carries the RAW id so a click can
// toggle state.filterModel. Active filter gets the `.is-active` ring. `opts.label` overrides
// the resolved family label — the server's board `display` ("Fable" vs "Opus") is more precise
// than icon resolution can be for two seats that share one glyph (both Claude).
function seatChip(raw, opts) {
  const m = resolveModel(raw);
  const label = (opts && opts.label) || m.label;
  const active = state.filterModel && state.filterModel === String(raw);
  const small = opts && opts.small ? ' chip-sm' : '';
  return `<button type="button" class="seat${small}${active ? ' is-active' : ''}" data-model="${esc(raw)}" title="filter sessions by ${esc(label)} (${esc(raw)})">
    ${modelIconHtml(m, 'seat-ic')}<span class="seat-name">${esc(label)}</span></button>`;
}
function seatChips(list) {
  const arr = list || [];
  if (!arr.length) return `<span class="muted">—</span>`;
  return `<span class="seats">${arr.map((m) => seatChip(m, { small: true })).join('')}</span>`;
}
// A clickable role/persona chip (brainstorm lenses). Filters by persona name.
function roleChip(name) {
  const active = state.filterRole && state.filterRole === String(name);
  return `<button type="button" class="role-chip${active ? ' is-active' : ''}" data-role="${esc(name)}" title="filter brainstorm sessions with the ${esc(name)} lens">${esc(name)}</button>`;
}
function taskChip(code) {
  if (!code) return '';
  const active = state.filterTask && state.filterTask === String(code);
  return `<button type="button" class="badge ticket${active ? ' is-active' : ''}" data-task="${esc(code)}" title="filter review iterations for task ${esc(code)}">${esc(code)}</button>`;
}

// ---- data loading ----------------------------------------------------------
async function loadAll() {
  try {
    const [runs, stats] = await Promise.all([api(`/api/runs?gap=${state.gap}`), api(`/api/stats?gap=${state.gap}`)]);
    state.runs = runs;
    state.stats = stats;
    updateTabBadges();
    render();
  } catch (e) {
    $('panel').innerHTML = `<div class="empty">Failed to load data: ${esc(e.message)}</div>`;
  }
}

// The tab buttons live in the static shell (they are NOT re-rendered with the panel), so
// their count badges are updated here whenever fresh stats land. Models & roles shows the
// number of currently-problematic raw-board models; a zero count hides the badge entirely.
function updateTabBadges() {
  const badge = $('models-badge');
  if (!badge) return;
  const n = (state.stats && state.stats.problematic_count) || 0;
  if (n > 0) {
    badge.textContent = String(n);
    badge.hidden = false;
    badge.title = `${n} raw-board model(s) currently problematic`;
  } else {
    badge.textContent = '';
    badge.hidden = true;
  }
}

async function health() {
  try {
    const h = await api('/api/health');
    const el = $('health');
    el.textContent = '● online';
    el.className = 'health ok';
    $('footer-paths').innerHTML = `logs: ${esc(h.log_dir)} &nbsp;·&nbsp; store: ${esc(h.store_path)}`;
  } catch {
    const el = $('health');
    el.textContent = '● offline';
    el.className = 'health bad';
  }
}

// ---- filtering -------------------------------------------------------------
// The active model/role filter applied to a session list. A model filter keeps runs whose
// `models` include that id (raw or resolved-family match). A lens/role filter narrows to
// BRAINSTORM runs: the per-summary payload carries no role list (roles live only on the
// session detail), so the list can honestly scope to brainstorms — where that lens is in play
// — and the chosen persona is highlighted once you open a session. The filter bar labels this
// truthfully ("brainstorm runs · lens X") rather than claiming a precise per-role match.
function filteredRuns(runs) {
  let out = runs || [];
  if (state.filterModel) {
    const want = state.filterModel;
    out = out.filter((r) => (r.models || []).some((m) => String(m) === want || resolveModel(m).key === resolveModel(want).key));
  }
  if (state.filterRole) out = out.filter((r) => r.mode === 'brainstorm' || r.topic);
  if (state.filterTask) out = out.filter((r) => String(r.task_code || '') === state.filterTask);
  return out;
}
// The active-filter banner shown atop a filtered list. Clear buttons reset the filter.
function filterBar() {
  const bits = [];
  if (state.filterModel) {
    const m = resolveModel(state.filterModel);
    bits.push(
      `<span class="filter-pill">${modelIconHtml(m, 'seat-ic')} ${esc(m.label)} <button class="filter-x" data-clear="model" aria-label="clear model filter">×</button></span>`,
    );
  }
  if (state.filterRole) {
    // Honest label: the per-summary payload carries no role list, so this scopes to brainstorm
    // runs (where the lens applies) and the chosen persona is highlighted once a session opens —
    // it does NOT claim a precise per-lens row match the data can't back.
    bits.push(`<span class="filter-pill" title="${esc(state.filterRole)} appears in brainstorm sessions — open one to see its turns">brainstorm runs <span class="muted">(${esc(state.filterRole)})</span> <button class="filter-x" data-clear="role" aria-label="clear lens filter">×</button></span>`);
  }
  if (state.filterTask) {
    bits.push(`<span class="filter-pill">${esc(state.filterTask)} <button class="filter-x" data-clear="task" aria-label="clear task filter">×</button></span>`);
  }
  if (!bits.length) return '';
  return `<div class="filterbar"><span class="muted">filtered by</span> ${bits.join(' ')} <button class="btn small" data-clear="all">clear all</button></div>`;
}

// ---- panels ----------------------------------------------------------------
const PANELS = {};

function emptyState(what, note) {
  return `<div class="empty"><strong>No ${esc(what)} yet.</strong>${
    note ? `<div class="note">${note}</div>` : ''
  }</div>`;
}

PANELS.overview = () => {
  const s = state.stats || {};
  const allRuns = state.runs || [];
  const runs = filteredRuns(allRuns);
  const cards = [
    ['sessions', s.session_count, 'review bursts'],
    ['calls', s.call_count, 'backend invocations'],
    ['success', s.success_rate != null ? Math.round(s.success_rate * 100) + '%' : '—', 'calls returning clean'],
    ['errors', s.error_calls, 'failed calls'],
    ['timeouts', s.timeout_calls, 'calls that aged out'],
    ['true-silence', s.true_silence_calls != null ? s.true_silence_calls : 0, 'calls reaped for producing zero output'],
    ['conscious', s.conscious_count, 'reviewed by overseer'],
  ];
  let html = `<div class="panel-head"><h2>Overview</h2><p class="sub">Sessions are time-clustered bursts of backend calls (review-cli emits no run id; gap = ${state.gap}s).</p></div>`;
  html += `<div class="cards">${cards
    .map(
      ([l, n, hint]) =>
        `<div class="card"><div class="num">${esc(n == null ? '—' : n)}</div><div class="lbl">${esc(l)}</div><div class="hint">${esc(hint)}</div></div>`,
    )
    .join('')}</div>`;
  const shown = Math.min(runs.length, 12);
  const countLabel = runs.length > 12 ? `${shown} of ${runs.length}` : `${runs.length}`;
  html += `<div class="section"><div class="section-head"><h3>Recent sessions</h3><span class="muted">${countLabel}</span></div>`;
  html += filterBar();
  html += runs.length
    ? `<div class="list">${runs.slice(0, 12).map(runRow).join('')}</div>`
    : emptyState(
        'sessions',
        'Run <code>review diff --task CODE</code> / <code>review quorum "Q" --task CODE</code> / <code>review brainstorm "TOPIC" --task CODE</code> and the per-call logs will appear here.',
      );
  html += `</div>`;
  return html;
};

// One session row. Left accent rail is colored by mode (mode-* class). Status, mode, links are
// a disciplined badge row; the models are seat chips; the body line is the most informative
// thing the session leaves behind (topic > request summary).
function runRow(r) {
  const status = r.has_error
    ? `<span class="badge err">${r.error_count} err</span>`
    : r.running
      ? `<span class="badge running">running</span>`
      : `<span class="badge ok">ok</span>`;
  const linkBadges = [
    ((r.links && r.links.prs) || []).map((p) => `<span class="badge pr">PR ${esc(p)}</span>`).join(''),
    ((r.links && r.links.tickets) || []).map((t) => `<span class="badge ticket">${esc(t)}</span>`).join(''),
    r.task_code ? taskChip(r.task_code) : '',
  ].join('');
  const consc = r.conscious ? `<span class="badge conscious">★ conscious</span>` : '';
  const body = sessionRequest(r);
  const fb = r.feedback
    ? `<div class="run-note">📝 ${esc(r.feedback.slice(0, 140))}${r.feedback.length > 140 ? '…' : ''}</div>`
    : '';
  return `<div class="run mode-${esc(r.mode)}" data-sid="${esc(r.session_id)}" data-open="chat">
    <div class="run-top">
      <div class="run-badges">${modeBadge(r.mode)}${status}${consc}${linkBadges}</div>
      <span class="run-time">${fmtTime(r.started)}</span>
    </div>
    <div class="run-mid">${seatChips(r.models)}<span class="run-meta">${r.call_count} call${r.call_count === 1 ? '' : 's'} · ${fmtDur(r.duration_seconds)}</span></div>
    ${body}${fb}
  </div>`;
}

// The most informative thing a session leaves behind. A brainstorm carries its real topic;
// other modes redact the literal prompt/diff, so we lead with a human summary of WHAT the run
// did and still surface the recorded invocation count (the real argv the backends ran with) so
// the panel keeps the durable signal the logs DO hold — without dumping a raw harness path.
function sessionRequest(r) {
  if (r.topic) {
    const t = r.topic.length > 200 ? r.topic.slice(0, 200) + '…' : r.topic;
    return `<div class="run-req"><span class="req-label">Topic</span> ${esc(t)}</div>`;
  }
  const verb = { review: 'Reviewed the working diff', panel: 'Panel review of the working diff', quorum: 'Quorum decision' }[r.mode];
  const summary = verb || `${cap(r.mode)} run`;
  const inv = r.invocations || [];
  const detail = inv.length
    ? `<span class="req-detail">${inv.length} invocation${inv.length === 1 ? '' : 's'} · prompt/diff redacted in logs</span>`
    : `<span class="muted">prompt/diff redacted in logs</span>`;
  return `<div class="run-req"><span class="req-label">Request</span> ${esc(summary)} · ${detail}</div>`;
}

PANELS.chat = () => {
  if (state.detail) return ''; // detail view renders async (see openDetail)
  const runs = filteredRuns(state.runs || []);
  let html = `<div class="panel-head"><h2>Chat logs</h2><p class="sub">Per-run transcripts — the streamed multi-model panel conversations. Click a session to open the full chat.</p></div>`;
  html += filterBar();
  html += runs.length
    ? `<div class="list">${runs.map(runRow).join('')}</div>`
    : emptyState('transcripts', 'No call logs found in the log dir.');
  return html;
};

PANELS.stats = () => {
  const s = state.stats;
  if (!s || !s.session_count) return `<div class="panel-head"><h2>Stats</h2></div>` + emptyState('stats', 'No sessions to aggregate.');
  let html = `<div class="panel-head"><h2>Stats</h2><p class="sub">Runs over time and counts by mode/model/role. Click a model bar to filter the board.</p></div>`;
  html += `<div class="section"><h3>Sessions per day</h3>${barChart(s.by_day)}</div>`;
  html += `<div class="section"><h3>By mode</h3>${barChart(s.by_mode)}</div>`;
  if (s.by_task && Object.keys(s.by_task).length)
    html += `<div class="section"><h3>By task</h3>${barChart(s.by_task, { task: true })}</div>`;
  html += `<div class="section"><h3>By model</h3>${barChart(s.by_model, { model: true })}</div>`;
  if (s.by_role && Object.keys(s.by_role).length)
    html += `<div class="section"><h3>By role / persona</h3>${barChart(s.by_role, { role: true })}</div>`;
  return html;
};

// Human label + status-pill class for a model's health class. The pill reuses the
// existing .badge palette (ok green, err red, degraded/nodata amber-ish).
const HEALTH_LABEL = {
  ok: 'ok',
  paywall: 'paywall / unavailable',
  auth: 'auth (bad key)',
  blocked: 'blocked (bot)',
  timeout: 'timeout',
  true_silence: 'true-silence (no output)',
  empty: 'empty output',
  error: 'error',
  no_data: 'no data',
};
const HARD_UNAVAILABLE = new Set(['paywall', 'auth', 'blocked']);
function healthPill(m) {
  if (m.status === 'no_data') return `<span class="badge nodata">${esc(HEALTH_LABEL.no_data)}</span>`;
  if (!m.problematic) return `<span class="badge ok">${esc(HEALTH_LABEL.ok)}</span>`;
  const cls = HARD_UNAVAILABLE.has(m.dominant_class) ? 'err' : 'degraded';
  return `<span class="badge ${cls}">${esc(HEALTH_LABEL[m.dominant_class] || m.dominant_class || 'down')}</span>`;
}
function modelHealthRow(m) {
  const rate = m.ok_rate == null ? '—' : Math.round(m.ok_rate * 100) + '%';
  const pct = m.ok_rate == null ? 0 : Math.round(m.ok_rate * 100);
  const sub = m.calls ? `${m.ok}/${m.calls} ok · ${m.fail} fail` : 'no calls in window';
  const role = m.role ? `<span class="lens">${esc(m.role)} lens</span>` : '';
  // The whole row is clickable to filter by this model.
  return `<div class="mh-row${m.problematic ? ' is-problematic' : ''}${m.status === 'no_data' ? ' is-idle' : ''}" data-model="${esc(m.model)}" title="filter sessions by ${esc(m.display || m.model)}">
      <div class="mh-id">${seatChip(m.model, { label: m.display })}${role}</div>
      ${healthPill(m)}
      <span class="mh-bar-track"><span class="mh-bar-fill" style="width:${pct}%"></span></span>
      <span class="mh-rate">${rate}</span>
      <span class="mh-sub">${esc(sub)}</span>
    </div>`;
}

PANELS.models = () => {
  const s = state.stats;
  if (!s) return `<div class="panel-head"><h2>Models &amp; roles</h2></div>` + emptyState('data');
  let html = `<div class="panel-head"><h2>Models &amp; roles</h2><p class="sub">The review board: each seat is a model with a lens. Click any seat to filter the dashboard to its sessions.</p></div>`;
  const mh = (s.model_health || []).slice().sort((a, b) => {
    const rank = (m) => (m.problematic ? 0 : m.status === 'no_data' ? 2 : 1);
    if (rank(a) !== rank(b)) return rank(a) - rank(b);
    return (a.ok_rate ?? 1) - (b.ok_rate ?? 1);
  });
  const probCount = s.problematic_count || 0;
  html += `<div class="section"><div class="section-head"><h3>Raw board health</h3><span class="muted">${probCount} problematic</span></div>`;
  html += mh.length
    ? `<div class="mh-list">${mh.map(modelHealthRow).join('')}</div>`
    : emptyState('model health', 'No calls in the window to classify.');
  html += `</div>`;
  const models = Object.entries(s.by_model || {}).sort((a, b) => b[1] - a[1]);
  html += `<div class="section"><h3>Usage</h3>`;
  html += models.length
    ? `<div class="usage-grid">${models
        .map(
          ([m, n]) =>
            `<div class="usage-cell" data-model="${esc(m)}" title="filter sessions by ${esc(resolveModel(m).label)}">${seatChip(m)}<span class="usage-n">${esc(n)}<span class="usage-lbl">session${n === 1 ? '' : 's'}</span></span></div>`,
        )
        .join('')}</div>`
    : emptyState('models');
  html += `</div>`;
  const roles = Object.entries(s.by_role || {}).sort((a, b) => b[1] - a[1]);
  html += `<div class="section"><h3>Lenses <span class="muted">/ brainstorm personas</span></h3>`;
  html += roles.length
    ? `<div class="role-grid">${roles
        .map(([r, n]) => `<div class="role-cell">${roleChip(r)}<span class="usage-n">${esc(n)}<span class="usage-lbl">appearance${n === 1 ? '' : 's'}</span></span></div>`)
        .join('')}</div>`
    : emptyState('roles', 'Roles/personas are only recorded for <code>review brainstorm</code> runs.');
  html += `</div>`;
  return html;
};

PANELS.metrics = () => {
  const s = state.stats;
  if (!s) return `<div class="panel-head"><h2>Metrics</h2></div>` + emptyState('metrics');
  const d = s.duration_seconds || {};
  let html = `<div class="panel-head"><h2>Metrics</h2><p class="sub">Durations and success/fail rates (durations = file create → last write, the honest proxy since review-cli records no explicit duration).</p></div>`;
  html += `<table class="kv">
    <tr><td>Total calls</td><td>${esc(s.call_count)}</td></tr>
    <tr><td>OK calls</td><td>${esc(s.ok_calls)}</td></tr>
    <tr><td>Error calls</td><td>${esc(s.error_calls)}</td></tr>
    <tr><td>Timeout calls</td><td>${esc(s.timeout_calls)}</td></tr>
    <tr><td>True-silence calls</td><td>${esc(s.true_silence_calls != null ? s.true_silence_calls : 0)}</td></tr>
    <tr><td>Running / unknown</td><td>${esc(s.running_calls != null ? s.running_calls : 0)}</td></tr>
    <tr><td>Success rate</td><td>${s.success_rate != null ? Math.round(s.success_rate * 100) + '%' : '—'}</td></tr>
    <tr><td>Duration min</td><td>${fmtDur(d.min)}</td></tr>
    <tr><td>Duration p50</td><td>${fmtDur(d.p50)}</td></tr>
    <tr><td>Duration p90</td><td>${fmtDur(d.p90)}</td></tr>
    <tr><td>Duration max</td><td>${fmtDur(d.max)}</td></tr>
  </table>`;
  if (!s.tokens_recorded || !s.cost_recorded) {
    html += `<div class="empty note" style="margin-top:18px">
      <strong>Token / cost not recorded.</strong> review-cli's streamed logs do not capture
      token usage or $ cost today. To populate this, review-core would need to log per-call
      token counts (e.g. parse the backend CLI's usage line) into the call log header or a
      sidecar JSON. The dashboard will surface it automatically once present.</div>`;
  }
  return html;
};

PANELS.feedback = () => {
  const runs = filteredRuns(state.runs || []);
  const withFb = runs.filter((r) => r.feedback);
  let html = `<div class="panel-head"><h2>Overseer feedback</h2><p class="sub">Feedback the overseer left on runs (persisted in <code>dashboard.json</code>). Open any session below to add or edit.</p></div>`;
  html += filterBar();
  html += `<div class="section"><h3>Feedback left (${withFb.length})</h3>`;
  html += withFb.length
    ? `<div class="list">${withFb
        .map(
          (r) =>
            `<div class="run mode-${esc(r.mode)}" data-sid="${esc(r.session_id)}" data-open="chat">
      <div class="run-top"><div class="run-badges">${modeBadge(r.mode)}</div><span class="run-time">${fmtTime(r.started)}</span></div>
      <div class="run-mid">${seatChips(r.models)}</div>
      <div class="run-note">📝 ${esc(r.feedback)}</div>
    </div>`,
        )
        .join('')}</div>`
    : emptyState('feedback', 'Open a session in Chat logs and add feedback in its panel.');
  html += `</div>`;
  html += `<div class="section"><h3>All sessions</h3><div class="list">${runs.map(runRow).join('')}</div></div>`;
  return html;
};

PANELS.modes = () => {
  const s = state.stats;
  if (!s || !s.by_mode) return `<div class="panel-head"><h2>Modes</h2></div>` + emptyState('modes');
  let html = `<div class="panel-head"><h2>Modes</h2><p class="sub">Breakdown by review mode. Mode is INFERRED from the call/round shape — review-cli does not stamp it in the log.</p></div>`;
  html += barChart(s.by_mode);
  html += filterBar();
  const runs = filteredRuns(state.runs || []);
  const byMode = {};
  runs.forEach((r) => {
    (byMode[r.mode] = byMode[r.mode] || []).push(r);
  });
  Object.keys(byMode)
    .sort()
    .forEach((m) => {
      html += `<div class="section"><div class="section-head mode-head">${modeBadge(m)}<span class="muted">${byMode[m].length} session${byMode[m].length === 1 ? '' : 's'}</span></div><div class="list">${byMode[m].slice(0, 8).map(runRow).join('')}</div></div>`;
    });
  return html;
};

// Human label + badge class for a per-error recovery state. recovered = a clean OK call ran
// concurrently-or-after this failed seat (the failover pool / retry produced a verdict);
// unrecovered = no clean OK call did — this run needs attention.
const RECOVERY_LABEL = { recovered: 'recovered', unrecovered: 'unrecovered' };
const RECOVERY_CLASS = { recovered: 'ok', unrecovered: 'err' };

// One error card: clickable to open the failing session's detail (scrolled to the call), with
// the failure CLASS, recovery status, and — when unrecovered — the planned fallback seat the
// failover pool would promote + a "take manual control" affordance.
function errorCard(sid, mode, started, e) {
  const m = resolveModel(e.model || e.backend);
  const cls = e.health_class || 'error';
  const rec = e.recovery || 'unrecovered';
  const recBadge = `<span class="badge ${RECOVERY_CLASS[rec] || 'err'}" title="recovery status">${esc(RECOVERY_LABEL[rec] || rec)}</span>`;
  // A timeout (ordinary or true-silence) is amber -- neither is a hard admin-level
  // block (paywall/auth/bot-block), which is what red is reserved for below. codex
  // review finding (review-cli#243 round 15): the two are NOT both "retryable" in the
  // sense retry.classify_failure actually uses -- rc=124 (ordinary timeout) IS
  // FailureClass.RETRYABLE (retried in-seat up to a cap before falling to reserve),
  // but rc=125 (true-silence) is FailureClass.SEAT_FATAL (straight to reserve, no
  // same-seat retry -- a seat that produced NOTHING at all is a stronger "broken"
  // signal, matching the escalating cooldown this reap also records). The color here
  // reflects "not a hard block", not "will be retried the same way".
  const classBadge = `<span class="badge ${cls === 'timeout' || cls === 'true_silence' ? 'degraded' : 'err'}">${esc(HEALTH_LABEL[cls] || cls)}</span>`;
  const summary = e.summary ? `<div class="err-summary"><code>${esc(e.summary)}</code></div>` : '';
  // Recovery action row: a recovered/partial error needs no action; an unrecovered one offers
  // the next fallback seat (retry path) and a manual-control button (give up on auto-failover).
  let action = '';
  if (rec === 'unrecovered') {
    const fb = e.fallback
      ? `<span class="err-fallback"><span class="muted">planned fallback →</span> ${seatChip(e.fallback.model, { small: true, label: e.fallback.display })} <span class="muted">(priority ${esc(e.fallback.priority)} · ${esc(e.fallback.role)} lens)</span></span>`
      : `<span class="err-fallback muted">no lower-priority reserve seat — the board is exhausted for this lens</span>`;
    action = `<div class="err-action">
      ${fb}
      <button class="btn small danger" data-manual="${esc(sid)}" data-manual-model="${esc(e.model || e.backend)}" data-manual-file="${esc(e.filename || '')}" title="take manual control: stop relying on auto-failover and act on this run yourself">⛬ take manual control</button>
    </div>`;
  } else {
    action = `<div class="err-action muted">A retry / next seat returned a clean verdict — the run still produced a result.</div>`;
  }
  return `<div class="err-card recovery-${esc(rec)}" data-sid="${esc(sid)}" data-open="chat" data-call-file="${esc(e.filename || '')}" tabindex="0" role="button">
    <div class="err-card-head">
      ${seatChip(e.model || e.backend, { small: true, label: m.label })}
      ${classBadge}${recBadge}
      <span class="muted err-round">round ${esc(e.round)}</span>
      <span class="muted err-time">${fmtTime(started)}</span>
      ${modeBadge(mode)}
    </div>
    ${summary}
    ${action}
  </div>`;
}

PANELS.errors = () => {
  const runs = filteredRuns((state.runs || []).filter((r) => r.has_error));
  // Flatten to individual errors so each failing seat is its own drill-down card.
  const allErrors = [];
  runs.forEach((r) => (r.errors || []).forEach((e) => allErrors.push({ sid: r.session_id, mode: r.mode, started: r.started, e })));
  const unrecovered = allErrors.filter((x) => x.e.recovery === 'unrecovered');
  let html = `<div class="panel-head"><h2>Errors</h2><p class="sub">Failed / timed-out calls, each with its failure class, recovery status, and — when a run didn't recover — the planned fallback seat. Click a card to open the run; take manual control when auto-failover is exhausted.</p></div>`;
  html += filterBar();
  if (!allErrors.length) {
    html += emptyState('errors', 'No failed calls in the current log window.');
    return html;
  }
  // Summary strip: total failing calls + how many runs never recovered (the actionable ones).
  html += `<div class="err-strip">
    <span class="err-stat"><strong>${allErrors.length}</strong> failed call${allErrors.length === 1 ? '' : 's'}</span>
    <span class="err-stat ${unrecovered.length ? 'is-bad' : ''}"><strong>${unrecovered.length}</strong> unrecovered <span class="muted">(need attention)</span></span>
  </div>`;
  if (unrecovered.length) {
    html += `<div class="section"><div class="section-head"><h3>Unrecovered <span class="muted">— auto-failover did not produce a clean verdict</span></h3></div>
      <div class="err-list">${unrecovered.map((x) => errorCard(x.sid, x.mode, x.started, x.e)).join('')}</div></div>`;
  }
  const recovered = allErrors.filter((x) => x.e.recovery !== 'unrecovered');
  if (recovered.length) {
    html += `<div class="section"><div class="section-head"><h3>Recovered <span class="muted">— a later seat / retry returned a verdict</span></h3></div>
      <div class="err-list">${recovered.map((x) => errorCard(x.sid, x.mode, x.started, x.e)).join('')}</div></div>`;
  }
  return html;
};

PANELS.tasks = () => {
  const runs = filteredRuns(state.runs || []);
  const s = state.stats || {};
  const taskGroups = state.filterTask
    ? (s.tasks || []).filter((t) => String(t.task_code || '') === state.filterTask)
    : (s.tasks || []);
  const conscious = runs.filter((r) => r.conscious);
  let html = `<div class="panel-head"><h2>Tasks</h2><p class="sub">Task-coded review history: iterations, model pools, and transcript links.</p></div>`;
  html += filterBar();
  html += `<div class="section"><h3>Review tasks (${taskGroups.length})</h3>`;
  html += taskGroups.length
    ? `<div class="list">${taskGroups.map((t) => taskGroupRow(t, runs)).join('')}</div>`
    : emptyState('task-coded reviews', 'Run a review mode with <code>--task CODE</code>.');
  html += `</div><div class="section"><h3>Conscious sessions (${conscious.length})</h3>`;
  html += conscious.length
    ? `<div class="list">${conscious.map(taskRow).join('')}</div>`
    : emptyState('conscious sessions', 'Toggle “mark conscious” on any session below.');
  html += `</div><div class="section"><h3>All sessions</h3><div class="list">${runs.map(taskRow).join('')}</div></div>`;
  return html;
};

function taskGroupRow(t, runs = state.runs || []) {
  const related = runs.filter((r) => r.task_code === t.task_code).sort((a, b) => String(b.started).localeCompare(String(a.started)));
  return `<div class="run mode-review">
    <div class="run-top">
      <div class="run-badges">${taskChip(t.task_code)}<span class="badge">${esc(t.iterations)} iteration${t.iterations === 1 ? '' : 's'}</span></div>
      <span class="run-time">${fmtTime(t.last_started)}</span>
    </div>
    <div class="run-mid">${seatChips(t.models)}<span class="run-meta">${esc((t.modes || []).join(', ') || 'review')}</span></div>
    ${related.length ? `<div class="list task-iterations">${related.slice(0, 4).map(runRow).join('')}</div>` : ''}
  </div>`;
}

function taskRow(r) {
  return `<div class="run mode-${esc(r.mode)}">
    <div class="run-top">
      <div class="run-badges">${modeBadge(r.mode)}${r.conscious ? `<span class="badge conscious">★ conscious</span>` : ''}</div>
      <span class="run-time">${fmtTime(r.started)}</span>
    </div>
    <div class="run-mid">${seatChips(r.models)}</div>
    <div class="run-actions">
      <button class="btn small" data-conscious="${esc(r.session_id)}" data-val="${r.conscious ? '0' : '1'}">
        ${r.conscious ? 'unmark' : 'mark conscious'}
      </button>
      <button class="btn small" data-sid="${esc(r.session_id)}" data-open="chat">open</button>
    </div>
  </div>`;
}

PANELS.prompts = () => {
  const runs = filteredRuns(state.runs || []);
  let html = `<div class="panel-head"><h2>Prompts</h2><p class="sub">What each run was asked to do. review-cli REDACTS the literal prompt/diff from logs (they may carry secrets) — a brainstorm keeps its real topic, other modes keep a request summary. Open a session for full per-call detail.</p></div>`;
  html += filterBar();
  html += runs.length
    ? `<div class="list">${runs
        .map(
          (r) =>
            `<div class="run mode-${esc(r.mode)}" data-sid="${esc(r.session_id)}" data-open="chat">
      <div class="run-top"><div class="run-badges">${modeBadge(r.mode)}</div><span class="run-time">${fmtTime(r.started)}</span></div>
      <div class="run-mid">${seatChips(r.models)}</div>
      ${sessionRequest(r)}
    </div>`,
        )
        .join('')}</div>`
    : emptyState('prompts');
  return html;
};

PANELS.links = () => {
  const runs = filteredRuns(state.runs || []);
  const linked = runs.filter((r) => r.links && ((r.links.prs || []).length || (r.links.tickets || []).length));
  let html = `<div class="panel-head"><h2>PRs &amp; tickets</h2><p class="sub">Each run links to the PRs/tickets it touched. Attach a PR# or HYP-id from a session's panel (auto-detected from the current repo branch where possible).</p></div>`;
  html += filterBar();
  html += `<div class="section"><h3>Linked sessions (${linked.length})</h3>`;
  html += linked.length
    ? `<div class="list">${linked
        .map(
          (r) =>
            `<div class="run mode-${esc(r.mode)}" data-sid="${esc(r.session_id)}" data-open="chat">
      <div class="run-top"><div class="run-badges">${modeBadge(r.mode)}
        ${(r.links.prs || []).map((p) => `<span class="badge pr">PR ${esc(p)}</span>`).join(' ')}
        ${(r.links.tickets || []).map((t) => ticketLink(t)).join(' ')}</div>
        <span class="run-time">${fmtTime(r.started)}</span>
      </div>
      <div class="run-mid">${seatChips(r.models)}</div>
    </div>`,
        )
        .join('')}</div>`
    : emptyState('links', 'Open a session and attach a PR# / ticket in its panel.');
  html += `</div><div class="section"><h3>All sessions</h3><div class="list">${runs.map(runRow).join('')}</div></div>`;
  return html;
};

function ticketLink(t) {
  const url = 'https://linear.app/glide-vc/issue/' + encodeURIComponent(t);
  return `<a class="badge ticket" target="_blank" rel="noreferrer noopener" href="${esc(url)}">${esc(t)}</a>`;
}

// ---- bar chart -------------------------------------------------------------
// `opts.model` renders the key as a clickable seat chip; `opts.role` as a clickable lens chip.
function barChart(obj, opts) {
  const entries = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return `<div class="muted">no data</div>`;
  const max = Math.max(...entries.map((e) => e[1])) || 1;
  return entries
    .map(([k, v]) => {
      let key;
      if (opts && opts.model) key = seatChip(k, { small: true });
      else if (opts && opts.role) key = roleChip(k);
      else if (opts && opts.task) key = taskChip(k);
      else key = `<span class="bar-key-txt" title="${esc(k)}">${esc(k)}</span>`;
      return `<div class="bar-row"><div class="k">${key}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${((v / max) * 100).toFixed(1)}%"></div></div>
      <div class="v">${esc(v)}</div></div>`;
    })
    .join('');
}

// ---- session detail (chat transcript) -------------------------------------
// `focusCallFile` (optional) auto-expands and scrolls to a specific call (used by the Errors
// tab so clicking an error card lands on the failing call, not the top of a long session).
async function openDetail(sid, focusCallFile) {
  state.detail = sid;
  setActiveTab('chat');
  const want = 'chat/' + encodeURIComponent(sid);
  if (location.hash !== '#' + want) {
    history.replaceState(null, '', '#' + want);
  }
  $('panel').innerHTML = `<div class="loading">Loading session…</div>`;
  let d;
  try {
    d = await api(`/api/runs/${encodeURIComponent(sid)}?gap=${state.gap}`);
  } catch (e) {
    $('panel').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  $('panel').innerHTML = renderDetail(d);
  wireDetail(d);
  // If we arrived from an error card, expand + scroll to the failing call.
  if (focusCallFile) focusCall(d, focusCallFile);
  // If we arrived via "take manual control", prime the feedback box with the manual note and
  // scroll the overseer controls into view (the place to record the manual decision).
  if (state.manualSeed && state.manualSeed.sid === sid) {
    const fb = $('fb');
    if (fb) {
      if (!fb.value.trim()) fb.value = state.manualSeed.text;
      fb.focus();
      fb.scrollIntoView({ block: 'center' });
    }
    state.manualSeed = null;
  }
}

// Expand + scroll to the call whose log filename matches (Errors-tab deep link).
function focusCall(d, filename) {
  const idx = (d.calls || []).findIndex((c) => c.filename === filename);
  if (idx < 0) return;
  const cb = $('cb-' + idx);
  const head = document.querySelector(`[data-call="${idx}"]`);
  if (cb) cb.style.display = 'block';
  if (head) {
    const ix = head.querySelector('.call-ix');
    if (ix) ix.textContent = '▾';
    head.classList.add('call-focused');
    head.scrollIntoView({ block: 'center' });
  }
}

function renderDetail(d) {
  let html = `<div class="detail-back"><button class="btn" id="back">← back to sessions</button></div>`;
  html += `<div class="panel-head detail-hero mode-${esc(d.mode)}">
    <div class="detail-title">${modeBadge(d.mode)}<h2>session</h2>${d.task_code ? taskChip(d.task_code) : ''}${d.conscious ? `<span class="badge conscious">★ conscious</span>` : ''}</div>
    <p class="sub">${fmtTime(d.started)} → ${fmtTime(d.ended)} · ${fmtDur(d.duration_seconds)}</p>
    <div class="run-mid">${seatChips(d.models)}</div>
  </div>`;

  // The request / topic — surfaced up top so the detail leads with WHAT was asked.
  html += `<div class="section request-card">${sessionRequest(d)}</div>`;

  // Overseer controls
  html += `<div class="section"><h3>Overseer</h3>`;
  html += `<div class="row">
    <button class="btn ${d.conscious ? 'primary' : ''}" id="toggle-conscious">${d.conscious ? '★ conscious (click to unmark)' : 'mark conscious'}</button>
  </div>`;
  html += `<label class="fld">Feedback</label>
    <textarea id="fb">${esc(d.feedback || '')}</textarea>
    <div class="row" style="margin-top:8px"><button class="btn primary" id="save-fb">save feedback</button></div>`;
  const prs = (d.links && d.links.prs) || [],
    tickets = (d.links && d.links.tickets) || [];
  html += `<label class="fld">PRs &amp; tickets</label>
    <div class="row" style="margin-bottom:8px">
      ${prs.map((p) => `<span class="badge pr">PR ${esc(p)} <a href="#" data-rmpr="${esc(p)}">×</a></span>`).join(' ')}
      ${tickets.map((t) => `${ticketLink(t)} <a href="#" data-rmticket="${esc(t)}">×</a>`).join(' ')}
      ${!prs.length && !tickets.length ? `<span class="muted">none</span>` : ''}
    </div>
    <div class="row">
      <input class="txt" id="pr-in" placeholder="#123" size="8" />
      <input class="txt" id="ticket-in" placeholder="HYP-742" size="12" />
      <button class="btn" id="add-link">attach</button>
      <button class="btn small" id="detect-link" title="detect ticket from current repo branch">detect from repo</button>
    </div></div>`;

  // Brainstorm transcript
  if (d.brainstorm) {
    html += `<div class="section"><h3>Brainstorm transcript</h3>`;
    html += `<div class="bs-meta"><div class="run-req"><span class="req-label">Topic</span> ${esc(d.brainstorm.topic)}</div>
      <div class="bs-panel"><span class="muted">panel</span> ${seatChips(d.brainstorm.panel)} <span class="muted">· moderator</span> ${d.brainstorm.moderator ? seatChip(d.brainstorm.moderator, { small: true }) : '<span class="muted">—</span>'}</div></div>`;
    (d.brainstorm.rounds || []).forEach((rnd) => {
      html += `<div class="bs-round"><div class="bs-round-head"><span class="round-no">Round ${esc(rnd.round)}</span> <span class="muted">${rnd.personas.length} persona${rnd.personas.length === 1 ? '' : 's'}</span></div>`;
      rnd.personas.forEach((p) => {
        html += `<div class="persona"><div class="persona-head">${roleChip(p.name)} ${seatChip(p.model, { small: true })}</div>
          <pre class="log">${esc(p.text)}</pre></div>`;
      });
      html += `</div>`;
    });
    html += `</div>`;
  }

  // Per-call logs
  html += `<div class="section"><h3>Calls (${(d.calls || []).length})</h3>`;
  if (!(d.calls || []).length && !d.brainstorm)
    html += emptyState('calls', "This session's per-call logs aged out of the log dir.");
  (d.calls || []).forEach((c, i) => {
    // codex review finding, review-cli#243 round 6: a timeout (ordinary OR true-
    // silence) is not a hard admin-level block, same distinction the Errors panel's
    // classBadge already draws (degraded/amber) vs. a hard error (err/red) -- see that
    // badge's own comment for why "retryable" is the wrong word for true-silence
    // specifically (it's SEAT_FATAL in retry.py, not retried the same way rc=124 is).
    // The per-call view was still showing both as "err" (pre-existing for timed_out; true_silenced
    // faithfully mirrored it in round 3), inconsistent with that declared semantics.
    const status = c.timed_out
      ? `<span class="badge degraded">timeout ${c.timeout_secs}s</span>`
      : c.true_silenced
        ? `<span class="badge degraded">true-silence ${c.true_silence_secs}s</span>`
        : c.has_error
          ? `<span class="badge err">error</span>`
          : c.completed === false
            ? `<span class="badge running">running</span>`
            : `<span class="badge ok">ok</span>`;
    html += `<div class="call">
      <div class="call-head" data-call="${i}">
        <span class="call-ix">▸</span>
        ${seatChip(c.model || c.backend, { small: true })}
        <span class="muted">round ${esc(c.round)}</span>
        ${status}
        <span class="muted">${fmtDur(c.duration_seconds)} · ${esc(c.size_bytes)}B</span>
        ${c.argv0 ? `<code class="call-argv" title="recorded invocation (argv[0], args redacted)">${esc(c.argv0)}</code>` : ''}
      </div>
      <div class="call-body" id="cb-${i}" style="display:none">
        <pre class="log">${renderBody(c.body)}</pre>
      </div>
    </div>`;
  });
  html += `</div>`;
  return html;
}

function renderBody(body) {
  return String(body || '')
    .split('\n')
    .map((ln) => (ln.startsWith('[stderr] ') ? `<span class="stderr">${esc(ln)}</span>` : esc(ln)))
    .join('\n');
}

function wireDetail(d) {
  const sid = d.session_id;
  $('back').onclick = () => {
    state.detail = null;
    navigate('chat');
  };
  document.querySelectorAll('[data-call]').forEach((el) => {
    el.onclick = () => {
      const cb = $('cb-' + el.dataset.call);
      const open = cb.style.display === 'none';
      cb.style.display = open ? 'block' : 'none';
      const ix = el.querySelector('.call-ix');
      if (ix) ix.textContent = open ? '▾' : '▸';
    };
  });
  $('toggle-conscious').onclick = async () => {
    const r = await api(`/api/runs/${encodeURIComponent(sid)}/conscious`, postJSON({ conscious: !d.conscious }));
    d.conscious = r.annotation.conscious;
    toast(d.conscious ? 'marked conscious' : 'unmarked');
    invalidate();
    openDetail(sid);
  };
  $('save-fb').onclick = async () => {
    const r = await api(`/api/runs/${encodeURIComponent(sid)}/feedback`, postJSON({ feedback: $('fb').value }));
    d.feedback = r.annotation.feedback;
    toast('feedback saved');
    invalidate();
  };
  $('add-link').onclick = async () => {
    const pr = $('pr-in').value.trim(),
      ticket = $('ticket-in').value.trim();
    if (!pr && !ticket) return;
    try {
      await api(
        `/api/runs/${encodeURIComponent(sid)}/links`,
        postJSON({ pr: pr || undefined, ticket: ticket || undefined }),
      );
      toast('linked');
      invalidate();
      openDetail(sid);
    } catch (e) {
      toast(e.message);
    }
  };
  $('detect-link').onclick = async () => {
    try {
      const d2 = await api('/api/detect-links');
      if (d2.tickets && d2.tickets.length) {
        $('ticket-in').value = d2.tickets[0];
        toast(`detected ${d2.tickets[0]} from branch ${d2.branch || '?'} — review & attach`);
      } else {
        toast(`no ticket in branch ${d2.branch || '(unknown)'} — type it manually`);
      }
    } catch (e) {
      toast(e.message);
    }
  };
  document.querySelectorAll('[data-rmpr]').forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      await api(`/api/runs/${encodeURIComponent(sid)}/links`, postJSON({ pr: a.dataset.rmpr, remove: true }));
      invalidate();
      openDetail(sid);
    };
  });
  document.querySelectorAll('[data-rmticket]').forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      await api(`/api/runs/${encodeURIComponent(sid)}/links`, postJSON({ ticket: a.dataset.rmticket, remove: true }));
      invalidate();
      openDetail(sid);
    };
  });
  // Seat / role chips inside the detail filter the board too (and return to the list).
  wireChips();
}

function postJSON(obj) {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj) };
}
function invalidate() {
  state.runs = null;
  loadAll();
}

// ---- live activity (Server-Sent Events) ------------------------------------
let _liveReload = null;
function setLive(text, cls) {
  const el = $('live');
  if (!el) return;
  el.textContent = text;
  el.className = 'live' + (cls ? ' ' + cls : '');
}
function liveStream() {
  if (typeof EventSource === 'undefined') {
    setLive('○ unavailable');
    return;
  }
  let es;
  try {
    es = new EventSource(`/events?gap=${state.gap}`);
  } catch {
    setLive('○ unavailable');
    return;
  }
  let flashTimer = null;
  const flash = (label) => {
    setLive(label, 'on');
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => setLive('● live'), 1200);
  };
  const scheduleReload = () => {
    if (_liveReload) return;
    _liveReload = setTimeout(() => {
      _liveReload = null;
      if (!(state.panel === 'chat' && state.detail)) {
        state.runs = null;
        loadAll();
      }
    }, 600);
  };
  es.addEventListener('open', () => setLive('● live'));
  es.addEventListener('run', () => {
    flash('● activity');
    scheduleReload();
  });
  es.addEventListener('log', (e) => {
    let d = {};
    try {
      d = JSON.parse(e.data);
    } catch {
      console.warn('live: bad log payload', e.data);
    }
    flash(d.backend ? `● ${resolveModel(d.backend).label}` : '● activity');
    scheduleReload();
  });
  es.onerror = () => {
    setLive('○ reconnecting', 'bad');
  };
}

// ---- shell -----------------------------------------------------------------
function setActiveTab(name) {
  state.panel = name;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.panel === name));
}

function applyHash() {
  const raw = (location.hash || '').replace(/^#/, '');
  if (!raw) {
    setActiveTab('overview');
    state.detail = null;
    render();
    return;
  }
  const [panel, sid] = raw.split('/');
  if (panel === 'chat' && sid) {
    state.detail = decodeURIComponent(sid);
    setActiveTab('chat');
    render();
    return;
  }
  if (PANELS[panel]) {
    state.detail = null;
    setActiveTab(panel);
    render();
  }
}
function navigate(hash) {
  if (location.hash === '#' + hash) applyHash();
  else location.hash = hash;
}

// Wire the clickable seat / role chips and filter-clear buttons present in the current panel.
function wireChips() {
  document.querySelectorAll('[data-model]').forEach((el) => {
    // Non-button rows carrying data-model (.mh-row, .usage-cell) get keyboard operability;
    // the inner seat <button> already handles its own keys and stops propagation.
    if (el.tagName !== 'BUTTON' && !el.hasAttribute('tabindex')) {
      el.setAttribute('tabindex', '0');
      el.setAttribute('role', 'button');
      el.addEventListener('keydown', (e) => {
        if (e.target !== el) return; // let an inner seat <button> handle its own key
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          el.click();
        }
      });
    }
    el.onclick = (e) => {
      e.stopPropagation();
      const m = el.dataset.model;
      state.filterModel = state.filterModel === m ? null : m;
      state.detail = null;
      // The models/stats panels don't list runs, so jump to the list-y overview to make the
      // filtered result visible. `navigate` flips the hash → hashchange → applyHash → render
      // (one path); calling render() here too would flash the old panel first. Otherwise
      // (already on a list panel, incl. the chat detail we just left) re-render in place.
      if (state.panel === 'models' || state.panel === 'stats') {
        navigate('overview');
      } else {
        render();
      }
    };
  });
  document.querySelectorAll('[data-role]').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const r = el.dataset.role;
      state.filterRole = state.filterRole === r ? null : r;
      state.detail = null;
      render();
    };
  });
  document.querySelectorAll('[data-clear]').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      const what = el.dataset.clear;
      if (what === 'model' || what === 'all') state.filterModel = null;
      if (what === 'role' || what === 'all') state.filterRole = null;
      if (what === 'task' || what === 'all') state.filterTask = null;
      render();
    };
  });
  document.querySelectorAll('[data-task]').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const t = el.dataset.task;
      state.filterTask = state.filterTask === t ? null : t;
      state.detail = null;
      if (state.panel !== 'tasks' && state.panel !== 'overview' && state.panel !== 'chat') {
        navigate('tasks');
      } else {
        render();
      }
    };
  });
}

function render() {
  if (state.panel === 'chat' && state.detail) {
    openDetail(state.detail);
    return;
  }
  const fn = PANELS[state.panel] || PANELS.overview;
  $('panel').innerHTML = fn();
  // wire run rows (open detail). A click on a seat/role chip inside a row is handled by
  // wireChips and stops propagation, so it won't also open the session.
  document.querySelectorAll("[data-open='chat']").forEach((el) => {
    // Make the whole row keyboard-operable, not just the inner <button> chips (the CSS claims
    // a keyboard-focus quality floor, so the row itself must be focusable + Enter/Space open).
    el.setAttribute('tabindex', '0');
    el.setAttribute('role', 'button');
    const open = (e) => {
      if (
        e.target.closest('[data-conscious]') ||
        e.target.closest('[data-model]') ||
        e.target.closest('[data-role]') ||
        e.target.closest('[data-task]') ||
        e.target.closest('[data-manual]')
      )
        return;
      // An error card carries the failing call's filename so the detail can auto-expand it.
      openDetail(el.dataset.sid, el.dataset.callFile || null);
    };
    el.onclick = open;
    el.onkeydown = (e) => {
      // Only the ROW itself activates on Enter/Space. A keypress on an inner seat/role
      // <button> must keep its own native activation — calling preventDefault on the
      // bubbled keydown here would cancel that button's click (and `open` would early-return
      // on the chip target anyway), silently breaking keyboard use of the chips.
      if (e.target !== el) return;
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        open(e);
      }
    };
  });
  document.querySelectorAll('[data-conscious]').forEach((el) => {
    el.onclick = async (e) => {
      e.stopPropagation();
      await api(
        `/api/runs/${encodeURIComponent(el.dataset.conscious)}/conscious`,
        postJSON({ conscious: el.dataset.val === '1' }),
      );
      toast('updated');
      invalidate();
    };
  });
  document.querySelectorAll('[data-manual]').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      e.preventDefault();
      openManualControl(el.dataset.manual, el.dataset.manualModel, el.dataset.manualFile || null);
    };
  });
  wireChips();
}

// Take manual control of a stuck/unrecovered run: open its detail (where the overseer can
// mark it conscious, leave feedback on what to do next, and attach the PR/ticket the manual
// follow-up lands under) and prime the feedback box with a manual-control note. This is the
// overseer's escape hatch when auto-failover is exhausted — review-cli has no live "retry this
// seat" RPC, so the honest action is to hand the run to the human with the context loaded.
async function openManualControl(sid, model, callFile) {
  const m = resolveModel(model);
  toast(`manual control: ${m.label} — opening run for overseer follow-up`);
  state.manualSeed = {
    sid,
    text: `MANUAL CONTROL — auto-failover exhausted for ${m.label}. Next step (decide & record): rerun with a different seat, fix the backend (key/paywall/block), or accept the partial result.`,
  };
  // Land on the failing call (so the overseer sees what failed) AND prime the feedback box.
  openDetail(sid, callFile);
}

function boot() {
  document.querySelectorAll('.tab').forEach((t) => {
    t.onclick = () => navigate(t.dataset.panel);
  });
  $('refresh').onclick = () => {
    state.detail = null;
    loadAll();
    toast('refreshed');
  };
  window.addEventListener('hashchange', applyHash);
  health();
  loadAll().then(applyHash);
  setInterval(health, 15000);
  liveStream();
}

document.addEventListener('DOMContentLoaded', boot);

// --- test hook (browser no-op) ----------------------------------------------
// In the browser `module` is undefined, so this whole block is skipped — the SPA is
// unaffected. Under Node (`node --test`) it exposes the PURE resolution/filter functions
// (and the `state` object the filter reads) so they can be unit-tested without a DOM.
// The test harness stubs `window`/`document` before requiring this file so the two
// top-level browser calls above (`window.onImgError = …`, `document.addEventListener`)
// are harmless no-ops. Keeping the export here (not a separate module) means the tests
// exercise the EXACT code the browser runs — no copy that can drift.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { resolveModel, filteredRuns, monogram, cap, state, PANELS };
}
