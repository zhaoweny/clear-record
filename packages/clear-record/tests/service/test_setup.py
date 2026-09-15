"""Guided agent setup: detection and verification against a fake endpoint.

Every probe here is driven through an injected ``opener`` (the
:class:`~clear_record.service.agent.EndpointRunner` seam), so the whole detect →
verify → record path is exercised with **no server running and no network**. The
fake answers the two protocols the setup speaks — OpenAI-compatible
``/v1/models`` and ``/v1/chat/completions``, plus Ollama's native ``/api/tags``
and ``/api/pull`` — so the same code that talks to a real Ollama is what the
tests drive.

Two rules get their own tests rather than a comment: an unreachable address is a
*result* carrying the install hint (never an exception), and a credential is only
ever a variable **name** — a value in the environment never reaches the config,
the state file or the JSON view.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from clear_record.service import setup
from clear_record.service.agent import load_agent_config
from clear_record.service.archive import tool_version

CANDIDATE = setup.EndpointCandidate(
    slug="ollama",
    label="Ollama",
    base_url="http://fake.test:11434/v1",
    install_hint=setup.LOCAL_CANDIDATES[0].install_hint,
)


class _Response:
    """The slice of an ``http.client`` response the setup reads."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class FakeEndpoint:
    """A minimal OpenAI-compatible server that also speaks Ollama's native API."""

    def __init__(
        self,
        *,
        models: tuple[str, ...] = ("qwen2.5:1.5b",),
        openai_models: bool = True,
        native_tags: bool = True,
        reply: str = "ok",
        chat_status: int | None = None,
        pull_error: str | None = None,
        native_pull: bool = True,
    ) -> None:
        self.models = models
        self.openai_models = openai_models
        self.native_tags = native_tags
        self.reply = reply
        self.chat_status = chat_status
        self.pull_error = pull_error
        self.native_pull = native_pull
        self.calls: list[tuple[str, str]] = []

    def opener(self, request, timeout=None) -> _Response:
        url = request.full_url
        method = request.get_method()
        self.calls.append((method, url))
        if url.endswith("/v1/models"):
            if not self.openai_models:
                raise self._not_found(url)
            return _Response({"data": [{"id": name} for name in self.models]})
        if url.endswith("/api/tags"):
            if not self.native_tags:
                raise self._not_found(url)
            return _Response({"models": [{"name": name} for name in self.models]})
        if url.endswith("/v1/chat/completions"):
            if self.chat_status is not None:
                raise urllib.error.HTTPError(
                    url, self.chat_status, "boom", {}, io.BytesIO(b"model overloaded")
                )
            return _Response({"choices": [{"message": {"content": self.reply}}]})
        if url.endswith("/api/pull"):
            if not self.native_pull:
                raise self._not_found(url)
            if self.pull_error is not None:
                return _Response({"error": self.pull_error})
            return _Response({"status": "success"})
        raise self._not_found(url)

    @staticmethod
    def _not_found(url: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url, 404, "not found", {}, io.BytesIO(b"no such route")
        )


def _refused(request, timeout=None):
    raise OSError("connection refused")


# --- detection --------------------------------------------------------------- #


def test_a_running_server_is_detected_and_verified_with_a_test_call() -> None:
    """The whole rung: reachable, model listed, and a real completion answered."""
    fake = FakeEndpoint()

    detection = setup.detect(candidates=(CANDIDATE,), opener=fake.opener)

    probe = detection.probes[0]
    assert probe.reachable is True
    assert probe.models == ("qwen2.5:1.5b",)
    assert probe.pull_supported is True  # the native tags endpoint answered
    assert probe.verified is True, probe.verify_detail
    assert probe.verified_model == "qwen2.5:1.5b"
    # A test call really happened, through the same ``/chat/completions`` route
    # the tasks use — a model list alone would not be verification.
    assert ("POST", "http://fake.test:11434/v1/chat/completions") in fake.calls
    assert detection.best is probe


def test_nothing_running_is_a_result_with_what_to_install() -> None:
    """No server is the common case, and it must read as an instruction."""
    detection = setup.detect(candidates=(CANDIDATE,), opener=_refused)

    probe = detection.probes[0]
    assert probe.reachable is False
    assert probe.verified is False
    assert detection.best is None
    assert "could not reach" in str(probe.detail)
    assert "connection refused" in str(probe.detail)
    # The hint is the plain "what to install or run" the ticket asks for.
    assert "ollama.com" in probe.candidate.install_hint


def test_a_server_that_serves_no_model_is_reachable_but_not_verified() -> None:
    """An empty model list is a fixable state (pull one), not a failure to hide."""
    fake = FakeEndpoint(models=(), openai_models=True, native_tags=True)

    detection = setup.detect(candidates=(CANDIDATE,), opener=fake.opener)

    probe = detection.probes[0]
    assert probe.reachable is True
    assert probe.models == ()
    assert probe.verified is False
    assert probe.pull_supported is True
    # No model means no test call to make; nothing was invented.
    assert ("POST", "http://fake.test:11434/v1/chat/completions") not in fake.calls


