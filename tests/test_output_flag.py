"""Unit tests for `-o FILE` / `--output FILE` (cli.py).

`-o` exists because agents run `review … > FILE`, which dies silently under zsh
`noclobber` when FILE already exists. review-cli is Python, so `-o` writes the
result via `open(...,"w")` — bypassing the shell redirect (and thus noclobber)
entirely. These tests pin the contract:
  * the result is teed to BOTH stdout and the file,
  * an existing file is OVERWRITTEN (the whole point — noclobber must not block),
  * parent dirs are created,
  * a bad path errors clearly (non-zero exit, actionable message),
  * argv parsing handles `-o FILE`, `--output FILE`, `--output=FILE`, `-oFILE`.

Same harness style as tests/test_cwd.py: plain test_* functions, __main__ runner.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib.cli import _extract_output_path, main  # noqa: E402


class _SkipTest(Exception):
    """A host prerequisite is absent for a standalone test run."""


_GIT_UNAVAILABLE_REASON: str | None | bool = None


def _git_unavailable_reason() -> str | None:
    global _GIT_UNAVAILABLE_REASON
    if _GIT_UNAVAILABLE_REASON is not None:
        return _GIT_UNAVAILABLE_REASON if isinstance(_GIT_UNAVAILABLE_REASON, str) else None
    try:
        version = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=15)
        if version.returncode != 0:
            reason = (version.stdout + version.stderr).strip() or f"`git --version` exited {version.returncode}"
            _GIT_UNAVAILABLE_REASON = reason
            return reason
        with tempfile.TemporaryDirectory() as d:
            init = subprocess.run(["git", "init", "-q"], cwd=d, capture_output=True, text=True, timeout=30)
        if init.returncode != 0:
            reason = (init.stdout + init.stderr).strip() or f"`git init` exited {init.returncode}"
            _GIT_UNAVAILABLE_REASON = reason
            return reason
    except (OSError, subprocess.SubprocessError) as exc:
        _GIT_UNAVAILABLE_REASON = str(exc)
        return str(exc)
    _GIT_UNAVAILABLE_REASON = False
    return None


def _require_git() -> None:
    reason = _git_unavailable_reason()
    if not reason:
        return
    msg = f"git-dependent output-file test skipped: {reason}"
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest  # noqa: PLC0415

        pytest.skip(msg)
    raise _SkipTest(msg)


def _run_main(argv: list[str]) -> tuple[int, str]:
    """Run cli.main with stdout captured; return (exit_code, captured_stdout).

    main() tees stdout itself when `-o` is present, so we capture the OUTER stdout
    here to assert the tee still prints live."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(argv)
    return rc, buf.getvalue()


# --- argv extraction -----------------------------------------------------------

def test_extract_space_form():
    out, rest = _extract_output_path(["-o", "/tmp/x.md", "--list-defaults"])
    assert out == Path("/tmp/x.md"), out
    assert rest == ["--list-defaults"], rest


def test_extract_long_form():
    out, rest = _extract_output_path(["--output", "/tmp/y.md", "--staged"])
    assert out == Path("/tmp/y.md"), out
    assert rest == ["--staged"], rest


def test_extract_equals_form():
    out, rest = _extract_output_path(["--output=/tmp/z.md", "--show-board"])
    assert out == Path("/tmp/z.md"), out
    assert rest == ["--show-board"], rest


def test_extract_glued_short_form():
    out, rest = _extract_output_path(["-o/tmp/g.md", "--show-board"])
    assert out == Path("/tmp/g.md"), out
    assert rest == ["--show-board"], rest


def test_extract_short_equals_form():
    # `-o=FILE` must be accepted (symmetry with `--output=FILE`), not silently dropped.
    out, rest = _extract_output_path(["-o=/tmp/e.md", "--show-board"])
    assert out == Path("/tmp/e.md"), out
    assert rest == ["--show-board"], rest


def test_extract_stops_at_double_dash():
    # `--` ends options: an `-o` AFTER it is a positional, not the output flag, and is
    # kept verbatim (a brainstorm topic / question can legitimately start with `-o`).
    out, rest = _extract_output_path(["-o", "/tmp/real.md", "--", "-o", "not-a-flag"])
    assert out == Path("/tmp/real.md"), out
    assert rest == ["--", "-o", "not-a-flag"], rest


