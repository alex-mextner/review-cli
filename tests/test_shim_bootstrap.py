#!/usr/bin/env python3
"""Smoke: the installed `bin/review` shim bootstraps its own sys.path.

WHY this exists: `review` is installed as a SYMLINK (install.sh -> ~/.local/bin/review
-> <repo>/bin/review). The only thing that makes `import reviewlib` resolve from an
ARBITRARY cwd is the shim itself — it realpath-resolves the symlink back to the repo
root and inserts it on sys.path. A regression here (or a stray pip/uv console-script
shadowing the symlink with a bare `from reviewlib.cli import main` and no bootstrap)
makes `review` die with ModuleNotFoundError: No module named 'reviewlib' in EVERY
shell. That actually happened: a deleted `/private/tmp/...` editable install left a
broken `/opt/homebrew/bin/review` ahead of the good symlink on PATH.

So this test invokes the REPO's `bin/review` (NOT whatever `review` is on PATH) as a
subprocess from a tmp dir OUTSIDE the repo, with PYTHONPATH CLEARED, and asserts a
cheap exit-0 command (`--list-defaults`) works — proving the shim found reviewlib on
its own, without cwd/PYTHONPATH help. Same plain-test_*-with-__main__ harness as
tests/test_cwd.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "bin" / "review"


def _run_from_clean_env(args, cwd, shim=SHIM):
    """Run `shim` (default: the repo shim) with a CLEARED PYTHONPATH from `cwd`.

    PYTHONPATH is removed entirely so a pass can only mean the shim bootstrapped
    sys.path itself. cwd is a tmp dir outside the repo so the repo root is never on
    sys.path by accident.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(shim), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def test_shim_is_a_file_and_executable():
    assert SHIM.is_file(), f"missing repo shim: {SHIM}"
    assert os.access(SHIM, os.X_OK), f"repo shim is not executable: {SHIM}"


def test_shim_imports_reviewlib_from_outside_repo_without_pythonpath():
    # A tmp dir OUTSIDE the repo, PYTHONPATH cleared: the ONLY way `import reviewlib`
    # can succeed is the shim's own realpath-based sys.path bootstrap.
    with tempfile.TemporaryDirectory() as d:
        proc = _run_from_clean_env(["--list-defaults"], cwd=d)
    assert proc.returncode == 0, (
        f"shim failed to run from {d!r} with cleared PYTHONPATH\n"
        f"rc={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    # --list-defaults prints the default backends; `codex` is always present. Its
    # presence proves cli.py ran, which means `import reviewlib` succeeded.
    assert "codex" in proc.stdout, proc.stdout
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert "No module named 'reviewlib'" not in proc.stderr, proc.stderr


def test_shim_imports_reviewlib_when_invoked_VIA_A_SYMLINK():
    # The PRODUCTION topology: `$BIN/review` is a SYMLINK into the repo (install.sh does
    # `ln -sfn <repo>/bin/review ~/.local/bin/review`), so the user runs the symlink, not
    # the repo file directly. CPython puts the SYMLINK's dir on sys.path[0] (it does NOT
    # resolve the link for sys.path), so the repo root is NOT on sys.path via startup — only
    # the shim's own os.path.realpath(__file__) bootstrap can make `import reviewlib` work.
    # The other tests invoke the repo file directly and so don't cover this link-resolution
    # path; THIS is the exact regression that broke `review` in the wild.
    with tempfile.TemporaryDirectory() as d:
        link = Path(d) / "review"
        link.symlink_to(SHIM)
        # cwd is a DIFFERENT tmp dir so neither the link's dir nor the cwd is the repo root.
        with tempfile.TemporaryDirectory() as cwd:
            proc = _run_from_clean_env(["--list-defaults"], cwd=cwd, shim=link)
    assert proc.returncode == 0, (
        f"shim invoked via a symlink failed to resolve reviewlib\n"
        f"rc={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "codex" in proc.stdout, proc.stdout
    assert "No module named 'reviewlib'" not in proc.stderr, proc.stderr


def test_shim_does_not_inject_cwd_into_sys_path():
    # Defensive: the shim must NOT add the cwd to sys.path. A stray `reviewlib/` in the
    # cwd (this tool reviews foreign repos!) must not shadow the real package. We plant a
    # decoy `reviewlib/__init__.py` in the cwd that would raise on import if loaded, then
    # confirm the shim still runs fine (it loaded the REAL package from the repo root).
    #
    # Scope note: `python3 /abs/path/bin/review` puts the SCRIPT's dir on sys.path[0], never
    # the cwd, so CPython startup alone cannot leak the cwd here. This guard therefore only
    # catches a regression where the SHIM ITSELF inserts os.getcwd()/""/"." into sys.path —
    # which is exactly the failure mode we care about; it is not a broader cwd-isolation test.
    with tempfile.TemporaryDirectory() as d:
        decoy = Path(d) / "reviewlib"
        decoy.mkdir()
        (decoy / "__init__.py").write_text(
            "raise RuntimeError('decoy reviewlib in cwd was imported — shim leaked cwd onto sys.path')\n"
        )
        proc = _run_from_clean_env(["--list-defaults"], cwd=d)
    assert proc.returncode == 0, (
        f"shim picked up the decoy reviewlib from cwd (or otherwise failed)\n"
        f"rc={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "decoy reviewlib in cwd was imported" not in proc.stderr, proc.stderr
    assert "codex" in proc.stdout, proc.stdout


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
