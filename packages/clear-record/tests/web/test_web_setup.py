"""Setup as system readiness: the version marker, first run, and update.

Setup never blocks a returning user (ADR-0027). The marker lives in the
service's own record (`<state>/agent-setup.json`) and is the *only* thing that
distinguishes a first run from an update; the console just reads it and writes it
on DISMISS or COMPLETE. These tests drive the real app, and the ordinary page
tests run behind the conftest marker fixture so they stay a returning user.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clear_record.service import Registry, setup
from clear_record.service.agent_flow import (
    LEG_BACKEND,
    LEG_MODEL,
    LEG_OK,
    TranscriptionStatus,
)
from clear_record.web import app as web_app
from clear_record.web.app import create_app


def _app(tmp_path, **kwargs):
    return create_app(
        Registry.open(db_path=tmp_path / "registry.sqlite3"),
        trusted_hosts=("testserver",),
        **kwargs,
    )


def _client(tmp_path, **kwargs) -> TestClient:
    return TestClient(_app(tmp_path, **kwargs), follow_redirects=False)


# --- first run --------------------------------------------------------------- #


@pytest.mark.own_setup_marker
def test_a_fresh_install_lands_on_setup(tmp_path) -> None:
    """No marker and no projects: the one time `/` redirects."""
    client = _client(tmp_path)

    response = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


@pytest.mark.own_setup_marker
def test_a_first_run_with_a_project_stays_on_projects(tmp_path) -> None:
    """A project is already a reason to be in the workspace, marker or not."""
    client = _client(tmp_path)
    assert client.post("/api/projects", json={"name": "Ops"}).status_code == 201

    response = client.get("/")

    assert response.status_code == 200
    assert "Ops" in response.text


@pytest.mark.own_setup_marker
def test_visiting_or_skipping_records_nothing(tmp_path) -> None:
    """Only DISMISS or COMPLETE writes the marker; a look-around must not."""
    client = _client(tmp_path)

    assert client.get("/setup").status_code == 200
    assert client.get("/setup?reason=update").status_code == 200
    assert client.get("/").status_code == 303

    assert setup.read_setup_state() == {}
    assert setup.setup_incomplete() is True


# --- the marker drives the nav link and the update notice -------------------- #


@pytest.mark.own_setup_marker
def test_the_setup_link_shows_until_the_marker_is_recorded(tmp_path) -> None:
    client = _client(tmp_path)

    assert 'href="/setup"' in client.get("/setup").text

    setup.record_seen_version()

    assert 'href="/setup"' not in client.get("/setup").text


@pytest.mark.own_setup_marker
def test_the_update_notice_links_to_setup_and_dismiss_records_the_marker(
    tmp_path,
) -> None:
    setup.update_setup_state(seen_version="0.0.0-old")
    client = _client(tmp_path)

    home = client.get("/")

    assert home.status_code == 200
    assert "setup-notice" in home.text
    assert 'href="/setup?reason=update"' in home.text
    assert 'href="/setup"' in home.text  # the nav link: still incomplete

    dismissed = client.post("/setup/dismiss")

    assert dismissed.status_code == 303
    assert dismissed.headers["location"] == "/"
    assert setup.seen_version() == setup.current_version()
    # The notice and the nav link are gone once the marker names this version.
    assert "setup-notice" not in client.get("/").text
    assert 'href="/setup"' not in client.get("/").text


@pytest.mark.own_setup_marker
def test_completing_the_wizard_records_the_marker(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.post("/setup/complete")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert setup.seen_version() == setup.current_version()
    assert setup.setup_incomplete() is False


@pytest.mark.own_setup_marker
def test_a_first_run_shows_the_setup_link_but_no_update_notice(tmp_path) -> None:
    """No marker is first run, not update: the Setup link, never the notice."""
    page = _client(tmp_path).get("/setup").text

    assert 'href="/setup"' in page
    assert "setup-notice" not in page


# --- the steps --------------------------------------------------------------- #


def test_the_setup_page_renders_the_numbered_steps(tmp_path) -> None:
    page = TestClient(_app(tmp_path)).get("/setup").text

    assert page.count('class="setup-step"') == 4
    for step in ("setup-welcome", "setup-transcription", "setup-agent", "setup-try"):
        assert f'id="{step}"' in page
    for label in ("Welcome", "Next: Transcription", "Next: Agent", "Next: Try it"):
        assert label in page
    # Every step is skippable: setup never blocks the workspace.
    assert page.count(">Skip<") >= 3
    # The agent step is the one shared panel, mounted by the same htmx URL.
    assert 'id="agent-setup"' in page
    assert 'hx-get="/ui/agent-setup"' in page
    # Try it is ticket 05: it points at the one acceptance test (the flow's Try it
    # stage) and the permanent copy in Settings -> Status.
    assert "Run it in the Agent step" in page
    assert 'href="/settings/status"' in page
    assert "/setup/complete" in page
    assert "/setup/dismiss" in page


# --- the Transcription step states readiness from the service ---------------- #


def _status(state, *, backend="apple", model=None, models_present=()):
    return TranscriptionStatus(
        state=state,
        backend=backend,
        model=model,
        models_dir="/home/u/models",
        models_present=models_present,
    )


def test_the_transcription_step_says_a_model_free_backend_needs_no_checkpoint(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        web_app,
        "transcription_status",
        lambda *args, **kwargs: _status(LEG_OK, backend="apple-speech"),
    )

    page = TestClient(_app(tmp_path)).get("/setup").text

    assert 'id="setup-transcription"' in page
    assert "Transcription is ready here: the apple-speech backend" in page
    assert "needs no checkpoint on this system" in page
    # The old step conflated the ASR checkpoint with the Agent step's LLM and
    # claimed read-only Models settings could fetch one. Neither may return.
    assert "The Agent step can pull" not in page
    assert "fetch one from Models settings" not in page


def test_the_transcription_step_shows_the_checkpoint_for_a_ggml_backend(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        web_app,
        "transcription_status",
        lambda *args, **kwargs: _status(LEG_OK, model="/home/u/models/ggml-small.bin"),
    )

    page = TestClient(_app(tmp_path)).get("/setup").text

    assert "Transcription is ready here: the apple backend" in page
    assert "Checkpoint on disk: /home/u/models/ggml-small.bin." in page


def test_the_transcription_step_names_a_missing_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        web_app,
        "transcription_status",
        lambda *args, **kwargs: _status(LEG_BACKEND, backend=None),
    )

    page = TestClient(_app(tmp_path)).get("/setup").text

    assert "No ASR backend is available on this machine yet" in page
    assert "/home/u/models" in page


def test_the_transcription_step_offers_to_download_a_missing_checkpoint(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        web_app,
        "transcription_status",
        lambda *args, **kwargs: _status(LEG_MODEL, models_present=("ggml-small.bin",)),
    )

    page = TestClient(_app(tmp_path)).get("/setup").text

    assert "The apple backend needs a model checkpoint" in page
    assert "Download it here" in page
    # The remediation is explicit and user-triggered: a button that reuses the
    # pinned downloader, never a promise that some later run will fetch it.
    assert 'hx-post="/ui/setup/download-model"' in page
    assert "Download the model" in page
    # The select names what is fetched, not a persisted model choice.
    assert "Checkpoint to download" in page
    assert '<select name="model">' in page
    assert "Models on disk: ggml-small.bin" in page
    assert "No model checkpoints are on disk yet." not in page


def test_a_ready_step_offers_no_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        web_app,
        "transcription_status",
        lambda *args, **kwargs: _status(LEG_OK, model="/home/u/models/ggml-small.bin"),
    )

    page = TestClient(_app(tmp_path)).get("/setup").text

    assert 'hx-post="/ui/setup/download-model"' not in page


def test_downloading_the_default_model_refreshes_the_step_to_ready(
    tmp_path, monkeypatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        web_app,
        "download_transcription_model",
        lambda *args, **kwargs: calls.append(1) or "",
    )

    def status(*args, **kwargs):
        # Model state before the click, ready after it.
        if calls:
            return _status(LEG_OK, model="/home/u/models/ggml-small.bin")
        return _status(LEG_MODEL)

    monkeypatch.setattr(web_app, "transcription_status", status)

    response = TestClient(_app(tmp_path)).post("/ui/setup/download-model")

    assert response.status_code == 200
    assert calls == [1]
    assert "Transcription is ready here" in response.text
    assert "Checkpoint on disk: /home/u/models/ggml-small.bin." in response.text


def test_downloading_the_model_offloads_the_synchronous_fetch(
    tmp_path, monkeypatch
) -> None:
    """The pinned fetch can run for minutes; it must not hold the event loop.

    transcription_status renders the refreshed step after the fetch and runs
    on the event loop, so it names the loop thread for comparison.
    """
    import threading

    seen: dict[str, threading.Thread] = {}

    def fake_download(*args, **kwargs):
        seen["download"] = threading.current_thread()
        return ""

    def fake_status(*args, **kwargs):
        seen["status"] = threading.current_thread()
        return _status(LEG_MODEL)

    monkeypatch.setattr(web_app, "download_transcription_model", fake_download)
    monkeypatch.setattr(web_app, "transcription_status", fake_status)

    response = TestClient(_app(tmp_path)).post("/ui/setup/download-model")

    assert response.status_code == 200
    assert seen["download"] is not seen["status"]


def test_a_failed_download_is_shown_in_the_step(tmp_path, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr(web_app, "download_transcription_model", boom)
    monkeypatch.setattr(
        web_app, "transcription_status", lambda *args, **kwargs: _status(LEG_MODEL)
    )

    response = TestClient(_app(tmp_path)).post("/ui/setup/download-model")

    assert response.status_code == 200
    assert "The download did not finish: RuntimeError: no network" in response.text


def test_the_update_reason_shows_the_update_copy(tmp_path) -> None:
    setup.update_setup_state(seen_version="0.0.0-old")

    page = TestClient(_app(tmp_path)).get("/setup?reason=update").text

    assert "setup-update" in page
    assert "was updated to" in page


def test_the_setup_page_mounts_the_same_agent_panel_as_settings(tmp_path) -> None:
    """One agent implementation, two entry points; /setup/agent is the same page."""
    client = TestClient(_app(tmp_path))

    for path in ("/setup", "/setup/agent", "/settings/agent"):
        page = client.get(path).text
        assert 'id="agent-setup"' in page, path
        assert 'hx-get="/ui/agent-setup"' in page, path


@pytest.mark.own_setup_marker
def test_status_offers_walking_setup_again(tmp_path) -> None:
    """Walk setup again forgets the marker, so the nav Setup link returns."""
    setup.record_seen_version()
    client = _client(tmp_path)

    page = client.get("/settings/status").text
    assert "/setup/restart" in page
    assert "Open the setup wizard" in page

    response = client.post("/setup/restart")

    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    assert setup.seen_version() is None
