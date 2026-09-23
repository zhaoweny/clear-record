#!/usr/bin/env python3
"""Agent setup: the terminal wizard for the human-only provisioning.

ADR-0031's onboarding half. The console panel (``web/templates/_agent_setup.html``)
covers what a browser can do without leaving the page; this wizard does the same
provisioning from a terminal, and — the reason it exists — walks a person through
the steps a program must not pretend to perform.

**Run it through `just`** (the pointer recipe lives in the justfile, the
orchestrator's control plane; ``agent-setup`` is the intended name, and the one
line below is what it runs)::

    just agent-setup
    # -> uv run --all-packages python scripts/agent_setup.py

``--all-packages`` is what makes the script work: it imports ``clear_record``, so
it has to run inside the project environment. ``--no-project`` would be the wrong
shape here — that is for a script that declares its own dependencies in a PEP-723
block and needs nothing from the project (``scripts/check_web_assets.py``). This
file has no such block, exactly like ``scripts/i18n.py``, because ``uv run python
<file>`` does not honour one and the project environment already supplies what it
imports.

What the wizard automates — through the same :mod:`clear_record.service.setup`
functions the console calls, so the two surfaces cannot disagree:

- find an MCP-capable harness on ``PATH``, or take a path the user points at;
- write the client's ``mcpServers`` entry for ``clear-record mcp``.

What it deliberately leaves to the human, and says so instead of pretending:

- **getting an agent harness** — pi-agent is the default *named* choice, never
  bundled and **never downloaded here**: the wizard says so, waits while the
  person installs one, then looks it up on ``PATH``;
- **choosing the harness and its MCP client-config path** — a client's own
  config location is defined nowhere in this repo, so the wizard asks and never
  guesses a default;
- **the model** — the wizard configures no endpoint and asks for no key, because
  clear-record no longer calls a model at all: the harness the user points at
  brings its own.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from clear_record.core import i18n
from clear_record.core.i18n import tr
from clear_record.service.setup import (
    MCP_SERVER_NAME,
    PI_AGENT,
    Harness,
    SetupError,
    find_harness,
    ignored_agent_config,
    mcp_server_entry,
    remember_harness,
    resolve_harness,
    setup_view,
    write_mcp_config,
)

#: The number of stages the wizard prints as ``[n/total]``. Keep it in step with
#: :func:`main` — a stage that is skipped still counts, so the progress is stable.
TOTAL_STAGES = 3


class Cancelled(Exception):
    """The user stopped the wizard (Ctrl-C, EOF, or declining a required step)."""


# --- terminal plumbing ------------------------------------------------------ #


def _say(text: str = "") -> None:
    print(text)


def _stage(number: int, title: str) -> None:
    """One stage header; the stages are the wizard's only structure."""
    print()
    print(f"--- [{number}/{TOTAL_STAGES}] {title} " + "-" * 6)


def _line(prompt: str) -> str:
    """``input`` that turns a closed stdin / Ctrl-C into a clean cancellation."""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None


