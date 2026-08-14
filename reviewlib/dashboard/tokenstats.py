"""Detailed, per-harness usage/health aggregation over review-cli's real on-disk call
logs — the data source behind ``review stat``.

Background (2026-08 token-burn investigation)
----------------------------------------------
review-cli has never recorded real token/cost numbers anywhere (``reviewlib.stats``'s
JSONL schema has no such field, by design — see its module docstring; the dashboard's
own ``compute_stats`` reports ``tokens_recorded: False`` / ``cost_recorded: False``
rather than fabricate them). The only place ANY usage number ever lands on disk is a
plain text line a handful of REST backends append to their own stdout
(``backends.py``'s ``_openai_compatible_request`` / ``review_claude_api``): a
``prompt_tokens=N output_tokens=N`` (or ``input_tokens=``) line. Nothing parsed that
back into a structured number before this module.

The three (really four, since `oc`/`omp`/`codex` are CLI-subprocess-backed and `cmd` is
just an alias for `commandcode`, NOT a separate backend) agentic CLI harnesses — opencode
(``oc``), Oh My Pi (``omp``), Codex CLI (``codex``), and Claude in CLI mode — carry NO
token number in the call log review-cli actually writes today: review-cli only tees
their raw stdout/stderr to the call log (``reviewlib.process._run_streamed``), and in
the DEFAULT invocation each CLI uses today (plain text / live-streamed output — the mode
this whole project's `tail -f`-a-live-log UX and every existing paywall/auth/context-
pollution body-substring signal in this module depend on), that stdout is prose with no
usage object embedded, so there is genuinely nothing to parse out of the calls this
project actually makes.

VERIFIED LIVE (2026-08-14, real invocations, not assumed — this is the check task
description explicitly required): all four DO expose exact usage/cost, but ONLY via a
separate, mutually-exclusive structured-output mode review-cli does not currently pass:
`codex exec --json` emits a `turn.completed` event with
`usage: {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}`;
`opencode run --format json` emits a `step_finish` event with
`tokens: {total, input, output, reasoning, cache: {read, write}}` and a `cost` field;
`omp --mode json` emits `usage` (with a nested `cost`) on its terminal event; `claude -p
--output-format json` emits top-level `usage`, `total_cost_usd`, and a per-model
`modelUsage[model].costUSD` breakdown. None of this is a "the CLI exposes nothing" case
— see `--harness`-scoped rows below and the tracking issue this finding was filed as
(review-cli#186) for why wiring it in is a separate, larger change: each
CLI's `--json`/structured mode REPLACES its human-readable stdout wholesale (not
additive), and `codex --ephemeral` (used here on purpose, for no on-disk session
persistence) rules out a post-hoc session-transcript read as an alternative — so
capturing real usage for these four would mean switching their entire invocation/output
format, which would break every existing consumer that scans `call.body` as prose (this
module's own SKILL.md/MEMORY.md/paywall/auth markers, `reviewlib.dashboard.parser`'s
HEALTH_* classification, and live-log `tail -f`), not just add a token count. Out of
scope for this change; tracked separately with the real JSON shapes captured above as
the starting point. In the real logs sampled for the investigation these four backends
were the large majority of actual calls (a 3,000-file sample: claude 970, codex 606,
opencode 122, omp 121, vs. z.ai 66 / commandcode 4 REST calls) — so even a fully-shipped
REST-only token count would cover only a small minority of real call volume, which is
exactly why the follow-up matters.

Given that, this module reports TWO tiers, never conflating them:
  * ``bytes`` — a universal proxy available for EVERY call regardless of backend (the
    call log's file size). Not tokens, but the best available cross-backend signal, and
    exactly the metric the investigation itself used to find the real outliers (a 6.5MB
    single call, codex logs averaging ~400KB vs claude's ~2.6KB).
  * ``tokens_real`` — an EXACT token count, but ONLY for the handful of calls from a REST
    backend that emits it itself (z.ai, commandcode, gemini, claude in API mode) — see
    ``extract_usage_tokens``'s docstring for why this is scoped this narrowly (a real
    cross-contamination case was found where one seat's quoted output, containing its
    OWN usage line, appeared inside a DIFFERENT seat's call log).

Also parses the retry/promotion sidecar logs (``reviewlib.process.write_retry_log``) that
nothing else in the codebase reads today — the durable trail of every in-seat retry,
seat-fatal short-circuit, and reserve promotion, including the Fable-specific pattern the
investigation found (the priority-1 board seat failing on a majority of runs with an
explicit session-limit / paywall notice).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..process import log_dir as _dashboard_log_dir
from .parser import (
    HEALTH_OK,
    HEALTH_PAYWALL,
    _CALL_RE,
    _normalize_body,
    _parse_stamp,
    _PAYWALL_SENTINEL,
    CallLog,
    classify_call,
    model_id_for_call,
)

__all__ = [
    "RetryEvent",
    "HarnessStats",
    "format_bytes",
    "parse_retry_log",
    "list_retry_events",
    "list_call_logs",
    "extract_usage_tokens",
    "compute_harness_stats",
    "compute_model_stats",
    "compute_fable_report",
    "top_oversized_calls",
    "compute_stat_report",
]


def format_bytes(n: float) -> str:
    """Human-readable byte count (``401.2KB``, ``2.9GB``). Binary (1024) units, one
    decimal place; bare bytes below 1KB print as an integer with no decimal."""
    if n < 1024:
        return f"{int(n)}B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f}{unit}"
    # Opus review finding: the loop above only divides 4 times (through TB), so
    # falling through to this line without one more division would print the
    # TB-scale value with a "PB" label (e.g. an exact 1 PB logged as "1024.0PB").
    # Effectively unreachable for a real per-call log, but a real off-by-one.
    n /= 1024.0
    return f"{n:.1f}PB"


# ---- retry / promotion event parsing --------------------------------------------------
# Filename: {stamp}-{safe_backend(model)}-retry-{seq:04d}.log (process.write_retry_log).
_RETRY_FILENAME_RE = re.compile(r"^(\d{8}T\d{6})_(\d+)Z-.+-retry-\d+\.log$")
# Header line: "[review-cli] RETRY-EVENT kind=<k> model=<m>[ attempt=N/M] delay=Ds exit=E".
# `model` is matched non-greedily so the trailing fixed fields (delay=/exit=) anchor the
# match even though a model string can itself contain spaces/brackets (e.g. the `promote`
# kind's "Fable [architect]->commandcode:zai-org/GLM-5.2").
_RETRY_HEADER_RE = re.compile(
    r"^\[review-cli\] RETRY-EVENT kind=(?P<kind>\S+) model=(?P<model>.+?)"
    r"(?: attempt=\d+/\d+)? delay=(?P<delay>\d+(?:\.\d+)?)s exit=(?P<exit>\S+)\s*$"
)
_DETAIL_PREFIX = "[detail] "


def _safe_parse_stamp(date_part: str, micros: str) -> datetime | None:
    """`_parse_stamp`, guarded against a filename whose embedded date matches the
    REGEX shape but is not a real calendar date (e.g. a month of 99) — `strptime`
    raises `ValueError` in that case. Every function in this module promises "never
    raises" (report-only tooling must not crash `review stat` on a malformed or
    foreign file) — kimi review finding. `None` means "treat as unparseable"."""
    try:
        return _parse_stamp(date_part, micros)
    except ValueError:
        return None


@dataclass
class RetryEvent:
    """One durable in-seat-retry / seat-fatal / reserve-promotion record."""

    path: str
    started: datetime
    kind: str  # "retry" | "seat-fatal" | "timeout-exhausted" | "retry-time-exhausted" | "promote"
    model: str  # for "promote": "<failed>-><promoted>"; otherwise the failing seat's own id
    delay: float
    exit_code: str
    detail: str

    @property
    def source_model(self) -> str:
        """The seat that FAILED (splits a `promote` event's `"<failed>-><promoted>"`
        shape; every other kind's `model` already names only the failing seat)."""
        if self.kind == "promote" and "->" in self.model:
            return self.model.split("->", 1)[0]
        return self.model


def parse_retry_log(path: Path) -> RetryEvent | None:
    """Parse one `*-retry-NNNN.log` sidecar. Returns None if the name/header doesn't
    match (never raises — this is report-only tooling, not a hot path)."""
    m = _RETRY_FILENAME_RE.match(path.name)
    if not m:
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = raw.splitlines()
    if not lines:
        return None
    hm = _RETRY_HEADER_RE.match(lines[0])
    if not hm:
        return None
    started = _safe_parse_stamp(m.group(1), m.group(2))
    if started is None:
        return None
    detail_lines = [
        ln[len(_DETAIL_PREFIX) :] for ln in lines[1:] if ln.startswith(_DETAIL_PREFIX)
    ]
    return RetryEvent(
        path=str(path),
        started=started,
        kind=hm.group("kind"),
        model=hm.group("model"),
        delay=float(hm.group("delay")),
        exit_code=hm.group("exit"),
        detail="\n".join(detail_lines),
    )


def list_retry_events(
    directory: Path, *, since: datetime | None = None
) -> list[RetryEvent]:
    """All retry/promotion events in `directory`, oldest first. The FILENAME timestamp
    (cheap, no file read) is checked against `since` before parsing, so a bounded window
    doesn't pay to open every historical sidecar."""
    events: list[RetryEvent] = []
    for path in sorted(directory.glob("*-retry-*.log")):
        m = _RETRY_FILENAME_RE.match(path.name)
        if not m:
            continue
        if since is not None:
            stamp = _safe_parse_stamp(m.group(1), m.group(2))
            if stamp is None or stamp < since:
                continue
        event = parse_retry_log(path)
        if event is not None:
            events.append(event)
    return events


def list_call_logs(directory: Path, *, since: datetime | None = None) -> list[CallLog]:
    """All per-call logs in `directory`, oldest first. Same cheap-filename-prefilter
    pattern as `list_retry_events`."""
    from .parser import (
        parse_call_log,
    )  # local import: avoids a hard cycle at module load

    calls: list[CallLog] = []
    for path in sorted(directory.glob("*.log")):
        m = _CALL_RE.match(path.name)
        if not m:
            continue
        # Validate the stamp UNCONDITIONALLY, not only when `since` is set (codex/kimi
        # review finding: `--days 0`/no-`since` skipped this check entirely, so a
        # malformed-date filename reached the EXTERNAL, unguarded `parser.parse_call_log`
        # -> `_parse_stamp` -> raw ValueError, crashing the whole "all history" report —
        # exactly the case `test_malformed_filename_date_is_skipped_not_a_crash` failed
        # to cover).
        stamp = _safe_parse_stamp(m.group(1), m.group(2))
        if stamp is None:
            continue
        if since is not None and stamp < since:
            continue
        call = parse_call_log(path)
        if call is not None:
            calls.append(call)
    return calls


# ---- real token extraction (REST backends only) ---------------------------------------
# The exact shape review-cli's OWN code appends to a REST response's stdout
# (backends.py _openai_compatible_request / review_claude_api): OpenAI-style
# `prompt_tokens=N output_tokens=N` (z.ai, commandcode) or Anthropic-style
# `input_tokens=N output_tokens=N` (claude in API mode) — always the LAST line(s).
_USAGE_LINE_RE = re.compile(r"^(?:prompt|input)_tokens=(\d+)\s+output_tokens=(\d+)\s*$")

# Backends whose body can carry a TRUSTWORTHY usage line: the keyed-HTTP REST backends
# that write it themselves. Deliberately EXCLUDES every CLI/agentic backend (codex,
# opencode, omp, claude in CLI mode) — the investigation found a real case where one
# seat's quoted transcript (containing its OWN usage line) appeared INSIDE a different
# seat's (codex's) call log. Scanning every backend's body for this pattern would
# misattribute that seat's tokens; scoping strictly to the backends that emit the line
# themselves avoids it entirely — never relaxed to "any backend" without a stronger
# anchor than a bare regex match anywhere in the body.
#
# codex review finding: `openrouter` was missing here even though `review_openrouter`
# (backends.py) dispatches through the exact same `_openai_compatible_request` helper
# z.ai/commandcode use — the one that appends the `prompt_tokens=N output_tokens=N`
# line this module parses. Without it, a real OpenRouter token count was silently
# dropped (never counted, never listed in `tokens_recorded_backends`) even though the
# data was sitting right there in the call log, same as its z.ai/commandcode siblings.
_REST_USAGE_BACKENDS = frozenset({"z.ai", "commandcode", "gemini", "openrouter"})


def extract_usage_tokens(call: CallLog) -> tuple[int, int] | None:
    """`(prompt_tokens, output_tokens)` iff `call` is from a REST backend that emits
    real usage AND the body's last non-blank line IS the usage line. `None` otherwise —
    including for every agentic CLI backend, by design (see module + set docstrings).

    codex/kimi review finding: the REST emitters (`_parse_openai_usage`,
    `review_claude_api`, the gemini path) always APPEND the `prompt_tokens=N
    output_tokens=N` line, even when the provider's response carried no usable
    `usage` object — in that case they synthesize `0 0`, "0/0 on any wrong shape" per
    `_parse_openai_usage`'s own docstring. Before this guard, a `(0, 0)` match was
    treated as a REAL count (`calls_with_real_tokens += 1`, `tokens_real: true`) —
    exactly the honesty conflation this module and the README promise never to make
    ("`tokens_real` is `true` only ... where an exact count was actually parsed"). A
    genuinely completed call never legitimately reports zero prompt tokens (the prompt
    is never empty), so `(0, 0)` is treated as "usage absent", not "usage is zero".

    codex review finding, round 2: `_parse_openai_usage` defaults EACH field to 0
    INDEPENDENTLY on a partially-malformed `usage` object (e.g. `prompt_tokens` present
    but `completion_tokens` missing) — so the original `prompt == 0 and output == 0`
    guard let a genuinely partial reading straight through as `(real_prompt, 0)`,
    reported as an EXACT output-token count of zero. For the OpenAI-compatible backends
    this scope actually covers (z.ai/commandcode/openrouter via `_openai_compatible_
    request`), that 0 can never be a real measurement either: the caller already fails
    CLOSED (rc=1, no usage line emitted at all) whenever the response text is empty, so
    by the time this line exists the model produced real output — a truly zero output-
    token count is impossible for a call that reached this point. Excluding on EITHER
    field being 0 (not just both) closes that gap the same way the `(0, 0)` case
    already was: erring toward "usage absent" rather than risk reporting a defaulted
    field as an exact measurement."""
    eligible = call.backend in _REST_USAGE_BACKENDS or (
        call.backend == "claude" and call.argv0.startswith("Anthropic API ")
    )
    if not eligible:
        return None
    lines = [ln for ln in call.body.splitlines() if ln.strip()]
    if not lines:
        return None
    m = _USAGE_LINE_RE.match(lines[-1].strip())
    if not m:
        return None
    prompt, output = int(m.group(1)), int(m.group(2))
    if prompt == 0 or output == 0:
        return None
    return prompt, output


# ---- context-pollution + embedded-diff signals -----------------------------------------
# The investigation found 86%/68% of a 400-file codex-log sample referenced SKILL.md /
# MEMORY.md respectively — an agentic seat pulling the operator's personal global skill
# library into a plain diff review. Surfaced per-harness so a recurrence shows up by
# reading `review stat`, not by another manual archaeology pass.
_SKILL_MD_MARKER = "SKILL.md"
_MEMORY_MD_MARKER = "MEMORY.md"
# A call log captures the backend's OWN stdout, not the (unlogged, private) prompt it
# received — so the RAW diff isn't reliably observable here. But an agentic seat that
# reads/echoes the diff (via its own tool calls, or by quoting it while reasoning) makes
# it show up in its own transcript; the 6.5MB outlier the investigation found was in fact
# ~100% a `diff --git` block for exactly this reason. A high count here is a cheap,
# real correlate of an oversized-diff review, not an exact measurement.
_DIFF_GIT_LINE_RE = re.compile(r"^diff --git ", re.MULTILINE)
_BINARY_STUB_RE = re.compile(r"^Binary files .+ differ$", re.MULTILINE)


# ---- per-harness aggregation ------------------------------------------------------------
@dataclass
class HarnessStats:
    """Aggregate counters for one `CallLog.backend` value (oc/opencode, commandcode,
    omp, codex, claude, z.ai, gemini, openrouter, ...)."""

    harness: str
    calls: int = 0
    ok: int = 0
    fail: int = 0
    running: int = 0
    bytes_total: int = 0
    bytes_list: list[int] = field(default_factory=list)
    tokens_prompt: int = 0
    tokens_output: int = 0
    calls_with_real_tokens: int = 0
    skill_md_calls: int = 0
    memory_md_calls: int = 0

    @staticmethod
    def _percentile(ordered: list[int], p: float) -> int:
        if not ordered:
            return 0
        idx = min(len(ordered) - 1, int(p * len(ordered)))
        return ordered[idx]

    def to_dict(self) -> dict:
        # GLM performance finding: p50/p90/max each independently sorted (or scanned)
        # `bytes_list` — sort ONCE here and index all three off the same `ordered` list.
        ordered = sorted(self.bytes_list)
        return {
            "harness": self.harness,
            "calls": self.calls,
            "ok": self.ok,
            "fail": self.fail,
            "running": self.running,
            "bytes_total": self.bytes_total,
            "bytes_avg": round(self.bytes_total / self.calls, 1) if self.calls else 0.0,
            "bytes_p50": self._percentile(ordered, 0.5),
            "bytes_p90": self._percentile(ordered, 0.9),
            "bytes_max": ordered[-1] if ordered else 0,
            "tokens_real": self.calls_with_real_tokens > 0,
            "tokens_prompt": self.tokens_prompt,
            "tokens_output": self.tokens_output,
            "calls_with_real_tokens": self.calls_with_real_tokens,
            "skill_md_calls": self.skill_md_calls,
            "memory_md_calls": self.memory_md_calls,
        }


def compute_harness_stats(calls: list[CallLog]) -> dict[str, HarnessStats]:
    """Group `calls` by raw backend name (the harness Alex asked about: oc/opencode,
    commandcode, omp, codex, claude, ...). `cc` is not a distinct harness — it is an
    alias review-cli resolves to the single `commandcode` backend
    (`reviewlib.config.MODEL_ALIASES`), so it lands in the same `commandcode` row here.

    codex review finding: the ok/fail split here used to key on `call.has_error`, which
    is authoritative on the raw EXIT CODE only (`has_error`'s own docstring: "never by
    grepping the body") — but a Fable "is currently unavailable" sentinel is EXIT 0, so
    it counted as `ok` in this table while the SAME call's `classify_call` (used by the
    Fable section right below and by `compute_model_stats`) buckets it as
    `HEALTH_PAYWALL`. That produced a genuinely confusing report: a harness row reading
    "claude: ok=50 fail=0" sitting right next to a Fable section reporting a dozen
    paywall failures for calls under that same `claude` backend. Keying on
    `classify_call(call) == HEALTH_OK` instead makes every section of this NEW report
    agree on what "ok" means, without touching `has_error`'s own established semantics
    (used elsewhere across the pre-existing dashboard) at all."""
    out: dict[str, HarnessStats] = {}
    for call in calls:
        stats = out.setdefault(call.backend, HarnessStats(harness=call.backend))
        stats.calls += 1
        stats.bytes_total += call.size_bytes
        stats.bytes_list.append(call.size_bytes)
        if not call.completed:
            stats.running += 1
        elif classify_call(call) == HEALTH_OK:
            stats.ok += 1
        else:
            stats.fail += 1
        usage = extract_usage_tokens(call)
        if usage is not None:
            stats.calls_with_real_tokens += 1
            stats.tokens_prompt += usage[0]
            stats.tokens_output += usage[1]
        if _SKILL_MD_MARKER in call.body:
            stats.skill_md_calls += 1
        if _MEMORY_MD_MARKER in call.body:
            stats.memory_md_calls += 1
    return out


def compute_model_stats(calls: list[CallLog]) -> dict[str, dict]:
    """Group `calls` by board-model id (`model_id_for_call` — the same resolver the
    dashboard's health view uses), with a HEALTH_* class breakdown per model."""
    counts: dict[str, dict] = {}
    for call in calls:
        model = model_id_for_call(call)
        entry = counts.setdefault(
            model, {"model": model, "calls": 0, "bytes_total": 0, "classes": {}}
        )
        entry["calls"] += 1
        entry["bytes_total"] += call.size_bytes
        cls = classify_call(call)
        entry["classes"][cls] = entry["classes"].get(cls, 0) + 1
    return counts


# ---- Fable-specific report ---------------------------------------------------------------
# The investigation's headline finding: the priority-1 board seat (claude:claude-fable-5,
# display "Fable") is dispatched on most default reviews and fails on a majority of them —
# 1,836/4,322 sampled failures an explicit session/usage-limit notice, 714 the rc=0
# administrative "... is currently unavailable" sentinel. Surfaced as its own section
# (not buried in the per-harness table) because it is a distinct, actionable pattern:
# review-cli's own default panel burns real Claude-account session quota before ever
# reaching a working seat (reviewlib.seat_cooldown is the fix for the dispatch side; this
# report is how you'd SEE the pattern without another manual log-archaeology pass).
_SESSION_LIMIT_MARKERS = ("session limit", "usage-credits", "usage credits")
# kimi review finding: a bare `"auth" in low` substring match mis-buckets an unrelated
# retry detail into "auth" whenever it happens to contain "author"/"authoritative" —
# plausible prose in a quoted model response, not an auth failure. A `\b`-anchored
# "auth" prefix does NOT fix this (both false positives ALSO start with the literal
# prefix "auth"), so this matches the specific phrases the investigation's own real
# samples showed for a genuine auth failure ("Not logged in", "authentication failed")
# instead of a bare substring/prefix.
_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "not logged in",
    "unauthorized",
    "auth failed",
)
# glm review finding, round 2: a bare `\b401\b` regex scanned `event.detail`, which is
# free-text (quoted provider/model prose or stderr), for a literal "401" — the same
# false-positive class the word markers above were already fixed against (a detail
# reading "...at line 401 of server.py" or a quoted "status=401" inside reviewed prose
# would land in "auth"). `RetryEvent.exit_code` is the STRUCTURED field the retry-log
# writer (`process.write_retry_log`) already populates from the failing REST call's own
# `returncode` — and for a REST backend an HTTP error's returncode literally IS the
# HTTP status code (`rc = exc.code or 1` in every `except urllib.error.HTTPError`
# branch in backends.py) — so matching on that exact field instead of scanning free
# text for the digits is both more precise and needs no regex at all.


