#!/usr/bin/env python3
"""Unit tests for the log-dir/log-file sandboxed-write fallback (review-cli#162).

A SANDBOXED caller (an agent harness's restricted Bash tool, a locked-down CI runner)
commonly denies writes outside the system temp dir — `~/Library/Logs`/`$XDG_STATE_HOME`
live outside most sandbox allow-lists. Before this fix, `log_dir()`'s `mkdir` (when the
directory doesn't exist yet) or a per-call `os.open()` (when it already does, e.g. from
earlier unsandboxed runs) raised an uncaught `PermissionError`/`OSError` straight out of
`_run_streamed` BEFORE the backend subprocess was even spawned — killing that seat
entirely (observed live: a Fable/claude-p seat, review-cli#162). Since every seat
(opus/codex/fable/opencode) hits the identical `_open_log -> log_dir` path, this was never
actually fable-specific — fable is simply the slowest seat and so the most likely to still
be running if a time-scoped sandbox grant is the trigger.

These tests exercise `log_dir()` and `_open_log_with_fallback()` directly with a
DELIBERATELY unwritable directory/file (chmod 0o000 — no network, no real model CLI,
no sandbox harness needed to reproduce the failure mode).
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import jobs as _j  # noqa: E402
from reviewlib import process as _p  # noqa: E402


def test_log_dir_falls_back_when_parent_is_unwritable(tmp_path, monkeypatch, capsys):
    """log_dir()'s mkdir path: an unwritable PARENT means the standard location can
    never be created. Must fall back to `_fallback_log_dir()`, not raise."""
    unwritable_parent = tmp_path / "locked"
    unwritable_parent.mkdir()
    os.chmod(unwritable_parent, 0)
    try:
        monkeypatch.setenv("REVIEW_LOG_DIR", str(unwritable_parent / "review-cli"))
        result = _p.log_dir()
        assert result.is_dir()
        assert result.exists()
        # Fell back to somewhere OTHER than the unwritable target.
        assert unwritable_parent not in result.parents
        err = capsys.readouterr().err
        assert "falling back to a temp dir" in err
    finally:
        os.chmod(
            unwritable_parent, stat.S_IRWXU
        )  # tmp_path cleanup needs write access back


def test_log_dir_succeeds_normally_when_writable(tmp_path, monkeypatch):
    """Sanity: the happy path (no permission problem) is unaffected — still returns the
    requested `$REVIEW_LOG_DIR`, not the fallback."""
    target = tmp_path / "review-cli-logs"
    monkeypatch.setenv("REVIEW_LOG_DIR", str(target))
    result = _p.log_dir()
    assert result == target
    assert result.is_dir()


def test_open_log_with_fallback_falls_back_when_file_open_denied(tmp_path, monkeypatch):
    """The DIRECTORY-EXISTS-but-file-open-denied case (the one `log_dir()`'s own mkdir
    fallback can't catch, since no mkdir is attempted when the dir already exists):
    directory present and readable, but not writable, so the per-call log FILE open
    itself fails. `_open_log_with_fallback` must retry under a writable fallback dir
    with the SAME filename, and report the fallback path it actually used."""
    readonly_dir = tmp_path / "review-cli"
    readonly_dir.mkdir()
    os.chmod(readonly_dir, stat.S_IRUSR | stat.S_IXUSR)  # read+traverse, no write
    try:
        monkeypatch.setenv(
            "REVIEW_FALLBACK_LOG_TAG", "irrelevant"
        )  # no-op, documents no hidden knob
        target_path = readonly_dir / "20260101T000000_000000Z-fake-r0.log"
        fd, actual_path = _p._open_log_with_fallback(target_path)
        os.close(fd)
        assert actual_path != target_path
        assert actual_path.name == target_path.name
        assert actual_path.exists()
        assert actual_path.read_bytes() == b""
    finally:
        os.chmod(readonly_dir, stat.S_IRWXU)


def test_run_streamed_survives_an_unwritable_log_dir(monkeypatch):
    """End-to-end: `_run_streamed` (the exact call every backend seat makes) must still
    run the backend and return a real result when its log dir is entirely unwritable —
    not raise and kill the whole seat. This is the precise regression (a seat dying
    with a raw PermissionError before the backend subprocess was even spawned)."""
    with tempfile.TemporaryDirectory() as d:
        locked = Path(d) / "locked"
        locked.mkdir()
        os.chmod(locked, 0)
        try:
            monkeypatch.setenv("REVIEW_LOG_DIR", str(locked / "review-cli"))
            result = _p._run_streamed(
                [sys.executable, "-c", "print('hello from a real subprocess')"],
                cwd=REPO_ROOT,
                timeout=10,
                backend="test-fallback",
            )
            assert result.returncode == 0, result.stderr
            assert "hello from a real subprocess" in result.stdout
        finally:
            os.chmod(locked, stat.S_IRWXU)


def test_open_log_with_fallback_uses_devnull_when_fallback_dir_also_unwritable(
    monkeypatch,
):
    """Double-failure case (codex review, review-cli#162 follow-up): the standard log
    file open is denied AND `_fallback_log_dir()`'s own target is unwritable too. The
    ONE hard invariant is that this can never raise — it must degrade all the way to
    `os.devnull` rather than let an uncaught OSError kill the seat exactly like the
    original bug did, just one level deeper."""
    monkeypatch.setattr(
        _p,
        "_fallback_log_dir",
        lambda: (_ for _ in ()).throw(OSError("temp root revoked too")),
    )
    target = Path("/this/does/not/exist/review-cli/fake.log")
    fd, actual_path = _p._open_log_with_fallback(target)
    try:
        assert actual_path == Path(os.devnull)
        os.write(fd, b"still alive\n")
    finally:
        os.close(fd)


def test_fallback_log_dir_falls_back_to_mkdtemp_when_fixed_path_unwritable(
    tmp_path, monkeypatch
):
    """`_fallback_log_dir()`'s own two-tier fallback: if the FIXED uid-keyed path can't
    be created (a stale unwritable leftover at that exact spot), fall back to a
    brand-new `tempfile.mkdtemp()` directory rather than raising (codex review,
    review-cli#162 follow-up — the single-tier version still died here)."""
    import tempfile as _tempfile

    fake_temp_root = tmp_path / "fake-temp-root"
    fake_temp_root.mkdir()
    # Pre-create the fixed uid-keyed path as read-only so `mkdir(exist_ok=True)` on it
    # succeeds (it already exists) but nothing further can be created there — the same
    # "exists but not writable" shape a stale root-owned leftover would produce.
    stale = fake_temp_root / f"review-cli-logs-{os.getuid()}"
    stale.mkdir(mode=0o500)
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_temp_root))
    try:
        result = _p._fallback_log_dir()
        assert result != stale
        assert result.is_dir()
        probe = result / "probe.txt"
        probe.write_text("ok")
        assert probe.read_text() == "ok"
    finally:
        os.chmod(stale, 0o700)


def test_jobs_dir_falls_back_when_it_exists_but_is_not_writable(tmp_path, monkeypatch):
    """`jobs.jobs_dir()`'s bug (codex review, review-cli#162 follow-up): a directory
    that already EXISTS but is not writable passes `mkdir(..., exist_ok=True)` silently
    (that call only checks existence, never permission) — the standard-location branch
    was reported as a success even though nothing can actually be written under it, and
    the first real failure only surfaced later, deep inside `_job_lock`'s own
    `os.open`. `jobs_dir()` must probe with a real create+delete and fall back exactly
    like the mkdir-fails case does."""
    preexisting_but_readonly = tmp_path / "jobs"
    preexisting_but_readonly.mkdir()
    os.chmod(
        preexisting_but_readonly, stat.S_IRUSR | stat.S_IXUSR
    )  # read+traverse only
    try:
        monkeypatch.setenv("REVIEW_JOBS_DIR", str(preexisting_but_readonly))
        result = _j.jobs_dir()
        assert result != preexisting_but_readonly
        assert result.is_dir()
        probe = result / "probe.txt"
        probe.write_text("ok")
        assert probe.read_text() == "ok"
    finally:
        os.chmod(preexisting_but_readonly, stat.S_IRWXU)


def test_write_job_rejects_a_malformed_id_before_touching_the_filesystem(
    tmp_path, monkeypatch
):
    """codex review (review-cli#162 follow-up, PR #164): `write_job()` used to open the
    LOCK file (`_job_lock` -> `os.open`) before `job_path()` ever validated the job-id
    shape — so a path-shaped `job_id` (an absolute path or a `../` traversal) could
    create/lock an arbitrary file OUTSIDE `jobs_dir()` before `InvalidJobId` was ever
    raised. Validation now runs in `_job_lock_path` too (the same choke point
    `job_path` uses), so a malformed id must raise BEFORE any file appears anywhere —
    not just eventually, from a DIFFERENT function than the one that actually failed
    first."""
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setenv("REVIEW_JOBS_DIR", str(jobs_dir))
    outside_target = tmp_path / "outside.lock"
    malformed_id = f"../{outside_target.stem}"
    try:
        _j.write_job(malformed_id, status="running")
        raise AssertionError("expected InvalidJobId for a path-traversal job id")
    except _j.InvalidJobId:
        pass
    # Nothing was created anywhere — neither inside jobs_dir (which may not even exist
    # yet) nor at the traversal target outside it.
    assert not outside_target.exists()
    assert not jobs_dir.exists() or not any(jobs_dir.iterdir())


def test_jobs_dir_fallback_is_discoverable_from_a_fresh_process_state(
    tmp_path, monkeypatch
):
    """codex review (review-cli#162 follow-up, PR #164): once BOTH the standard jobs
    location AND the fixed uid-keyed temp path are unusable, `jobs_dir()`'s last
    resort mints a brand-new `tempfile.mkdtemp()` directory — which is random by
    design. Simulating a totally independent LATER process (no module-level cache,
    since that only helps repeat calls within the SAME process) must still find the
    SAME directory a prior process minted, via the fixed pointer file — not silently
    create a second, different one that a `review status`/`review jobs` call could
    never find the original job record in."""
    import tempfile as _tempfile

    fake_temp_root = tmp_path / "fake-temp-root"
    fake_temp_root.mkdir()
    unwritable_std = tmp_path / "std-jobs"
    unwritable_std.mkdir(mode=0o500)
    try:
        monkeypatch.setenv("REVIEW_JOBS_DIR", str(unwritable_std))
        monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_temp_root))
        # Also make the FIXED uid-keyed temp path unusable so this hits the mkdtemp tier.
        stale_fixed = fake_temp_root / f"review-cli-jobs-{os.getuid()}"
        stale_fixed.mkdir(mode=0o500)
        try:
            first = _j.jobs_dir()
            # Simulate a totally fresh process: clear the per-process memoization cache
            # (a fresh process would never have populated it) and call again.
            _j._jobs_dir_fallback_cache = None
            second = _j.jobs_dir()
            assert first == second
        finally:
            os.chmod(stale_fixed, 0o700)
    finally:
        os.chmod(unwritable_std, 0o700)
        _j._jobs_dir_fallback_cache = None


