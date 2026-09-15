"""Test-only host trust for the console's request guard (ADR-0021), and the
setup marker most page tests run behind.

The guard trusts loopback hosts by default. FastAPI's ``TestClient`` speaks as
``Host: testserver``, which is not loopback, so the suite names it in
``CR_TRUSTED_HOSTS`` — exactly the operator escape hatch a reverse-proxy setup
uses (the guard docs in :mod:`clear_record.web.guard`). The loopback default is
exercised directly, with an explicit ``trusted_hosts=()``, in
``test_web_guard.py``.

A fresh install now redirects ``/`` to ``/setup`` (ticket 04), so the ordinary
page tests record the version marker and play a returning user. Tests that
exercise the first run or the update notice opt out with the
``own_setup_marker`` marker and arrange their own.
"""

from __future__ import annotations

import pytest

from clear_record.service import setup


@pytest.fixture(autouse=True)
def _trust_the_test_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CR_TRUSTED_HOSTS", "testserver")


@pytest.fixture(autouse=True)
def _setup_marker_seen(request, _hermetic_english_environment) -> None:
    """Record the current version so page tests behave as a returning user."""
    if request.node.get_closest_marker("own_setup_marker"):
        return
    setup.update_setup_state(seen_version=setup.current_version())