def test_a_verification_failure_is_reported_not_swallowed() -> None:
    """A server that answers on /models but fails the call is not called usable."""
    fake = FakeEndpoint(chat_status=500)

    detection = setup.detect(candidates=(CANDIDATE,), opener=fake.opener)

    probe = detection.probes[0]
    assert probe.reachable is True
    assert probe.verified is False
    assert probe.verify_detail is not None
    assert "HTTP 500" in str(probe.verify_detail)
    assert detection.best is probe  # still offered, but flagged unverified


def test_a_users_own_endpoint_is_probed_first() -> None:
    """Setup never offers to replace a working endpoint with a discovered one."""
    fake = FakeEndpoint()

    detection = setup.detect(
        candidates=(CANDIDATE,),
        endpoint="http://mine.test:9999/v1",
        opener=fake.opener,
        verify=False,
    )

    assert detection.probes[0].slug == setup.CUSTOM_SLUG
    assert detection.probes[0].reachable is True
    assert detection.probes[0].models == ("qwen2.5:1.5b",)


# --- verification and the key rule ------------------------------------------ #


def test_verify_fails_closed_when_a_named_key_variable_is_unset() -> None:
    """A named but unset variable refuses rather than calling unauthenticated."""
    fake = FakeEndpoint()

    result = setup.verify_endpoint(
        "http://fake.test:11434/v1",
        model="qwen2.5:1.5b",
        api_key_env="CR_SETUP_TEST_KEY",
        opener=fake.opener,
        environ={},
    )

    assert result.ok is False
    assert "CR_SETUP_TEST_KEY" in str(result.detail)  # the NAME, never a value
    assert fake.calls == []  # nothing was sent


def test_a_local_endpoint_needs_no_key_at_all() -> None:
    """With no ``api_key_env`` no auth header is required and the call proceeds."""
    seen: dict = {}

    def opener(request, timeout=None):
        seen["auth"] = request.get_header("Authorization")
        return _Response({"choices": [{"message": {"content": "ok"}}]})

    result = setup.verify_endpoint(
        "http://fake.test:11434/v1", model="m", opener=opener
    )

    assert result.ok is True
    assert seen["auth"] is None


# --- pulling a small model --------------------------------------------------- #


def test_pull_model_uses_the_native_api_and_reports_a_refusal() -> None:
    fake = FakeEndpoint()
    pulled = setup.pull_model(
        "qwen2.5:1.5b", endpoint="http://fake.test:11434/v1", opener=fake.opener
    )
    assert pulled.ok is True
    assert ("POST", "http://fake.test:11434/api/pull") in fake.calls

    refusing = FakeEndpoint(pull_error="model not found")
    failed = setup.pull_model(
        "nope", endpoint="http://fake.test:11434/v1", opener=refusing.opener
    )
    assert failed.ok is False
    assert "model not found" in str(failed.detail)


def test_pull_against_a_server_without_the_native_api_is_refused() -> None:
    """LM Studio and llama.cpp have no pull route; the failure names the URL."""
    fake = FakeEndpoint(native_pull=False)

    failed = setup.pull_model(
        "qwen2.5:1.5b", endpoint="http://fake.test:1234/v1", opener=fake.opener
    )

    assert failed.ok is False
    assert "HTTP 404" in str(failed.detail)


# --- recording the choice (config write) ------------------------------------- #


def test_settings_are_written_to_a_managed_block_and_read_back(tmp_path) -> None:
    config = tmp_path / "config.toml"

    written = setup.write_agent_settings(
        "http://127.0.0.1:11434/v1", model="qwen2.5:1.5b", config_file=config
    )

    assert written == config
    text = config.read_text(encoding="utf-8")
    assert setup.MANAGED_BEGIN in text and setup.MANAGED_END in text
    # The runner reads the written plumbing back through its one resolver.
    resolved = load_agent_config(environ={}, config_file=config)
    assert resolved.endpoint == "http://127.0.0.1:11434/v1"
    assert resolved.model == "qwen2.5:1.5b"
    assert resolved.api_key_env is None
    assert resolved.ok


def test_a_second_write_replaces_only_the_managed_block(tmp_path) -> None:
    """A user's own tables and comments survive setup re-running."""
    config = tmp_path / "config.toml"
    config.write_text('[paths]\ndata_dir = "/tmp/x"  # mine\n', encoding="utf-8")

    setup.write_agent_settings("http://a.test/v1", model="one", config_file=config)
    setup.write_agent_settings("http://b.test/v1", model="two", config_file=config)

    text = config.read_text(encoding="utf-8")
    assert "# mine" in text  # untouched
    assert text.count("[agent]") == 1  # not appended twice
    assert "http://b.test/v1" in text and "http://a.test/v1" not in text


def test_a_hand_written_agent_table_is_refused_not_clobbered(tmp_path) -> None:
    """Setup will not append a second [agent] table to the user's own."""
    config = tmp_path / "config.toml"
    config.write_text('[agent]\nendpoint = "http://mine.test/v1"\n', encoding="utf-8")

    with pytest.raises(setup.SetupError) as excinfo:
        setup.write_agent_settings("http://other.test/v1", config_file=config)

    assert "already has an [agent] table" in str(excinfo.value)
    assert config.read_text(encoding="utf-8").count("[agent]") == 1


