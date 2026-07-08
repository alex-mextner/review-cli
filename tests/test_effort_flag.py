"""Unit tests for `--effort` — per-run reasoning effort across backends (review-cli#126).

Contract under test (see backends.py "Run-scoped reasoning effort" section):
  * `_apply_effort_args` (cli) parses the repeatable flag values and exports
    $REVIEW_EFFORT (global) / $REVIEW_EFFORT_<PROVIDER> (scoped). The flag wins
    within its scope: a bare (global) LEVEL first clears every pre-existing
    $REVIEW_EFFORT* var; a PROVIDER=LEVEL value overrides only its own provider.
  * `effort_for` (backends) reads them back, provider-scoped beating global, and
    rejects unknown levels loudly instead of forwarding garbage to a backend.
  * Each capable backend threads the level into its own control surface:
      codex    -> `-c model_reasoning_effort=<level>` (max mapped to xhigh)
      claude   -> CLI `--effort <level>` (direct) / API `output_config.effort`
      opencode -> `--variant <level>` (verbatim passthrough, no mapping)
  * Backends with no effort control (gemini, zai, commandcode, openrouter) and the
    legacy claude-p fallback WARN on stderr — never a silent ignore.

Style: fixture-free plain functions with a globals()-loop `__main__` runner, so the
file runs BOTH under pytest and as the standalone `python tests/test_effort_flag.py`
subprocess smoke.py's `run_unit` spawns (a pytest-fixture file would silently run
NOTHING there). Mock harness mirrors tests/test_opencode_realrepo.py: `_run_streamed`
+ `_which` are patched so the tests are hermetic (no real backend binaries needed).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import backends as review_backends  # noqa: E402
from reviewlib.cli import _apply_effort_args  # noqa: E402


@contextlib.contextmanager
def _clean_effort_env(**env: str):
    """Hermetic effort env + fresh warn-dedup set: clears every REVIEW_EFFORT* var,
    applies the requested ones, and restores the original environment afterwards."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("REVIEW_EFFORT")}
    for key in saved:
        del os.environ[key]
    os.environ.update(env)
    saved_warned = review_backends._EFFORT_WARNED
    review_backends._EFFORT_WARNED = set()
    try:
        yield
    finally:
        for key in [k for k in os.environ if k.startswith("REVIEW_EFFORT")]:
            del os.environ[key]
        os.environ.update(saved)
        review_backends._EFFORT_WARNED = saved_warned


@contextlib.contextmanager
def _captured_stderr():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


