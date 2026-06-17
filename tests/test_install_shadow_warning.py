#!/usr/bin/env python3
"""install.sh shadow-warning: only advise UNINSTALL for a genuinely-broken shadow.

WHY this exists: install.sh, after symlinking the working `review` shim, probes
`command -v review`. If something EARLIER on PATH wins (a "shadow"), it warns. The
diagnosis must NOT conflate two very different shadows:

  - a genuinely-broken stale pip/uv console-script (its interpreter raises
    ModuleNotFoundError: No module named 'reviewlib') -> tell the user to UNINSTALL it;
  - a HEALTHY install (e.g. `pipx install review-cli`, a documented method) that just
    happens to sit earlier on PATH and imports reviewlib FINE -> only EXPLAIN that it
    shadows the new symlink; advising `pip uninstall -y review-cli` here would tell the
    user to remove a perfectly valid install.

Both shadows are a regular file with a `#!`-shebang that mentions `reviewlib`, so the
file's SHAPE cannot tell them apart — only running its interpreter's `import reviewlib`
probe can. This is the regression guard: the uninstall advice is gated on that probe
actually FAILING.

It drives the REAL install.sh end-to-end in a fully isolated sandbox: a temp HOME (so
the symlink + install-skill never touch the real ~/.local/bin or ~/.agents), a PATH with
a fixture `review` placed EARLIER than the install's bin dir, and the fixture pointed at
an interpreter chosen to make the probe go the way the case needs. Same
plain-test_*-with-__main__ harness as tests/test_shim_bootstrap.py.

install.sh's deciding probe is `<interp> -I -c 'import reviewlib'` — ISOLATED mode, which
ignores PYTHONPATH and drops the cwd/script dir from sys.path. So "healthy vs broken" turns
ONLY on the shadow interpreter's own site-packages, never on where install.sh runs or what
PYTHONPATH is set. We exploit that to make the test both realistic and a tight guard:
  - every case runs install.sh FROM THE REPO ROOT with PYTHONPATH=<repo> — the documented
    fresh-install flow, AND the exact setup that would mask a missing `-I` (a from-source
    ./reviewlib/ reachable via cwd would make a broken shadow probe succeed);
  - the HEALTHY fixture is a venv whose site-packages exposes reviewlib via a `.pth` file
    pointing at the repo (how `pip install -e` exposes a package, but OFFLINE), so the
    isolated probe genuinely succeeds — mirrors `pipx install review-cli`;
  - the BROKEN fixture is a clean venv with NO reviewlib, so the isolated probe genuinely
    fails — like a stale console-script whose editable target was deleted.
If a future edit dropped `-I`, the broken case would import the repo's reviewlib off cwd
and be mislabeled healthy — and this test would fail. That is the point.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

# Mirror a pip/uv console-script: a regular file (NOT a symlink) whose first line is an
# absolute python shebang and whose body does the bare `from reviewlib.cli import main`
# with no sys.path bootstrap. This is BOTH the healthy and the broken shape; whether it is
# "stale" depends solely on whether the shebang interpreter can import reviewlib.
_CONSOLE_SCRIPT_TMPL = """\
#!{interp}
import sys
from reviewlib.cli import main
sys.exit(main())
"""


def _venv_python(venv_dir: Path) -> Path:
    py = venv_dir / "bin" / "python"
    if not py.exists():  # Windows layout, defensive — CI/dev here are POSIX.
        py = venv_dir / "Scripts" / "python.exe"
    return py


def _interp_that_imports_reviewlib(sandbox: Path):
    """An interpreter whose `import reviewlib` SUCCEEDS from its OWN site-packages.

    The deciding probe runs `<interp> -I -c 'import reviewlib'` — ISOLATED mode, so it
    ignores PYTHONPATH and the cwd. The healthy fixture must therefore mirror a REAL install
    (e.g. `pipx install review-cli`): reviewlib reachable from the venv's site-packages, NOT
    merely via PYTHONPATH/cwd (the crutch -I defeats). We do that with a `.pth` file in
    site-packages pointing at the repo — exactly how `pip install -e` exposes a package, but
    OFFLINE and instant. We deliberately do NOT `pip install -e`: `python -m venv` ships no
    setuptools/wheel, so a PEP-517 editable build would hit PyPI for the build backend and
    break this test's offline guarantee. `.pth` entries ARE processed under `-I` (site init
    still runs), so the isolated probe genuinely succeeds. PRE-FLIGHT-assert it does, so a
    broken setup fails loudly HERE, not as a confusing downstream mislabel.
    """
    venv_dir = sandbox / "reviewlib-venv"
    venv.create(str(venv_dir), with_pip=False)
    py = _venv_python(venv_dir)
    site = subprocess.run(
        [str(py), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        capture_output=True,
        text=True,
    )
    assert site.returncode == 0, f"could not locate venv site-packages\n{site.stderr!r}"
    (Path(site.stdout.strip()) / "reviewlib_devpath.pth").write_text(str(REPO_ROOT) + "\n")
    probe = subprocess.run(
        [str(py), "-I", "-c", "import reviewlib"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, (
        "healthy-case venv cannot `import reviewlib` in isolated mode after the .pth was "
        f"written; the healthy-shadow case can't be exercised.\nstderr={probe.stderr!r}"
    )
    return str(py)


def _interp_without_reviewlib(sandbox: Path):
    """A fresh venv python that CANNOT import reviewlib, for the broken case.

    A venv without --system-site-packages and without reviewlib installed raises
    ModuleNotFoundError under the isolated `-I` probe — exactly like a stale console-script
    whose editable target was deleted. Returns the absolute path to that venv's python.

    We assert the brokenness is caused SOLELY by isolation, so the "-I regression guard" is
    not a tautology: the same interpreter WOULD import reviewlib if it saw PYTHONPATH=<repo>
    (proving it's a real Python that could load the repo), but FAILS under `-I` (which install
    .sh uses). If a future change created this venv with --system-site-packages, or the host
    leaked reviewlib in some other way, these asserts would fire instead of silently weakening
    the guard to "a venv that could never import reviewlib anyway".
    """
    venv_dir = sandbox / "noreviewlib-venv"
    venv.create(str(venv_dir), with_pip=False)
    py = str(_venv_python(venv_dir))
    with_pp = subprocess.run(
        [py, "-c", "import reviewlib"],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert with_pp.returncode == 0, (
        "broken-case venv can't import reviewlib EVEN with PYTHONPATH=<repo>; the -I guard "
        f"would be a tautology (it'd fail for the wrong reason).\nstderr={with_pp.stderr!r}"
    )
    isolated = subprocess.run(
        [py, "-I", "-c", "import reviewlib"],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert isolated.returncode != 0, (
        "broken-case venv imports reviewlib UNDER -I — isolation isn't isolating (e.g. venv "
        "made with --system-site-packages, or reviewlib in its own site-packages); the broken "
        "case wouldn't actually be broken."
    )
    return py


# A shebang interpreter that install.sh can never resolve to a runnable Python: an
# absolute path that does not exist. The token-walk yields it, command -v can't find it,
# and the `-f -x` check fails, so interp_bin ends up empty — the conservative "can't prove
# breakage" fallthrough. (A made-up absolute path, not a real binary, so the test never
# depends on coreutils layout differences across OSes.)
_UNRESOLVABLE_INTERP = "/nonexistent/python-for-shadow-test"

def _non_python_interp() -> str:
    """A REAL, runnable binary that is NOT Python, for the `notpython` fallthrough.

    `sh` exists and is executable everywhere this runs, so the `-f -x` check passes, but
    `<sh> -I -c 'import sys'` fails the python-ness probe, so install.sh clears interp_bin —
    the SAME conservative fallthrough, reached via a different branch (resolved-but-not-
    Python rather than never-resolved). Resolved via PATH rather than hardcoding `/bin/sh`,
    which is empty on some minimal layouts (e.g. NixOS, stripped containers).
    """
    sh = shutil.which("sh") or shutil.which("bash")
    assert sh, "no POSIX shell on PATH; cannot build the non-Python-interpreter fixture"
    return sh


def _run_install(*, case: str):
    """Run install.sh once with a fixture `review` shadowing it on PATH.

    case:
      "healthy"      -> shebang interpreter imports reviewlib (valid shadow, e.g. pipx);
      "broken"       -> shebang interpreter cannot import reviewlib (stale console-script);
      "unresolvable" -> shebang interpreter can't be resolved to a runnable file at all;
      "notpython"    -> shebang resolves to a real non-Python binary (fails the python-ness
                        probe). Both of the latter mean install.sh CANNOT prove breakage, so
                        it must take the conservative fallthrough (no uninstall advice).
      "symlink"      -> the shadow is a SYMLINK, so the candidate `-f && ! -L` guard fails and
                        the diagnosis is skipped entirely -> generic message only;
      "noreviewlib"  -> a regular `#!`-shebang file that does NOT mention `reviewlib`, so the
                        candidate `grep -q reviewlib` guard fails -> generic message only.
                        These two lock in that the consolidated `else` arm still routes non-
                        candidate shadows to the plain generic remediation, never the stale
                        block nor the "imports reviewlib fine" line.

    Returns the combined stdout+stderr of the installer.
    """
    assert case in (
        "healthy",
        "broken",
        "unresolvable",
        "notpython",
        "symlink",
        "noreviewlib",
    ), case
    sandbox_str = tempfile.mkdtemp(prefix="rev-install-shadow-")
    sandbox = Path(sandbox_str)
    try:
        home = sandbox / "home"
        shadow_dir = sandbox / "earlier-bin"
        for d in (home, shadow_dir):
            d.mkdir(parents=True)

        shadow = shadow_dir / "review"
        if case == "symlink":
            # A symlink shadow: install.sh's candidate guard requires a regular file
            # (`-f && ! -L`), so a symlink never enters the diagnosis at all. Point it at a
            # real executable so `command -v` still resolves it as a runnable `review`.
            target = shadow_dir / "review-real"
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
            shadow.symlink_to(target.name)
        elif case == "noreviewlib":
            # A regular `#!`-shebang executable that does NOT mention reviewlib — the candidate
            # `grep -q reviewlib` guard fails, so it's never diagnosed as a console-script.
            shadow.write_text("#!/bin/sh\necho hi\n")
            shadow.chmod(0o755)
        else:
            if case == "healthy":
                interp = _interp_that_imports_reviewlib(sandbox)
            elif case == "broken":
                interp = _interp_without_reviewlib(sandbox)
            elif case == "notpython":
                interp = _non_python_interp()
            else:
                interp = _UNRESOLVABLE_INTERP
            shadow.write_text(_CONSOLE_SCRIPT_TMPL.format(interp=interp))
            shadow.chmod(0o755)

        env = dict(os.environ)
        # PREPEND the synthetic entries to the INHERITED PATH; never replace it. install.sh
        # shells out to realpath/head/grep/command etc., which on macOS-with-homebrew or
        # distros using /usr/local/bin live outside a hardcoded short list — clobbering PATH
        # would fail the install for reasons unrelated to the shadow logic under test.
        #   1) shadow_dir FIRST so `command -v review` resolves to our fixture (not the
        #      symlink install.sh creates under $HOME/.local/bin);
        #   2) the dir of the interpreter running this test next, so install.sh's own
        #      `python3` is this venv/conda/system Python (no layout-specific .venv guess).
        py_bin = str(Path(sys.executable).parent)
        env["PATH"] = os.pathsep.join(
            [str(shadow_dir), py_bin, env.get("PATH", "")]
        ).rstrip(os.pathsep)
        env["HOME"] = str(home)
        # Run install.sh FROM THE REPO ROOT with PYTHONPATH SET — the documented fresh-
        # install flow (`cd review-cli && ./install.sh`), and the exact configuration that
        # used to mask the bug: a from-source `./reviewlib/` is reachable via cwd/PYTHONPATH.
        # The deciding probe uses `-I` (isolated), so neither leaks into it; whether a shadow
        # is "healthy" depends ONLY on its own interpreter's site-packages. Running every
        # case from here is the regression guard for that — a probe that lost `-I` would
        # mislabel the broken case healthy and this test would catch it.
        env["PYTHONPATH"] = str(REPO_ROOT)

        proc = subprocess.run(
            ["bash", str(INSTALL_SH)],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(sandbox_str, ignore_errors=True)

    out = proc.stdout + proc.stderr
    # install.sh is `set -e`; a non-zero exit means the installer itself broke, not a
    # shadow-diagnosis difference — fail loudly with the full output so it's debuggable.
    assert proc.returncode == 0, f"install.sh exited {proc.returncode}\n{out}"
    # Sanity: it MUST have detected the shadow at all, else the test proves nothing.
    assert "SHADOWED" in out, f"installer did not detect the planted shadow\n{out}"
    return out


def test_healthy_shadow_is_explained_not_uninstalled():
    out = _run_install(case="healthy")
    # It must say the shadow is fine and recommend a PATH/rename fix...
    assert "imports reviewlib fine" in out, out
    assert "NOT broken" in out, out
    # ...and must NOT advise uninstalling a valid install, nor invoke the stale-script
    # diagnosis. Match the SPECIFIC remediation phrases, not a bare "stale" substring (a
    # future generic note mentioning the word would false-trip that).
    assert "uninstall -y review-cli" not in out, (
        "healthy pipx/pip shadow was told to UNINSTALL — the exact bug this guards\n" + out
    )
    assert "stale pip/uv console-script" not in out, out
    # The `rm '<path>'` removal line lives ONLY in the stale block; a valid install must
    # never be handed a remove-the-file command either.
    assert "rm '" not in out, out


def test_broken_stale_console_script_gets_uninstall_remediation():
    out = _run_install(case="broken")
    # A genuinely-broken stale console-script: name it stale, confirm the failed probe,
    # and give the copy-paste uninstall + rm remediation.
    assert "stale pip/uv console-script" in out, out
    assert "cannot `import reviewlib`" in out, out
    assert "uninstall -y review-cli" in out, out
    # It must NOT misclassify the broken script as a healthy shadow.
    assert "imports reviewlib fine" not in out, out


def _assert_conservative_fallthrough(out: str):
    # Breakage is UNPROVABLE (the probe never ran or rejected a non-Python interpreter), so
    # install.sh must NOT call it stale nor advise uninstall — advising removal on a guess is
    # the exact harm. It must NOT claim the shadow imports reviewlib fine either (no probe ran
    # successfully). It DOES still emit the generic PATH-ordering remediation.
    assert "uninstall -y review-cli" not in out, (
        "unprovable shadow was told to UNINSTALL on a guess — the conservative path failed\n"
        + out
    )
    assert "stale pip/uv console-script" not in out, out
    assert "imports reviewlib fine" not in out, out
    assert "rm '" not in out, out  # the stale-only file-removal line must not leak here
    assert "precedes the dir holding" in out, out


def test_unresolvable_interpreter_does_not_advise_uninstall():
    # Conservative fallthrough A: a candidate shadow whose shebang Python install.sh can't
    # resolve to a runnable file at all (interp_bin never set).
    _assert_conservative_fallthrough(_run_install(case="unresolvable"))


def test_non_python_interpreter_does_not_advise_uninstall():
    # Conservative fallthrough B: a candidate shadow whose shebang resolves to a REAL but
    # non-Python binary (/bin/sh). It passes `-f -x`, then fails the `-I -c 'import sys'`
    # python-ness probe, so install.sh clears interp_bin and declines the uninstall advice —
    # exactly so a mis-parsed shebang (e.g. coreutils `dir`) can't yield a garbage `dir -m
    # pip uninstall` line.
    _assert_conservative_fallthrough(_run_install(case="notpython"))


def test_symlink_shadow_gets_generic_message():
    # A symlink shadow is NOT a candidate (the `-f && ! -L` guard rejects it), so it must
    # route straight to the generic remediation — never the stale block, never the
    # "imports reviewlib fine" line. Same observable shape as the conservative fallthrough.
    _assert_conservative_fallthrough(_run_install(case="symlink"))


def test_regular_file_without_reviewlib_gets_generic_message():
    # A regular `#!`-shebang file that does not mention reviewlib is NOT a candidate (the
    # `grep -q reviewlib` guard rejects it), so it too gets the plain generic remediation,
    # never the stale block nor a healthy-shadow claim.
    _assert_conservative_fallthrough(_run_install(case="noreviewlib"))


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