def _is_fable_source(model: str) -> bool:
    return "fable" in model.lower()


def _is_cached_skip(call: CallLog) -> bool:
    """A synthetic result from seat_cooldown's cached-skip path
    (backends._cooldown_skip_result) — the seat was NEVER actually dispatched, so this
    must be excluded from dispatch_attempts/paywall/failure_rate. Getting this wrong
    defeats the entire point of the Fable report: three independent review passes
    (Opus, codex, kimi-code/k3) converged on the same bug — a cached skip was counted
    as BOTH a dispatch attempt AND a paywall failure, so after the cooldown fix ships
    and starts avoiding real dispatches, the report kept showing the SAME high
    failure rate the fix exists to reduce, instead of a falling dispatch count.

    Anchored on `call.argv0` (the redacted header field `_cooldown_skip_result`
    exclusively controls: `"seat-cooldown skip (claude)"`), NOT a body-text substring
    match — kimi review finding: the body is model-generated prose, and both substrings
    ("cached:", "seat_cooldown") appear verbatim in THIS diff's own source/docs, so a
    genuine Claude/Fable review quoting `_cooldown_skip_result`'s code (exactly the
    kind of review that produced this finding) would misclassify a REAL dispatch as a
    cached skip. `argv0` cannot be reproduced by quoted prose — it is set by the
    literal `command = "seat-cooldown skip (claude)"` string, never by review output."""
    return call.argv0.startswith("seat-cooldown skip")