def test_a_key_is_only_ever_a_variable_name(tmp_path, monkeypatch) -> None:
    """A value in the environment never reaches the config or the state file."""
    monkeypatch.setenv("CR_SETUP_TEST_KEY", "s3cret-value-must-not-appear")
    config = tmp_path / "config.toml"

    setup.write_agent_settings(
        "https://hosted.test/v1",
        model="m",
        api_key_env="CR_SETUP_TEST_KEY",
        config_file=config,
    )

    text = config.read_text(encoding="utf-8")
    assert 'api_key_env = "CR_SETUP_TEST_KEY"' in text  # the NAME is recorded
    assert "s3cret-value-must-not-appear" not in text


# --- MCP rung: point at a harness, write its client config ------------------- #


def test_find_harness_reports_what_is_on_path() -> None:
    found = setup.find_harness(
        ("pi-agent", "other"),
        which=lambda name: "/usr/bin/pi-agent" if name == "pi-agent" else None,
    )

    assert [h.path for h in found] == ["/usr/bin/pi-agent", None]
    assert found[0].found is True
    assert found[1].found is False


def test_resolve_harness_refuses_something_that_is_not_runnable(tmp_path) -> None:
    plain = tmp_path / "pi-agent"
    plain.write_text("#!/bin/sh\n", encoding="utf-8")
    plain.chmod(0o644)

    with pytest.raises(setup.SetupError):
        setup.resolve_harness(plain)

    plain.chmod(0o755)
    harness = setup.resolve_harness(plain)
    assert harness.found is True
    assert harness.path == str(plain)


def test_mcp_config_registers_the_server_and_preserves_others(tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}}}), encoding="utf-8"
    )

    setup.write_mcp_config(config)

    document = json.loads(config.read_text(encoding="utf-8"))
    assert document["mcpServers"]["other"] == {"command": "other"}
    assert document["mcpServers"][setup.MCP_SERVER_NAME] == {
        "command": "clear-record",
        "args": ["mcp"],
    }


def test_the_setup_record_has_no_field_for_a_credential(tmp_path) -> None:
    """Only allow-listed, non-secret facts are persisted."""
    record = setup.update_setup_state(path=tmp_path / "state.json", model="m")

    assert record == {"model": "m"}
    with pytest.raises(setup.SetupError):
        setup.update_setup_state(path=tmp_path / "state.json", api_key="value")


# --- the version marker (ticket 04) ------------------------------------------ #


def test_the_version_marker_is_an_allowed_non_secret_fact(tmp_path) -> None:
    record = tmp_path / "state.json"

    setup.record_seen_version(path=record, version="1.2.3")

    assert setup.read_setup_state(path=record) == {"seen_version": "1.2.3"}
    assert setup.seen_version(state={"seen_version": "1.2.3"}) == "1.2.3"
    assert setup.seen_version(state={}) is None
    # A key outside the allow-list is still refused, marker or not.
    with pytest.raises(setup.SetupError):
        setup.update_setup_state(path=record, seen="1.2.3")


def test_setup_incomplete_is_the_marker_compared_with_the_current_version() -> None:
    assert setup.setup_incomplete(state={}, version="1.2.3") is True
    seen = {"seen_version": "1.2.3"}
    assert setup.setup_incomplete(state=seen, version="1.2.3") is False
    assert setup.setup_incomplete(state=seen, version="1.2.4") is True


def test_the_current_version_comes_from_package_metadata() -> None:
    assert setup.current_version() == tool_version()


# --- the view ---------------------------------------------------------------- #


def test_the_view_says_plainly_when_nothing_is_configured(tmp_path) -> None:
    view = setup.setup_view(environ={}, config_file=tmp_path / "absent.toml", state={})

    assert view.state == setup.STATE_NOT_CONFIGURED
    assert view.configured is False
    assert view.endpoint is None
    assert view.problems == ()
    assert view.detection is None
    # The machine view carries the variable NAME (here, unset) and no value.
    assert view.api_key_env is None
    assert view.as_dict()["api_key_env"] is None


def test_the_view_is_ready_once_an_endpoint_is_recorded(tmp_path) -> None:
    config = tmp_path / "config.toml"
    setup.write_agent_settings(
        "http://127.0.0.1:11434/v1", model="m", config_file=config
    )

    view = setup.setup_view(environ={}, config_file=config, state={})

    assert view.state == setup.STATE_READY
    assert view.configured is True
    assert view.endpoint == "http://127.0.0.1:11434/v1"


def test_a_malformed_config_is_a_problem_state(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[agent\nendpoint = 1\n", encoding="utf-8")

    view = setup.setup_view(environ={}, config_file=config, state={})

    assert view.state == setup.STATE_PROBLEM
    assert view.problems


def test_the_candidate_list_names_the_three_local_servers() -> None:
    assert {candidate.slug for candidate in setup.LOCAL_CANDIDATES} == {
        "ollama",
        "lm_studio",
        "llama_cpp",
    }
    assert Path(setup.SETUP_FILENAME).name == "agent-setup.json"