def _ask(prompt: str, *, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    reply = _line(f"{prompt}{suffix}: ").strip()
    return reply or default


def _confirm(prompt: str, *, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    reply = _line(f"{prompt} [{hint}]: ").strip().lower()
    if not reply:
        return default
    return reply in {"y", "yes"}


def _render(message) -> str:
    """A service error in the user's locale (the ``managed.py`` split)."""
    return message.render(tr)


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# --- stage 1: point at a harness -------------------------------------------- #


def stage_harness() -> Harness | None:
    _stage(1, tr("Point at an agent harness (MCP)"))
    while True:
        found = find_harness()
        for harness in found:
            if harness.found:
                _say(
                    tr(
                        "{name} is on PATH at {path}.",
                        name=harness.name,
                        path=harness.path,
                    )
                )
            else:
                _say(tr("{name} is not on PATH.", name=harness.name))
        usable = [harness for harness in found if harness.found]
        if usable and _confirm(tr("Point at it?"), default=True):
            # ``remember_harness`` returns the setup *record*; the stage returns
            # the harness itself, because the next stage names it.
            remember_harness(usable[0])
            return usable[0]
        # The "download one" rung, told honestly: this is the human's step.
        _say(
            tr(
                "{name} is not bundled, and this wizard will not download one for you.",
                name=PI_AGENT,
            )
        )
        _say(
            tr(
                "Install {name} (or any other MCP-capable client) your own way — "
                "then this wizard can point at it.",
                name=PI_AGENT,
            )
        )
        reply = _ask(
            tr(
                "Path to the harness; press Enter to look on PATH again, or type "
                "'skip' to continue without the MCP rung"
            )
        )
        if reply.lower() == "skip":
            return None
        if not reply:
            continue  # a human may have just installed it; look again
        try:
            pointed = resolve_harness(reply)
        except SetupError as exc:
            _say(_render(exc.message))
            continue
        remember_harness(pointed)
        return pointed


# --- stage 2: point at the MCP client config -------------------------------- #


def stage_mcp_config(harness: Harness | None) -> Path | None:
    _stage(2, tr("Point at the MCP client config"))
    if harness is None:
        _say(
            tr(
                "No harness was pointed at, so there is no client config to write. "
                "Skipping."
            )
        )
        return None
    _say(
        tr(
            "The config file belongs to your MCP client ({harness}); its location "
            "is not something clear-record can know, so name it here.",
            harness=harness.name,
        )
    )
    _say(tr("clear-record will add this entry and leave every other one alone:"))
    print()
    print(
        _indent(
            json.dumps({"mcpServers": {MCP_SERVER_NAME: mcp_server_entry()}}, indent=2)
        )
    )
    print()
    while True:
        reply = _ask(tr("Path to the MCP client config (.json); leave empty to skip"))
        if not reply:
            return None
        if not _confirm(tr("Write the entry into {path}?", path=reply), default=True):
            continue
        try:
            path = write_mcp_config(reply)
        except SetupError as exc:
            _say(_render(exc.message))
            continue
        _say(tr("Registered the clear-record MCP server in {path}.", path=str(path)))
        return path


# --- stage 3: what changed, and what is still the user's -------------------- #


def stage_summary(harness: Harness | None, mcp_config: Path | None) -> None:
    _stage(3, tr("Done"))
    if harness is not None:
        _say(tr("Agent harness: {path}", path=harness.path or harness.name))
    else:
        _say(tr("Agent harness: none recorded."))
    if mcp_config is not None:
        _say(tr("MCP client config: {path}", path=str(mcp_config)))
    else:
        _say(tr("MCP client config: not written."))
    print()
    _say(tr("Steps that stay yours:"))
    _say(
        tr(
            "- install {name} (or another MCP-capable client) if it is not on PATH "
            "yet;",
            name=PI_AGENT,
        )
    )
    _say(
        tr(
            "- bring a model: clear-record holds none and asks for no key, so the "
            "harness runs its own;"
        )
    )


# --- the session ------------------------------------------------------------ #


def _show_current() -> None:
    view = setup_view()
    if view.harness:
        _say(tr("Harness already pointed at: {path}", path=view.harness))
    else:
        _say(tr("No agent harness is pointed at yet."))
    if view.mcp_config:
        _say(tr("MCP client config already written: {path}", path=view.mcp_config))
    for problem in view.problems:
        _say(tr("Setup problem: {problem}", problem=problem))
    for ignored in view.ignored:
        _say(tr("Ignored: {message}", message=ignored))


def main() -> int:
    i18n.install_if_unset()
    _say(tr("clear-record agent setup"))
    _say(
        tr(
            "This wizard points an MCP client at clear-record's tools. "
            "clear-record calls no model itself."
        )
    )
    _say(tr("It never asks for a credential, because the MCP server needs none."))
    _show_current()
    try:
        harness = stage_harness()
        mcp_config = stage_mcp_config(harness)
        stage_summary(harness, mcp_config)
    except Cancelled:
        print()
        _say(
            tr(
                "Cancelled — nothing further was written; anything you confirmed "
                "above stands."
            )
        )
        return 130
    _say()
    for ignored in ignored_agent_config():
        _say(tr("Ignored: {message}", message=ignored))
    return 0


if __name__ == "__main__":
    sys.exit(main())
