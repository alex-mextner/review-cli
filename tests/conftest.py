"""Pytest-wide test-isolation guard for process-global backend state.

WHY: several suites drive the real `cli._dispatch` (the `--retry`, staged-diff, no-repo,
mode-subcommand paths). `_dispatch` runs `load_config()` +
`backends.configure_unpaid_providers(config['unpaid_providers'])` against the REAL
`~/.config/review-cli/config.yaml`, which sets the process-wide `_CONFIG_UNPAID_PROVIDERS`
(e.g. {commandcode, gemini}). It also lazily fills the payment-preflight cache. Neither is
reset when the test ends, so the leaked "commandcode is unpaid" state silently marked
`oc:commandcode/...` seats DEAD in a LATER suite's availability assertions
(test_reviewer_board), and the same class of leak repeatedly broke the provider-failover /
pool-guard suites. Individual per-test restores are whack-a-mole across 18 `_dispatch`
callers; this autouse fixture snapshots the two globals before every test and restores them
after, so no test can leak backend-availability state into another. It only saves+restores
(never mutates during the test), so it can't hide a real product bug.

Scope: pytest only. The plain-script `__main__` harnesses (which CI's smoke.py also drives)
don't load conftest; the suites that leak via their own `_dispatch` calls additionally
restore inline so the script path is covered too.
"""

from __future__ import annotations

import pytest

import reviewlib.backends as _backends
import reviewlib.provider_failover as _pf
from _brainstorm_env_isolation import _ISOLATED_ENV_KEYS


@pytest.fixture(autouse=True)
def _isolate_backend_global_state():
    saved_unpaid = _backends._CONFIG_UNPAID_PROVIDERS
    saved_preflight = dict(_backends._PAYMENT_PREFLIGHT_CACHE)
    # cli._dispatch also calls provider_failover.configure_provider_chains(config['provider_chains']),
    # mutating the process-wide _CONFIG_OVERRIDES. Snapshot it too so a _dispatch-driving test
    # with a `provider_chains:` config can't leak the override map into a later suite (same leak
    # class this fixture exists to close).
    saved_chain_overrides = dict(_pf._CONFIG_OVERRIDES)
    try:
        yield
    finally:
        _backends._CONFIG_UNPAID_PROVIDERS = saved_unpaid
        _backends._PAYMENT_PREFLIGHT_CACHE.clear()
        _backends._PAYMENT_PREFLIGHT_CACHE.update(saved_preflight)
        _pf._CONFIG_OVERRIDES = saved_chain_overrides


@pytest.fixture(autouse=True)
def _isolate_log_and_stats_paths(tmp_path, monkeypatch):
    """Default EVERY test to a throwaway `$REVIEW_LOG_DIR` / `$REVIEW_STATS_FILE`.

    Root cause of the dashboard's "TOPIC: topic" / M1,M2 junk (Alex, 2026-09-02): most
    suites that drive the REAL `mode_brainstorm` deliberately redirect `REVIEW_LOG_DIR`
    to a throwaway temp dir per-test — but 5 call sites across `test_diff_cap.py` and
    `test_brainstorm_diff.py` called `mode_brainstorm(...)` with NO such override
    (`log_dir()` then falls back to the real per-user path, `~/Library/Logs/review-cli`).
    Every unisolated `pytest` run of this repo's OWN suite (run constantly across the
    many parallel worktrees while iterating — not any external scheduled job) wrote a
    real `{ts}-brainstorm.md` with a literal placeholder topic ("topic") and generic
    "m1"/"m2" model labels straight into the dashboard's live log dir. Those 5 call
    sites now redirect explicitly too (`tests/_brainstorm_env_isolation.py`, imported
    for its `_ISOLATED_ENV_KEYS`, is the single source of truth for which env vars that
    helper AND this fixture isolate — keep them in sync by editing that one list, not
    by hand-editing both places).

    This fixture closes the class, not just the known instances: every test now gets
    an isolated `REVIEW_LOG_DIR`/`REVIEW_STATS_FILE` by default, so a FUTURE test that
    forgets to redirect them can no longer touch the developer's real files. A test that
    still wants its own explicit temp dir (most already do) is unaffected — it just
    overrides this default within its own scope and monkeypatch restores this fixture's
    value afterward, same as it always restored the pre-test value.
    """
    for key in _ISOLATED_ENV_KEYS:
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