@contextlib.contextmanager
def _patched(obj, **attrs):
    """setattr-patch `obj` for the block, restoring the originals afterwards."""
    saved = {name: getattr(obj, name) for name in attrs}
    for name, value in attrs.items():
        setattr(obj, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(obj, name, value)


class _Captured:
    """Stand-in for the CompletedProcess `_run_streamed` returns; records argv."""

    def __init__(self) -> None:
        self.argv: list[str] | None = None

    def __call__(self, argv, *args, **kwargs):
        self.argv = list(argv)

        class _Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return _Proc()


# ---------------------------------------------------------------------------
# flag parsing -> env export
# ---------------------------------------------------------------------------


def test_apply_global_level_exports_review_effort():
    with _clean_effort_env():
        assert _apply_effort_args(["xhigh"]) is None
        assert os.environ["REVIEW_EFFORT"] == "xhigh"


def test_apply_provider_scoped_level():
    with _clean_effort_env():
        assert _apply_effort_args(["codex=high", "xhigh"]) is None
        assert os.environ["REVIEW_EFFORT_CODEX"] == "high"
        assert os.environ["REVIEW_EFFORT"] == "xhigh"


def test_apply_flag_wins_over_stale_scoped_env():
    # README promises "the flag wins": a stale REVIEW_EFFORT_CODEX export must not
    # silently beat an explicit global `--effort xhigh` for the codex seat.
    with _clean_effort_env(REVIEW_EFFORT_CODEX="low"):
        assert _apply_effort_args(["xhigh"]) is None
        assert "REVIEW_EFFORT_CODEX" not in os.environ
        assert review_backends.effort_for("codex") == "xhigh"


def test_apply_scoped_flag_preserves_env_global():
    # `--effort codex=low` with REVIEW_EFFORT=high in the env: the scoped flag
    # overrides ONLY codex; the env-provided global stays in force for other seats.
    with _clean_effort_env(REVIEW_EFFORT="high"):
        assert _apply_effort_args(["codex=low"]) is None
        assert review_backends.effort_for("codex") == "low"
        assert review_backends.effort_for("claude") == "high"


def test_apply_without_values_leaves_env_in_force():
    with _clean_effort_env(REVIEW_EFFORT_CODEX="low"):
        assert _apply_effort_args([]) is None
        assert review_backends.effort_for("codex") == "low"


def test_apply_rejects_unknown_level():
    with _clean_effort_env():
        err = _apply_effort_args(["turbo"])
        assert err is not None and "turbo" in err and "xhigh" in err


def test_apply_rejects_bogus_provider():
    with _clean_effort_env():
        err = _apply_effort_args(["co dex=high"])
        assert err is not None and "provider" in err


def test_apply_is_case_insensitive_and_skips_blanks():
    with _clean_effort_env():
        assert _apply_effort_args(["  ", "XHigh"]) is None
        assert os.environ["REVIEW_EFFORT"] == "xhigh"


# ---------------------------------------------------------------------------
# effort_for: env readback + precedence
# ---------------------------------------------------------------------------


def test_effort_for_none_when_unset():
    with _clean_effort_env():
        assert review_backends.effort_for("codex") is None


def test_effort_for_provider_beats_global():
    with _clean_effort_env(REVIEW_EFFORT="low", REVIEW_EFFORT_CODEX="xhigh"):
        assert review_backends.effort_for("codex") == "xhigh"
        assert review_backends.effort_for("claude") == "low"


def test_effort_for_invalid_scoped_env_falls_back_to_global():
    # A typo'd scoped var warns but must not also suppress a valid global level.
    with _clean_effort_env(REVIEW_EFFORT="xhigh", REVIEW_EFFORT_CODEX="warpspeed"), _captured_stderr() as err:
        assert review_backends.effort_for("codex") == "xhigh"
    assert "warpspeed" in err.getvalue()


def test_effort_for_rejects_unknown_env_level():
    with _clean_effort_env(REVIEW_EFFORT="warpspeed"), _captured_stderr() as err:
        assert review_backends.effort_for("codex") is None
    assert "warpspeed" in err.getvalue()


# ---------------------------------------------------------------------------
# codex: -c model_reasoning_effort=<level>, max -> xhigh
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _capture_codex():
    captured = _Captured()
    with _patched(
        review_backends,
        _run_streamed=captured,
        _which=lambda name: f"/mock/bin/{name}",
    ):
        yield captured


def test_codex_gets_reasoning_effort_config():
    with _clean_effort_env(REVIEW_EFFORT="xhigh"), _capture_codex() as captured:
        review_backends.review_codex("codex", "p", "d", Path("."), 60)
    idx = captured.argv.index("-c")
    assert captured.argv[idx + 1] == "model_reasoning_effort=xhigh"


def test_codex_maps_max_to_xhigh_with_warning():
    with (
        _clean_effort_env(REVIEW_EFFORT="max"),
        _capture_codex() as captured,
        _captured_stderr() as err,
    ):
        review_backends.review_codex("codex", "p", "d", Path("."), 60)
    assert "model_reasoning_effort=xhigh" in captured.argv
    assert "xhigh" in err.getvalue()


def test_codex_argv_unchanged_without_effort():
    with _clean_effort_env(), _capture_codex() as captured:
        review_backends.review_codex("codex", "p", "d", Path("."), 60)
    assert "-c" not in captured.argv


# ---------------------------------------------------------------------------
# claude CLI: --effort <level> on the direct print path; warn on claude-p
# ---------------------------------------------------------------------------


def test_claude_cli_argv_direct_gets_effort_flag():
    with _clean_effort_env(REVIEW_EFFORT="xhigh"):
        argv = review_backends._claude_cli_argv(
            "/mock/claude", True, "fable", Path("."), 60
        )
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_claude_cli_argv_direct_no_effort_by_default():
    with _clean_effort_env():
        argv = review_backends._claude_cli_argv(
            "/mock/claude", True, None, Path("."), 60
        )
    assert "--effort" not in argv


def test_claude_p_fallback_warns_instead_of_flag():
    with _clean_effort_env(REVIEW_EFFORT="xhigh"), _captured_stderr() as err:
        argv = review_backends._claude_cli_argv(
            "/mock/claude-p", False, None, Path("."), 60
        )
    assert "--effort" not in argv
    assert "claude-p" in err.getvalue()


def test_claude_scoped_override_reaches_cli():
    with _clean_effort_env(REVIEW_EFFORT="low", REVIEW_EFFORT_CLAUDE="max"):
        argv = review_backends._claude_cli_argv(
            "/mock/claude", True, "fable", Path("."), 60
        )
    assert argv[argv.index("--effort") + 1] == "max"


# ---------------------------------------------------------------------------
# claude API: output_config.effort in the POST body
# ---------------------------------------------------------------------------


def _run_claude_api_capturing_body() -> dict:
    bodies: list[dict] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"content": [{"type": "text", "text": "ok"}], "usage": {}}
            ).encode()

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        return _Resp()

    with (
        _patched(
            review_backends,
            _anthropic_api_config=lambda: {
                "base": "https://api.example",
                "auth": ("x-api-key", "k"),
            },
        ),
        _patched(review_backends.urllib.request, urlopen=fake_urlopen),
    ):
        review_backends.review_claude_api(
            "claude:claude-fable-5", "p", "d", Path("."), 60
        )
    return bodies[0]


