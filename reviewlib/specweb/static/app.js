/* Spec-web reviewer — vanilla JS, no deps. Single implicit reviewer (no author field).
 *
 * Flow: fetch the server-rendered spec HTML + headings, inject it; let the reviewer
 * SELECT text to open a popup -> composer -> POST a note. A note is a QUESTION (expects an
 * answer from the spec author) or a REMARK (feedback that does not); the kind is shown via
 * a coloured chip + icon. Notes enter the pending batch; "Submit review" flips them to
 * submitted; answer inline via a reply box that threads under each note; each note can be
 * Edited in place. On reload, notes re-anchor by locating their quote within the recorded
 * section (pragmatic quote-within-section search); an unfindable quote shows as
 * "unanchored". Internal cross-reference links push the prior scroll position so "← Back"
 * returns there. Desktop = two panes; mobile = comments as a bottom sheet that collapses to
 * its header bar (which carries a count badge so added notes are visible while collapsed).
 */
(function () {
  'use strict';

  var els = {
    specBody: document.getElementById('specBody'),
    specTitle: document.getElementById('specTitle'),
    specPane: document.getElementById('specPane'),
    layout: document.getElementById('layout'),
    navBack: document.getElementById('navBack'),
    commentsList: document.getElementById('commentsList'),
    commentsEmpty: document.getElementById('commentsEmpty'),
    pendingTray: document.getElementById('pendingTray'),
    pendingLabel: document.getElementById('pendingLabel'),
    submitReview: document.getElementById('submitReview'),
    filterSelect: document.getElementById('filterSelect'),
    commentsCount: document.getElementById('commentsCount'),
    selPopup: document.getElementById('selPopup'),
    selRemark: document.getElementById('selRemark'),
    selQuestion: document.getElementById('selQuestion'),
    composerBackdrop: document.getElementById('composerBackdrop'),
    composer: document.getElementById('composer'),
    composerHead: document.getElementById('composerHead'),
    composerHint: document.getElementById('composerHint'),
    composerQuote: document.getElementById('composerQuote'),
    composerDraftNote: document.getElementById('composerDraftNote'),
    composerBody: document.getElementById('composerBody'),
    composerCancel: document.getElementById('composerCancel'),
    composerSubmit: document.getElementById('composerSubmit'),
    commentsCollapse: document.getElementById('commentsCollapse'),
    divider: document.getElementById('divider'),
  };

  var state = {
    headings: [], // [{level,text,id}]
    comments: [], // server comments
    drafts: {}, // slot -> {body,kind,quote,section_id,section_title,start,end,updated}
    filter: 'all',
    pendingSelection: null, // {quote, section_id, section_title, start, end}
    pendingKind: 'remark', // 'question' | 'remark' for the OPEN composer
    editingId: null, // comment id being edited inline in the composer (null = new note)
    activeDraftSlot: null, // the draft slot the OPEN composer autosaves to (null = closed)
    // Internal-link navigation history: each entry is the spec pane's scrollTop at the
    // moment an in-doc link was followed, so "← Back" returns to the exact prior position.
    navHistory: [],
  };

  // The author stamped on a reply left by the AGENT (`review spec-web reply`), mirrored
  // from store.AGENT_AUTHOR — drives the distinct agent-reply styling/badge in the UI.
  // SYNC: reviewlib/specweb/store.py AGENT_AUTHOR (a test asserts they match).
  var AGENT_AUTHOR = 'agent';
  // Composer autosave debounce: persist the in-progress draft to the server this long
  // after the last keystroke, so a page reload mid-typing never loses the text.
  var DRAFT_DEBOUNCE_MS = 500;
  // Poll the comments API on this interval so an OPEN page picks up an agent reply left via
  // `review spec-web reply` (and any other out-of-band change) without a manual refresh.
  var COMMENTS_POLL_MS = 5000;
  // The fixed slot id of the new-note composer draft, and the edit-slot format.
  // SYNC: reviewlib/specweb/store.py NEW_DRAFT_SLOT / edit_draft_slot (a test asserts match).
  var NEW_DRAFT_SLOT = 'new';
  function editDraftSlot(id) {
    return 'edit:' + id;
  }

  // A note is a QUESTION (expects an answer from the spec author) or a REMARK (feedback
  // that does not). Single source of truth for each kind's label/icon/hint.
  var KINDS = {
    question: {
      label: 'Question',
      icon: '❓',
      hint: 'A question expects an answer from the spec author.',
      replyLabel: 'Answer', // a question is answered
    },
    remark: {
      label: 'Remark',
      icon: '💬',
      hint: 'A remark is feedback that does not expect an answer.',
      replyLabel: 'Reply', // a remark is replied to
    },
  };
  function kindOf(c) {
    return c && c.kind === 'question' ? 'question' : 'remark';
  }
  // Cap the internal-link nav history so a long chain of cross-references can't grow it
  // unboundedly (oldest entries are dropped first).
  var MAX_NAV_HISTORY = 50;

  // ---- helpers ------------------------------------------------------------
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function api(method, url, body, signal) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    if (signal) opts.signal = signal;
    return fetch(url, opts).then(function (r) {
      if (!r.ok)
        return r.json().then(function (j) {
          throw new Error(j.error || r.statusText);
        });
      return r.json();
    });
  }

  // Look up a heading id WITHIN the rendered spec pane only. document.getElementById would
  // return an app-shell element when a spec heading's slug collides with a shell id (e.g. a
  // `# Layout` heading -> "layout", which also names the outer <div id="layout">). Scoping
  // to els.specBody keeps internal links + re-anchoring pointed at the real heading.
  function specEl(id) {
    if (!id) return null;
    var nodes = els.specBody.querySelectorAll('[id]');
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].id === id) return nodes[i];
    }
    return null;
  }

  // ---- load ---------------------------------------------------------------
  function loadSpec() {
    return api('GET', '/api/spec')
      .then(function (data) {
        state.headings = data.headings || [];
        els.specBody.innerHTML = data.html || '<p>(empty spec)</p>';
        if (data.title) els.specTitle.textContent = data.title;
        wireInDocLinks();
      })
      .catch(function (e) {
        els.specBody.innerHTML = '<div class="loading">Failed to load spec: ' + esc(e.message) + '</div>';
      });
  }
  function loadComments() {
    return api('GET', '/api/comments').then(function (list) {
      state.comments = Array.isArray(list) ? list : [];
      renderComments();
      reanchorAll();
    });
  }
  // Drafts are the reviewer's in-progress composer text, persisted server-side (debounced)
  // so a reload never loses a half-typed note. Fetched on boot; the newest one is restored
  // into the composer (see restoreDraftOnLoad).
  function loadDrafts() {
    return api('GET', '/api/drafts')
      .then(function (map) {
        state.drafts = map && typeof map === 'object' ? map : {};
      })
      .catch(function () {
        state.drafts = {};
      });
  }

  // In-doc anchor links (e.g. [§9.4](#94-...)) scroll inside the spec pane + flash. Each
  // jump PUSHES the current scroll position onto a small history so "← Back" returns the
  // reader to where they were before following the cross-reference (the #1 mobile gripe:
  // a tap on an internal link strands you with no way back).
  function wireInDocLinks() {
    var links = els.specBody.querySelectorAll('a[href^="#"]');
    Array.prototype.forEach.call(links, function (a) {
      a.addEventListener('click', function (e) {
        var id = a.getAttribute('href').slice(1);
        var t = specEl(id);
        if (t) {
          e.preventDefault();
          pushNavHistory();
          scrollSpecTo(t);
        }
      });
    });
  }
  // The spec scrolls INSIDE .spec-pane (overflow-y:auto), not the window — read/write that
  // element's scrollTop, never window.scrollY, or Back would jump to the wrong place.
  function pushNavHistory() {
    state.navHistory.push(els.specPane.scrollTop);
    if (state.navHistory.length > MAX_NAV_HISTORY) state.navHistory.shift();
    updateBackButton();
  }
  function navBack() {
    if (!state.navHistory.length) return;
    var top = state.navHistory.pop();
    els.specPane.scrollTo({ top: top, behavior: 'smooth' });
    updateBackButton();
  }
  function updateBackButton() {
    els.navBack.hidden = state.navHistory.length === 0;
  }
  function scrollSpecTo(el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    el.classList.add('flash');
    setTimeout(function () {
      el.classList.remove('flash');
    }, 1400);
  }

  // ---- selection -> popup -------------------------------------------------
  function sectionForNode(node) {
    var el = node.nodeType === 3 ? node.parentNode : node;
    // If the selection is INSIDE a heading, that heading IS the section (compareDocument-
    // Position is 0 for the element vs itself, so the loop below would otherwise skip it and
    // wrongly record the PREVIOUS heading).
    var ownHeading = el && el.closest ? el.closest('.md-h') : null;
    if (ownHeading) {
      return { id: ownHeading.id, title: (ownHeading.textContent || '').replace(/#$/, '').trim() };
    }
    // Else walk back to the nearest PRECEDING heading.
    var headings = els.specBody.querySelectorAll('.md-h');
    var best = null;
    Array.prototype.forEach.call(headings, function (h) {
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) {
        best = h; // h precedes el
      }
    });
    if (!best) return { id: '', title: '' };
    return { id: best.id, title: (best.textContent || '').replace(/#$/, '').trim() };
  }

  function sectionText(sectionId) {
    // The concatenated text of a section = from its heading up to the next same-or-higher
    // heading. Used for char-offset hints + fuzzy re-anchoring.
    var h = sectionId ? specEl(sectionId) : null;
    if (!h) return els.specBody.textContent || '';
    var level = parseInt((h.tagName || 'H6').slice(1), 10) || 6;
    var text = '';
    var n = h.nextSibling;
    while (n) {
      if (n.nodeType === 1 && /^H[1-6]$/.test(n.tagName)) {
        var lv = parseInt(n.tagName.slice(1), 10);
        if (lv <= level) break;
      }
      text += n.textContent || '';
      n = n.nextSibling;
    }
    return text;
  }

  // A selection lives inside the spec body when BOTH endpoints are within it. Checking only
  // anchorNode misses a selection dragged from outside in, and (on touch) iOS sometimes
  // reports the anchor on a boundary node; require focusNode too so we never act on a
  // selection that is only partly in the spec.
  function selectionInSpec(sel) {
    if (!sel || sel.rangeCount === 0) return false;
    return els.specBody.contains(sel.anchorNode) && els.specBody.contains(sel.focusNode);
  }

  // Single input-agnostic entry point. Called from BOTH pointerup (immediate, desktop-
  // friendly) and a debounced selectionchange (the signal iOS Safari actually delivers when
  // the native selection finalizes — it does not emit a clean pointerup over the text). One
  // code path for mouse, touch and pen: no per-device branching.
  function onSelection() {
    // While the composer modal is open the pendingSelection is already captured; a late
    // selectionchange/pointerup must not re-show the popup behind the dialog.
    if (!els.composerBackdrop.hidden) return;
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) {
      hidePopup();
      return;
    }
    var text = (sel.toString() || '').trim();
    if (!text || !selectionInSpec(sel)) {
      hidePopup();
      return;
    }
    var range = sel.getRangeAt(0);
    var sec = sectionForNode(sel.anchorNode);
    // Record the ACTUAL char offset of this selection within its section (not the first
    // indexOf match) so a quote that repeats in the section re-anchors to the occurrence
    // the user actually selected, not the first one.
    var start = selectionOffsetInSection(sec.id, range);
    // CAPTURE the selected text + anchor/offsets NOW, at show time, and stash it. iOS clears
    // the live selection the instant the user taps the popup (the native callout dismisses
    // it), so reading window.getSelection() at button-tap time returns empty — every later
    // read (openComposer, submit) must come from this stash, never from getSelection().
    state.pendingSelection = {
      quote: text,
      section_id: sec.id,
      section_title: sec.title,
      start: start != null ? start : null,
      end: start != null ? start + text.length : null,
    };
    var rect = range.getBoundingClientRect();
    showPopup(rect);
  }

  // selectionchange fires continuously while a selection is being dragged/extended; debounce
  // so we only act once the selection has SETTLED (this many ms of quiet). This is the
  // reliable iOS finalize signal; on desktop pointerup already fired, so it is a harmless
  // re-confirm.
  var SELECTION_SETTLE_MS = 200;
  // A real pointer emits pointerup THEN a synthetic click a few hundred ms later; suppress
  // the trailing click within this window so the popup button does not fire twice.
  var POINTER_CLICK_ECHO_MS = 700;
  var selectionChangeTimer = null;
  function cancelPendingSelectionCheck() {
    if (selectionChangeTimer) {
      clearTimeout(selectionChangeTimer);
      selectionChangeTimer = null;
    }
  }
  function scheduleSelectionCheck() {
    cancelPendingSelectionCheck();
    selectionChangeTimer = setTimeout(function () {
      selectionChangeTimer = null;
      onSelection();
    }, SELECTION_SETTLE_MS);
  }

  // Char offset of a range's start within the concatenated text of its section. Measures
  // the text from the section heading up to the range start; matches sectionText()'s walk.
  function selectionOffsetInSection(sectionId, range) {
    var h = sectionId ? specEl(sectionId) : null;
    var startNode = range.startContainer;
    var startOff = range.startOffset;
    var offset = 0;
    var nodes;
    if (h) {
      var level = parseInt((h.tagName || 'H6').slice(1), 10) || 6;
      nodes = [];
      var n = h.nextSibling;
      while (n) {
        if (n.nodeType === 1 && /^H[1-6]$/.test(n.tagName) && parseInt(n.tagName.slice(1), 10) <= level) break;
        nodes.push(n);
        n = n.nextSibling;
      }
    } else {
      nodes = [els.specBody];
    }
    var found = false;
    function walk(node) {
      if (found) return;
      if (node === startNode) {
        if (node.nodeType === 3) {
          offset += startOff;
          found = true;
          return;
        }
      }
      if (node.nodeType === 3) {
        offset += node.nodeValue.length;
        return;
      }
      for (var i = 0; i < node.childNodes.length && !found; i++) {
        if (node.childNodes[i] === startNode && startNode.nodeType !== 3) {
          // range start is an element boundary inside node
          for (var j = 0; j < startOff && j < startNode.childNodes.length; j++) {
            offset += (startNode.childNodes[j].textContent || '').length;
          }
          found = true;
          return;
        }
        walk(node.childNodes[i]);
      }
    }
    for (var k = 0; k < nodes.length && !found; k++) walk(nodes[k]);
    return found ? offset : null;
  }

  function showPopup(rect) {
    var p = els.selPopup;
    p.hidden = false;
    var pw = p.offsetWidth || 220,
      ph = p.offsetHeight || 34;
    var top = window.scrollY + rect.top - ph - 8;
    if (top < window.scrollY + 4) top = window.scrollY + rect.bottom + 8;
    var left = window.scrollX + rect.left + rect.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.scrollX + document.documentElement.clientWidth - pw - 8));
    p.style.top = top + 'px';
    p.style.left = left + 'px';
  }
  function hidePopup() {
    els.selPopup.hidden = true;
  }

  // ---- composer -----------------------------------------------------------
  // The composer serves TWO flows: creating a new note from a selection (kind = question /
  // remark) and editing an existing note's text in place. state.editingId distinguishes
  // them on submit. Both flows route through showComposer so the setup lives in one place.
  function showComposer(opts) {
    var meta = KINDS[opts.kind];
    els.composerHead.textContent = meta.icon + ' ' + opts.headPrefix + ' ' + meta.label.toLowerCase();
    els.composerHint.textContent = meta.hint;
    els.composerQuote.textContent = opts.quote || '';
    els.composerQuote.hidden = !opts.quote;
    els.composerBody.value = opts.body || '';
    els.composerBody.placeholder =
      opts.kind === 'question' ? 'Ask your question…' : 'Write your remark…';
    els.composerSubmit.textContent = opts.submitLabel;
    // The slot this composer autosaves its draft to: 'new' for a new note, 'edit:<id>' for
    // an edit. Set BEFORE showing so an immediate keystroke saves to the right slot.
    state.activeDraftSlot = opts.slot || null;
    els.composerDraftNote.hidden = !opts.draftRestored;
    els.composerBackdrop.hidden = false;
    syncComposerSubmit();
    els.composerBody.focus();
  }
  // The submit button is disabled while the body is empty, so an empty note can never be
  // saved (and an empty edit can't silently discard the original on submit).
  function syncComposerSubmit() {
    els.composerSubmit.disabled = !(els.composerBody.value || '').trim();
  }
  function openComposer(kind) {
    if (!state.pendingSelection) return;
    hidePopup();
    state.editingId = null;
    state.pendingKind = kind === 'question' ? 'question' : 'remark';
    showComposer({
      kind: state.pendingKind,
      headPrefix: 'New',
      quote: state.pendingSelection.quote,
      body: '',
      submitLabel: 'Add to review',
      slot: NEW_DRAFT_SLOT,
    });
  }
  function openEditComposer(c) {
    state.pendingSelection = null;
    state.editingId = c.id;
    // A saved edit-in-progress draft for this comment wins over the stored body (the
    // reviewer was mid-edit when they reloaded); else start from the comment's text. The
    // draft's kind wins too, so a mid-edit kind change survives the reload.
    var draft = state.drafts[editDraftSlot(c.id)];
    state.pendingKind = draft && draft.kind ? (draft.kind === 'question' ? 'question' : 'remark') : kindOf(c);
    showComposer({
      kind: state.pendingKind,
      headPrefix: 'Edit',
      quote: c.quote || '',
      body: draft && draft.body ? draft.body : c.body || '',
      submitLabel: 'Save',
      slot: editDraftSlot(c.id),
      draftRestored: !!(draft && draft.body),
    });
  }
  function closeComposer() {
    // Cancel/Escape/backdrop close = KEEP the draft: persist the CURRENT box contents
    // synchronously (superseding any not-yet-fired debounce) so closing never loses the
    // latest text and never leaves a stale earlier draft to restore. An empty box clears
    // the slot. The create/edit SUCCESS paths null activeDraftSlot first (the server already
    // dropped the slot), so this flush is a no-op there — it won't re-create a saved note's
    // draft.
    cancelDraftSave();
    var slot = state.activeDraftSlot;
    if (slot) persistDraft(slot, els.composerBody.value || '');
    els.composerBackdrop.hidden = true;
    els.composerDraftNote.hidden = true;
    state.pendingSelection = null;
    state.editingId = null;
    state.pendingKind = 'remark'; // reset all composer state together (no stale kind)
    state.activeDraftSlot = null;
  }
  function submitComposer() {
    var body = (els.composerBody.value || '').trim();
    // An empty body is a no-op: keep the composer open (the submit button is already
    // disabled in this state) so an accidental submit never discards an in-progress edit.
    if (!body) return;
    // EDIT flow: PATCH the existing note's body. The edit UI offers no way to change the
    // kind, so omit it — the server preserves the existing kind when none is sent.
    if (state.editingId) {
      var id = state.editingId;
      cancelPendingDraftSave(); // cancel + abort any in-flight autosave before the edit
      var editSlot = editDraftSlot(id);
      state.activeDraftSlot = null; // close's keep-draft flush is a no-op (note is saved)
      api('POST', '/api/comments/' + id + '/edit', { body: body })
        .then(function () {
          closeComposer();
          // Trailing explicit clear: the LAST write to the slot is a delete, so a stale
          // autosave that slipped past the abort can't leave the draft behind.
          return clearDraftOnServer(editSlot).then(loadComments);
        })
        .catch(function (e) {
          // The save FAILED — the composer stays open. Re-arm autosave on this slot so the
          // reviewer's text isn't lost on a later close/reload (nulling the slot above would
          // otherwise disable it).
          state.activeDraftSlot = editSlot;
          alert('Failed to save: ' + e.message);
        });
      return;
    }
    // CREATE flow: a new note anchored to the current selection.
    var sel = state.pendingSelection;
    if (!sel) {
      closeComposer();
      return;
    }
    cancelPendingDraftSave(); // cancel + abort any in-flight autosave before the create
    state.activeDraftSlot = null; // close's keep-draft flush is a no-op (note is saved)
    api('POST', '/api/comments', {
      quote: sel.quote,
      body: body,
      kind: state.pendingKind,
      section_id: sel.section_id,
      section_title: sel.section_title,
      start: sel.start,
      end: sel.end,
    })
      .then(function () {
        closeComposer();
        window.getSelection().removeAllRanges();
        // Trailing explicit clear: the LAST write to the 'new' slot is a delete, so a stale
        // autosave that slipped past the abort can't resurrect the draft over the saved note.
        return clearDraftOnServer(NEW_DRAFT_SLOT).then(loadComments);
      })
      .catch(function (e) {
        // The create FAILED — the composer stays open. Re-arm autosave on the 'new' slot so
        // the reviewer's text isn't lost on a later close/reload.
        state.activeDraftSlot = NEW_DRAFT_SLOT;
        alert('Failed to add note: ' + e.message);
      });
  }

  // ---- draft autosave / restore -------------------------------------------
  // Persist the OPEN composer's in-progress text to the server, debounced, so a reload
  // never loses a half-typed note. Keyed by the active slot ('new' or 'edit:<id>'); the
  // selection context rides along for a new note so the composer can be re-opened anchored.
  var draftSaveTimer = null;
  var draftSaveAbort = null; // AbortController for an in-flight autosave POST
  function cancelDraftSave() {
    if (draftSaveTimer) {
      clearTimeout(draftSaveTimer);
      draftSaveTimer = null;
    }
    // ALSO abort an autosave already on the wire — cancelling only the timer would let a
    // request that fired ~just before create/edit land AFTER the server cleared the slot,
    // resurrecting a stale draft. Aborting closes that window (belt; the trailing explicit
    // clear after create/edit is the braces).
    if (draftSaveAbort) {
      try {
        draftSaveAbort.abort();
      } catch (e) {
        /* AbortController may be unavailable on an ancient browser — ignore */
      }
      draftSaveAbort = null;
    }
  }
  function draftPayload(slot, body) {
    var p = { body: body, kind: state.pendingKind };
    // A NEW note's draft carries its selection anchor so restore can re-open it in place.
    if (slot === NEW_DRAFT_SLOT && state.pendingSelection) {
      var sel = state.pendingSelection;
      p.quote = sel.quote;
      p.section_id = sel.section_id;
      p.section_title = sel.section_title;
      p.start = sel.start;
      p.end = sel.end;
    }
    return p;
  }
  function persistDraft(slot, body) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    draftSaveAbort = ctrl;
    api('POST', '/api/drafts/' + encodeURIComponent(slot), draftPayload(slot, body), ctrl && ctrl.signal)
      .then(function (r) {
        if (draftSaveAbort === ctrl) draftSaveAbort = null;
        // Mirror the server's view locally so a same-session re-open sees the latest text
        // (and a cleared slot drops it). r.draft is null when the body was emptied.
        if (r && r.draft) state.drafts[slot] = r.draft;
        else delete state.drafts[slot];
      })
      .catch(function () {
        if (draftSaveAbort === ctrl) draftSaveAbort = null;
        // Autosave is best-effort; a failed/aborted save must never interrupt typing. The
        // next keystroke re-tries. (Submit/edit still send the authoritative body.)
      });
  }
  // Explicitly clear a draft slot on the server (a POST with an empty body deletes it). Used
  // as the TRAILING write after a comment is created/edited, so even if a stale autosave
  // slipped through, the LAST write to the slot is this delete — the slot ends up empty and
  // restoreDraftOnLoad can never reopen a composer over an already-saved note.
  function clearDraftOnServer(slot) {
    return api('POST', '/api/drafts/' + encodeURIComponent(slot), { body: '' })
      .then(function () {
        delete state.drafts[slot];
      })
      .catch(function () {
        delete state.drafts[slot]; // best-effort; local mirror cleared regardless
      });
  }
  function scheduleDraftSave() {
    var slot = state.activeDraftSlot;
    if (!slot) return;
    cancelDraftSave();
    var body = els.composerBody.value || '';
    // Emptying the box CLEARS the draft IMMEDIATELY — not after the debounce. Otherwise a
    // reviewer who deletes the text and closes/reloads before the timer fires would have the
    // stale draft restored later (the contract is "an emptied composer has nothing to
    // recover"). A non-empty body keeps debouncing.
    if (!body.trim()) {
      persistDraft(slot, body); // empty body -> server clears the slot
      return;
    }
    draftSaveTimer = setTimeout(function () {
      draftSaveTimer = null;
      persistDraft(slot, body);
    }, DRAFT_DEBOUNCE_MS);
  }
  // Flush a pending debounced save IMMEDIATELY (on submit/edit, before we clear the slot)
  // so an outstanding timer can't re-persist a slot we are about to drop.
  function cancelPendingDraftSave() {
    cancelDraftSave();
  }

  // On boot, re-open the composer with the most-recently-updated saved draft so the
  // reviewer continues exactly where they left off. A 'new' draft re-opens anchored to its
  // saved selection; an 'edit:<id>' draft re-opens that comment's edit composer.
  function restoreDraftOnLoad() {
    if (!els.composerBackdrop.hidden) return; // composer already open — don't clobber
    var newest = null;
    var newestSlot = null;
    // Branch on the MAP KEY (the authoritative slot id), not the draft's server-stamped
    // `.slot` field — one source of truth for "which slot is this".
    Object.keys(state.drafts).forEach(function (slot) {
      var d = state.drafts[slot];
      if (!d || !d.body || !d.body.trim()) return;
      if (!newest || (d.updated || '') > (newest.updated || '')) {
        newest = d;
        newestSlot = slot;
      }
    });
    if (!newest) return;
    if (newestSlot === NEW_DRAFT_SLOT) {
      state.pendingSelection = {
        quote: newest.quote || '',
        section_id: newest.section_id || '',
        section_title: newest.section_title || '',
        start: typeof newest.start === 'number' ? newest.start : null,
        end: typeof newest.end === 'number' ? newest.end : null,
      };
      state.editingId = null;
      state.pendingKind = newest.kind === 'question' ? 'question' : 'remark';
      showComposer({
        kind: state.pendingKind,
        headPrefix: 'New',
        quote: newest.quote || '',
        body: newest.body,
        submitLabel: 'Add to review',
        slot: NEW_DRAFT_SLOT,
        draftRestored: true,
      });
      return;
    }
    // edit:<id> — find the comment and re-open its edit composer (openEditComposer reads the
    // saved draft body itself).
    var cid = (newestSlot || '').slice('edit:'.length);
    var c = state.comments.filter(function (x) {
      return x.id === cid;
    })[0];
    if (c) openEditComposer(c);
  }

  // ---- comment rendering --------------------------------------------------
  function statusPill(status) {
    return '<span class="status-pill status-' + esc(status) + '">' + esc(status) + '</span>';
  }
  // The kind chip makes question-vs-remark obvious at a glance (distinct icon + colour).
  // Interpolated values are escaped per this file's convention, even though KINDS holds
  // only hardcoded glyphs today.
  function kindChip(c) {
    var k = kindOf(c);
    var meta = KINDS[k];
    return '<span class="kind-chip kind-' + k + '">' + esc(meta.icon) + ' ' + esc(meta.label) + '</span>';
  }
  function renderComments() {
    var list = state.comments.filter(function (c) {
      return state.filter === 'all' || c.status === state.filter;
    });
    // Toggle via the `hidden` attribute (not inline display) so the mobile collapsed CSS
    // rule `.comments-collapsed .comments-empty { display:none }` can still win — an inline
    // display:block would override it and leave the empty message visible when collapsed.
    els.commentsEmpty.hidden = state.comments.length > 0;
    els.commentsList.innerHTML = '';
    list.forEach(function (c) {
      els.commentsList.appendChild(renderComment(c));
    });
    updatePendingTray();
    updateCommentsCount();
  }
  // The collapsed bar shows a count badge so the reviewer knows there are notes without
  // expanding the panel (the panel collapses to just its header).
  function updateCommentsCount() {
    var n = state.comments.length;
    els.commentsCount.textContent = String(n);
    // A bare number is meaningless to a screen reader; label it.
    els.commentsCount.setAttribute('aria-label', n + (n === 1 ? ' note' : ' notes'));
    els.commentsCount.hidden = n === 0;
  }
  function renderComment(c) {
    var div = document.createElement('div');
    div.className = 'comment kind-border-' + kindOf(c) + (c._unanchored ? ' unanchored' : '');
    div.dataset.id = c.id;
    var meta =
      '<div class="comment-meta">' +
      kindChip(c) +
      statusPill(c.status) +
      (c.section_title ? '<span class="comment-section">· ' + esc(c.section_title) + '</span>' : '') +
      (c._unanchored ? '<span class="unanchored-flag">· unanchored</span>' : '') +
      '</div>';
    var quote = c.quote ? '<blockquote class="comment-quote">' + esc(c.quote) + '</blockquote>' : '';
    var bodyHtml = '<div class="comment-body">' + esc(c.body) + '</div>';
    var replies = '';
    if (c.replies && c.replies.length) {
      replies =
        '<div class="replies">' +
        c.replies
          .map(function (r) {
            // An AGENT reply (left via `review spec-web reply`) is the spec author
            // answering — style it distinctly with an accent border + a labelled badge so
            // it reads apart from the reviewer's own threaded replies.
            var isAgent = r.author === AGENT_AUTHOR;
            var meta = isAgent
              ? '<div class="reply-meta"><span class="reply-author-badge">Agent</span></div>'
              : '';
            return (
              '<div class="reply' + (isAgent ? ' reply-agent' : '') + '">' +
              meta +
              '<div class="reply-body">' +
              esc(r.body) +
              '</div></div>'
            );
          })
          .join('') +
        '</div>';
    }
    // A question expects an answer, so it leads with "Answer"; a remark leads with "Reply".
    var replyLabel = KINDS[kindOf(c)].replyLabel;
    var actions =
      '<div class="comment-actions">' +
      '<button data-act="edit">Edit</button>' +
      '<button data-act="reply">' + replyLabel + '</button>' +
      (c.status === 'resolved'
        ? '<button data-act="unresolve">Reopen</button>'
        : '<button data-act="resolve">Resolve</button>') +
      '<button data-act="delete">Delete</button>' +
      '</div>';
    var replyBox =
      '<div class="reply-box">' +
      '<textarea rows="2" placeholder="Write an answer…"></textarea>' +
      '<button class="primary reply-send">' + replyLabel + '</button></div>';
    div.innerHTML = meta + quote + bodyHtml + replies + actions + replyBox;

    div.addEventListener('click', function (e) {
      var act = e.target.getAttribute('data-act');
      if (act) {
        e.stopPropagation();
        return handleAction(c, act, div);
      }
      if (e.target.closest('.reply-box')) return;
      focusQuote(c);
    });
    div.querySelector('.reply-send').addEventListener('click', function (e) {
      e.stopPropagation();
      var ta = div.querySelector('.reply-box textarea');
      var body = (ta.value || '').trim();
      if (!body) return;
      api('POST', '/api/comments/' + c.id + '/reply', { body: body })
        .then(function () {
          return loadComments();
        })
        .catch(function (err) {
          alert('Reply failed: ' + err.message);
        });
    });
    return div;
  }

  function handleAction(c, act, div) {
    if (act === 'edit') {
      openEditComposer(c);
      return;
    }
    if (act === 'reply') {
      var box = div.querySelector('.reply-box');
      box.classList.toggle('open');
      if (box.classList.contains('open')) box.querySelector('textarea').focus();
      return;
    }
    if (act === 'delete') {
      if (!confirm('Delete this note?')) return;
      api('POST', '/api/comments/' + c.id + '/delete', {})
        .then(function () {
          return loadComments();
        })
        .catch(function (e) {
          alert('Delete failed: ' + e.message);
        });
      return;
    }
    if (act === 'resolve' || act === 'unresolve') {
      // Reopen restores the PRE-resolve state: a submitted thread that has replies is
      // 'answered' (so it stays in the Answered filter), an answered/submitted thread with
      // no replies is 'submitted', an un-submitted one is 'pending'.
      var hasReplies = c.replies && c.replies.length;
      var reopened = c.batch ? (hasReplies ? 'answered' : 'submitted') : 'pending';
      var next = act === 'resolve' ? 'resolved' : reopened;
      api('POST', '/api/comments/' + c.id + '/status', { status: next })
        .then(function () {
          return loadComments();
        })
        .catch(function (e) {
          alert('Status change failed: ' + e.message);
        });
    }
  }

  function focusQuote(c) {
    Array.prototype.forEach.call(els.commentsList.querySelectorAll('.comment.active'), function (n) {
      n.classList.remove('active');
    });
    var card = els.commentsList.querySelector('.comment[data-id="' + c.id + '"]');
    if (card) card.classList.add('active');
    Array.prototype.forEach.call(
      els.specBody.querySelectorAll('mark.sw-quote.active, .sw-quote-block.active'),
      function (m) {
        m.classList.remove('active');
      },
    );
    // exact inline mark first (a mark may carry several comment ids); else the cross-node
    // block anchor
    var mark = null;
    var allMarks = els.specBody.querySelectorAll('mark.sw-quote');
    for (var mi = 0; mi < allMarks.length; mi++) {
      if (markIds(allMarks[mi]).indexOf(c.id) !== -1) {
        mark = allMarks[mi];
        break;
      }
    }
    if (!mark) {
      var blocks = els.specBody.querySelectorAll('.sw-quote-block');
      for (var i = 0; i < blocks.length; i++) {
        if (blockIds(blocks[i]).indexOf(c.id) !== -1) {
          mark = blocks[i];
          break;
        }
      }
    }
    if (mark) {
      mark.classList.add('active');
      scrollSpecTo(mark);
    }
  }

  function updatePendingTray() {
    var pending = state.comments.filter(function (c) {
      return c.status === 'pending';
    });
    if (pending.length) {
      els.pendingTray.hidden = false;
      els.pendingLabel.textContent = 'Pending review (' + pending.length + ')';
    } else {
      els.pendingTray.hidden = true;
    }
  }

  // ---- re-anchoring -------------------------------------------------------
  // For each comment with a quote, find the quote inside its section and wrap it in a
  // <mark>. If not found anywhere, flag the comment as unanchored (never crash).
  function reanchorAll() {
    Array.prototype.forEach.call(els.specBody.querySelectorAll('mark.sw-quote'), function (m) {
      var parent = m.parentNode;
      parent.replaceChild(document.createTextNode(m.textContent), m);
      parent.normalize();
    });
    // also clear any block-level (cross-node) anchors from a previous pass
    Array.prototype.forEach.call(els.specBody.querySelectorAll('.sw-quote-block'), function (el) {
      el.classList.remove('sw-quote-block', 'active');
      delete el.dataset.swIds;
    });
    state.comments.forEach(function (c) {
      c._unanchored = false;
      if (!c.quote) {
        c._unanchored = true;
        return;
      }
      var anchored = highlightQuote(c);
      if (!anchored) c._unanchored = true;
    });
    renderComments();
  }

  function highlightQuote(c) {
    var scope = c.section_id && specEl(c.section_id) ? sectionRange(c.section_id) : null;
    // Prefer the occurrence at the persisted section offset (so a quote that repeats in
    // its section re-anchors to the one the user actually selected, not the first match).
    var want = typeof c.start === 'number' && c.start >= 0 ? c.start : null;
    // 1) exact single-text-node match WITHIN the recorded section.
    if (highlightInScope(c.quote, c.id, scope, want)) return true;
    // 2) cross-node match WITHIN the recorded section (a selection spanning inline markup —
    //    link/bold/code — that no single text node holds). Do this BEFORE the whole-document
    //    fallback so the comment anchors in ITS OWN section, not a same phrase elsewhere.
    if (scope && highlightBlockContaining(c.quote, c.id, scope)) return true;
    // 3) exact match anywhere in the document (offset no longer meaningful -> first match).
    if (highlightInScope(c.quote, c.id, null, null)) return true;
    // 4) cross-node match anywhere in the document.
    if (highlightBlockContaining(c.quote, c.id, null)) return true;
    // 5) last resort: a shorter prefix (handles minor edits to the spec text).
    if (c.quote.length > 24 && highlightInScope(c.quote.slice(0, 24), c.id, scope, want)) return true;
    return false;
  }

  function highlightBlockContaining(needle, id, scopeNodes) {
    var norm = function (s) {
      return (s || '').replace(/\s+/g, ' ').trim();
    };
    var want = norm(needle);
    if (!want) return false;
    var roots = scopeNodes || [els.specBody];
    var candidates = [];
    for (var i = 0; i < roots.length; i++) {
      var root = roots[i];
      if (root.nodeType !== 1) continue;
      // the root block itself + its descendant block-ish elements
      candidates.push(root);
      var els2 = root.querySelectorAll('p,li,td,th,blockquote,pre,figcaption,h1,h2,h3,h4,h5,h6');
      for (var j = 0; j < els2.length; j++) candidates.push(els2[j]);
    }
    // pick the SMALLEST element that contains the quote (most specific anchor)
    var best = null;
    for (var k = 0; k < candidates.length; k++) {
      var el = candidates[k];
      if (el.querySelector && el.querySelector('mark.sw-quote')) continue;
      if (norm(el.textContent).indexOf(want) >= 0) {
        if (!best || (el.textContent || '').length < (best.textContent || '').length) best = el;
      }
    }
    if (!best) return false;
    var already = best.classList.contains('sw-quote-block');
    best.classList.add('sw-quote-block');
    // Carry MULTIPLE ids (several comments may cross-node-anchor to the same block) — like
    // inline marks. Overwriting a single data-sw-id would orphan the earlier comment from
    // focusQuote.
    blockAddId(best, id);
    if (!already) {
      best.addEventListener('click', function () {
        var ids = blockIds(best);
        for (var i = 0; i < ids.length; i++) {
          var c = state.comments.filter(function (x) {
            return x.id === ids[i];
          })[0];
          if (c) {
            setFilterAndShow(c);
            return;
          }
        }
      });
    }
    return true;
  }

  function blockIds(el) {
    return (el.dataset.swIds || '').split(' ').filter(Boolean);
  }
  function blockAddId(el, id) {
    var ids = blockIds(el);
    if (ids.indexOf(id) === -1) ids.push(id);
    el.dataset.swIds = ids.join(' ');
  }

  // Build a {start, end} sibling range for a section so we only search within it.
  function sectionRange(sectionId) {
    var h = specEl(sectionId);
    if (!h) return null;
    var level = parseInt(h.tagName.slice(1), 10) || 6;
    var nodes = [];
    var n = h.nextSibling;
    while (n) {
      if (n.nodeType === 1 && /^H[1-6]$/.test(n.tagName) && parseInt(n.tagName.slice(1), 10) <= level) break;
      nodes.push(n);
      n = n.nextSibling;
    }
    return nodes;
  }

  function highlightInScope(needle, id, scopeNodes, wantOffset) {
    needle = (needle || '').trim();
    if (!needle) return false;
    var roots = scopeNodes || [els.specBody];
    // Collect every fresh text-node match across the scope, tracking a running section
    // offset so we can choose the occurrence closest to the persisted selection offset.
    var matches = [];
    var running = 0;
    for (var i = 0; i < roots.length; i++) {
      running = collectMatches(roots[i], needle, matches, running);
    }
    // `matches` holds BOTH fresh text-node occurrences (kind 'wrap') and occurrences a
    // PREVIOUS comment already wrapped in a mark (kind 'share') — each with a section
    // offset. Pick the one closest to the recorded selection offset, so a SECOND comment on
    // the same occurrence of a repeated quote shares that exact mark instead of jumping to a
    // different occurrence (multiple PR-style threads on one passage).
    if (!matches.length) return false;
    var pick = matches[0];
    if (typeof wantOffset === 'number' && wantOffset >= 0) {
      var best = Infinity;
      for (var j = 0; j < matches.length; j++) {
        var dd = Math.abs(matches[j].sectionOffset - wantOffset);
        if (dd < best) {
          best = dd;
          pick = matches[j];
        }
      }
    }
    if (pick.kind === 'share') {
      addIdToMark(pick.mark, id);
      return true;
    }
    return wrapMatch(pick, id);
  }

  function markIds(mark) {
    return (mark.dataset.ids || '').split(' ').filter(Boolean);
  }
  function addIdToMark(mark, id) {
    var ids = markIds(mark);
    if (ids.indexOf(id) === -1) ids.push(id);
    mark.dataset.ids = ids.join(' ');
  }

  // Walk text nodes under root, appending candidates for each occurrence of needle, with a
  // running section offset. Fresh text-node occurrences are kind 'wrap'; an occurrence that
  // a previous comment already wrapped (the text node's containing mark holds exactly the
  // needle) is kind 'share' so a duplicate thread reuses that same mark. Returns the updated
  // running offset. (Pragmatic: cross-node matches handled by highlightBlockContaining.)
  function collectMatches(root, needle, out, running) {
    if (root.nodeType === 3) {
      scanTextNode(root, needle, out, running);
      return running + root.nodeValue.length;
    }
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    var tn;
    while ((tn = walker.nextNode())) {
      var mark = tn.parentNode && tn.parentNode.closest && tn.parentNode.closest('mark.sw-quote');
      if (!mark) {
        scanTextNode(tn, needle, out, running);
      } else if ((mark.textContent || '') === needle && mark.dataset.swSeen !== needle) {
        // record ONE share candidate per existing mark (at the mark's first text offset)
        mark.dataset.swSeen = needle;
        out.push({ kind: 'share', mark: mark, sectionOffset: running });
      }
      running += tn.nodeValue.length;
    }
    // clear the per-pass dedup flag
    var seen = root.nodeType === 1 ? root.querySelectorAll('mark.sw-quote[data-sw-seen]') : [];
    for (var i = 0; i < seen.length; i++) delete seen[i].dataset.swSeen;
    return running;
  }

  function scanTextNode(tn, needle, out, baseOffset) {
    var from = 0;
    var idx;
    while ((idx = tn.nodeValue.indexOf(needle, from)) >= 0) {
      out.push({ kind: 'wrap', node: tn, idx: idx, len: needle.length, sectionOffset: baseOffset + idx });
      from = idx + Math.max(1, needle.length);
    }
  }

  function wrapMatch(match, id) {
    var tn = match.node;
    var idx = match.idx;
    var nodeLen = (tn.nodeValue || '').length;
    if (idx < 0 || idx >= nodeLen) return false;
    var range = document.createRange();
    range.setStart(tn, idx);
    range.setEnd(tn, Math.min(idx + match.len, nodeLen));
    var mark = document.createElement('mark');
    mark.className = 'sw-quote';
    mark.dataset.ids = id; // space-separated id list (a quote may carry several threads)
    try {
      range.surroundContents(mark);
    } catch (e) {
      return false;
    }
    mark.addEventListener('click', function () {
      // focus the FIRST still-present comment sharing this mark
      var ids = markIds(mark);
      for (var i = 0; i < ids.length; i++) {
        var c = state.comments.filter(function (x) {
          return x.id === ids[i];
        })[0];
        if (c) {
          setFilterAndShow(c);
          return;
        }
      }
    });
    return true;
  }
  function setFilterAndShow(c) {
    // Make sure the comment is visible under the active filter, then focus it.
    if (state.filter !== 'all' && c.status !== state.filter) {
      setFilter('all');
    }
    var card = els.commentsList.querySelector('.comment[data-id="' + c.id + '"]');
    if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    focusQuote(c);
  }

  // ---- filters / submit ---------------------------------------------------
  // The filter is a compact <select> (one row on mobile, no wrapping pills); keep its
  // value in sync when the filter is changed programmatically (e.g. setFilterAndShow).
  function setFilter(f) {
    state.filter = f;
    if (els.filterSelect.value !== f) els.filterSelect.value = f;
    renderComments();
  }

  // ---- pane collapse / resize --------------------------------------------
  function toggleComments() {
    els.layout.classList.toggle('comments-collapsed');
    var collapsed = els.layout.classList.contains('comments-collapsed');
    els.commentsCollapse.setAttribute('aria-expanded', String(!collapsed));
  }
  function wireDivider() {
    var dragging = false;
    els.divider.addEventListener('mousedown', function (e) {
      dragging = true;
      e.preventDefault();
    });
    window.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      var pane = document.getElementById('commentsPane');
      var w = window.innerWidth - e.clientX;
      w = Math.max(260, Math.min(w, window.innerWidth - 320));
      pane.style.flex = '0 0 ' + w + 'px';
    });
    window.addEventListener('mouseup', function () {
      dragging = false;
    });
  }

  // ---- wire ---------------------------------------------------------------
  function wire() {
    // UNIVERSAL pointer path: pointerup covers mouse, touch and pen in one handler — no
    // separate mouse-vs-touch branches. It is the immediate, desktop-friendly trigger.
    document.addEventListener('pointerup', function (e) {
      // A tap inside the popup itself is a button press, not a new selection — ignore it so
      // showing/using the popup never re-evaluates (and clears) the selection underneath.
      if (els.selPopup.contains(e.target)) return;
      // Defer one tick so the browser has committed the final selection for this pointerup.
      setTimeout(onSelection, 0);
    });
    // selectionchange is the signal iOS Safari DOES deliver when the native selection
    // finalizes (pointerup over selected text is swallowed by the callout). Debounced so it
    // only fires once the selection settles. When the selection collapses, hide immediately.
    document.addEventListener('selectionchange', function () {
      var sel = window.getSelection();
      if (!sel || sel.isCollapsed) {
        cancelPendingSelectionCheck();
        hidePopup();
        return;
      }
      scheduleSelectionCheck();
    });
    // Popup buttons: pointerdown (preventDefault) keeps the selection alive — a bare click is
    // flaky after a touch selection on iOS and can fire after the selection has been cleared.
    // pointerup then opens the composer using the ALREADY-STASHED selection.
    function wirePopupButton(btn, kind) {
      // A real pointer emits pointerup THEN a synthetic click. We act on pointerup and stamp
      // the time; the trailing click within this window is the pointer's own echo and is
      // ignored. A keyboard/assistive activation (Enter/Space) produces a click with NO
      // recent pointerup, so it still opens — and because the guard is a timestamp, not a
      // boolean, it self-heals (a missed click never leaves a future interaction suppressed).
      var lastPointerAt = 0;
      btn.addEventListener('pointerdown', function (e) {
        // Stop the browser from collapsing the selection / focusing away before we read it.
        e.preventDefault();
      });
      btn.addEventListener('pointerup', function (e) {
        e.preventDefault();
        lastPointerAt = Date.now();
        openComposer(kind);
      });
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        if (Date.now() - lastPointerAt < POINTER_CLICK_ECHO_MS) return; // pointer's own echo
        openComposer(kind);
      });
    }
    wirePopupButton(els.selQuestion, 'question');
    wirePopupButton(els.selRemark, 'remark');
    // The framing hint lives once in KINDS; mirror it onto the popup buttons' tooltips.
    els.selQuestion.title = KINDS.question.hint;
    els.selRemark.title = KINDS.remark.hint;
    els.composerCancel.addEventListener('click', closeComposer);
    els.composerSubmit.addEventListener('click', submitComposer);
    els.composerBackdrop.addEventListener('click', function (e) {
      if (e.target === els.composerBackdrop) closeComposer();
    });
    els.composerBody.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submitComposer();
      if (e.key === 'Escape') closeComposer();
    });
    // Keep the submit button's enabled state in sync with the body (disabled while empty)
    // and autosave the in-progress draft (debounced) so a reload never loses the text.
    els.composerBody.addEventListener('input', function () {
      syncComposerSubmit();
      scheduleDraftSave();
    });
    els.filterSelect.addEventListener('change', function () {
      setFilter(els.filterSelect.value);
    });
    els.submitReview.addEventListener('click', function () {
      api('POST', '/api/submit', {})
        .then(function (r) {
          return loadComments().then(function () {
            if (r.count) setFilter('submitted');
          });
        })
        .catch(function (e) {
          alert('Submit failed: ' + e.message);
        });
    });
    els.commentsCollapse.addEventListener('click', toggleComments);
    els.navBack.addEventListener('click', navBack);
    wireDivider();
  }

  // True when an inline reply box is OPEN and holds typed-but-unsent text. loadComments()
  // rebuilds the whole comments list, which would discard a half-typed reply — so polling
  // must hold off while the reviewer is composing one.
  function hasUnsentReply() {
    var boxes = els.commentsList.querySelectorAll('.reply-box.open');
    for (var i = 0; i < boxes.length; i++) {
      var ta = boxes[i].querySelector('textarea');
      if (ta && (ta.value || '').trim()) return true;
    }
    return false;
  }
  // Poll the comments API so an OPEN page picks up an agent reply (left via
  // `review spec-web reply`) without a manual reload. Best-effort — a failed poll just
  // retries on the next tick.
  function startCommentsPolling() {
    setInterval(function () {
      // Skip while: the composer is open (a re-render mid-edit is pointless); the tab is
      // backgrounded (no point polling a page nobody is looking at); or a half-typed inline
      // reply is open (a re-render would wipe the reviewer's unsent text).
      if (!els.composerBackdrop.hidden || document.hidden || hasUnsentReply()) return;
      loadComments().catch(function () {});
    }, COMMENTS_POLL_MS);
  }

  // ---- boot ---------------------------------------------------------------
  wire();
  loadSpec()
    .then(function () {
      return Promise.all([loadComments(), loadDrafts()]);
    })
    .then(function () {
      restoreDraftOnLoad();
      startCommentsPolling();
    });
})();
