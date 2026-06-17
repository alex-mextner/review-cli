/* review-cli dashboard SPA — vanilla JS, no build step, no framework.
 * Talks to the local-only stdlib server's /api/* JSON endpoints. All rendering is
 * string-templated with strict HTML escaping (logs contain arbitrary model output). */
'use strict';

const state = {
  panel: 'overview',
  runs: null, // [summary]
  stats: null,
  detail: null, // session_id currently open in chat panel
  gap: 90,
};

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
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
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
  return `<span class="badge mode-${esc(m)}">${esc(m)}</span>`;
}
function $(id) {
  return document.getElementById(id);
}

// ---- data loading ----------------------------------------------------------
async function loadAll() {
  try {
    const [runs, stats] = await Promise.all([api(`/api/runs?gap=${state.gap}`), api(`/api/stats?gap=${state.gap}`)]);
    state.runs = runs;
    state.stats = stats;
    render();
  } catch (e) {
    $('panel').innerHTML = `<div class="empty">Failed to load data: ${esc(e.message)}</div>`;
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

// ---- panels ----------------------------------------------------------------
const PANELS = {};

function emptyState(what, note) {
  return `<div class="empty"><strong>No ${esc(what)} yet.</strong>${
    note ? `<div class="note">${note}</div>` : ''
  }</div>`;
}

PANELS.overview = () => {
  const s = state.stats || {};
  const runs = state.runs || [];
  const cards = [
    ['sessions', s.session_count],
    ['calls', s.call_count],
    ['success rate', s.success_rate != null ? Math.round(s.success_rate * 100) + '%' : '—'],
    ['errors', s.error_calls],
    ['timeouts', s.timeout_calls],
    ['conscious', s.conscious_count],
    ['feedback', s.feedback_count],
  ];
  let html = `<h2>Overview</h2><p class="sub">Sessions are time-clustered bursts of backend calls (review-cli emits no run id; gap = ${state.gap}s).</p>`;
  html += `<div class="cards">${cards
    .map(
      ([l, n]) =>
        `<div class="card"><div class="num">${esc(n == null ? '—' : n)}</div><div class="lbl">${esc(l)}</div></div>`,
    )
    .join('')}</div>`;
  html += `<div class="section"><h3>Recent sessions</h3>`;
  html += runs.length
    ? `<div class="list">${runs.slice(0, 12).map(runRow).join('')}</div>`
    : emptyState(
        'sessions',
        'Run <code>review</code> / <code>review --quorum</code> / <code>review --brainstorm</code> and the per-call logs will appear here.',
      );
  html += `</div>`;
  return html;
};

function runRow(r) {
  const badges = [
    modeBadge(r.mode),
    r.has_error
      ? `<span class="badge err">${r.error_count} err</span>`
      : r.running
        ? `<span class="badge running">running</span>`
        : `<span class="badge ok">ok</span>`,
    r.conscious ? `<span class="badge conscious">★ conscious</span>` : '',
    ((r.links && r.links.prs) || []).map((p) => `<span class="badge pr">${esc(p)}</span>`).join(''),
    ((r.links && r.links.tickets) || []).map((t) => `<span class="badge ticket">${esc(t)}</span>`).join(''),
  ].join(' ');
  // Body line: the brainstorm topic when present, else the recorded invocation(s) so a
  // panel/review row isn't blank (the literal prompt is redacted in the logs).
  const inv = r.invocations || [];
  let bodyLine = '';
  if (r.topic) {
    bodyLine = `<div class="run-body muted">${esc(r.topic.slice(0, 160))}${r.topic.length > 160 ? '…' : ''}</div>`;
  } else if (inv.length) {
    const joined = inv.join(', ');
    bodyLine = `<div class="run-body muted">${esc(joined.slice(0, 160))}${joined.length > 160 ? '…' : ''}</div>`;
  }
  const fb = r.feedback
    ? `<div class="run-body" style="color:var(--amber)">📝 ${esc(r.feedback.slice(0, 120))}</div>`
    : '';
  return `<div class="run" data-sid="${esc(r.session_id)}" data-open="chat">
    <div class="run-head">
      ${badges}
      <span class="run-time">${fmtTime(r.started)}</span>
      <span class="run-models">${esc((r.models || []).join(', ') || '—')}</span>
      <span class="run-models">· ${r.call_count} call(s) · ${fmtDur(r.duration_seconds)}</span>
    </div>${bodyLine}${fb}
  </div>`;
}

PANELS.chat = () => {
  if (state.detail) return ''; // detail view renders async (see openDetail)
  const runs = state.runs || [];
  let html = `<h2>Chat logs</h2><p class="sub">Per-run transcripts — the streamed multi-model panel conversations. Click a session to open the full chat.</p>`;
  html += runs.length
    ? `<div class="list">${runs.map(runRow).join('')}</div>`
    : emptyState('transcripts', 'No call logs found in the log dir.');
  return html;
};

PANELS.stats = () => {
  const s = state.stats;
  if (!s || !s.session_count) return `<h2>Stats</h2>` + emptyState('stats', 'No sessions to aggregate.');
  let html = `<h2>Stats</h2><p class="sub">Runs over time and counts by mode/model/role.</p>`;
  html += `<div class="section"><h3>Sessions per day</h3>${barChart(s.by_day)}</div>`;
  html += `<div class="section"><h3>By mode</h3>${barChart(s.by_mode)}</div>`;
  html += `<div class="section"><h3>By model</h3>${barChart(s.by_model)}</div>`;
  if (s.by_role && Object.keys(s.by_role).length)
    html += `<div class="section"><h3>By role / persona</h3>${barChart(s.by_role)}</div>`;
  return html;
};

PANELS.models = () => {
  const s = state.stats;
  if (!s) return `<h2>Models &amp; roles</h2>` + emptyState('data');
  let html = `<h2>Models &amp; roles</h2><p class="sub">Which models and personas were used, and in what roles.</p>`;
  const models = Object.entries(s.by_model || {}).sort((a, b) => b[1] - a[1]);
  html += `<div class="section"><h3>Models</h3>`;
  html += models.length
    ? `<div class="list">${models
        .map(
          ([m, n]) =>
            `<div class="run"><div class="run-head"><span class="tag">${esc(m)}</span><span class="run-models">used in ${n} session(s)</span></div></div>`,
        )
        .join('')}</div>`
    : emptyState('models');
  html += `</div>`;
  const roles = Object.entries(s.by_role || {}).sort((a, b) => b[1] - a[1]);
  html += `<div class="section"><h3>Roles / personas <span class="muted">(brainstorm panels)</span></h3>`;
  html += roles.length
    ? `<div class="list">${roles
        .map(
          ([r, n]) =>
            `<div class="run"><div class="run-head"><span class="badge mode-brainstorm">${esc(r)}</span><span class="run-models">${n} appearance(s)</span></div></div>`,
        )
        .join('')}</div>`
    : emptyState('roles', 'Roles/personas are only recorded for <code>review --brainstorm</code> runs.');
  html += `</div>`;
  return html;
};

PANELS.metrics = () => {
  const s = state.stats;
  if (!s) return `<h2>Metrics</h2>` + emptyState('metrics');
  const d = s.duration_seconds || {};
  let html = `<h2>Metrics</h2><p class="sub">Durations and success/fail rates (durations = file create → last write, the honest proxy since review-cli records no explicit duration).</p>`;
  html += `<table class="kv">
    <tr><td>Total calls</td><td>${esc(s.call_count)}</td></tr>
    <tr><td>OK calls</td><td>${esc(s.ok_calls)}</td></tr>
    <tr><td>Error calls</td><td>${esc(s.error_calls)}</td></tr>
    <tr><td>Timeout calls</td><td>${esc(s.timeout_calls)}</td></tr>
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
  const runs = state.runs || [];
  const withFb = runs.filter((r) => r.feedback);
  let html = `<h2>Overseer feedback</h2><p class="sub">Feedback the overseer left on runs (persisted in <code>dashboard.json</code>). Open any session below to add or edit.</p>`;
  html += `<div class="section"><h3>Feedback left (${withFb.length})</h3>`;
  html += withFb.length
    ? `<div class="list">${withFb
        .map(
          (r) =>
            `<div class="run" data-sid="${esc(r.session_id)}" data-open="chat">
      <div class="run-head">${modeBadge(r.mode)}<span class="run-time">${fmtTime(r.started)}</span></div>
      <div class="run-body" style="color:var(--amber)">📝 ${esc(r.feedback)}</div>
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
  if (!s || !s.by_mode) return `<h2>Modes</h2>` + emptyState('modes');
  let html = `<h2>Modes</h2><p class="sub">Breakdown by review mode. Mode is INFERRED from the call/round shape — review-cli does not stamp it in the log.</p>`;
  html += barChart(s.by_mode);
  const runs = state.runs || [];
  const byMode = {};
  runs.forEach((r) => {
    (byMode[r.mode] = byMode[r.mode] || []).push(r);
  });
  Object.keys(byMode)
    .sort()
    .forEach((m) => {
      html += `<div class="section"><h3>${esc(m)} (${byMode[m].length})</h3><div class="list">${byMode[m].slice(0, 8).map(runRow).join('')}</div></div>`;
    });
  return html;
};

PANELS.errors = () => {
  const runs = (state.runs || []).filter((r) => r.has_error);
  let html = `<h2>Errors</h2><p class="sub">Failed / timed-out runs with error details.</p>`;
  html += runs.length
    ? `<div class="list">${runs.map(runRow).join('')}</div>`
    : emptyState('errors', 'No failed runs in the current log window.');
  return html;
};

PANELS.tasks = () => {
  const runs = state.runs || [];
  const conscious = runs.filter((r) => r.conscious);
  let html = `<h2>Tasks</h2><p class="sub">Mark a session as <strong>conscious</strong> (deliberately reviewed / acted on). Conscious-marked sessions are surfaced first.</p>`;
  html += `<div class="section"><h3>Conscious sessions (${conscious.length})</h3>`;
  html += conscious.length
    ? `<div class="list">${conscious.map(taskRow).join('')}</div>`
    : emptyState('conscious sessions', 'Toggle “mark conscious” on any session below.');
  html += `</div><div class="section"><h3>All sessions</h3><div class="list">${runs.map(taskRow).join('')}</div></div>`;
  return html;
};

function taskRow(r) {
  return `<div class="run">
    <div class="run-head">
      ${modeBadge(r.mode)}
      ${r.conscious ? `<span class="badge conscious">★ conscious</span>` : ''}
      <span class="run-time">${fmtTime(r.started)}</span>
      <span class="run-models">${esc((r.models || []).join(', '))}</span>
      <button class="btn small" data-conscious="${esc(r.session_id)}" data-val="${r.conscious ? '0' : '1'}">
        ${r.conscious ? 'unmark' : 'mark conscious'}
      </button>
      <button class="btn small" data-sid="${esc(r.session_id)}" data-open="chat">open</button>
    </div>
  </div>`;
}

// The prompt body for a run row in the Prompts panel. review-cli redacts the literal
// prompt/diff, so the durable signal is the brainstorm topic (when present) and otherwise
// the recorded invocation(s) — the command/endpoint each backend was called with. Show the
// most specific thing we have, falling back to the redacted note only when nothing was
// recorded (e.g. a brainstorm whose per-call logs aged out and carried no topic).
function promptBody(r) {
  if (r.topic) return `<div class="run-body"><strong>Topic:</strong> ${esc(r.topic)}</div>`;
  const inv = r.invocations || [];
  if (inv.length)
    return `<div class="run-body"><strong>Invoked:</strong> ${inv.map((i) => `<code>${esc(i)}</code>`).join(', ')} <span class="muted">· prompt/diff redacted in logs</span></div>`;
  return `<div class="run-body muted">prompt redacted in logs — argv only</div>`;
}

PANELS.prompts = () => {
  const runs = state.runs || [];
  let html = `<h2>Prompts</h2><p class="sub">The prompts/argv used per run. review-cli REDACTS the prompt/diff from logs (they may carry secrets) — only the invoked command (argv[0]) is recorded, plus the brainstorm topic where present. Open a session for full per-call detail.</p>`;
  html += runs.length
    ? `<div class="list">${runs
        .map(
          (r) =>
            `<div class="run" data-sid="${esc(r.session_id)}" data-open="chat">
      <div class="run-head">${modeBadge(r.mode)}<span class="run-time">${fmtTime(r.started)}</span></div>
      ${promptBody(r)}
    </div>`,
        )
        .join('')}</div>`
    : emptyState('prompts');
  return html;
};

PANELS.links = () => {
  const runs = state.runs || [];
  const linked = runs.filter((r) => r.links && ((r.links.prs || []).length || (r.links.tickets || []).length));
  let html = `<h2>PRs &amp; tickets</h2><p class="sub">Each run links to the PRs/tickets it touched. Attach a PR# or HYP-id from a session's panel (auto-detected from the current repo branch where possible).</p>`;
  html += `<div class="section"><h3>Linked sessions (${linked.length})</h3>`;
  html += linked.length
    ? `<div class="list">${linked
        .map(
          (r) =>
            `<div class="run" data-sid="${esc(r.session_id)}" data-open="chat">
      <div class="run-head">${modeBadge(r.mode)}<span class="run-time">${fmtTime(r.started)}</span>
        ${(r.links.prs || []).map((p) => prLink(p)).join(' ')}
        ${(r.links.tickets || []).map((t) => ticketLink(t)).join(' ')}
      </div>
    </div>`,
        )
        .join('')}</div>`
    : emptyState('links', 'Open a session and attach a PR# / ticket in its panel.');
  html += `</div><div class="section"><h3>All sessions</h3><div class="list">${runs.map(runRow).join('')}</div></div>`;
  return html;
};

function prLink(p) {
  // local-only dashboard: render as a labelled badge (we don't know the repo origin).
  return `<span class="badge pr">${esc(p)}</span>`;
}
function ticketLink(t) {
  const url = 'https://linear.app/glide-vc/issue/' + encodeURIComponent(t);
  return `<a class="badge ticket" target="_blank" rel="noreferrer noopener" href="${esc(url)}">${esc(t)}</a>`;
}

// ---- bar chart -------------------------------------------------------------
function barChart(obj) {
  const entries = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
  if (!entries.length) return `<div class="muted">no data</div>`;
  const max = Math.max(...entries.map((e) => e[1])) || 1;
  return entries
    .map(
      ([k, v]) =>
        `<div class="bar-row"><div class="k" title="${esc(k)}">${esc(k)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${((v / max) * 100).toFixed(1)}%"></div></div>
      <div class="v">${esc(v)}</div></div>`,
    )
    .join('');
}

// ---- session detail (chat transcript) -------------------------------------
async function openDetail(sid) {
  state.detail = sid;
  setActiveTab('chat');
  // keep the URL deep-linkable without re-triggering applyHash
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
}

function renderDetail(d) {
  let html = `<div class="detail-back"><button class="btn" id="back">← back to sessions</button></div>`;
  html += `<h2>${modeBadge(d.mode)} session</h2>`;
  html += `<p class="sub">${fmtTime(d.started)} → ${fmtTime(d.ended)} · ${fmtDur(d.duration_seconds)} · models: ${esc((d.models || []).join(', ') || '—')}</p>`;

  // Overseer controls
  html += `<div class="section"><h3>Overseer</h3>`;
  html += `<div class="row">
    <button class="btn ${d.conscious ? 'primary' : ''}" id="toggle-conscious">${d.conscious ? '★ conscious (click to unmark)' : 'mark conscious'}</button>
  </div>`;
  html += `<label class="fld">Feedback</label>
    <textarea id="fb">${esc(d.feedback || '')}</textarea>
    <div class="row" style="margin-top:8px"><button class="btn primary" id="save-fb">save feedback</button></div>`;
  // links
  const prs = (d.links && d.links.prs) || [],
    tickets = (d.links && d.links.tickets) || [];
  html += `<label class="fld">PRs &amp; tickets</label>
    <div class="row" style="margin-bottom:8px">
      ${prs.map((p) => `<span class="badge pr">${esc(p)} <a href="#" data-rmpr="${esc(p)}">×</a></span>`).join(' ')}
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
    html += `<div class="run-body"><strong>Topic:</strong> ${esc(d.brainstorm.topic)}</div>`;
    html += `<div class="run-body muted">panel: ${esc((d.brainstorm.panel || []).join(', '))} · moderator: ${esc(d.brainstorm.moderator || '—')}</div>`;
    (d.brainstorm.rounds || []).forEach((rnd) => {
      html += `<div class="call"><div class="call-head"><strong>Round ${esc(rnd.round)}</strong> <span class="muted">${rnd.personas.length} persona(s)</span></div>`;
      html += `<div class="call-body">`;
      rnd.personas.forEach((p) => {
        html += `<div class="section"><span class="badge mode-brainstorm">${esc(p.name)}</span> <span class="tag">${esc(p.model)}</span>
          <pre class="log">${esc(p.text)}</pre></div>`;
      });
      html += `</div></div>`;
    });
    html += `</div>`;
  }

  // Per-call logs
  html += `<div class="section"><h3>Calls (${(d.calls || []).length})</h3>`;
  if (!(d.calls || []).length && !d.brainstorm)
    html += emptyState('calls', "This session's per-call logs aged out of the log dir.");
  (d.calls || []).forEach((c, i) => {
    const status = c.timed_out
      ? `<span class="badge err">timeout ${c.timeout_secs}s</span>`
      : c.has_error
        ? `<span class="badge err">error</span>`
        : c.completed === false
          ? `<span class="badge running">running</span>`
          : `<span class="badge ok">ok</span>`;
    html += `<div class="call">
      <div class="call-head" data-call="${i}">
        <span class="tag">${esc(c.backend)}</span>
        <span class="muted">round ${esc(c.round)}</span>
        ${status}
        <span class="muted">${fmtDur(c.duration_seconds)} · ${esc(c.size_bytes)}B</span>
        <span class="muted" style="margin-left:auto">${esc(c.argv0)}</span>
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
  // Highlight [stderr] lines.
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
      cb.style.display = cb.style.display === 'none' ? 'block' : 'none';
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
    // Logs carry no per-session cwd, so we detect from the branch of the dir the
    // dashboard was launched in (usually the repo under review). Prefill, don't auto-save.
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
}

function postJSON(obj) {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj) };
}
function invalidate() {
  state.runs = null;
  loadAll();
}

// ---- live activity (Server-Sent Events) ------------------------------------
// Subscribe to /events: the server tails the log dir and pushes a `run` summary (and a
// per-file `log` event) whenever a review writes/appends an artifact. We coalesce a burst
// of events into one reload so a streaming brainstorm doesn't hammer the API, and flash a
// "live" indicator so it's visible the stream is connected.
let _liveReload = null;
function setLive(text, cls) {
  const el = $('live');
  if (!el) return;
  el.textContent = text;
  el.className = 'live' + (cls ? ' ' + cls : '');
}
function liveStream() {
  if (typeof EventSource === 'undefined') {
    setLive('○ unavailable'); // very old browser: stays on manual refresh
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
    if (_liveReload) return; // already coalescing this burst
    _liveReload = setTimeout(() => {
      _liveReload = null;
      // Don't yank a session detail the user is reading; refresh list-y panels only.
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
    flash(d.backend ? `● ${d.backend}` : '● activity');
    // A brainstorm streams `log` events for a long time before its session window closes
    // (so no `run` lands until the end). Reload on `log` too — coalesced — so the list
    // reflects an in-progress run, not just the indicator flash.
    scheduleReload();
  });
  es.onerror = () => {
    setLive('○ reconnecting', 'bad');
    // EventSource auto-reconnects (honoring the server's `retry:`); nothing else to do.
  };
}

// ---- shell -----------------------------------------------------------------
function setActiveTab(name) {
  state.panel = name;
  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.panel === name));
}

// Hash routing so tabs (and an open session) are deep-linkable / screenshot-able:
//   #stats, #models, …  -> panel
//   #chat/<session_id>  -> open that session's transcript
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

function render() {
  if (state.panel === 'chat' && state.detail) {
    openDetail(state.detail);
    return;
  }
  const fn = PANELS[state.panel] || PANELS.overview;
  $('panel').innerHTML = fn();
  // wire run rows
  document.querySelectorAll("[data-open='chat']").forEach((el) => {
    el.onclick = (e) => {
      if (e.target.closest('[data-conscious]')) return;
      openDetail(el.dataset.sid);
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
  // Load data first, then apply the initial hash so a deep-linked panel/session renders.
  loadAll().then(applyHash);
  setInterval(health, 15000);
  // Live stream: push updates as reviews run, so the dashboard updates without a refresh.
  liveStream();
}

document.addEventListener('DOMContentLoaded', boot);
