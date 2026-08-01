"""Unit tests for the omp (Oh My Pi) agentic read-only backend (review-cli#174).

`omp:<provider>/<model>` (e.g. `omp:kimi-code/k3`) routes to `review_omp`, which launches
`omp -p --no-session --no-extensions --no-skills --tools read,grep,glob --add-dir <repo>
--config <cage.yml> --model <sel>` from a NEUTRAL temp cwd (omp executes project-shipped
`.mcp.json`/`.omp/tools` from its launch cwd — verified live — so it must never launch
inside the reviewed repo) with the prompt+diff handed over as an `@payload.md` message
arg (omp does NOT read the prompt from stdin, and argv-passing a big payload would hit
the ~1 MB ARG_MAX ceiling — the `@file` transport dodges both; verified against omp v17).

These tests pin the dispatch contract WITHOUT a live omp (non-deterministic): they patch
BOTH `_run_streamed` (to capture the argv/cwd the backend would launch) AND `_which` (so
the tests are HERMETIC — they pass on a CI box with no `omp` binary on PATH). The auth
probe is exercised against a throwaway sqlite db via the `OMP_AUTH_DB` env override, so
the developer's real ~/.omp/agent/agent.db is never touched.

Mock harness style mirrors tests/test_opencode_realrepo.py.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as review_backends  # noqa: E402


class _Captured:
    """Stand-in for the CompletedProcess `_run_streamed` returns; records argv/cwd and
    reads the `@<payloadfile>` WHILE it still exists (review_omp deletes it after the
    run), so tests can assert on the exact message omp would receive."""

    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.cwd: Path | None = None
        self.timeout: int | None = None
        self.header_argv0: str | None = None
        self.payload_existed = False
        self.payload_text: str | None = None
        self.payload_path: Path | None = None
        self.cage_text: str | None = None
        self.env: dict | None = None

    def __call__(self, argv, cwd, timeout, backend, round_no=0, announce=False, header_argv0=None, env=None):
        self.argv = list(argv)
        self.cwd = Path(cwd)
        self.timeout = timeout
        self.header_argv0 = header_argv0
        self.env = env
        at_args = [a for a in self.argv if a.startswith("@")]
        if at_args:
            self.payload_path = Path(at_args[-1][1:])
            self.payload_existed = self.payload_path.is_file()
            if self.payload_existed:
                self.payload_text = self.payload_path.read_text(encoding="utf-8")
        if "--config" in self.argv:
            cage = Path(self.argv[self.argv.index("--config") + 1])
            if cage.is_file():
                self.cage_text = cage.read_text(encoding="utf-8")

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Proc()


@contextlib.contextmanager
def _capture_omp():
    """Patch `_run_streamed` (capture) and `_which` (hermetic — no real omp binary
    needed) for one test, restoring both afterward (single restore point, mirroring
    test_opencode_realrepo's `_capture_opencode`)."""
    cap = _Captured()
    orig_run = review_backends._run_streamed
    orig_which = review_backends._which
    review_backends._run_streamed = cap  # type: ignore[assignment]
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    try:
        yield cap
    finally:
        review_backends._run_streamed = orig_run
        review_backends._which = orig_which


@contextlib.contextmanager
def _omp_auth_db(provider_rows: list[tuple[str, str | None]]):
    """Point the omp auth probe at a THROWAWAY sqlite db carrying `provider_rows`
    [(provider, disabled_cause)], restoring the env afterward. Hermetic: the real
    ~/.omp/agent/agent.db is never read."""
    fd, name = tempfile.mkstemp(prefix="review-cli-omp-auth-", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(name)
    try:
        conn.execute(
            "CREATE TABLE auth_credentials ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, "
            "credential_type TEXT NOT NULL, data TEXT NOT NULL, "
            "disabled_cause TEXT DEFAULT NULL)"
        )
        for provider, disabled in provider_rows:
            conn.execute(
                "INSERT INTO auth_credentials (provider, credential_type, data, disabled_cause)"
                " VALUES (?, 'oauth', '{}', ?)",
                (provider, disabled),
            )
        conn.commit()
    finally:
        conn.close()
    saved = os.environ.get(review_backends._OMP_AUTH_DB_ENV)
    os.environ[review_backends._OMP_AUTH_DB_ENV] = name
    try:
        yield Path(name)
    finally:
        if saved is None:
            os.environ.pop(review_backends._OMP_AUTH_DB_ENV, None)
        else:
            os.environ[review_backends._OMP_AUTH_DB_ENV] = saved
        Path(name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Routing / resolution
# ---------------------------------------------------------------------------


def test_omp_routes_to_review_omp():
    assert review_backends.resolve_backend("omp") is review_backends.review_omp
    assert review_backends.resolve_backend("omp:kimi-code/k3") is review_backends.review_omp
    # The matcher lowercases before dispatch, like every other named route.
    assert review_backends.resolve_backend("OMP:Kimi-Code/K3") is review_backends.review_omp
    # `omp:` must NOT fall through to the opencode catch-all.
    assert review_backends._match_named_backend("omp:kimi-code/k3") is review_backends.review_omp


def test_omp_is_a_known_backend_token():
    assert review_backends.is_known_backend_token("omp") is True
    assert review_backends.is_known_backend_token("omp:kimi-code/k3") is True


def test_omp_route_name_and_effective_provider():
    assert review_backends.provider_route_name("omp:kimi-code/k3") == "omp"
    assert review_backends.provider_route_name("omp") == "omp"
    # The provider under the transport is `omp` itself, so REVIEW_UNPAID_PROVIDERS /
    # config.yaml unpaid_providers can gate the seat.
    assert review_backends.effective_provider("omp:kimi-code/k3") == "omp"


def test_omp_default_routes_live():
    # A config.yaml seat `omp:kimi-code/k3` must pass the #25 anti-rot guard: the full id
    # resolves to a NAMED backend and `omp` is not a dead provider.
    assert review_backends.default_routes_live("omp:kimi-code/k3") is True
    assert review_backends.default_routes_live("omp") is True


def test_omp_provider_from_model():
    assert review_backends._omp_provider_from_model("omp:kimi-code/k3") == "kimi-code"
    assert review_backends._omp_provider_from_model("OMP:Kimi-Code/K3") == "kimi-code"
    assert review_backends._omp_provider_from_model("omp") is None
    assert review_backends._omp_provider_from_model("codex") is None


# ---------------------------------------------------------------------------
# Launch contract (captured, no real omp)
# ---------------------------------------------------------------------------


def test_omp_launches_print_ephemeral_readonly_with_at_file_payload():
    with _capture_omp() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            res = review_backends.review_omp("omp:kimi-code/k3", "Review.", "some diff", repo, 60)
        assert res.returncode == 0, res
        argv = cap.argv or []
        assert argv[0].endswith("/omp"), argv
        # Non-interactive, ephemeral, read-only cage.
        assert "-p" in argv, argv
        assert "--no-session" in argv, argv
        assert "--tools" in argv, argv
        assert argv[argv.index("--tools") + 1] == "read,grep,glob", argv
        # No write/bash-capable tool may be enabled.
        tools = argv[argv.index("--tools") + 1].split(",")
        assert not ({"bash", "edit", "write"} & set(tools)), tools
        # Extension/skill discovery off.
        assert "--no-extensions" in argv, argv
        assert "--no-skills" in argv, argv
        # CAGE (review of #174, verified live): omp EXECUTES project-shipped .mcp.json /
        # .omp/tools from its launch cwd, so it must launch from a NEUTRAL temp dir...
        assert cap.cwd is not None and cap.cwd != repo, (cap.cwd, repo)
        assert repo not in (cap.cwd.parents if cap.cwd else []), cap.cwd
        assert cap.cwd.name.startswith("review-cli-omp-"), cap.cwd
        # ...with the repo mounted read-only as a workspace instead.
        assert "--add-dir" in argv, argv
        assert Path(argv[argv.index("--add-dir") + 1]) == repo, argv
        # The seat's HOME is SANITIZED (kills user-scope MCP discovery: ~/.claude.json
        # et al. — the MCP-exec hole), while PI_CODING_AGENT_DIR pins the REAL agent
        # dir so provider auth still resolves; OMP_PROFILE is dropped so nothing
        # re-derives profile paths from the fake HOME.
        env = cap.env or {}
        assert env.get("HOME") == str(cap.cwd / "home"), env.get("HOME")
        assert env.get("HOME") != os.path.expanduser("~"), env.get("HOME")
        assert env.get("PI_CODING_AGENT_DIR") == str(review_backends._omp_agent_dir()), env
        assert "OMP_PROFILE" not in env, env
        # The neutral launch cwd (and the payload with it) is cleaned up after the run.
        assert not cap.cwd.exists(), cap.cwd
        # The config overlay disables the read tool's URL path (fetch), the xd:// device
        # transport (which carries write/edit/bash around --tools), and project MCP.
        assert "--config" in argv, argv
        cage = cap.cage_text or ""
        assert "enabled: false" in cage, cage
        assert "xdev: false" in cage, cage
        assert "enableProjectConfig: false" in cage, cage
        # The model selector after `omp:` goes to --model verbatim.
        assert "--model" in argv, argv
        assert argv[argv.index("--model") + 1] == "kimi-code/k3", argv
        # Payload travels as an @file arg, existed during the call, and carried the
        # prompt + diff — NOT the diff on argv (ARG_MAX) and NOT stdin (unsupported).
        assert cap.payload_existed is True, cap.argv
        assert cap.payload_text is not None and "Review." in cap.payload_text
        assert "some diff" in cap.payload_text
        assert "some diff" not in " ".join(a for a in argv if not a.startswith("@")), argv
        # The payload file lives OUTSIDE the reviewed repo.
        assert cap.payload_path is not None
        assert repo not in cap.payload_path.parents, cap.payload_path
        # The sidecar log header carries the model selector for dashboard attribution.
        assert cap.header_argv0 == "omp -m kimi-code/k3", cap.header_argv0
        assert "some diff" not in (cap.header_argv0 or ""), cap.header_argv0


def test_omp_message_invites_reading_files_read_only():
    with _capture_omp() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            review_backends.review_omp("omp:kimi-code/k3", "Review.", "DIFFBODY", repo, 60)
        message = cap.payload_text or ""
        assert "read" in message.lower(), message
        assert "DIFFBODY" in message, message
        # The read-only posture is spelled out (defense in depth behind the tool cage)...
        assert "do not edit" in message.lower(), message
        # ...the egress boundary is named (prompt-level backup to fetch.enabled: false)...
        assert "network" in message.lower(), message
        # ...and the model is told WHERE the repo is mounted (neutral-cwd containment).
        assert str(repo) in message, message


def test_omp_bare_seat_passes_no_model_flag():
    with _capture_omp() as cap:
        with tempfile.TemporaryDirectory() as d:
            review_backends.review_omp("omp", "Review.", "diff", Path(d), 60)
        argv = cap.argv or []
        assert "--model" not in argv, argv
        assert cap.header_argv0 is None, cap.header_argv0


def test_omp_keeps_requested_timeout():
    with _capture_omp() as cap:
        with tempfile.TemporaryDirectory() as d:
            review_backends.review_omp("omp:kimi-code/k3", "Review.", "DIFF", Path(d), 1200)
        assert cap.timeout == 1200, cap.timeout


def test_omp_effort_maps_to_thinking_flag():
    with _capture_omp() as cap:
        with tempfile.TemporaryDirectory() as d:
            review_backends.review_omp("omp:kimi-code/k3", "Review.", "DIFF", Path(d), 60, effort="high")
        argv = cap.argv or []
        assert "--thinking" in argv, argv
        assert argv[argv.index("--thinking") + 1] == "high", argv


def test_omp_run_does_not_write_anything_into_repo():
    # The payload temp file must land in the SYSTEM temp dir, never in the reviewed tree.
    def _tree(root: Path) -> set[str]:
        return {str(p.relative_to(root)) for p in root.rglob("*")}

    with _capture_omp() as cap:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            before = _tree(repo)
            review_backends.review_omp("omp:m", "Review.", "DIFF", repo, 60)
            after = _tree(repo)
        assert before == after, after - before
        assert cap.payload_path is not None and repo not in cap.payload_path.parents


# ---------------------------------------------------------------------------
# Availability probe (binary + offline sqlite auth check)
# ---------------------------------------------------------------------------


def test_omp_unavailable_without_binary():
    orig_which_optional = review_backends._which_optional
    review_backends._which_optional = lambda name: None  # type: ignore[assignment]
    try:
        reason = review_backends.backend_unavailable_reason("omp:kimi-code/k3")
        assert reason is not None
        assert "omp" in reason, reason
        assert review_backends.backend_available("omp:kimi-code/k3") is False
    finally:
        review_backends._which_optional = orig_which_optional


def test_omp_unavailable_without_auth_db():
    orig_which = review_backends._which
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    saved = os.environ.get(review_backends._OMP_AUTH_DB_ENV)
    os.environ[review_backends._OMP_AUTH_DB_ENV] = "/nonexistent/no-such-dir/agent.db"
    try:
        reason = review_backends.backend_unavailable_reason("omp:kimi-code/k3")
        assert reason is not None
        assert "kimi-code" in reason, reason
        assert review_backends.backend_available("omp:kimi-code/k3") is False
    finally:
        review_backends._which = orig_which
        if saved is None:
            os.environ.pop(review_backends._OMP_AUTH_DB_ENV, None)
        else:
            os.environ[review_backends._OMP_AUTH_DB_ENV] = saved


def test_omp_available_with_provider_credential():
    orig_which = review_backends._which
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    try:
        with _omp_auth_db([("kimi-code", None)]):
            assert review_backends.backend_unavailable_reason("omp:kimi-code/k3") is None
            assert review_backends.backend_available("omp:kimi-code/k3") is True
    finally:
        review_backends._which = orig_which


def test_omp_availability_is_provider_scoped():
    orig_which = review_backends._which
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    try:
        with _omp_auth_db([("kimi-code", None), ("openai", "quota exhausted")]):
            # A credential for ANOTHER provider does not make the kimi-code seat live...
            assert review_backends.backend_unavailable_reason("omp:openai/gpt-5.5") is not None
            # ...and a DISABLED credential does not count.
            assert review_backends.backend_unavailable_reason("omp:kimi-code/k3") is None
        with _omp_auth_db([("kimi-code", "token revoked")]):
            assert review_backends.backend_unavailable_reason("omp:kimi-code/k3") is not None
        # A bare `omp` seat is live when ANY usable credential exists.
        with _omp_auth_db([("kimi-code", None)]):
            assert review_backends.backend_unavailable_reason("omp") is None
    finally:
        review_backends._which = orig_which


def test_omp_auth_db_unreadable_degrades_to_unavailable():
    # A corrupt/non-sqlite file must degrade to "unauthenticated", never a traceback.
    orig_which = review_backends._which
    review_backends._which = lambda name: f"/fake/bin/{name}"  # type: ignore[assignment]
    saved = os.environ.get(review_backends._OMP_AUTH_DB_ENV)
    fd, name = tempfile.mkstemp(prefix="review-cli-omp-corrupt-", suffix=".db")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("this is not a sqlite database")
        os.environ[review_backends._OMP_AUTH_DB_ENV] = name
        reason = review_backends.backend_unavailable_reason("omp:kimi-code/k3")
        assert reason is not None
        assert review_backends.backend_available("omp:kimi-code/k3") is False
    finally:
        review_backends._which = orig_which
        if saved is None:
            os.environ.pop(review_backends._OMP_AUTH_DB_ENV, None)
        else:
            os.environ[review_backends._OMP_AUTH_DB_ENV] = saved
        Path(name).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Unpaid-provider gating + board scope label + dashboard attribution
# ---------------------------------------------------------------------------


def test_omp_seat_skipped_when_provider_marked_unpaid():
    review_backends.configure_unpaid_providers(["omp"])
    try:
        res = review_backends.review_omp("omp:kimi-code/k3", "q", "", REPO_ROOT, 10)
        assert res.returncode == 1, res
        assert "unpaid" in res.stderr, res.stderr
        assert review_backends.backend_available("omp:kimi-code/k3") is False
    finally:
        review_backends.configure_unpaid_providers(None)


def test_show_board_scope_label_marks_omp_agentic():
    # omp runs read-only (read/grep/glob tools) in the real cwd like codex — agentic
    # regardless of the repo bit (mirrors `_seat_reads_repo`'s codex branch).
    from reviewlib.cli import _seat_reads_repo  # noqa: PLC0415

    assert _seat_reads_repo("omp:kimi-code/k3", True) is True
    assert _seat_reads_repo("omp:kimi-code/k3", False) is True


# ---------------------------------------------------------------------------
# Forced transport mode / profile-aware auth db / probe cache / facade export
# ---------------------------------------------------------------------------


def test_omp_forced_api_mode_fails_loudly():
    """omp is CLI-only: REVIEW_OMP_MODE=api must FAIL (mirrors the z.ai/commandcode
    forced-mode contract), never silently launch the CLI — in BOTH the dispatch path
    (rc=1 result naming the env var) and the availability probe."""
    saved_mode = os.environ.get("REVIEW_OMP_MODE")
    saved_log = os.environ.get("REVIEW_LOG_DIR")
    os.environ["REVIEW_OMP_MODE"] = "api"
    # Keep the failure sidecar log out of the developer's real log dir.
    with tempfile.TemporaryDirectory() as log_dir:
        os.environ["REVIEW_LOG_DIR"] = log_dir
        try:
            with _capture_omp() as cap:
                res = review_backends.review_omp("omp:kimi-code/k3", "q", "", REPO_ROOT, 10)
                assert cap.argv is None  # never launched
            assert res.returncode == 1, res
            assert "REVIEW_OMP_MODE" in res.stderr, res.stderr
            reason = review_backends.backend_unavailable_reason("omp:kimi-code/k3")
            assert reason is not None and "REVIEW_OMP_MODE" in reason, reason
            assert review_backends.backend_available("omp:kimi-code/k3") is False
        finally:
            if saved_mode is None:
                os.environ.pop("REVIEW_OMP_MODE", None)
            else:
                os.environ["REVIEW_OMP_MODE"] = saved_mode
            if saved_log is None:
                os.environ.pop("REVIEW_LOG_DIR", None)
            else:
                os.environ["REVIEW_LOG_DIR"] = saved_log


def test_omp_auth_db_follows_omp_storage_env():
    """The probe must look where omp ITSELF would (codex review of #174):
    PI_CODING_AGENT_DIR replaces the storage dir; OMP_PROFILE isolates under
    ~/.omp/profiles/<name>/agent; the OMP_AUTH_DB test override wins over both."""
    saved = {k: os.environ.get(k) for k in ("PI_CODING_AGENT_DIR", "OMP_PROFILE", review_backends._OMP_AUTH_DB_ENV)}
    try:
        os.environ.pop(review_backends._OMP_AUTH_DB_ENV, None)
        os.environ.pop("OMP_PROFILE", None)
        os.environ["PI_CODING_AGENT_DIR"] = "/tmp/custom-agent"
        assert review_backends._omp_auth_db() == Path("/tmp/custom-agent/agent.db")
        os.environ.pop("PI_CODING_AGENT_DIR", None)
        os.environ["OMP_PROFILE"] = "work"
        assert review_backends._omp_auth_db() == Path.home() / ".omp" / "profiles" / "work" / "agent" / "agent.db"
        # The explicit storage dir beats the profile; the test override beats everything.
        os.environ["PI_CODING_AGENT_DIR"] = "/tmp/custom-agent"
        assert review_backends._omp_auth_db() == Path("/tmp/custom-agent/agent.db")
        os.environ[review_backends._OMP_AUTH_DB_ENV] = "/tmp/override.db"
        assert review_backends._omp_auth_db() == Path("/tmp/override.db")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_omp_auth_cache_invalidates_on_db_change():
    """The probe memo (glm review of #174) must track the db's mtime: a credential change
    mid-process (login/logout) is picked up on the very next probe, never served stale."""
    with _omp_auth_db([("kimi-code", None)]) as db:
        assert review_backends._omp_auth_available("kimi-code") is True
        # Second probe: memoized, same answer (and it must NOT error).
        assert review_backends._omp_auth_available("kimi-code") is True
        # Revoke the credential in place and force a NEWER mtime, so the memo miss is
        # about content, not filesystem timestamp granularity.
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("UPDATE auth_credentials SET disabled_cause = 'revoked'")
            conn.commit()
        finally:
            conn.close()
        stat = db.stat()
        os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert review_backends._omp_auth_available("kimi-code") is False


def test_omp_auth_probe_hydrates_all_providers_in_one_read():
    """glm review of #174: N omp seats on N distinct providers must cost ONE db read per
    board pass — a stamp miss hydrates EVERY live provider, not just the asked one."""
    calls = []
    orig_probe = review_backends._omp_auth_probe

    def _counting(db):
        calls.append(str(db))
        return orig_probe(db)

    with _omp_auth_db([("kimi-code", None), ("openai", None), ("deepseek", "dead")]):
        review_backends._omp_auth_probe = _counting  # type: ignore[assignment]
        try:
            assert review_backends._omp_auth_available("kimi-code") is True
            assert review_backends._omp_auth_available("openai") is True
            assert review_backends._omp_auth_available("deepseek") is False
            assert review_backends._omp_auth_available(None) is True
        finally:
            review_backends._omp_auth_probe = orig_probe
    assert len(calls) == 1, calls  # 4 probes, 1 db read


def test_omp_seat_env_pins_profile_agent_dir():
    """With OMP_PROFILE set, the seat env must pin PI_CODING_AGENT_DIR at the PROFILE's
    agent dir (auth resolves) AND drop OMP_PROFILE itself (nothing re-derives profile
    paths from the fake HOME) — the composition the launch-contract test doesn't set up."""
    saved = os.environ.get("OMP_PROFILE")
    os.environ["OMP_PROFILE"] = "work"
    try:
        with _capture_omp() as cap:
            with tempfile.TemporaryDirectory() as d:
                review_backends.review_omp("omp:kimi-code/k3", "q", "", Path(d), 10)
            env = cap.env or {}
            expected = str(Path.home() / ".omp" / "profiles" / "work" / "agent")
            assert env.get("PI_CODING_AGENT_DIR") == expected, env
            assert "OMP_PROFILE" not in env, env
    finally:
        if saved is None:
            os.environ.pop("OMP_PROFILE", None)
        else:
            os.environ["OMP_PROFILE"] = saved


def test_omp_cage_overlay_is_valid_yaml_with_expected_keys():
    """The cage overlay is the security boundary's third layer — pin it as PARSED YAML
    (a typo that still looks right in a string assert would silently drop a layer)."""
    import yaml  # noqa: PLC0415

    overlay = yaml.safe_load(review_backends._OMP_CAGE_OVERLAY)
    assert overlay == {
        "fetch": {"enabled": False},
        "tools": {"xdev": False},
        "mcp": {"enableProjectConfig": False},
    }, overlay


def test_omp_exported_from_facade():
    """Every backend resolve_backend can return is re-exported from the reviewlib facade
    (codex review of #174) — a new backend missing here breaks `from reviewlib import
    review_*` callers while everything internal still works."""
    import reviewlib  # noqa: PLC0415

    assert reviewlib.review_omp is review_backends.review_omp
    assert "review_omp" in reviewlib.__all__


def test_dashboard_attributes_omp_call_to_omp_board_seat():
    # `omp -m <provider/model>` header argv0 must attribute to the `omp:<provider/model>`
    # board seat — mirroring the opencode -> `oc:` mapping (review-cli#24 class of bug:
    # without it every omp seat collapses to one `omp` row and shows `no_data`).
    from datetime import datetime, timezone

    from reviewlib.dashboard import parser as p

    call = p.CallLog("", "x.log", datetime(2026, 8, 1, tzinfo=timezone.utc), "omp", 0,
                     "omp -m kimi-code/k3", "")
    assert p.model_id_for_call(call) == "omp:kimi-code/k3"
    # A bare omp call with no -m stays the backend name (no mis-attribution).
    bare = p.CallLog("", "y.log", datetime(2026, 8, 1, tzinfo=timezone.utc), "omp", 0,
                     "/opt/homebrew/bin/omp", "")
    assert p.model_id_for_call(bare) == "omp"


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
