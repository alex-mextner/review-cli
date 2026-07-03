"""Deliver a submitted spec-web review into the OWNING AGENT's live tmux pane.

Why this exists: a submitted review used to only sit in the JSON store waiting for a
``watch_submits`` poller — if no process was watching (the daemon is long-lived, launching
agents come and go), the reviewer's comments reached NOBODY. Now every spec (or the daemon
itself) carries an ``--agent <name>`` owner, and on Submit the batch is INJECTED into that
agent's tmux pane as a prompt, the same way ``tg-ctl`` injects inbound Telegram messages
into a live Claude Code session (``[TG from …]``).

The injection recipe is tg-ctl's live-proven one (tg-cli features/tg-ctl/inject.ts):
  * single line  -> ``tmux send-keys -t <pane> -l <text>`` (literal mode, special-char safe);
  * multi-line   -> ``tmux load-buffer -`` + ``tmux paste-buffer -p -d -t <pane>``
                    (bracketed paste — a literal LF submits early in canonical-mode REPLs);
  * then a PACED separate ``send-keys Enter`` after ~500ms — a combined or too-fast Enter is
    dropped by the Ink TUI (the single most common failure mode in tmux-injecting bots).

Discovery runs ``tmux list-panes -a`` with an EXPLICIT UTF-8 locale: under launchd the daemon
inherits no locale and tmux then escapes the TAB separators in ``-F`` output to ``_``,
collapsing every line to one field (the tg-ctl "not in tmux" saga — see tg-cli's PANE_FORMAT
comment). The agent NAME matches a tmux window name first, then a session name (both exact,
then case-insensitive) — e.g. ``--agent ext`` reaches the pane of the tmux session ``ext``.

Everything here is BEST-EFFORT by contract: a failed delivery must never fail the Submit
(the review is already durably in the store); the caller logs the (ok, detail) outcome.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# Tab-separated so names with spaces survive; tabs are safe under the forced UTF-8 locale.
_PANE_FORMAT = "#{pane_id}\t#{session_name}\t#{window_name}\t#{window_active}\t#{pane_active}"

# Pre-Enter gap (tg-ctl's competitor-source-proven pacing for Ink TUIs).
_ENTER_GAP_SECONDS = 0.5

# Clip the reviewer's free text so one batch can't paste an essay into the agent's prompt.
_QUOTE_CLIP = 160
_BODY_CLIP = 1000


def _subproc_env() -> dict:
    """A guaranteed-UTF-8 env for tmux calls (launchd inherits no locale; see module doc)."""
    import os

    env = dict(os.environ)
    env["LC_ALL"] = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
    return env


def _tmux(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        env=_subproc_env(),
    )


def list_panes() -> list[dict]:
    """Every pane on the tmux server as {pane_id, session, window, window_active, pane_active}.

    tmux absent / no server / bad output -> [] (never raises): delivery then degrades to
    "no pane found", which the caller logs."""
    try:
        out = _tmux(["list-panes", "-a", "-F", _PANE_FORMAT])
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    rows: list[dict] = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue  # a mangled line (wrong locale / odd name) is dropped, not crashed on
        rows.append(
            {
                "pane_id": parts[0],
                "session": parts[1],
                "window": parts[2],
                "window_active": parts[3] == "1",
                "pane_active": parts[4] == "1",
            }
        )
    return rows


def match_agent_pane(rows: list[dict], agent: str) -> str | None:
    """The pane id for ``agent`` from a pane listing (pure — unit-testable).

    Match order: window NAME exact -> session name exact -> both case-insensitive. A window
    match prefers that window's ACTIVE pane; a session match requires the session's active
    window's active pane (the pane the agent is actually attached to)."""
    def _pick(pred) -> str | None:
        matched = [r for r in rows if pred(r)]
        if not matched:
            return None
        active = [r for r in matched if r["pane_active"]]
        return (active[0] if active else matched[0])["pane_id"]

    for fold in (False, True):
        want = agent.lower() if fold else agent
        name_of = (lambda v: v.lower()) if fold else (lambda v: v)
        pane = _pick(lambda r: name_of(r["window"]) == want)
        if pane:
            return pane
        pane = _pick(lambda r: name_of(r["session"]) == want and r["window_active"])
        if pane:
            return pane
    return None


def find_agent_pane(agent: str) -> str | None:
    return match_agent_pane(list_panes(), agent)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())  # collapse newlines/runs: each comment is ONE line
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_review_message(spec_name: str, spec_path: Path | str, review: dict, batch: str | None) -> str:
    """The prompt injected into the agent's pane for ONE submitted batch (pure).

    Only THIS batch's comments are included (older batches were delivered on their own
    submit). Shape (one line per comment, the format the CTO specified):

        [SPEC-WEB review on <name> — N comment(s) submitted]
        [SPEC-WEB comment on <name> §<section>] "<quote>" — <body> (question, id <id>)
        [SPEC-WEB reply with: review spec-web reply <id> "<answer>" --spec <path>]
    """
    comments = [c for c in (review.get("comments") or []) if batch is None or c.get("batch") == batch]
    lines = [f"[SPEC-WEB review on {spec_name} — {len(comments)} comment(s) submitted]"]
    for c in comments:
        section = (c.get("section_title") or c.get("section_id") or "").strip()
        at = f" §{section}" if section else ""
        quote = _clip(c.get("quote") or "", _QUOTE_CLIP)
        quoted = f' "{quote}" —' if quote else ""
        kind = c.get("kind") or "remark"
        lines.append(
            f"[SPEC-WEB comment on {spec_name}{at}]{quoted} {_clip(c.get('body') or '', _BODY_CLIP)}"
            f" ({kind}, id {c.get('id', '?')})"
        )
    lines.append(f'[SPEC-WEB reply with: review spec-web reply <id> "<answer>" --spec {spec_path}]')
    return "\n".join(lines)


def inject_text(pane_id: str, text: str) -> tuple[bool, str]:
    """Inject ``text`` + a paced Enter into a tmux pane (tg-ctl's recipe). (ok, detail)."""
    try:
        if "\n" in text:
            if _tmux(["load-buffer", "-"], stdin=text).returncode != 0:
                return False, "tmux load-buffer failed"
            if _tmux(["paste-buffer", "-p", "-d", "-t", pane_id]).returncode != 0:
                return False, "tmux paste-buffer failed"
        else:
            if _tmux(["send-keys", "-t", pane_id, "-l", text]).returncode != 0:
                return False, "tmux send-keys failed"
        time.sleep(_ENTER_GAP_SECONDS)
        if _tmux(["send-keys", "-t", pane_id, "Enter"]).returncode != 0:
            return False, "tmux send-keys Enter failed"
        return True, f"injected into pane {pane_id}"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"tmux error: {exc}"


def deliver_review(
    *, agent: str, spec_name: str, spec_path: Path | str, review: dict, batch: str | None
) -> tuple[bool, str]:
    """Deliver one submitted batch to ``agent``'s pane. Best-effort, never raises."""
    try:
        pane = find_agent_pane(agent)
        if pane is None:
            return False, f"no tmux window/session named '{agent}'"
        ok, detail = inject_text(pane, format_review_message(spec_name, spec_path, review, batch))
        return ok, detail
    except Exception as exc:  # noqa: BLE001 — delivery must never break the submit
        return False, f"{type(exc).__name__}: {exc}"
