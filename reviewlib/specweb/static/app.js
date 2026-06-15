/* Spec-web reviewer — vanilla JS, no deps.
 *
 * Flow: fetch the server-rendered spec HTML + headings, inject it; let the reviewer
 * SELECT text to open a popup -> composer -> POST a comment (enters the pending batch);
 * accumulate pending comments, "Submit review" flips them to submitted; answer inline via
 * a reply box that threads under each comment. On reload, comments re-anchor by locating
 * their quote within the recorded section (pragmatic quote-within-section search); an
 * unfindable quote shows as "unanchored" in the sidebar. Desktop = two panes; mobile =
 * comments as a bottom sheet. Both panes collapse/expand.
 */
(function () {
  'use strict';

  var els = {
    specBody: document.getElementById('specBody'),
    specTitle: document.getElementById('specTitle'),
    layout: document.getElementById('layout'),
    commentsList: document.getElementById('commentsList'),
    commentsEmpty: document.getElementById('commentsEmpty'),
    pendingTray: document.getElementById('pendingTray'),
    pendingLabel: document.getElementById('pendingLabel'),
    submitReview: document.getElementById('submitReview'),
    filters: document.getElementById('filters'),
    selPopup: document.getElementById('selPopup'),
    selComment: document.getElementById('selComment'),
    selQuestion: document.getElementById('selQuestion'),
    composerBackdrop: document.getElementById('composerBackdrop'),
    composer: document.getElementById('composer'),
    composerHead: document.getElementById('composerHead'),
    composerQuote: document.getElementById('composerQuote'),
    composerBody: document.getElementById('composerBody'),
    composerCancel: document.getElementById('composerCancel'),
    composerSubmit: document.getElementById('composerSubmit'),
    authorInput: document.getElementById('authorInput'),
    toggleSpec: document.getElementById('toggleSpec'),
    toggleComments: document.getElementById('toggleComments'),
    commentsCollapse: document.getElementById('commentsCollapse'),
    divider: document.getElementById('divider'),
  };

  var state = {
    headings: [], // [{level,text,id}]
    comments: [], // server comments
    filter: 'all',
    pendingSelection: null, // {quote, section_id, section_title, start, end}
  };

  // ---- helpers ------------------------------------------------------------
  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function author() {
    var a = (els.authorInput.value || '').trim();
    return a || 'reviewer';
  }
  function api(method, url, body) {
    var opts = { method: method, headers: {} };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
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

  // In-doc anchor links (e.g. [§9.4](#94-...)) scroll inside the spec pane + flash.
  function wireInDocLinks() {
    var links = els.specBody.querySelectorAll('a[href^="#"]');
    Array.prototype.forEach.call(links, function (a) {
      a.addEventListener('click', function (e) {
        var id = a.getAttribute('href').slice(1);
        var t = specEl(id);
        if (t) {
          e.preventDefault();
          scrollSpecTo(t);
        }
      });
    });
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
  function openComposer(kind) {
    if (!state.pendingSelection) return;
    hidePopup();
    els.composerHead.textContent = kind === 'question' ? 'Ask a question' : 'Add a comment';
    els.composerQuote.textContent = state.pendingSelection.quote;
    els.composerBody.value = '';
    els.composerBackdrop.hidden = false;
    els.composerBody.focus();
  }
  function closeComposer() {
    els.composerBackdrop.hidden = true;
    state.pendingSelection = null;
  }
  function submitComposer() {
    var sel = state.pendingSelection;
    var body = (els.composerBody.value || '').trim();
    if (!sel || !body) {
      closeComposer();
      return;
    }
    api('POST', '/api/comments', {
      quote: sel.quote,
      body: body,
      author: author(),
      section_id: sel.section_id,
      section_title: sel.section_title,
      start: sel.start,
      end: sel.end,
    })
      .then(function () {
        closeComposer();
        window.getSelection().removeAllRanges();
        return loadComments();
      })
      .catch(function (e) {
        alert('Failed to add comment: ' + e.message);
      });
  }

  // ---- comment rendering --------------------------------------------------
  function statusPill(status) {
    return '<span class="status-pill status-' + esc(status) + '">' + esc(status) + '</span>';
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
  }
  function renderComment(c) {
    var div = document.createElement('div');
    div.className = 'comment' + (c._unanchored ? ' unanchored' : '');
    div.dataset.id = c.id;
    var meta =
      '<div class="comment-meta">' +
      statusPill(c.status) +
      '<span>' +
      esc(c.author) +
      '</span>' +
      (c.section_title ? '<span>· ' + esc(c.section_title) + '</span>' : '') +
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
            return (
              '<div class="reply"><div class="reply-meta">' +
              esc(r.author) +
              '</div>' +
              '<div class="reply-body">' +
              esc(r.body) +
              '</div></div>'
            );
          })
          .join('') +
        '</div>';
    }
    var actions =
      '<div class="comment-actions">' +
      '<button data-act="reply">Reply</button>' +
      (c.status === 'resolved'
        ? '<button data-act="unresolve">Reopen</button>'
        : '<button data-act="resolve">Resolve</button>') +
      '<button data-act="delete">Delete</button>' +
      '</div>';
    var replyBox =
      '<div class="reply-box">' +
      '<textarea rows="2" placeholder="Write an answer…"></textarea>' +
      '<button class="primary reply-send">Reply</button></div>';
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
      api('POST', '/api/comments/' + c.id + '/reply', { body: body, author: author() })
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
    if (act === 'reply') {
      var box = div.querySelector('.reply-box');
      box.classList.toggle('open');
      if (box.classList.contains('open')) box.querySelector('textarea').focus();
      return;
    }
    if (act === 'delete') {
      if (!confirm('Delete this comment?')) return;
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
  function setFilter(f) {
    state.filter = f;
    Array.prototype.forEach.call(els.filters.querySelectorAll('button'), function (b) {
      b.classList.toggle('active', b.dataset.filter === f);
    });
    renderComments();
  }

  // ---- pane collapse / resize --------------------------------------------
  function toggleClass(cls) {
    els.layout.classList.toggle(cls);
    // never let both be collapsed at once
    if (els.layout.classList.contains('spec-collapsed') && els.layout.classList.contains('comments-collapsed')) {
      els.layout.classList.remove(cls === 'spec-collapsed' ? 'comments-collapsed' : 'spec-collapsed');
    }
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
    wirePopupButton(els.selComment, 'comment');
    wirePopupButton(els.selQuestion, 'question');
    els.composerCancel.addEventListener('click', closeComposer);
    els.composerSubmit.addEventListener('click', submitComposer);
    els.composerBackdrop.addEventListener('click', function (e) {
      if (e.target === els.composerBackdrop) closeComposer();
    });
    els.composerBody.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') submitComposer();
      if (e.key === 'Escape') closeComposer();
    });
    els.filters.addEventListener('click', function (e) {
      if (e.target.dataset.filter) setFilter(e.target.dataset.filter);
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
    els.toggleSpec.addEventListener('click', function () {
      toggleClass('spec-collapsed');
    });
    els.toggleComments.addEventListener('click', function () {
      toggleClass('comments-collapsed');
    });
    els.commentsCollapse.addEventListener('click', function () {
      toggleClass('comments-collapsed');
    });
    wireDivider();
    try {
      var savedAuthor = localStorage.getItem('specweb-author');
      if (savedAuthor) els.authorInput.value = savedAuthor;
    } catch (e) {
      /* ignore */
    }
    els.authorInput.addEventListener('change', function () {
      try {
        localStorage.setItem('specweb-author', els.authorInput.value);
      } catch (e) {
        /* ignore */
      }
    });
  }

  // ---- boot ---------------------------------------------------------------
  wire();
  loadSpec().then(loadComments);
})();
