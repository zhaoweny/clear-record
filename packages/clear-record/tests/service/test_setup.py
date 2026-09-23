"""Agent setup: the harness and MCP rungs, the record, and the ignored 0.2 config.

There is no endpoint rung any more (ADR-0031): clear-record calls no model, so
this module covers what remains — pointing at an MCP-capable harness, registering
the server in a client config, and the non-secret record of both. It also pins
the release's config decision: the 0.2 ``[agent]`` table and ``CR_AGENT_*``
variables are **reported and ignored**, never migrated and never fatal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clear_record.service import setup
from clear_record.service.archive import tool_version


def _harness(tmp_path: Path) -> Path:
    """An executable file the setup will accept as a harness."""
    path = tmp_path / "pi-agent"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- the harness rung -------------------------------------------------------- #


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


def test_the_mcp_entry_names_no_environment_or_credential() -> None:
    """The server needs none, so the entry cannot carry one."""
    entry = setup.mcp_server_entry()

    assert set(entry) == {"command", "args"}
    assert Path(setup.SETUP_FILENAME).name == "agent-setup.json"


# --- the setup record has no credential to hold ------------------------------ #


def test_the_setup_record_has_no_field_for_a_credential(tmp_path) -> None:
    """Only allow-listed, non-secret facts are persisted."""
    record = setup.update_setup_state(
        path=tmp_path / "state.json", harness="/usr/bin/x"
    )

    assert record == {"harness": "/usr/bin/x"}
    for refused in ("api_key", "api_key_env", "endpoint", "model"):
        with pytest.raises(setup.SetupError):
            setup.update_setup_state(path=tmp_path / "state.json", **{refused: "v"})


# --- the version marker (first run vs after an update) ---------------------- #


def test_the_version_marker_is_an_allowed_non_secret_fact(tmp_path) -> None:
    record = tmp_path / "state.json"

    setup.record_seen_version(path=record, version="1.2.3")

    assert setup.read_setup_state(path=record) == {"seen_version": "1.2.3"}
    assert setup.seen_version(state={"seen_version": "1.2.3"}) == "1.2.3"
    assert setup.seen_version(state={}) is None
    # A key outside the allow-list is still refused, marker or not.
    with pytest.raises(setup.SetupError):
        setup.update_setup_state(path=record, seen="1.2.3")


def test_forgetting_the_marker_writes_no_file_when_none_exists(tmp_path) -> None:
    """Regression: it used to create an empty agent-setup.json on the click."""
    absent = tmp_path / "state.json"

    assert setup.clear_seen_version(path=absent) == {}
    assert not absent.exists()


def test_forgetting_the_marker_keeps_every_other_recorded_fact(tmp_path) -> None:
    record = tmp_path / "state.json"
    setup.update_setup_state(path=record, seen_version="1.2.3", harness="/usr/bin/x")

    setup.clear_seen_version(path=record)

    assert setup.read_setup_state(path=record) == {"harness": "/usr/bin/x"}


def test_setup_incomplete_is_the_marker_compared_with_the_current_version() -> None:
    assert setup.setup_incomplete(state={}, version="1.2.3") is True
    seen = {"seen_version": "1.2.3"}
    assert setup.setup_incomplete(state=seen, version="1.2.3") is False
    assert setup.setup_incomplete(state=seen, version="1.2.4") is True


def test_the_current_version_comes_from_package_metadata() -> None:
    assert setup.current_version() == tool_version()


# --- the removed 0.2 agent configuration: reported, never fatal -------------- #


def test_an_agent_table_is_reported_and_not_touched(tmp_path) -> None:
    config = tmp_path / "config.toml"
    original = (
        '[paths]\ndata_dir = "/tmp/x"\n\n[agent]\nendpoint = "http://mine.test/v1"\n'
    )
    config.write_text(original, encoding="utf-8")

    ignored = setup.ignored_agent_config(environ={}, config_file=config)

    assert len(ignored) == 1
    assert "[agent]" in ignored[0]
    assert "ignored" in ignored[0]
    # Not migrated and not fatal: the user's file is byte-for-byte untouched.
    assert config.read_text(encoding="utf-8") == original


def test_the_agent_environment_variables_are_reported(tmp_path) -> None:
    ignored = setup.ignored_agent_config(
        environ={
            "CR_AGENT_ENDPOINT": "http://x",
            "CR_AGENT_MODEL": "m",
            "PATH": "/bin",
        },
        config_file=tmp_path / "absent.toml",
    )

    assert len(ignored) == 1
    assert "CR_AGENT_ENDPOINT" in ignored[0] and "CR_AGENT_MODEL" in ignored[0]
    assert "CR_AGENT_TIMEOUT" not in ignored[0]


def test_a_config_file_without_an_agent_table_says_nothing(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[paths]\ndata_dir = "/tmp/x"\n', encoding="utf-8")

    assert setup.ignored_agent_config(environ={}, config_file=config) == ()


# --- the view ---------------------------------------------------------------- #


def test_the_view_says_plainly_when_nothing_is_set_up(tmp_path) -> None:
    view = setup.setup_view(environ={}, config_file=tmp_path / "absent.toml", state={})

    assert view.state == setup.STATE_NOT_CONFIGURED
    assert view.ready is False
    assert view.harness is None and view.mcp_config is None
    assert view.problems == () and view.ignored == ()
    # The machine view carries no credential field at all.
    assert "api_key_env" not in view.as_dict().model_dump()
    assert view.as_dict().ready is False


def test_the_view_is_ready_once_both_paths_are_recorded(tmp_path) -> None:
    harness = _harness(tmp_path)
    client = tmp_path / "mcp.json"
    client.write_text("{}", encoding="utf-8")
    state = {"harness": str(harness), "mcp_config": str(client)}

    view = setup.setup_view(environ={}, config_file=None, state=state)

    assert view.state == setup.STATE_READY
    assert view.ready is True
    assert view.as_dict().harness == str(harness)


def test_a_recorded_path_that_is_gone_is_a_problem_state(tmp_path) -> None:
    state = {
        "harness": str(tmp_path / "gone"),
        "mcp_config": str(tmp_path / "also-gone"),
    }

    view = setup.setup_view(environ={}, config_file=None, state=state)

    assert view.state == setup.STATE_PROBLEM
    assert len(view.problems) == 2
    assert all("not a file" in problem for problem in view.problems)


def test_the_view_reports_the_ignored_config_and_still_answers(tmp_path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[agent]\nendpoint = "http://mine.test/v1"\n', encoding="utf-8")

    view = setup.setup_view(
        environ={"CR_AGENT_MODEL": "m"}, config_file=config, state={}
    )

    assert view.state == setup.STATE_NOT_CONFIGURED  # ignored, not a problem
    assert len(view.ignored) == 2
