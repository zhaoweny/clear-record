"""The agent flow: one implementation, two entry points, and the Try it check.

The flow is the same `#agent-setup` fragment at /setup/agent and /settings/agent,
and Settings -> Status hosts the same `#hello-check` POST. The service call is
monkeypatched so the **rendering** of both the success path and a finding is
asserted with no system voice, no ASR backend and no model.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fastapi.testclient import TestClient

from clear_record.pipeline.auto import Message
from clear_record.service import Registry
from clear_record.service.agent_flow import (
    LEG_OK,
    LEG_TTS,
    TRANSCRIPT_TOOL,
    HelloCheck,
)
from clear_record.service.hello_tape import HelloTape
from clear_record.web import app as web_app
from clear_record.web.app import create_app

STAGES = (
    ("agent-stage-harness", "Harness"),
    ("agent-stage-mcp", "MCP config"),
    ("agent-stage-try", "Try it"),
)


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            Registry.open(db_path=tmp_path / "r.sqlite3"),
            trusted_hosts=("testserver",),
        )
    )


def _ok() -> HelloCheck:
    return HelloCheck(
        leg=LEG_OK,
        message=Message(
            "the hello-world tape ran through ingest and transcription; the "
            "transcript is below"
        ),
        tape=HelloTape(
            path=Path("/state/hello-check/tape/hello-world-en.wav"),
            engine="stub",
            lang="en",
            phrase="Hello world.",
        ),
        backend="stub",
        transcript="00:00:00.000 [mic] hello world",
        segments=1,
        mcp_config="/home/u/mcp.json",
        harness="/usr/bin/pi-agent",
        tool=TRANSCRIPT_TOOL,
        entry={"command": "clear-record", "args": ["mcp"]},
    )


def _finding() -> HelloCheck:
    return HelloCheck(
        leg=LEG_TTS,
        message=Message(
            "no system voice is installed, so the hello-world tape could not be created"
        ),
        tool=TRANSCRIPT_TOOL,
        entry={"command": "clear-record", "args": ["mcp"]},
    )


# --- one flow, two entry points ---------------------------------------------- #


def test_both_entry_points_mount_the_same_flow(tmp_path) -> None:
    client = _client(tmp_path)

    for path in ("/settings/agent", "/setup/agent"):
        page = client.get(path).text
        assert 'id="agent-setup"' in page, path
        assert 'hx-get="/ui/agent-setup"' in page, path


def test_the_flow_has_the_three_numbered_stages(tmp_path) -> None:
    panel = _client(tmp_path).get("/ui/agent-setup").text

    assert 'class="agent-flow"' in panel
    assert panel.count('class="agent-stage"') == 3
    for stage_id, label in STAGES:
        assert f'id="{stage_id}"' in panel, stage_id
        assert label in panel, label
    # The Try it stage hosts the one check, posting to the one route.
    assert 'id="hello-check"' in panel
    assert 'hx-post="/ui/hello-check"' in panel


def test_the_setup_wizard_agent_step_embeds_the_flow(tmp_path) -> None:
    page = _client(tmp_path).get("/setup").text

    assert 'id="agent-setup"' in page
    assert 'hx-get="/ui/agent-setup"' in page
    # The Try it step points at the acceptance test, not a second copy.
    assert 'id="setup-try"' in page
    assert 'href="/settings/status"' in page
    assert "Hello-world check" in page


# --- the standalone MCP section is unchanged --------------------------------- #


def test_the_mcp_fragment_still_renders_standalone(tmp_path) -> None:
    client = _client(tmp_path)

    panel = client.get("/ui/agent-setup?part=mcp").text

    assert 'class="mcp-setup"' in panel
    assert 'class="mcp-harness"' in panel
    assert 'class="mcp-config"' in panel
    assert 'name="config" value=""' in panel
    assert "/ui/agent-setup/mcp/harness" in panel
    assert "/ui/agent-setup/mcp/config" in panel
    # Settings -> MCP still mounts exactly this fragment.
    page = client.get("/settings/mcp").text
    assert 'hx-get="/ui/agent-setup?part=mcp"' in page


# --- Try it: the success path and a finding ---------------------------------- #


def test_running_the_check_shows_the_transcript_and_the_mcp_leg(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(web_app, "run_hello_check", lambda **kwargs: _ok())
    client = _client(tmp_path)

    response = client.post("/ui/hello-check")

    assert response.status_code == 200
    assert 'data-leg="ok"' in response.text
    assert "00:00:00.000 [mic] hello world" in response.text
    # The exposed tool and the exact client entry are both on the result.
    assert "read_transcript" in response.text
    assert "clear-record mcp" in response.text
    # The run control survives the swap, so a returning user can re-run.
    assert 'hx-post="/ui/hello-check"' in response.text


def test_the_success_result_labels_only_what_the_check_proved(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(web_app, "run_hello_check", lambda **kwargs: _ok())

    text = _client(tmp_path).post("/ui/hello-check").text

    # Three plainly-labelled states: the local chain, the integration config,
    # and the round-trip the console cannot prove.
    assert "Transcription ready" in text
    assert "Agent integration configured" in text
    assert "Agent round-trip" in text
    # The round-trip is never a green badge from this check, and the copy says so.
    assert 'status-ok">Agent round-trip' not in text
    assert 'status-neutral">Agent round-trip' in text
    assert "Not verified by this check" in text
    assert "just agent-drive" in text
    # The badge that overclaimed the whole system is gone.
    assert "the whole system worked once" not in text


def test_a_missing_mcp_config_is_named_not_overclaimed(tmp_path, monkeypatch) -> None:
    result = dataclasses.replace(_ok(), mcp_config=None)
    monkeypatch.setattr(web_app, "run_hello_check", lambda **kwargs: result)

    text = _client(tmp_path).post("/ui/hello-check").text

    assert "Agent integration not configured" in text
    assert "No MCP client config is pointed at yet" in text
    assert "Agent integration configured" not in text


def test_a_missing_harness_is_named(tmp_path, monkeypatch) -> None:
    result = dataclasses.replace(_ok(), harness=None)
    monkeypatch.setattr(web_app, "run_hello_check", lambda **kwargs: result)

    text = _client(tmp_path).post("/ui/hello-check").text

    assert "Agent integration not configured" in text
    assert "No harness is recorded yet" in text


def test_a_missing_config_and_harness_are_both_named(tmp_path, monkeypatch) -> None:
    result = dataclasses.replace(_ok(), mcp_config=None, harness=None)
    monkeypatch.setattr(web_app, "run_hello_check", lambda **kwargs: result)

    text = _client(tmp_path).post("/ui/hello-check").text

    assert "Neither an MCP client config nor a harness is recorded yet" in text


def test_a_finding_names_the_leg_that_stopped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(web_app, "run_hello_check", lambda **kwargs: _finding())
    client = _client(tmp_path)

    response = client.post("/ui/hello-check")

    assert response.status_code == 200
    assert 'data-leg="tts"' in response.text
    assert "leg: tts" in response.text
    assert "no system voice is installed" in response.text


def test_the_check_is_skippable_and_the_status_copy_is_re_runnable(
    tmp_path,
) -> None:
    client = _client(tmp_path)

    status = client.get("/settings/status").text

    assert 'id="hello-check"' in status
    assert 'hx-post="/ui/hello-check"' in status
    assert "Run the check" in status
    # The idle state says nothing ran yet; no result is pre-rendered.
    assert "data-leg=" not in status


def test_the_check_language_follows_the_request_locale(tmp_path, monkeypatch) -> None:
    seen: dict = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return _ok()

    monkeypatch.setattr(web_app, "run_hello_check", fake)
    client = _client(tmp_path)

    client.post("/ui/hello-check", headers={"Accept-Language": "zh-CN"})

    assert seen.get("lang") == "zh_CN"
