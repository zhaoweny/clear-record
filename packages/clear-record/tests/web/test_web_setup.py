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
    for step in ("setup-welcome", "setup-models", "setup-agent", "setup-first-record"):
        assert f'id="{step}"' in page
    for label in ("Welcome", "Next: Models", "Next: Agent", "Next: First record"):
        assert label in page
    # Every step is skippable: setup never blocks the workspace.
    assert page.count(">Skip<") >= 3
    # The agent step is the one shared panel, mounted by the same htmx URL.
    assert 'id="agent-setup"' in page
    assert 'hx-get="/ui/agent-setup"' in page
    # First record is ticket 05: it points at the one acceptance test (the flow's
    # Try it stage) and the permanent copy in Settings -> Status.
    assert "Run it in the Agent step" in page
    assert 'href="/settings/status"' in page
    assert "/setup/complete" in page
    assert "/setup/dismiss" in page


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