def test_read_job_finds_a_record_in_the_secondary_pointer_dir_after_sandbox_widens(
    tmp_path, monkeypatch
):
    """codex review (review-cli#162 follow-up, PR #164, round 2): a job's bookkeeping
    write and a LATER read of it can resolve `jobs_dir()` to two DIFFERENT
    directories even without any code bug — e.g. a `--detach` spawn hits the
    mkdtemp() fallback tier (records a pointer) while the standard cache location is
    still unwritable, but by the time `review status` is called the sandbox grant
    has WIDENED and the standard location is writable again, so `jobs_dir()` no
    longer even reaches the fallback tier and never looks at the pointer. Simulates
    exactly this: write a job record directly into a fallback dir (with a pointer),
    then call `read_job`/`list_jobs` against a jobs_dir() that resolves to the
    STANDARD (writable) location — the record must still be found.

    Deliberately does NOT use `$REVIEW_JOBS_DIR` (codex review round 3: secondary
    discovery is intentionally skipped entirely when that override is set — an
    explicit override means "use exactly this directory", and every test in this
    suite otherwise relies on it for isolation, so honoring it here would make the
    fix accidentally test itself out of existence for the one case it's supposed to
    help). Instead monkeypatches `Path.home()` so the STANDARD (no-override)
    resolution branch lands under an isolated fake home."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("REVIEW_JOBS_DIR", raising=False)
    fallback_dir = tmp_path / "fallback-jobs"
    fallback_dir.mkdir()

    monkeypatch.setattr(_j, "_read_fallback_pointer", lambda: fallback_dir)

    job_id = _j.new_job_id()
    (fallback_dir / f"{job_id}.json").write_text(
        '{"job_id": "%s", "status": "done", "pid": 1}' % job_id, encoding="utf-8"
    )

    rec = _j.read_job(job_id)
    assert rec is not None and rec["status"] == "done"
    all_jobs = _j.list_jobs()
    assert any(j["job_id"] == job_id for j in all_jobs)


def test_secondary_jobs_dir_skips_discovery_when_review_jobs_dir_is_set(
    tmp_path, monkeypatch
):
    """codex review (review-cli#162 follow-up, PR #164, round 3): an explicit
    `$REVIEW_JOBS_DIR` override must NOT be silently widened by also consulting the
    machine-wide pointer — that would leak unrelated job records (potentially
    carrying another run's prompt/diff in its `argv`) across a boundary the caller
    set up specifically to isolate. `_secondary_jobs_dir()` must return None whenever
    the override is present, even if a pointer to a genuinely different directory
    exists."""
    override_dir = tmp_path / "override-jobs"
    override_dir.mkdir()
    other_dir = tmp_path / "unrelated-other-jobs"
    other_dir.mkdir()
    monkeypatch.setenv("REVIEW_JOBS_DIR", str(override_dir))
    monkeypatch.setattr(_j, "_read_fallback_pointer", lambda: other_dir)
    assert _j._secondary_jobs_dir() is None


def test_open_pointer_lock_refuses_a_foreign_owned_lock_file(tmp_path, monkeypatch):
    """codex review (review-cli#162 follow-up, PR #164, round 3): the lock file lives
    at an equally predictable path as the pointer it protects. A foreign-owned but
    world-writable regular file pre-planted there would pass the `os.open` (its perm
    bits allow it), but `flock(LOCK_EX)` could then block INDEFINITELY on whatever
    that OTHER user's process is doing with the same file — turning a best-effort
    coordination mechanism into a hang. `_open_pointer_lock` must check ownership
    and raise BEFORE ever calling `flock`, not after."""
    import tempfile as _tempfile

    fake_temp_root = tmp_path / "fake-temp-root"
    fake_temp_root.mkdir()
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_temp_root))

    class _ForeignStat:
        st_uid = os.getuid() + 1  # never actually equals our own uid

    monkeypatch.setattr(os, "fstat", lambda fd: _ForeignStat())
    try:
        _j._open_pointer_lock()
        raise AssertionError("expected OSError for a foreign-owned lock file")
    except OSError:
        pass


def test_read_fallback_pointer_does_not_hang_on_a_preplanted_fifo(
    tmp_path, monkeypatch
):
    """codex review (review-cli#162 follow-up, PR #164, round 4): another local user
    could plant a FIFO (not a symlink, so `O_NOFOLLOW` alone doesn't stop it) at the
    pointer's predictable path. A blocking `O_RDONLY` open on a FIFO with no writer
    hangs forever — `O_NONBLOCK` must make the open return immediately, and the
    subsequent `stat.S_ISREG` check must then reject it as "not a regular file"
    rather than trying to read from it."""
    import tempfile as _tempfile

    fake_temp_root = tmp_path / "fake-temp-root"
    fake_temp_root.mkdir()
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_temp_root))
    os.mkfifo(_j._fallback_pointer_path())
    # No writer ever opens the other end — if this hangs, the test itself times out
    # (pytest's default has no global timeout, but a hang here would block the whole
    # suite; a passing run proves it returned promptly).
    assert _j._read_fallback_pointer() is None


def test_write_fallback_pointer_refuses_a_foreign_owned_regular_file(
    tmp_path, monkeypatch
):
    """codex review (review-cli#162 follow-up, PR #164, round 4): a foreign-owned but
    world-writable regular file could already sit at the pointer's predictable path.
    `_write_fallback_pointer` must NOT truncate/overwrite it — that would mean this
    process modifying a file it doesn't own just because loose permissions allowed
    the write. Simulated via a monkeypatched `Path.lstat` reporting a foreign owner
    for the pointer path specifically."""
    import tempfile as _tempfile

    fake_temp_root = tmp_path / "fake-temp-root"
    fake_temp_root.mkdir()
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_temp_root))
    pointer_path = _j._fallback_pointer_path()
    pointer_path.write_text("original content owned by someone else")

    real_lstat = Path.lstat

    class _ForeignStat:
        st_uid = os.getuid() + 1
        st_mode = 0o100644  # a regular file, just foreign-owned

    def _fake_lstat(self, *a, **k):
        if self == pointer_path:
            return _ForeignStat()
        return real_lstat(self, *a, **k)

    monkeypatch.setattr(Path, "lstat", _fake_lstat)
    _j._write_fallback_pointer(tmp_path / "some-new-fallback-dir")
    assert pointer_path.read_text() == "original content owned by someone else"


def test_write_fallback_pointer_is_atomic_no_empty_read_window(tmp_path, monkeypatch):
    """codex review (review-cli#162 follow-up, PR #164, round 4): the pointer is
    published via a same-directory temp file + `os.replace`, not a truncate-in-place
    write — so a reader can never observe a transient EMPTY pointer file mid-write.
    Proven here by asserting no `.tmp*` sibling survives and the final content is
    exactly what was written in one shot (a truncate-in-place version would still
    pass this specific assertion, but this at least pins the visible contract; the
    atomicity itself is inherent to `os.replace` being a single syscall)."""
    import tempfile as _tempfile

    fake_temp_root = tmp_path / "fake-temp-root"
    fake_temp_root.mkdir()
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_temp_root))
    target = tmp_path / "the-fallback-dir"
    _j._write_fallback_pointer(target)
    pointer_path = _j._fallback_pointer_path()
    assert pointer_path.read_text() == str(target)
    leftovers = list(fake_temp_root.glob("*.tmp*"))
    assert leftovers == []


def test_fallback_pointer_rejects_a_symlinked_pointer_file(tmp_path, monkeypatch):
    """codex review (review-cli#162 follow-up, PR #164): the pointer file lives at a
    PREDICTABLE path under the shared system temp root. Another local user could
    pre-plant a symlink there pointing at a directory THEY control, hoping
    `_read_fallback_pointer` naively trusts it and redirects this process's job
    bookkeeping (secret-bearing argv) somewhere the attacker can read. `O_NOFOLLOW`
    must make the read fail closed (treated as "nothing recorded yet"), never follow
    the symlink.

    Monkeypatches `tempfile.gettempdir()` to an isolated `tmp_path` (codex review,
    round 2) — a prior version of this test deleted and replaced the REAL, LIVE
    pointer file under the actual shared system temp dir, which on this
    multi-agent machine could erase discovery metadata for a genuinely running
    `--detach` job while this test suite happened to execute."""
    import tempfile as _tempfile

    fake_temp_root = tmp_path / "fake-temp-root"
    fake_temp_root.mkdir()
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_temp_root))
    attacker_dir = tmp_path / "attacker-controlled"
    attacker_dir.mkdir()
    pointer_path = _j._fallback_pointer_path()
    pointer_path.symlink_to(attacker_dir / "fake-pointer.txt")
    assert _j._read_fallback_pointer() is None


def test_fallback_pointer_write_refuses_to_follow_a_preexisting_symlink(
    tmp_path, monkeypatch
):
    """codex review (review-cli#162 follow-up, PR #164): if a symlink already sits at
    the pointer's predictable path, `_write_fallback_pointer` must NOT follow it and
    truncate whatever it points at (the write side of the same symlink-attack
    surface `_read_fallback_pointer`'s `O_NOFOLLOW` guards on the read side) — it
    must fail closed (best-effort, no exception) and leave the symlink's target
    untouched.

    Same isolation fix as the sibling test above — never touch the real, live
    pointer file under the actual shared system temp dir."""
    import tempfile as _tempfile

    fake_temp_root = tmp_path / "fake-temp-root"
    fake_temp_root.mkdir()
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_temp_root))
    victim = tmp_path / "victim-file.txt"
    victim.write_text("do not touch me")
    pointer_path = _j._fallback_pointer_path()
    pointer_path.symlink_to(victim)
    _j._write_fallback_pointer(tmp_path / "some-fallback-dir")
    assert victim.read_text() == "do not touch me"


if __name__ == "__main__":
    failures = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:

                class _Env:
                    """Standalone monkeypatch shim: `setenv`/`setattr` (existing) plus
                    `delenv`, the third capability a later fallback test needs
                    (clearing `$REVIEW_JOBS_DIR` so the no-override code path is
                    exercised) — codex review flagged the shim as missing this too,
                    which raised AttributeError under this standalone runner (used by
                    `tests/smoke.py`) even though pytest's real monkeypatch supports
                    it."""

                    def __init__(self):
                        self._saved_env: dict[str, str | None] = {}
                        self._saved_attrs: list[tuple[object, str, object, bool]] = []

                    def setenv(self, k, v):
                        self._saved_env.setdefault(k, os.environ.get(k))
                        os.environ[k] = v

                    def delenv(self, k, raising=True):
                        self._saved_env.setdefault(k, os.environ.get(k))
                        had = k in os.environ
                        os.environ.pop(k, None)
                        if raising and not had:
                            raise KeyError(k)

                    def setattr(self, obj, name, value):
                        had = hasattr(obj, name)
                        old_value = getattr(obj, name, None)
                        self._saved_attrs.append((obj, name, old_value, had))
                        setattr(obj, name, value)

                    def restore(self):
                        for k, v in self._saved_env.items():
                            if v is None:
                                os.environ.pop(k, None)
                            else:
                                os.environ[k] = v
                        for obj, name, old_value, had in reversed(self._saved_attrs):
                            if had:
                                setattr(obj, name, old_value)
                            else:
                                delattr(obj, name)

                mp = _Env()
                try:
                    params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
                    kwargs = {}
                    if "tmp_path" in params:
                        kwargs["tmp_path"] = Path(d)
                    if "monkeypatch" in params:
                        kwargs["monkeypatch"] = mp
                    if "capsys" in params:
                        kwargs["capsys"] = (
                            None  # capsys-dependent test needs pytest; skip standalone
                        )
                        if "capsys" in params:
                            print(f"SKIP {name} (needs pytest capsys)")
                            continue
                    fn(**kwargs)
                    print(f"PASS {name}")
                except Exception as exc:  # noqa: BLE001
                    print(f"FAIL {name}: {exc}")
                    failures.append(name)
                finally:
                    mp.restore()
    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        raise SystemExit(1)
    print("\nAll tests passed.")
