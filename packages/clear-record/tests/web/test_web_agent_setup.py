"""The console's agent-setup panel (ADR-0018's onboarding half).

The point of these tests is the state that panel is about: with nothing
configured the console says so **plainly**, with a way to fix it, instead of
letting an agent task fail opaquely. The detect and record routes are driven with
the service's own functions against the in-process fake endpoint from the
service suite (``tests/service/test_setup.py``), so the panel is exercised over
real probe results rather than hand-written fixtures.

The credential rule is asserted at the surface too: the value of the variable a
hosted endpoint names never appears in the panel, the JSON view or the config
file — only the variable's name.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from clear_record.cli.auto import Message
from clear_record.core import paths
from clear_record.service import Registry, setup
from clear_record.web import app as web_app
from clear_record.web.app import create_app


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class FakeEndpoint:
    """The same minimal fake the service suite drives (kept local, no import)."""

    def __init__(self, *, models: tuple[str, ...] = ("qwen2.5:1.5b",)) -> None:
        self.models = models

    def opener(self, request, timeout=None) -> _Response:
        url = request.full_url
        if url.endswith("/v1/models"):
            return _Response({"data": [{"id": name} for name in self.models]})
        if url.endswith("/api/tags"):
            return _Response({"models": [{"name": name} for name in self.models]})
        if url.endswith("/v1/chat/completions"):
            return _Response({"choices": [{"message": {"content": "ok"}}]})
        raise AssertionError(f"unexpected call to {url}")


def _refused(request, timeout=None):
    raise OSError("connection refused")


FAKE = setup.EndpointCandidate(
    slug="ollama",
    label="Ollama",
    base_url="http://fake.test:11434/v1",
    install_hint=setup.LOCAL_CANDIDATES[0].install_hint,
)


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(Registry.open(db_path=tmp_path / "r.sqlite3")))


def _panel(client: TestClient) -> str:
    response = client.get("/ui/agent-setup")
    assert response.status_code == 200, response.text
    return response.text


# --- the "nothing configured" state ----------------------------------------- #


def test_the_console_shows_plainly_that_no_endpoint_is_configured(tmp_path) -> None:
    client = _client(tmp_path)

    panel = _panel(client)

    assert "no endpoint configured" in panel
    assert "No agent endpoint is configured yet, so agent tasks cannot run." in panel
    # ...and a way to fix it: an address the user can type, plus the detect action.
    assert "/ui/agent-setup/use" in panel
    assert "/ui/agent-setup/detect" in panel
    # The state is on the page that loads the panel (htmx may not have run).
    assert 'id="agent-setup"' in client.get("/settings/agent").text


def test_the_json_api_starts_not_configured(tmp_path) -> None:
    view = _client(tmp_path).get("/api/agent/setup").json()

    assert view["state"] == "not_configured"
    assert view["configured"] is False
    assert view["endpoint"] is None
    assert view["detection"] is None


# --- detect ------------------------------------------------------------------ #


def test_detecting_a_fake_endpoint_shows_it_verified_and_usable(
    tmp_path, monkeypatch
) -> None:
    fake = FakeEndpoint()
    detection = setup.detect(candidates=(FAKE,), opener=fake.opener)
    monkeypatch.setattr(web_app, "detect", lambda: detection)
    client = _client(tmp_path)

    panel = client.get("/ui/agent-setup/detect").text

    assert "Ollama" in panel
    assert "verified" in panel
    assert "qwen2.5:1.5b" in panel
    assert "Verify and use" in panel
    assert "Pull a small model" in panel  # the native tags endpoint answered
    # The probed endpoint rides in hx-vals as JSON (tojson), not interpolated by
    # hand, so a quote in a URL cannot break the attribute.
    assert '{"endpoint": "http://fake.test:11434/v1"}' in panel


def test_nothing_running_says_what_to_install(tmp_path, monkeypatch) -> None:
    detection = setup.detect(candidates=(FAKE,), opener=_refused)
    monkeypatch.setattr(web_app, "detect", lambda: detection)
    client = _client(tmp_path)

    panel = client.get("/ui/agent-setup/detect").text

    assert "not running" in panel
    assert "could not reach" in panel
    assert "ollama.com" in panel  # the plain instruction the step asks for


# --- verify and record ------------------------------------------------------- #


def _verifier(fake: FakeEndpoint, environ: dict):
    def verify(endpoint, *, model=None, api_key_env=None, **_kwargs):
        return setup.verify_endpoint(
            endpoint,
            model=model,
            api_key_env=api_key_env,
            opener=fake.opener,
            environ=environ,
        )

    return verify


def test_verify_and_record_writes_the_config_and_shows_it_ready(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(web_app, "verify_endpoint", _verifier(FakeEndpoint(), {}))
    client = _client(tmp_path)

    response = client.post(
        "/ui/agent-setup/use",
        data={"endpoint": FAKE.base_url, "model": "qwen2.5:1.5b", "api_key_env": ""},
    )

    assert response.status_code == 200
    assert "Recorded" in response.text
    assert "agent ready" in response.text
    # The runner's own resolver reads back exactly what the console recorded.
    config = paths.config_path()
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert setup.MANAGED_BEGIN in text
    view = client.get("/api/agent/setup").json()
    assert view["state"] == "ready"
    assert view["endpoint"] == FAKE.base_url
    assert view["model"] == "qwen2.5:1.5b"


def test_a_failed_test_call_records_nothing(tmp_path, monkeypatch) -> None:
    def refusing(endpoint, *, model=None, api_key_env=None, **_kwargs):
        return setup.Verification(
            ok=False,
            endpoint=endpoint,
            model=model,
            detail=Message("endpoint refused the test call"),
        )

    monkeypatch.setattr(web_app, "verify_endpoint", refusing)
    client = _client(tmp_path)

    response = client.post(
        "/ui/agent-setup/use", data={"endpoint": FAKE.base_url, "model": ""}
    )

    # 200, not 400: base.html sets htmx noSwap for every 4xx, so a 400 panel
    # would never be swapped in and the user would see nothing.
    assert response.status_code == 200
    assert "endpoint refused the test call" in response.text
    assert 'class="run-error"' in response.text
    assert not paths.config_path().is_file()
    assert client.get("/api/agent/setup").json()["state"] == "not_configured"


def test_a_key_value_never_reaches_the_console_or_the_config(
    tmp_path, monkeypatch
) -> None:
    """The endpoint names its variable; the value stays in the environment."""
    sentinel = "s3cret-value-must-not-appear"
    monkeypatch.setattr(
        web_app,
        "verify_endpoint",
        _verifier(FakeEndpoint(), {"CR_SETUP_SECRET": sentinel}),
    )
    client = _client(tmp_path)

    response = client.post(
        "/ui/agent-setup/use",
        data={
            "endpoint": "https://hosted.test/v1",
            "model": "m",
            "api_key_env": "CR_SETUP_SECRET",
        },
    )
    panel = response.text + client.get("/api/agent/setup").text + _panel(client)
    config = paths.config_path().read_text(encoding="utf-8")

    assert response.status_code == 200
    assert sentinel not in panel
    assert sentinel not in config
    assert "CR_SETUP_SECRET" in config  # the NAME is recorded, and only the name
    # The panel says where the key comes from without ever holding it.
    assert "CR_SETUP_SECRET" in client.get("/api/agent/setup").json()["api_key_env"]


def test_recording_the_endpoint_reaches_a_meeting_without_a_restart(
    tmp_path, monkeypatch
) -> None:
    """A settings write refreshes the app's pinned config in place.

    ``write_agent_settings`` drops the process-wide default, but the app pinned
    the old object at startup. Without a refresh the setup panel reads "agent
    ready" from the config file while a meeting still reports "No agent is
    configured." until the server restarts -- the novice's dead end.
    """
    from clear_record.service import reset_default_config

    monkeypatch.setattr(web_app, "verify_endpoint", _verifier(FakeEndpoint(), {}))
    reset_default_config()
    client = _client(tmp_path)
    client.post("/api/projects", json={"name": "Ops"})
    meeting = client.post(
        "/api/projects/ops/meetings",
        json={"title": "Kickoff", "workspace_path": str(tmp_path)},
    ).json()
    agent_url = f"/api/meetings/{meeting['id']}/agent"
    assert client.get(agent_url).json()["configured"] is False

    response = client.post(
        "/ui/agent-setup/use",
        data={"endpoint": FAKE.base_url, "model": "qwen2.5:1.5b", "api_key_env": ""},
    )

    assert response.status_code == 200
    assert client.get(agent_url).json()["configured"] is True


def test_a_config_problem_is_rendered_verbatim(tmp_path, monkeypatch) -> None:
    """The service's own config-problem text reaches the panel.

    A config problem is a runtime string (it embeds the config path), not an
    extractable message ID, so the template shows it verbatim rather than
    wrapping it in a tr() lookup that could never match.
    """
    view = setup.SetupView(state="problem", problems=("a bad [agent] table",))
    monkeypatch.setattr(web_app, "setup_view", lambda **_kwargs: view)

    panel = _panel(_client(tmp_path))

    assert "a bad [agent] table" in panel


# --- the MCP rung: point at a harness, write its client config --------------- #


def _executable(path: Path) -> Path:
    """A file ``resolve_harness`` will accept: a regular, executable file."""
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


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
    # A harness is a setup fact, not agent plumbing: the TOML keeps no trace.
    assert not paths.config_path().is_file()


def test_pointing_at_something_unrunnable_records_nothing(tmp_path) -> None:
    client = _client(tmp_path)
    plain = tmp_path / "pi-agent"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    plain.chmod(0o644)

    response = client.post("/ui/agent-setup/mcp/harness", data={"harness": str(plain)})

    # 200, not 400, for the same noSwap reason as the endpoint verification.
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
