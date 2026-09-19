"""The terminal wizard (``scripts/agent_setup.py``) and its two hard rules.

The wizard drives the same :mod:`clear_record.service.setup` functions the console
does, so what is worth testing here is not the probing — the service suite covers
that against a fake endpoint — but the rules that live **only** at this surface:

- a scripted session that names an environment variable writes the *name*, and a
  value sitting in the environment never reaches the config or the output;
- when no harness is on ``PATH`` the wizard says plainly that getting one is the
  human's step, and writes no client config.

The session is scripted by replacing ``builtins.input``: the wizard is a plain
interactive program with no prompt library, so feeding it lines is the whole seam.
Every service call that would touch the network is replaced with a fake, and the
conftest fixture keeps the app directories inside ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from clear_record.core import paths
from clear_record.service import setup

REPO_ROOT = Path(__file__).resolve().parents[3]
WIZARD_PATH = REPO_ROOT / "scripts" / "agent_setup.py"


@pytest.fixture
def wizard() -> types.ModuleType:
    """The script, loaded as a module (it is not part of any package)."""
    spec = importlib.util.spec_from_file_location("agent_setup_wizard", WIZARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``dataclasses`` looks the class's module up in ``sys.modules`` while the
    # decorator runs, so the module must be registered *before* execution.
    sys.modules["agent_setup_wizard"] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("agent_setup_wizard", None)


def _scripted(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    """Feed the wizard ``answers`` in order; running out is a clean EOF."""
    remaining = list(answers)

    def fake_input(prompt: str = "") -> str:
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def _no_detection() -> setup.Detection:
    return setup.Detection(probes=())


def _verification_ok(*, ok: bool = True):
    def verify(endpoint, *, model=None, api_key_env=None, **_kwargs):
        return setup.Verification(
            ok=ok,
            endpoint=endpoint,
            model=model or "qwen2.5:1.5b",
            detail=None if ok else setup.Message("endpoint refused the test call"),
        )

    return verify


def test_a_scripted_session_names_the_variable_and_never_writes_a_value(
    wizard, tmp_path, monkeypatch, capsys
) -> None:
    """The BYOK rule at the wizard surface: a name is configuration, a value is not."""
    sentinel = "s3cret-value-the-wizard-must-not-write"
    monkeypatch.setenv("CR_WIZARD_KEY", sentinel)
    monkeypatch.setattr(wizard, "detect", _no_detection)
    monkeypatch.setattr(wizard, "verify_endpoint", _verification_ok())
    harness = setup.Harness(name="pi-agent", path="/opt/bin/pi-agent")
    monkeypatch.setattr(wizard, "find_harness", lambda: (harness,))
    mcp_path = tmp_path / "client" / "mcp.json"
    _scripted(
        monkeypatch,
        [
            "https://hosted.test/v1",  # the endpoint to record
            "m",  # the model
            "CR_WIZARD_KEY",  # the variable's NAME, never its value
            "",  # confirm the config write
            "",  # point at the harness found on PATH
            str(mcp_path),  # the client config path, chosen by the user
            "",  # confirm the MCP entry write
        ],
    )

    code = wizard.main()
    printed = capsys.readouterr().out

    assert code == 0
    config = paths.config_path().read_text(encoding="utf-8")
    assert 'api_key_env = "CR_WIZARD_KEY"' in config  # the NAME is recorded
    assert sentinel not in config
    assert sentinel not in printed
    document = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert document["mcpServers"][setup.MCP_SERVER_NAME] == {
        "command": "clear-record",
        "args": ["mcp"],
    }
    record = setup.read_setup_state()
    assert record["harness"] == "/opt/bin/pi-agent"
    assert record["mcp_config"] == str(mcp_path)


def test_without_a_harness_it_says_the_download_is_the_users_step(
    wizard, tmp_path, monkeypatch, capsys
) -> None:
    """No harness on PATH: name the human action, write no client config."""
    monkeypatch.setattr(wizard, "detect", _no_detection)
    monkeypatch.setattr(wizard, "verify_endpoint", _verification_ok())
    monkeypatch.setattr(
        wizard, "find_harness", lambda: (setup.Harness(name="pi-agent", path=None),)
    )
    _scripted(
        monkeypatch,
        [
            "http://127.0.0.1:11434/v1",  # a local endpoint needs no key name
            "",  # accept the server's model
            "",  # confirm the config write
            "skip",  # skip the MCP rung rather than install anything
        ],
    )

    code = wizard.main()
    printed = capsys.readouterr().out

    assert code == 0
    assert "pi-agent is not bundled" in printed
    assert "will not download one for you" in printed
    assert paths.config_path().is_file()
    assert "mcp_config" not in setup.read_setup_state()


def test_a_failed_test_call_records_nothing(wizard, monkeypatch, capsys) -> None:
    """Fail closed: an endpoint that cannot answer is never written as working."""
    monkeypatch.setattr(wizard, "detect", _no_detection)
    monkeypatch.setattr(wizard, "verify_endpoint", _verification_ok(ok=False))
    _scripted(
        monkeypatch,
        [
            "http://127.0.0.1:11434/v1",
            "",  # accept the server's model
            "n",  # do not try another endpoint
        ],
    )

    code = wizard.main()

    assert code == 130
    assert "endpoint refused the test call" in capsys.readouterr().out
    assert not paths.config_path().is_file()
    assert setup.read_setup_state() == {}


def test_a_refused_config_write_is_reported_not_raised(
    wizard, tmp_path, monkeypatch, capsys
) -> None:
    """A hand-written [agent] table is the user's: setup reports it, never clobbers."""
    hand_written = '[agent]\nendpoint = "http://hand.written/v1"\n'
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(hand_written, encoding="utf-8")
    monkeypatch.setattr(wizard, "detect", _no_detection)
    monkeypatch.setattr(wizard, "verify_endpoint", _verification_ok())
    monkeypatch.setattr(
        wizard, "find_harness", lambda: (setup.Harness(name="pi-agent", path=None),)
    )
    _scripted(
        monkeypatch,
        [
            "http://127.0.0.1:11434/v1",
            "",  # accept the server's model
            "",  # confirm the config write, which the service then refuses
            "skip",  # skip the MCP rung
        ],
    )

    code = wizard.main()

    assert code == 0
    assert "Could not write the config" in capsys.readouterr().out
    assert paths.config_path().read_text(encoding="utf-8") == hand_written