def test_claude_api_body_gets_output_config_effort():
    with _clean_effort_env(REVIEW_EFFORT="xhigh"):
        body = _run_claude_api_capturing_body()
    assert body["output_config"] == {"effort": "xhigh"}
    assert body["model"] == "claude-fable-5"


def test_claude_api_body_clean_without_effort():
    with _clean_effort_env():
        body = _run_claude_api_capturing_body()
    assert "output_config" not in body


# ---------------------------------------------------------------------------
# opencode: --variant <level> (verbatim, even `max` — unlike codex, no mapping)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _capture_opencode(in_repo: bool):
    captured = _Captured()
    with _patched(
        review_backends,
        _run_streamed=captured,
        _which=lambda name: f"/mock/bin/{name}",
        _ensure_opencode_readonly_agent=lambda cwd, model: None,
        _opencode_runs_in_repo=lambda cwd: in_repo,
        _run=lambda *a, **k: None,  # temp-dir `git init` in the fallback branch
    ):
        yield captured


def test_opencode_gets_variant_flag_in_repo_and_fallback():
    for in_repo in (True, False):
        with (
            _clean_effort_env(REVIEW_EFFORT="high"),
            _capture_opencode(in_repo) as captured,
        ):
            review_backends.review_opencode("oc:zai/glm-5.2", "p", "d", Path("."), 60)
        idx = captured.argv.index("--variant")
        assert captured.argv[idx + 1] == "high", f"in_repo={in_repo}"


def test_opencode_passes_max_verbatim():
    # Deliberate asymmetry with codex: --variant values are provider-specific, so
    # `max` is passed through untouched rather than mapped.
    with _clean_effort_env(REVIEW_EFFORT="max"), _capture_opencode(True) as captured:
        review_backends.review_opencode("oc:zai/glm-5.2", "p", "d", Path("."), 60)
    assert captured.argv[captured.argv.index("--variant") + 1] == "max"


def test_opencode_no_variant_by_default():
    with _clean_effort_env(), _capture_opencode(True) as captured:
        review_backends.review_opencode("oc:zai/glm-5.2", "p", "d", Path("."), 60)
    assert "--variant" not in captured.argv


# ---------------------------------------------------------------------------
# unsupported backends: explicit warning, once per process, even on early failure
# ---------------------------------------------------------------------------


def _raise_runtime_error():
    raise RuntimeError("no key")


def test_gemini_warns_when_effort_set():
    with (
        _clean_effort_env(REVIEW_EFFORT="xhigh"),
        _patched(review_backends, _gemini_key=_raise_runtime_error),
        _captured_stderr() as err,
    ):
        result = review_backends.review_gemini("gemini", "p", "d", Path("."), 60)
    assert result.returncode != 0  # hermetic: the request itself fails on the stub key
    text = err.getvalue()
    assert "gemini" in text and "does not support --effort" in text


def test_gemini_silent_without_effort():
    with (
        _clean_effort_env(),
        _patched(review_backends, _gemini_key=_raise_runtime_error),
        _captured_stderr() as err,
    ):
        review_backends.review_gemini("gemini", "p", "d", Path("."), 60)
    assert "--effort" not in err.getvalue()


def test_openai_compatible_backends_warn_even_on_config_failure():
    # zai/commandcode/openrouter warn BEFORE their config/key early-return, mirroring
    # gemini — a seat that then fails on config must still not silently eat --effort.
    for backend_fn, name, mode_env in (
        (review_backends.review_zai, "zai", "REVIEW_ZAI_MODE"),
        (review_backends.review_commandcode, "commandcode", "REVIEW_COMMANDCODE_MODE"),
        (review_backends.review_openrouter, "openrouter", "REVIEW_OPENROUTER_MODE"),
    ):
        saved = os.environ.get(mode_env)
        os.environ[mode_env] = (
            "cli"  # api-only backends: forced cli = config-error path
        )
        try:
            with _clean_effort_env(REVIEW_EFFORT="xhigh"), _captured_stderr() as err:
                result = backend_fn(name, "p", "d", Path("."), 60)
            assert result.returncode != 0, name
            text = err.getvalue()
            assert name in text and "does not support --effort" in text, name
        finally:
            if saved is None:
                del os.environ[mode_env]
            else:
                os.environ[mode_env] = saved


def test_unsupported_warning_fires_once():
    with _clean_effort_env(REVIEW_EFFORT="xhigh"), _captured_stderr() as err:
        review_backends._warn_effort_unsupported("gemini")
        review_backends._warn_effort_unsupported("gemini")
    assert err.getvalue().count("does not support --effort") == 1


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
    print(f"\n{'FAILED' if failures else 'OK'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
