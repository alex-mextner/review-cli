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


if __name__ == "__main__":
    failures = []
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:

                class _Env:
                    """Standalone monkeypatch shim: `setenv` (existing) plus `setattr`,
                    the second capability the new fallback tests need (patching
                    `_fallback_log_dir`/`tempfile.gettempdir` to force the double-
                    failure branches) — codex review flagged the shim as
                    setenv-only, which raised AttributeError under this standalone
                    runner (used by `tests/smoke.py`) even though pytest's real
                    monkeypatch supports both."""

                    def __init__(self):
                        self._saved_env: dict[str, str | None] = {}
                        self._saved_attrs: list[tuple[object, str, object, bool]] = []

                    def setenv(self, k, v):
                        self._saved_env.setdefault(k, os.environ.get(k))
                        os.environ[k] = v

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