def test_extract_double_dash_with_no_output_flag():
    out, rest = _extract_output_path(["--prompt", "--", "-o-topic"])
    assert out is None, out
    assert rest == ["--prompt", "--", "-o-topic"], rest


def test_extract_does_not_steal_value_of_value_taking_flag():
    # `review review --prompt --output` — here `--output` is the PROMPT value (a
    # value-taking option's argument), NOT the output flag. The pre-scan must NOT
    # intercept it, or argparse would then error "--prompt: expected one argument".
    # (The mode flags moved to subcommands, so --prompt is the canonical value-taker now.)
    out, rest = _extract_output_path(["--prompt", "--output", "FILE.md"])
    assert out is None, out
    assert rest == ["--prompt", "--output", "FILE.md"], rest
    # Same for --moderator consuming a value that looks like the glued short form.
    out, rest = _extract_output_path(["--moderator", "-otext"])
    assert out is None, out
    assert rest == ["--moderator", "-otext"], rest
    # But a REAL -o after the value-taking option's value still works.
    out, rest = _extract_output_path(["--prompt", "Q", "-o", "real.md"])
    assert out == Path("real.md"), out
    assert rest == ["--prompt", "Q"], rest


def test_extract_does_not_steal_retry_value():
    # `--retry` is a diff-mode int option (reviewlib/modes/review.py) listed in
    # _VALUE_TAKING_OPTS. The pre-scan must skip its argument, so an `-o`-shaped retry
    # value is NOT mis-read as the output flag — and a real `-o` that follows still wins.
    # This guards the exact regression the _VALUE_TAKING_OPTS entry prevents: a future
    # refactor dropping `--retry` from the set would silently fail this case.
    # `-o…`-shaped value after --retry is its argument, not the output flag.
    out, rest = _extract_output_path(["--retry", "-osmth", "-o", "out.md"])
    assert out == Path("out.md"), out
    assert rest == ["--retry", "-osmth"], rest
    # A legitimate `--retry N -o FILE` still resolves the output to FILE.
    out, rest = _extract_output_path(["--retry", "3", "-o", "out.md"])
    assert out == Path("out.md"), out
    assert rest == ["--retry", "3"], rest


def test_extract_does_not_steal_task_value():
    # `--task CODE` is global and CODE is intentionally one opaque token. The pre-scan
    # must not steal a valid task code just because it happens to look like --output/-o.
    out, rest = _extract_output_path(["diff", "--task", "--output", "-o", "out.md"])
    assert out == Path("out.md"), out
    assert rest == ["diff", "--task", "--output"], rest


def test_extract_absent_returns_none_and_unchanged():
    out, rest = _extract_output_path(["--show-board", "-m", "codex"])
    assert out is None, out
    assert rest == ["--show-board", "-m", "codex"], rest


def test_extract_bare_flag_left_for_argparse():
    # A `-o` with no following value is LEFT in argv so argparse reports the usage
    # error instead of being silently swallowed.
    out, rest = _extract_output_path(["-o"])
    assert out is None, out
    assert rest == ["-o"], rest


def test_extract_expands_user():
    out, _ = _extract_output_path(["-o", "~/some.md", "--show-board"])
    assert "~" not in str(out), out


# --- end-to-end via main() (uses --list-defaults, which prints to stdout) -------

def test_writes_file_and_still_prints_to_stdout():
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "out.md"
        rc, printed = _run_main(["-o", str(target), "--list-defaults"])
        assert rc == 0, rc
        # File written...
        assert target.is_file(), target
        body = target.read_text(encoding="utf-8")
        assert "codex" in body, body
        # ...AND stdout still got it (tee, not redirect).
        assert "codex" in printed, printed
        assert body == printed, (body, printed)


def test_overwrites_existing_file():
    # The bug this flag fixes: `> FILE` refuses to overwrite under noclobber. `-o`
    # MUST overwrite, unconditionally.
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "out.md"
        target.write_text("STALE CONTENT THAT MUST BE GONE\n", encoding="utf-8")
        rc, _ = _run_main(["-o", str(target), "--list-defaults"])
        assert rc == 0, rc
        body = target.read_text(encoding="utf-8")
        assert "STALE CONTENT" not in body, body
        assert "codex" in body, body


