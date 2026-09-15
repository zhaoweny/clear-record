"""The console's agent-setup panel (ADR-0018's onboarding half, ticket 20).

The point of these tests is the state the ticket calls out: with nothing
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

from fastapi.testclient import TestClient

from clear_record.cli.auto import Message
from clear_record.service import Registry, paths, setup
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
    assert 'id="agent-setup"' in client.get("/").text


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


def test_nothing_running_says_what_to_install(tmp_path, monkeypatch) -> None:
    detection = setup.detect(candidates=(FAKE,), opener=_refused)
    monkeypatch.setattr(web_app, "detect", lambda: detection)
    client = _client(tmp_path)

    panel = client.get("/ui/agent-setup/detect").text

    assert "not running" in panel
    assert "could not reach" in panel
    assert "ollama.com" in panel  # the plain instruction the ticket asks for


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

    assert response.status_code == 400
    assert "endpoint refused the test call" in response.text
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