# Fable/kimi review finding (round 2 — a genuinely self-contradictory bug, not a naming
# nitpick): a cached-cooldown-skip ReviewResult mimics the exact rc=0 "is currently
# unavailable" sentinel shape ON PURPOSE (see seat_cooldown.py's module docstring —
# "every downstream consumer... recognises it via the SAME code path already exercised
# for a live paywall response"). `panel.result_is_usable` therefore treats it as NOT
# usable, exactly like a live paywall failure — which means it DOES reach the failover
# loop and DOES get logged via `write_retry_log`, TWICE per skip (retry.classify_failure
# routes the rc=0 sentinel straight to `kind="seat-fatal"`, then panel.py's failover
# loop separately writes `kind="promote"` when it backfills from reserve). Both retry-
# event `detail` fields are populated from the skip's own synthetic `result.stdout`
# (`process.write_retry_log`: `detail = result.stderr or result.stdout`), so BOTH carry
# the literal `_cooldown_skip_result`-authored text below — a compute_fable_report that
# didn't filter these would count every cache HIT during a cooldown window as a fresh
# session-limit occurrence, doubling the exact distortion `_is_cached_skip` already
# fixes on the call-log side. Anchored on this literal, deterministic, code-authored
# substring (not model prose) for the same reason `_is_cached_skip` anchors on argv0.
_COOLDOWN_SKIP_DETAIL_MARKER = "reviewlib.seat_cooldown"


