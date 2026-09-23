"""The console's agent-setup panel (ADR-0031's onboarding half).

The point of these tests is the state that panel is about: with nothing set up
the console says so **plainly**, with the two rungs that fix it — point at a
harness, register the MCP server — instead of letting a draft fail opaquely.

There is no endpoint rung and no key here, and that absence is asserted: a page
that would prompt for a model or a credential is a bug now. What the panel does
report is the 0.2 ``[agent]`` configuration this version ignores.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from clear_record.core import paths
from clear_record.service import Registry, setup
from clear_record.web import app as web_app
from clear_record.web.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(Registry.open(db_path=tmp_path / "r.sqlite3")))


def _panel(client: TestClient) -> str:
    response = client.get("/ui/agent-setup")
    assert response.status_code == 200, response.text
    return response.text


def _executable(path: Path) -> Path:
    """A file ``resolve_harness`` will accept: a regular, executable file."""
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- the "nothing set up" state --------------------------------------------- #


def test_the_console_shows_plainly_that_no_harness_is_configured(tmp_path) -> None:
    client = _client(tmp_path)

    panel = _panel(client)

    assert "no harness configured" in panel
    # ...and the two rungs that fix it, and no endpoint form or key field.
    assert "/ui/agent-setup/mcp/harness" in panel
    assert "/ui/agent-setup/mcp/config" in panel
    assert "api_key_env" not in panel
    assert "/ui/agent-setup/use" not in panel
    # The state is on the page that loads the panel (htmx may not have run).
    assert 'id="agent-setup"' in client.get("/settings/agent").text


def test_the_json_api_starts_not_configured_and_holds_no_credential_field(
    tmp_path,
) -> None:
    view = _client(tmp_path).get("/api/agent/setup").json()

    assert view["state"] == "not_configured"
    assert view["ready"] is False
    assert view["harness"] is None
    assert view["mcp_config"] is None
    assert "endpoint" not in view and "api_key_env" not in view
    assert view["ignored"] == []


def test_the_panel_is_ready_once_both_paths_are_recorded(tmp_path) -> None:
    harness = _executable(tmp_path / "pi-agent")
    client_file = tmp_path / "mcp.json"
    client_file.write_text("{}", encoding="utf-8")
    setup.update_setup_state(harness=str(harness), mcp_config=str(client_file))
    client = _client(tmp_path)

    assert "agent ready" in _panel(client)
    assert client.get("/api/agent/setup").json()["state"] == "ready"


def test_a_recorded_path_that_is_gone_is_a_problem_state(tmp_path) -> None:
    setup.update_setup_state(harness=str(tmp_path / "gone"))
    client = _client(tmp_path)

    panel = _panel(client)

    assert "agent setup problem" in panel
    assert "not a file" in panel


def test_a_config_problem_is_rendered_verbatim(tmp_path, monkeypatch) -> None:
    """The service's own text reaches the panel.

    A setup problem is a runtime string (it embeds a path), not an extractable
    message ID, so the template shows it verbatim rather than wrapping it in a
    tr() lookup that could never match.
    """
    view = setup.SetupView(state="problem", problems=("a bad recorded path",))
    monkeypatch.setattr(web_app, "setup_view", lambda **_kwargs: view)

    panel = _panel(_client(tmp_path))

    assert "a bad recorded path" in panel


def test_the_panel_reports_the_ignored_0_2_agent_config(tmp_path) -> None:
    """An existing [agent] table is named on the page and left where it is."""
    hand_written = '[agent]\nendpoint = "http://hand.written/v1"\n'
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(hand_written, encoding="utf-8")

    panel = _panel(_client(tmp_path))

    assert "ignored" in panel
    assert "[agent]" in panel
    # The console neither migrated it nor asked for a key because of it.
    assert paths.config_path().read_text(encoding="utf-8") == hand_written


# --- the MCP rung: point at a harness, write its client config --------------- #


def test_the_panel_asks_for_the_mcp_locations_instead_of_guessing_them(
    tmp_path,
) -> None:
    """The harness path and the client-config path are both the user's choice.

    An empty config field is the assertion that matters: a pre-filled value here
    would be a location clear-record invented for an external client, and this
    repo defines none.
    """
    client = _client(tmp_path)

    panel = _panel(client)

    assert "/ui/agent-setup/mcp/harness" in panel
    assert "/ui/agent-setup/mcp/config" in panel
    assert 'name="config" value=""' in panel
    # The "download one" rung is honest about being a human action.
    assert "pi-agent is not bundled" in panel
    assert "will not download one for you" in panel


def test_the_panel_offers_a_harness_it_found_on_path(tmp_path, monkeypatch) -> None:
    found = setup.Harness(name="pi-agent", path="/opt/bin/pi-agent")
    monkeypatch.setattr(web_app, "find_harness", lambda: (found,))
    client = _client(tmp_path)

    panel = _panel(client)

    assert "/opt/bin/pi-agent" in panel
    assert "Point at this" in panel
    # The path rides in ``hx-vals`` (not an input the CSS scanner would read as a
    # utility class), so accepting the hint posts it back as JSON.
    assert 'hx-vals=\'{"harness": "/opt/bin/pi-agent"}\'' in panel


def test_pointing_at_a_harness_records_the_users_path(tmp_path) -> None:
    client = _client(tmp_path)
    harness = _executable(tmp_path / "pi-agent")

    response = client.post(
        "/ui/agent-setup/mcp/harness", data={"harness": str(harness)}
    )

    assert response.status_code == 200
    assert "Pointed at the agent harness" in response.text
    assert client.get("/api/agent/setup").json()["harness"] == str(harness)
    # A harness is a setup fact, not agent plumbing: no config file is written.
    assert not paths.config_path().is_file()


def test_pointing_at_something_unrunnable_records_nothing(tmp_path) -> None:
    client = _client(tmp_path)
    plain = tmp_path / "pi-agent"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    plain.chmod(0o644)

    response = client.post("/ui/agent-setup/mcp/harness", data={"harness": str(plain)})

    # 200, not 400: base.html sets htmx noSwap for every 4xx, so a 400 panel
    # would never be swapped in and the user would see nothing.
    assert response.status_code == 200
    assert "is not executable" in response.text
    assert client.get("/api/agent/setup").json()["harness"] is None


def test_the_mcp_config_is_written_only_where_the_user_chose(tmp_path) -> None:
    client = _client(tmp_path)
    chosen = tmp_path / "client" / "mcp.json"

    response = client.post("/ui/agent-setup/mcp/config", data={"config": str(chosen)})

    assert response.status_code == 200
    assert "Registered the clear-record MCP server" in response.text
    document = json.loads(chosen.read_text(encoding="utf-8"))
    assert document["mcpServers"][setup.MCP_SERVER_NAME] == {
        "command": "clear-record",
        "args": ["mcp"],
    }
    assert client.get("/api/agent/setup").json()["mcp_config"] == str(chosen)
    # Nothing beside the chosen file was created: no location was guessed.
    assert [path.name for path in chosen.parent.iterdir()] == ["mcp.json"]


def test_the_mcp_config_route_requires_a_path(tmp_path) -> None:
    """No path, no write — the console cannot silently fall back to a default."""
    client = _client(tmp_path)

    response = client.post("/ui/agent-setup/mcp/config", data={})

    assert response.status_code == 422
