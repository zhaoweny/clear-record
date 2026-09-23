"""The terminal wizard (``scripts/agent_setup.py``) and its rules.

The wizard drives the same :mod:`clear_record.service.setup` functions the console
does, so what is worth testing here is not the plumbing — the service suite covers
that — but the rules that live **only** at this surface:

- a scripted session that points at a harness found on ``PATH`` and names a client
  config writes exactly the one ``mcpServers`` entry, records both paths, and
  writes **no [`agent`] table at all** (there is no model to configure);
- when no harness is on ``PATH`` the wizard says plainly that getting one is the
  human's step, and writes no client config;
- a 0.2 ``[agent]`` table sitting in the user's config is **reported as ignored**,
  never touched.

The session is scripted by replacing ``builtins.input``: the wizard is a plain
interactive program with no prompt library, so feeding it lines is the whole seam.
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


def test_a_scripted_session_writes_the_one_mcp_entry_and_no_agent_table(
    wizard, tmp_path, monkeypatch, capsys
) -> None:
    harness = setup.Harness(name="pi-agent", path="/opt/bin/pi-agent")
    monkeypatch.setattr(wizard, "find_harness", lambda: (harness,))
    mcp_path = tmp_path / "client" / "mcp.json"
    _scripted(
        monkeypatch,
        [
            "",  # point at the harness found on PATH
            str(mcp_path),  # the client config path, chosen by the user
            "",  # confirm the MCP entry write
        ],
    )

    code = wizard.main()
    printed = capsys.readouterr().out

    assert code == 0
    # No model is configured and no key is asked for: there is no [agent] table.
    assert not paths.config_path().exists()
    assert "credential" in printed  # and the wizard says so
    document = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert document["mcpServers"][setup.MCP_SERVER_NAME] == {
        "command": "clear-record",
        "args": ["mcp"],
    }
    record = setup.read_setup_state()
    assert record["harness"] == "/opt/bin/pi-agent"
    assert record["mcp_config"] == str(mcp_path)


def test_without_a_harness_it_says_the_download_is_the_users_step(
    wizard, monkeypatch, capsys
) -> None:
    """No harness on PATH: name the human action, write no client config."""
    monkeypatch.setattr(
        wizard, "find_harness", lambda: (setup.Harness(name="pi-agent", path=None),)
    )
    _scripted(monkeypatch, ["skip"])  # skip the MCP rung rather than install anything

    code = wizard.main()
    printed = capsys.readouterr().out

    assert code == 0
    assert "pi-agent is not bundled" in printed
    assert "will not download one for you" in printed
    assert "mcp_config" not in setup.read_setup_state()
    assert not paths.config_path().exists()


def test_a_refused_harness_path_is_reported_not_raised(
    wizard, tmp_path, monkeypatch, capsys
) -> None:
    """A path that is not runnable is the service's refusal, shown and skipped."""
    monkeypatch.setattr(
        wizard, "find_harness", lambda: (setup.Harness(name="pi-agent", path=None),)
    )
    bogus = tmp_path / "not-a-harness"
    bogus.write_text("plain file\n", encoding="utf-8")
    _scripted(monkeypatch, [str(bogus), "skip"])

    code = wizard.main()
    printed = capsys.readouterr().out

    assert code == 0
    assert "is not executable" in printed
    assert "mcp_config" not in setup.read_setup_state()


def test_a_hand_written_agent_table_is_reported_as_ignored_and_left_alone(
    wizard, monkeypatch, capsys
) -> None:
    """The release's config decision: report the 0.2 plumbing, never move it."""
    hand_written = '[agent]\nendpoint = "http://hand.written/v1"\n'
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text(hand_written, encoding="utf-8")
    monkeypatch.setattr(
        wizard, "find_harness", lambda: (setup.Harness(name="pi-agent", path=None),)
    )
    _scripted(monkeypatch, ["skip"])

    code = wizard.main()
    printed = capsys.readouterr().out

    assert code == 0
    assert "Ignored" in printed
    assert "[agent]" in printed
    # Byte-for-byte untouched: not migrated, not clobbered.
    assert paths.config_path().read_text(encoding="utf-8") == hand_written
