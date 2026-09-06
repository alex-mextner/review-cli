#!/usr/bin/env python3
"""Regression: the pool guard's liveness probe must consider provider failover.

codex P2 on review-cli#157 ("Let the guard consider live failover providers"):
`cli._evaluate_pool_or_bail` wired the guard's `available=` straight to
`backends.backend_available(model)` — a raw probe of the REQUESTED spelling only. In an
environment where the requested head provider is unavailable (e.g. no `ZAI_API_KEY`) but a
later provider in the model's failover chain is live (e.g. authenticated `oc:zai`), this
marked the seat DOWN and let the guard propose/exit — even though `provider_chain` would
have picked the live alternate at dispatch time. The guard's liveness check must agree with
what will actually be dispatched.

Offline: `backends.backend_available` / `backends.runtime_provider_marked_unpaid` /
`backends.backend_unavailable_reason` are patched with manual save/restore (not the pytest
`monkeypatch` fixture), so this also runs unchanged under the standalone `__main__` runner
(mirrors tests/test_reviewer_board.py's convention).
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reviewlib.backends as backends  # noqa: E402
from reviewlib import cli  # noqa: E402


class _PatchBackends:
    """Save/restore `backends.backend_available` + `.backend_unavailable_reason` +
    `.runtime_provider_marked_unpaid` around a test body."""

    def __init__(self, *, available, reason, unpaid=lambda _m: False):
        self._available, self._reason, self._unpaid = available, reason, unpaid

    def __enter__(self):
        self._saved = (
            backends.backend_available,
            backends.backend_unavailable_reason,
            backends.runtime_provider_marked_unpaid,
        )
        backends.backend_available = self._available
        backends.backend_unavailable_reason = self._reason
        backends.runtime_provider_marked_unpaid = self._unpaid
        return self

    def __exit__(self, *exc):
        (
            backends.backend_available,
            backends.backend_unavailable_reason,
            backends.runtime_provider_marked_unpaid,
        ) = self._saved
        return False


def test_guard_proceeds_when_only_a_failover_alternate_is_live():
    """glm52 -> zai:glm-5.2 (down, no key) -> oc:zai/glm-5.2 (live). The guard must NOT
    bail just because the head spelling is unavailable."""

    def _available(model: str) -> bool:
        return model == "oc:zai/glm-5.2"

    def _reason(model: str) -> str | None:
        return None if _available(model) else f"{model}: no key configured"

    with _PatchBackends(available=_available, reason=_reason):
        rc = cli._evaluate_pool_or_bail(
            config={},
            config_models=[],
            config_has_board=False,
            user_seats=(("glm52", "glm52"),),
            explicit_models=["glm52"],
            pool_arg=None,
            default_pool=1,
        )
    assert rc is None, "guard bailed even though a failover alternate is live"


def test_guard_still_bails_when_every_provider_in_the_chain_is_down():
    """Sanity check the other direction: the fix must not make the guard blind — if NO
    provider in the chain is reachable, it still reports down."""

    with _PatchBackends(
        available=lambda _m: False,
        reason=lambda m: f"{m}: no key configured",
    ):
        rc = cli._evaluate_pool_or_bail(
            config={},
            config_models=[],
            config_has_board=False,
            user_seats=(("glm52", "glm52"),),
            explicit_models=["glm52"],
            pool_arg=None,
            default_pool=1,
        )
    assert rc is not None, "guard proceeded even though every chain provider is down"


def test_guard_synthesizes_a_reason_when_the_head_reason_is_blank():
    """A defensive fallback (codex review of #157, finding 1): if `_guard_available` says
    the WHOLE chain is down but `backend_unavailable_reason` (which only probes the head
    spelling) happens to return None, the guard must still print a real reason, not a blank
    one. This shape isn't reachable through the real `backend_available`/
    `backend_unavailable_reason` pairing (both are backed by the same single source of
    truth), but a defensive synthesis costs nothing on the common path and closes the gap."""

    with _PatchBackends(
        available=lambda _m: False,  # nothing in the chain is reachable
        reason=lambda _m: None,  # ...yet the head-only reason probe is silent
    ):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli._evaluate_pool_or_bail(
                config={},
                config_models=[],
                config_has_board=False,
                user_seats=(("glm52", "glm52"),),
                explicit_models=["glm52"],
                pool_arg=None,
                default_pool=1,
            )
    assert rc is not None, "guard should have bailed"
    assert (
        "no provider in its failover chain is currently available" in err.getvalue()
    ), err.getvalue()


def test_chain_aware_available_agrees_with_the_guard_on_a_head_down_alternate_live_seat():
    """`cli._chain_aware_available` is the ONE liveness predicate the guard, the ETA's
    `planned_pool` split, and the real board dispatch split (`_mode_review_board`) all
    share — codex P1 on review of #157: before this fix only the guard was chain-aware,
    so it could approve a pool size the (raw) split then silently shrank. This proves the
    shared function itself agrees with `any_provider_available` (the guard's own
    liveness check) for the exact head-down/alternate-live shape the P1 was about;
    `test_board_startup_split_includes_a_seat_whose_head_is_down_but_has_a_live_alternate`
    (tests/test_provider_failover.py) proves the REAL dispatch split honors it end to end."""

    def _available(model: str) -> bool:
        return model == "oc:zai/glm-5.2"

    with _PatchBackends(available=_available, reason=lambda _m: None):
        assert cli._chain_aware_available("zai:glm-5.2") is True, (
            "a live failover alternate must count as available"
        )
        assert cli._chain_aware_available("codex") is False, (
            "a single-provider model with no live provider must be unavailable"
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