def _is_cached_skip_retry_event(event: RetryEvent) -> bool:
    return _COOLDOWN_SKIP_DETAIL_MARKER in event.detail


def compute_fable_report(calls: list[CallLog], retry_events: list[RetryEvent]) -> dict:
    """Fable dispatch/failure counts from BOTH data sources: the per-call logs (a REAL
    dispatch whose body carries the rc=0 paywall sentinel — cached skips are excluded,
    see `_is_cached_skip`) and the retry-event sidecars (every seat-fatal / promote
    event attributed back to Fable as the failing seat, with cached-skip-originated
    events ALSO excluded — see `_is_cached_skip_retry_event`).

    Fable/kimi review finding (round 2): an earlier version of this docstring claimed
    "a cached skip never reaches the failover loop's promote path, so it has no
    retry-event" — that is FALSE, and self-contradicts the cooldown module's own stated
    design. A cached-skip result mimics the live paywall sentinel shape ON PURPOSE so
    every downstream consumer (including `panel.result_is_usable`) treats it exactly
    like a real failure — which means it DOES get logged, TWICE per skip
    (`retry.classify_failure` routes the rc=0 sentinel straight to a `seat-fatal` event,
    then the failover loop's reserve backfill writes a SEPARATE `promote` event), both
    carrying the skip's own cached reason text in `detail`. Without the exclusion below,
    every review run during an active cooldown window would count as a FRESH
    session-limit occurrence — the same distortion `_is_cached_skip` fixes on the
    call-log side, just reached through the retry-event path instead.

    HONEST LIMITATION (codex/kimi review finding, not fixed here — it is a pre-existing
    constraint of `model_id_for_call`, not something this report introduced): a claude
    CLI-mode call is only ever attributable to Fable when its body carries the exact
    paywall sentinel ("... is currently unavailable"); every OTHER claude-p call —
    including a genuinely SUCCESSFUL Fable dispatch, or a Fable failure shaped like a
    session-limit notice rather than the sentinel — is attributed to the Opus seat
    instead, because review-cli's own CLI-mode logs never record which model was
    actually requested. So `dispatch_attempts`/`failure_rate` here are a LOWER BOUND
    on real Fable activity, not a true count: they see only the sentinel-shaped subset,
    which is disproportionately failures — `failure_rate` will read close to 1.0 by
    construction even once the cooldown fix (or Fable itself) is working well.
    `cached_skips` is exact (its argv0 is unambiguous); `dispatch_attempts` is not. A
    real fix would need `review_claude_cli` to record the REQUESTED model in its own
    call-log header, independent of the response body — out of scope for this change.

    This SAME constraint means a cached-cooldown-skip for a NON-Fable claude model
    (e.g. Opus) can also be misattributed to Fable here, since `_cooldown_skip_result`'s
    body deliberately mirrors the same short "is currently unavailable" sentinel shape
    regardless of which claude model actually triggered it (codex review finding,
    round 3) — tracked as review-cli#190, same root cause and same out-of-scope
    boundary as the paragraph above."""
    fable_calls = [
        c
        for c in calls
        if _is_fable_source(model_id_for_call(c)) or _is_fable_source(c.backend)
    ]
    cached_skip_calls = [c for c in fable_calls if _is_cached_skip(c)]
    dispatch_calls = [c for c in fable_calls if not _is_cached_skip(c)]

    paywall = sum(1 for c in dispatch_calls if classify_call(c) == HEALTH_PAYWALL)
    # UNION, not a sum of two predicates that can overlap: classify_call checks the
    # paywall sentinel BEFORE the timeout/has_error branches, so a call can be BOTH
    # HEALTH_PAYWALL and has_error==True (a non-zero-exit paywall body, or a footerless
    # legacy log whose has_error falls back to a stderr/error-marker heuristic).
    # Summing both would push failure_rate above 1.0 (codex/kimi review finding).
    failures = sum(
        1 for c in dispatch_calls if classify_call(c) == HEALTH_PAYWALL or c.has_error
    )

    fable_events = [
        e
        for e in retry_events
        if _is_fable_source(e.source_model) and not _is_cached_skip_retry_event(e)
    ]
    cached_skip_events = sum(
        1
        for e in retry_events
        if _is_fable_source(e.source_model) and _is_cached_skip_retry_event(e)
    )
    reasons = {"session_limit": 0, "paywall": 0, "auth": 0, "other": 0}
    for event in fable_events:
        low = event.detail.lower()
        if any(marker in low for marker in _SESSION_LIMIT_MARKERS):
            reasons["session_limit"] += 1
        elif _PAYWALL_SENTINEL in _normalize_body(event.detail):
            reasons["paywall"] += 1
        elif (
            any(marker in low for marker in _AUTH_FAILURE_MARKERS)
            or event.exit_code == "401"
        ):
            reasons["auth"] += 1
        else:
            reasons["other"] += 1

    dispatches = len(dispatch_calls)
    return {
        "dispatch_attempts": dispatches,
        "cached_skips": len(cached_skip_calls),
        "paywall_sentinel_calls": paywall,
        "failure_rate": round(failures / dispatches, 4) if dispatches else None,
        "retry_events": len(fable_events),
        "retry_event_reasons": reasons,
        # A cached skip is logged TWICE per skip (seat-fatal + promote — see this
        # function's docstring); both are excluded from `retry_events`/reasons above so
        # a cooldown window's cache HITS never masquerade as fresh occurrences. Surfaced
        # here (not silently dropped) so the report is honest about how much retry-log
        # volume the cooldown fix is already suppressing.
        "cached_skip_retry_events_excluded": cached_skip_events,
    }