def test_creates_parent_dirs():
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "a" / "b" / "c" / "out.md"
        rc, _ = _run_main(["-o", str(target), "--list-defaults"])
        assert rc == 0, rc
        assert target.is_file(), target
        assert "codex" in target.read_text(encoding="utf-8")


def test_bad_path_errors_clearly_nonzero():
    with tempfile.TemporaryDirectory() as d:
        # Make a PLAIN FILE where a parent dir would need to be -> mkdir fails.
        blocker = Path(d) / "blocker"
        blocker.write_text("", encoding="utf-8")
        target = blocker / "sub" / "out.md"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, _ = _run_main(["-o", str(target), "--list-defaults"])
        assert rc == 1, rc
        msg = err.getvalue()
        assert "-o" in msg and str(target) in msg, msg


def test_file_written_even_on_nonzero_review_exit():
    # A review with no diff exits non-zero ("No diff to review."), but the file must
    # STILL be written (a caller that asked for `-o` always gets a file). We force a
    # clean tree by pointing -C at an empty git repo.
    _require_git()
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        target = Path(d) / "out.md"
        rc, _ = _run_main(["diff", "--task", "TEST-1", "-C", str(repo), "-o", str(target)])
        # No diff -> non-zero exit, but the file exists (empty result is fine).
        assert rc != 0, rc
        assert target.is_file(), target


