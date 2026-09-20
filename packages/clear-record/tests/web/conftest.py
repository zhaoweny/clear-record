"""Test-only host trust for the console's request guard (ADR-0021), and the
setup marker most page tests run behind.

The guard trusts loopback hosts by default. FastAPI's ``TestClient`` speaks as
``Host: testserver``, which is not loopback, so the suite names it in
``CR_TRUSTED_HOSTS`` — exactly the operator escape hatch a reverse-proxy setup
uses (the guard docs in :mod:`clear_record.web.guard`). The loopback default is
exercised directly, with an explicit ``trusted_hosts=()``, in
``test_web_guard.py``.

A fresh install now redirects ``/`` to ``/setup``, so the ordinary
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


@pytest.fixture(autouse=True)
def _stub_transcription_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep setup-page renders off the real ASR backend probes.

    ``/setup`` now states transcription readiness from
    ``service.agent_flow.transcription_status()``. That call probes the machine
    (and can compile/run the Apple Speech helper), which is neither hermetic nor
    fast per test. The readiness-specific tests override this stub.
    """
    from clear_record.service.agent_flow import TranscriptionStatus
    from clear_record.web import app as web_app

    monkeypatch.setattr(
        web_app,
        "transcription_status",
        lambda *args, **kwargs: TranscriptionStatus(
            state="ok",
            backend="apple-speech",
            model=None,
            models_dir="/models",
            models_present=(),
        ),
    )


@pytest.fixture(autouse=True)
def _stub_backend_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the Meetings tab off the real provider probes.

    The run form derives its backend options from the service's
    ``available_backend_ids`` (it must show ``apple-speech`` here and must not
    show a backend this machine lacks). That probe can compile/run the Apple
    Speech helper, so pin it; a test that asserts the derivation overrides
    this with its own list.
    """
    from clear_record.web import app as web_app

    monkeypatch.setattr(
        web_app, "available_backend_ids", lambda: ("apple", "apple-speech")
    )
