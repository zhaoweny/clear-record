"""Test-only host trust for the console's request guard (ADR-0021).

The guard trusts loopback hosts by default. FastAPI's ``TestClient`` speaks as
``Host: testserver``, which is not loopback, so the suite names it in
``CR_TRUSTED_HOSTS`` — exactly the operator escape hatch a reverse-proxy setup
uses (the guard docs in :mod:`clear_record.web.guard`). The loopback default is
exercised directly, with an explicit ``trusted_hosts=()``, in
``test_web_guard.py``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _trust_the_test_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CR_TRUSTED_HOSTS", "testserver")