def test_real_review_result_text_lands_in_file():
    # Beyond --list-defaults (a bare print): a REAL diff review must put its formatted
    # RESULT into the file too. We mock the backend so no model is called, give the repo
    # a staged diff, and assert the reviewer's text is BOTH printed and written. This
    # guards the contract "write the review RESULT to a file", not just trivial output.
    from reviewlib import backends  # noqa: PLC0415

    _require_git()
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
        (repo / "f.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)

        sentinel = "SENTINEL-REVIEW-VERDICT-XYZ"

        def _fake_backend(model, prompt, diff, cwd, timeout, round_no=0):
            return backends.ReviewResult(
                model=model, command="fake", returncode=0, stdout=sentinel, stderr="",
            )

        orig_avail = backends.backend_available
        # The plain `-m` path (mode_review) resolves the backend via
        # reviewlib.modes.review.resolve_backend — patch exactly that one (and force
        # availability), nothing else.
        import reviewlib.modes.review as review_mod  # noqa: PLC0415

        orig_mode_resolve = review_mod.resolve_backend
        review_mod.resolve_backend = lambda _m: _fake_backend  # type: ignore[assignment]
        backends.backend_available = lambda _m: True  # type: ignore[assignment]
        try:
            target = Path(d) / "out.md"
            rc, printed = _run_main(
                ["diff", "--task", "TEST-1", "-C", str(repo), "--staged", "-m", "codex", "-o", str(target)],
            )
            body = target.read_text(encoding="utf-8")
            assert sentinel in body, body          # the verdict reached the file
            assert sentinel in printed, printed     # and still printed to stdout
            assert rc == 0, rc
        finally:
            review_mod.resolve_backend = orig_mode_resolve
            backends.backend_available = orig_avail


def test_output_file_strips_ansi_escapes():
    # The saved file must be clean text even if the captured stream carried ANSI colour
    # codes (a coloured TTY run, or a backend's passed-through output). The LIVE stream
    # is untouched; only the file is sanitized.
    from reviewlib.cli import _write_output_file  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "out.md"
        # CSI colours + an OSC hyperlink sequence — both must be stripped.
        colored = (
            "\x1b[31mRED finding\x1b[0m and \x1b[1mbold\x1b[22m plain "
            "\x1b]8;;https://x\x07link\x1b]8;;\x07"
        )
        _write_output_file(target, colored)
        body = target.read_text(encoding="utf-8")
        assert "\x1b" not in body, repr(body)
        assert body == "RED finding and bold plain link", repr(body)


def test_tee_survives_isatty_and_encoding_access():
    # The tee stands in for sys.stdout; helpers may probe isatty()/encoding. Both must
    # work under `-o` (they delegate to the live stream), not raise.
    from reviewlib.cli import _Tee  # noqa: PLC0415

    buf = io.StringIO()
    tee = _Tee(sys.stdout, buf)
    # Should not raise; returns a bool / str.
    assert isinstance(tee.isatty(), bool)
    assert isinstance(tee.encoding, str)
    tee.write("x")
    assert buf.getvalue() == "x"


def test_argparse_error_does_not_truncate_existing_output_file():
    # DATA-LOSS GUARD: `review --bad-flag -o important.md` makes argparse raise
    # SystemExit BEFORE any review runs. The pre-existing file must NOT be truncated
    # (the old `finally`-always-writes would clobber it with an empty string).
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "important.md"
        target.write_text("PRECIOUS USER DATA\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                main(["--definitely-not-a-flag", "-o", str(target)])
            except SystemExit:
                pass
        assert target.read_text(encoding="utf-8") == "PRECIOUS USER DATA\n", target.read_text()


def test_help_with_output_flag_does_not_truncate_file():
    # `review --help -o important.md` also exits via SystemExit (argparse prints help
    # and exits 0) — must not clobber the target with the help text.
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "important.md"
        target.write_text("KEEP ME\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                main(["--help", "-o", str(target)])
            except SystemExit:
                pass
        assert target.read_text(encoding="utf-8") == "KEEP ME\n", target.read_text()


def test_removed_flag_with_output_flag_does_not_truncate_file():
    # DATA-LOSS GUARD (codex P2): a REMOVED flag (`--mcp`/`--ln`) is a usage error rejected
    # via `_reject_removed_flags`, which RETURNS 2 (it does not raise SystemExit). The `-o`
    # tee path must NOT treat that as a completed dispatch and persist the empty captured
    # stdout — `review --mcp -o important.md` must leave the pre-existing file untouched,
    # exactly like the argparse-usage-error case above.
    for bad in ("--mcp", "--ln"):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "important.md"
            target.write_text("PRECIOUS USER DATA\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main([bad, "-o", str(target)])
            assert rc == 2, (bad, rc)
            assert target.read_text(encoding="utf-8") == "PRECIOUS USER DATA\n", (
                bad, target.read_text())


def test_removed_subcommand_with_output_flag_does_not_truncate_file():
    # DATA-LOSS GUARD (codex P1): the renamed-away `review review` verb is a usage error
    # rejected (RETURNS 2) before any review runs. Like the removed FLAGS, it is pre-rejected
    # in main() BEFORE the `-o` tee is armed, so `review review -o important.md` must leave
    # the pre-existing file untouched (not clobber it with the empty captured stdout).
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "important.md"
        target.write_text("PRECIOUS USER DATA\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = main(["review", "-o", str(target)])
        assert rc == 2, rc
        assert target.read_text(encoding="utf-8") == "PRECIOUS USER DATA\n", target.read_text()


def test_no_subcommand_with_args_and_output_flag_does_not_truncate_file():
    # DATA-LOSS GUARD (codex P1): `review -C <repo> -o important.md` (flags, no verb) now
    # prints help + a `review diff` pointer and exits via SystemExit(2) — a help/usage dump
    # must NOT write the `-o` file (it would clobber the target with help text / empty).
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "important.md"
        target.write_text("PRECIOUS USER DATA\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                main(["-C", d, "-o", str(target)])
            except SystemExit as exc:
                assert exc.code == 2, exc.code
        assert target.read_text(encoding="utf-8") == "PRECIOUS USER DATA\n", target.read_text()


def test_bare_review_help_with_output_flag_does_not_truncate_file():
    # A truly bare `review -o important.md` prints the help to stdout and exits via
    # SystemExit(0), like `review --help` — it must NOT clobber the target with the help.
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "important.md"
        target.write_text("KEEP ME\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                main(["-o", str(target)])
            except SystemExit as exc:
                assert exc.code == 0, exc.code
        assert target.read_text(encoding="utf-8") == "KEEP ME\n", target.read_text()


def test_subcommand_only_flag_without_verb_with_output_does_not_truncate_file():
    # DATA-LOSS GUARD (codex review): `review --staged -o important.md` / `review --visual
    # shot.png -o important.md` (a subcommand-scoped flag, no verb) is rejected with the
    # friendly `review diff` pointer BEFORE the `-o` tee is armed (like the removed-flag
    # guards) — it must leave the pre-existing file untouched, not clobber it with empty
    # captured stdout.
    for argv_prefix in (["--staged"], ["--visual", "shot.png"]):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "important.md"
            target.write_text("PRECIOUS USER DATA\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main([*argv_prefix, "-o", str(target)])
            assert rc == 2, (argv_prefix, rc)
            assert target.read_text(encoding="utf-8") == "PRECIOUS USER DATA\n", (
                argv_prefix, target.read_text())


def test_value_taking_opts_are_all_value_taking():
    # Finding #2 guard: every entry in _VALUE_TAKING_OPTS must REALLY consume a value in
    # the real parser, else the pre-scan would skip a non-value token (and `-o` after a
    # store_true flag would be lost). Introspect the SAME parser _dispatch builds and
    # assert each listed option's action consumes exactly one value (nargs is None and
    # it is not a store_const/store_true action), and that no store_true flag leaked in.
    from reviewlib.cli import _VALUE_TAKING_OPTS  # noqa: PLC0415

    store_true_flags = {
        "--staged", "--list-defaults", "--show-board", "--json", "--strict",
        "--no-ai", "--no-local-model",
    }
    # None of the store_true flags must be in the value-taking set.
    assert not (_VALUE_TAKING_OPTS & store_true_flags), _VALUE_TAKING_OPTS & store_true_flags

    # Each listed opt errors "expected one argument" when given no value. We pass a
    # leading no-op value-taking flag with a value so the parser reaches the bare opt
    # without triggering a real review (it errors during parse). Each opt must be routed
    # through a SUBCOMMAND whose parser actually DEFINES it — the option-scoping (ROADMAP
    # "subcommand-only options belong in the subcommand help") means the top-level parser no
    # longer carries the mode/visual-only flags, so a top-level `--prompt …` would now
    # error "unrecognized arguments", not "expected one argument".
    brainstorm_only = {"--rounds", "--max-rounds"}
    specweb_only = {"--spec"}        # lives ONLY on the `spec-web reply` subparser
    moderator_only = {"--moderator"}  # lives on quorum / brainstorm, NOT the diff review
    # ONLY on the `qa` subparser (modes/qa.py): the Phase-2 executor flags + the Phase-3
    # SUT-env value-taking flags (--stage-url / --config).
    qa_only = {"--suites", "--kind", "--report", "--max-cases", "--stage-url", "--config"}
    for opt in sorted(_VALUE_TAKING_OPTS):
        if opt in ("-o", "--output"):
            continue  # handled by the pre-scan, covered by other tests
        if opt in brainstorm_only:
            argv = ["brainstorm", "topic", opt]
        elif opt in specweb_only:
            # route through the subcommand whose parser actually defines --spec
            argv = ["spec-web", "reply", "cid", "ans", opt]
        elif opt in moderator_only:
            argv = ["quorum", "q", opt]  # --moderator is a quorum/brainstorm flag
        elif opt in qa_only:
            # --suites lives only on the qa parser; lead with --timeout so the parser
            # reaches the bare opt at the end without running anything.
            argv = ["qa", "--timeout", "100", opt]
        else:
            # Everything else (global + the review-only --prompt + the visual group) lives
            # on the `diff` subcommand parser. Lead with --timeout (global, value-taking) so
            # the parser reaches the bare opt at the end without running a review.
            argv = ["diff", "--timeout", "100", opt]
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                main(argv)
            except SystemExit:
                pass
        msg = err.getvalue()
        assert "expected one argument" in msg, (opt, msg)


def test_help_topic_usage_error_with_output_flag_does_not_truncate_file():
    # DATA-LOSS GUARD (premium merge-gate, same class as #37): the NEW `review help <topic>`
    # command, on a USAGE error (unknown topic / extra trailing args / a bad topic via the
    # `--help <topic>` alias), used to `return 2` from `_help_subcommand` — which the `-o` tee
    # path treats as a completed dispatch and persists the (empty) captured stdout, TRUNCATING
    # a pre-existing `-o` target to empty. A usage error must behave like argparse's own usage
    # errors w.r.t. `-o`: raise SystemExit BEFORE the tee writes, so the file is left untouched.
    # `review help bogus-topic -o existing.md` must NOT empty existing.md.
    # (argv, expected stderr fragment). Both usage-error shapes (unknown topic / extra trailing
    # args) are covered through BOTH spellings — the `help` subcommand AND the `--help`/`-h <topic>`
    # alias — since the alias routes all trailing tokens through the same _help_subcommand check.
    usage_error_cases = (
        (["help", "bogus-topic"], "unknown topic"),
        (["help", "config", "extra-arg"], "extra arguments"),
        (["--help", "bogustopic"], "unknown topic"),
        (["-h", "bogustopic"], "unknown topic"),
        (["--help", "config", "extra-arg"], "extra arguments"),
        (["-h", "config", "extra-arg"], "extra arguments"),
    )
    for prefix, msg_fragment in usage_error_cases:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "important.md"
            target.write_text("PRECIOUS USER DATA\n", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                try:
                    rc = main([*prefix, "-o", str(target)])
                except SystemExit as exc:
                    rc = exc.code
            assert rc == 2, (prefix, rc)
            actual = target.read_text(encoding="utf-8")
            assert actual == "PRECIOUS USER DATA\n", (prefix, repr(actual))
            # The helpful diagnostic must still reach stderr (a future refactor must not drop the
            # message before the raise) — and it must NOT have been teed into the file.
            assert msg_fragment in err.getvalue(), (prefix, err.getvalue())

    # The other half of the contract: a usage error must not CREATE a fresh empty `-o` file
    # either (if the tee ever switched to "open in 'w' then conditionally write", a brand-new
    # empty file would silently appear). `review help bogus -o newfile.md` must touch nothing.
    for prefix, _ in usage_error_cases:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "newfile.md"  # does NOT pre-exist
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    rc = main([*prefix, "-o", str(target)])
                except SystemExit as exc:
                    rc = exc.code
            assert rc == 2, (prefix, rc)
            assert not target.exists(), (prefix, "usage error created an empty -o file")


def test_help_topic_usage_error_exits_2_without_output_flag():
    # The usage-error → `raise SystemExit(2)` path must surface a clean exit code 2 (NOT 1, and
    # NOT argparse's exit-0 help) on its own, with no `-o` involved — so a script can detect a
    # bad `review help <topic>` invocation. Pins that main() does not translate the SystemExit
    # into a different code on the way out (the SAME usage-error shapes as the truncation guard
    # above: unknown topic / extra trailing args, through both `help` and the `--help`/`-h` alias).
    for prefix in (
        ["help", "bogus-topic"],
        ["help", "config", "extra-arg"],
        ["--help", "bogustopic"],
        ["-h", "bogustopic"],
        ["--help", "config", "extra-arg"],
        ["-h", "config", "extra-arg"],
    ):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = main(list(prefix))
            except SystemExit as exc:
                rc = exc.code
        assert rc == 2, (prefix, rc)


def test_help_valid_topic_with_output_flag_writes_topic_text():
    # The flip side of the data-loss guard: a SUCCESSFUL `review help <topic> -o FILE` (and the
    # bare `review help -o FILE` listing) is real output, not a usage error — it MUST still tee
    # the topic reference into the file (like `--list-defaults -o FILE`). Only the usage-ERROR
    # branches skip the write; the happy path keeps writing.
    for prefix in (["help", "config"], ["help"]):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "out.md"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = main([*prefix, "-o", str(target)])
            assert rc == 0, (prefix, rc)
            assert target.is_file(), (prefix, target)
            assert target.read_text(encoding="utf-8").strip(), (prefix, "empty topic-help file")


def test_help_documents_output_flag():
    # `--help` must advertise `-o` and explicitly steer away from `> FILE`.
    err = io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            main(["--help"])
        except SystemExit:
            pass
    text = out.getvalue() + err.getvalue()
    assert "-o" in text or "--output" in text, text


if __name__ == "__main__":
    failures = 0
    skipped = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except _SkipTest as exc:
                skipped += 1
                print(f"SKIP {name}: {exc}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    if skipped:
        print(f"SKIP {skipped} checks")
    sys.exit(1 if failures else 0)