# ---- oversized-call outliers --------------------------------------------------------------
def top_oversized_calls(calls: list[CallLog], *, limit: int = 10) -> list[dict]:
    """The `limit` largest calls by log size — the investigation's own method for
    finding real outliers (the 6.5MB / 583-file diff, the 392KB skill/memory-pollution
    call with a 3.5KB diff). Each entry names the embedded-diff signal (see module
    docstring) so an oversized-diff call is distinguishable from an oversized-
    agentic-exploration call at a glance, without re-opening the raw log."""
    ranked = sorted(calls, key=lambda c: c.size_bytes, reverse=True)[:limit]
    out = []
    for call in ranked:
        out.append(
            {
                "backend": call.backend,
                "model": model_id_for_call(call),
                "task_code": call.task_code,
                "started": call.started.isoformat(),
                "size_bytes": call.size_bytes,
                "diff_git_files": len(_DIFF_GIT_LINE_RE.findall(call.body)),
                "binary_stub_files": len(_BINARY_STUB_RE.findall(call.body)),
                "skill_md": _SKILL_MD_MARKER in call.body,
                "memory_md": _MEMORY_MD_MARKER in call.body,
                "path": call.path,
            }
        )
    return out


# ---- top-level report assembly -----------------------------------------------------------
def compute_stat_report(
    directory: Path | None = None, *, since: datetime | None = None, top: int = 10
) -> dict:
    """Assemble the full `review stat` report: per-harness breakdown, per-model health,
    the Fable pattern, retry/promotion totals, and the largest-call outliers."""
    directory = directory if directory is not None else _dashboard_log_dir()
    calls = list_call_logs(directory, since=since)
    retry_events = list_retry_events(directory, since=since)

    retry_by_kind: dict[str, int] = {}
    for event in retry_events:
        retry_by_kind[event.kind] = retry_by_kind.get(event.kind, 0) + 1

    harnesses = compute_harness_stats(calls)
    return {
        "log_dir": str(directory),
        "since": since.isoformat() if since else None,
        "call_count": len(calls),
        "retry_event_count": len(retry_events),
        # Honest, explicit like the dashboard's compute_stats: real numbers exist ONLY
        # for the REST-backed subset (see extract_usage_tokens); never fabricated for
        # the agentic CLI harnesses that carry none.
        "tokens_recorded_backends": sorted(
            _REST_USAGE_BACKENDS | {"claude (API mode)"}
        ),
        "harnesses": {name: hs.to_dict() for name, hs in sorted(harnesses.items())},
        "models": compute_model_stats(calls),
        "fable": compute_fable_report(calls, retry_events),
        "retry_events_by_kind": retry_by_kind,
        "top_oversized_calls": top_oversized_calls(calls, limit=top),
    }
