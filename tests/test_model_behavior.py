"""Unit tests for reviewlib.model_behavior — the versioned per-model true-silence
registry (Alex's request 2026-08-19/20: distinguish a model that never says anything
at all from one that has produced output and gone quiet, and let that per-model
knowledge be updated as model behavior drifts across releases).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from reviewlib import model_behavior  # noqa: E402


def _with_env(**env):
    class _Ctx:
        def __enter__(self):
            self._saved = {k: os.environ.get(k) for k in env}
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, *exc):
            for k, old in self._saved.items():
                if old is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = old
            return False

    return _Ctx()


def test_unlisted_model_gets_the_conservative_default():
    assert (
        model_behavior.true_silence_timeout_seconds("some:brand-new-model")
        == model_behavior._DEFAULT_TRUE_SILENCE_SECONDS
    )
    assert model_behavior.is_known_silent_thinker("some:brand-new-model") is False


def test_default_matches_alex_5_minute_request():
    assert model_behavior._DEFAULT_TRUE_SILENCE_SECONDS == 5 * 60


def test_registry_override_wins_over_the_default():
    behavior = model_behavior.ModelBehavior(
        silent_thinker=True,
        true_silence_seconds=900,
        updated="2026-08-20",
        note="test override",
    )
    model_behavior._REGISTRY["test:overridden-model"] = behavior
    try:
        assert (
            model_behavior.true_silence_timeout_seconds("test:overridden-model") == 900
        )
        assert model_behavior.is_known_silent_thinker("test:overridden-model") is True
    finally:
        del model_behavior._REGISTRY["test:overridden-model"]


def test_env_override_wins_over_both_default_and_registry():
    """The blanket env override is a coarser, more-recently-expressed operator
    intent than either the default or a per-model registry entry."""
    behavior = model_behavior.ModelBehavior(
        silent_thinker=False,
        true_silence_seconds=900,
        updated="2026-08-20",
        note="test override",
    )
    model_behavior._REGISTRY["test:overridden-model-2"] = behavior
    try:
        with _with_env(REVIEW_TRUE_SILENCE_SECONDS="42"):
            assert (
                model_behavior.true_silence_timeout_seconds("test:overridden-model-2")
                == 42
            )
            assert model_behavior.true_silence_timeout_seconds("unlisted:model") == 42
    finally:
        del model_behavior._REGISTRY["test:overridden-model-2"]


def test_env_override_non_positive_means_explicit_disable():
    """codex + Fable review finding (round 4): ANY value <= 0 must mean "disable the
    check" (returns None, matching process._run_streamed's own
    true_silence_timeout=None contract) — mirroring seat_cooldown's own
    $REVIEW_SEAT_COOLDOWN_SECONDS<=0 disable convention (the feature this one
    directly feeds a cooldown into). An earlier version of this function only treated
    EXACTLY 0 as disable, contradicting its own documented "<= 0 disables" help text
    for any negative value."""
    with _with_env(REVIEW_TRUE_SILENCE_SECONDS="0"):
        assert model_behavior.true_silence_timeout_seconds("unlisted:model") is None
    with _with_env(REVIEW_TRUE_SILENCE_SECONDS="-5"):
        assert model_behavior.true_silence_timeout_seconds("unlisted:model") is None


def test_registry_entry_non_positive_true_silence_seconds_also_disables():
    """codex review finding (round 10): the SAME <= 0 -> disable convention the env
    override enforces above must ALSO apply to a per-model _REGISTRY entry (or a
    hypothetical _DEFAULT_BEHAVIOR change) -- an un-normalized 0/negative
    true_silence_seconds is `is not None`, so it would ARM _run_streamed's check with
    a near-zero budget and cause an instant false reap + escalating cooldown bench of
    a healthy seat on its very first poll. Latent today (_REGISTRY ships empty), but
    exactly the footgun a future maintainer adding an entry could hit with no guard
    catching it."""
    zero = model_behavior.ModelBehavior(
        silent_thinker=False,
        true_silence_seconds=0,
        updated="2026-08-25",
        note="test: zero means disabled, same as the env override convention",
    )
    negative = model_behavior.ModelBehavior(
        silent_thinker=False,
        true_silence_seconds=-5,
        updated="2026-08-25",
        note="test: negative also means disabled",
    )
    model_behavior._REGISTRY["test:zero-true-silence"] = zero
    model_behavior._REGISTRY["test:negative-true-silence"] = negative
    try:
        assert (
            model_behavior.true_silence_timeout_seconds("test:zero-true-silence")
            is None
        )
        assert (
            model_behavior.true_silence_timeout_seconds("test:negative-true-silence")
            is None
        )
    finally:
        del model_behavior._REGISTRY["test:zero-true-silence"]
        del model_behavior._REGISTRY["test:negative-true-silence"]


def test_env_override_ignores_garbage_values():
    with _with_env(REVIEW_TRUE_SILENCE_SECONDS="not-a-number"):
        assert (
            model_behavior.true_silence_timeout_seconds("unlisted:model")
            == model_behavior._DEFAULT_TRUE_SILENCE_SECONDS
        )
    with _with_env(REVIEW_TRUE_SILENCE_SECONDS=""):
        assert (
            model_behavior.true_silence_timeout_seconds("unlisted:model")
            == model_behavior._DEFAULT_TRUE_SILENCE_SECONDS
        )


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
