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
