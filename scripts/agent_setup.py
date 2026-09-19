#!/usr/bin/env python3
"""Guided agent setup: the terminal wizard for the human-only provisioning.

Ticket 20 / ADR-0018's onboarding half. The console panel
(``web/templates/_agent_setup.html``) covers what a browser can do without
leaving the page; this wizard does the same provisioning from a terminal, and —
the reason it exists — walks a person through the steps a program must not
pretend to perform.

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

- probe the known local OpenAI-compatible servers and test-call the first
  usable one;
- verify the endpoint the user chooses with a real ``/chat/completions`` call;
- pull a small model over Ollama's native API, where the server supports it;
- write the managed ``[agent]`` block and the client's ``mcpServers`` entry.

What it deliberately leaves to the human, and says so instead of pretending:

- **installing and running a local model server** (Ollama / LM Studio /
  llama.cpp) — the wizard reports the service's own install hint and carries on;
- **getting an agent harness** — pi-agent is the default *named* choice, never
  bundled and **never downloaded here**: the wizard says so, waits while the
  person installs one, then looks it up on ``PATH``;
- **choosing the harness and its MCP client-config path** — a client's own
  config location is defined nowhere in this repo, so the wizard asks and never
  guesses a default;
- **providing a credential** — the wizard can ask for the *name* of an
  environment variable and names it in the config; it never reads or writes a
  value, and the file it writes has no field a value could occupy.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import urllib.parse
from pathlib import Path

from clear_record.cli.auto import Message
from clear_record.core import i18n
from clear_record.core.i18n import tr
from clear_record.core.paths import config_path
from clear_record.service.setup import (
    DEFAULT_SMALL_MODEL,
    MCP_SERVER_NAME,
    PI_AGENT,
    Detection,
    Harness,
    Probe,
    SetupError,
    detect,
    find_harness,
    mcp_server_entry,
    pull_model,
    remember_harness,
    render_agent_block,
    resolve_harness,
    setup_view,
    verify_endpoint,
    write_agent_settings,
    write_mcp_config,
)

#: The number of stages the wizard prints as ``[n/total]``. Keep it in step with
#: :func:`main` — a stage that is skipped still counts, so the progress is stable.
TOTAL_STAGES = 7

#: A usable environment-variable name: the credential rule's only shape here.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Hosts that need no key at all, so the wizard does not even ask for a name.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


class Cancelled(Exception):
    """The user stopped the wizard (Ctrl-C, EOF, or declining a required step)."""


@dataclasses.dataclass
class Target:
    """The endpoint the wizard settled on, plus how it proved itself."""

    endpoint: str
    model: str | None
    api_key_env: str | None
    probe: Probe | None = None


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


def _render(message: Message) -> str:
    """A service error in the user's locale (the ``managed.py`` split)."""
    return message.render(tr)


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _is_local(endpoint: str) -> bool:
    host = urllib.parse.urlsplit(endpoint).hostname or ""
    return host in _LOCAL_HOSTS or host.endswith(".localhost")


# --- stage 1: detect -------------------------------------------------------- #


def stage_detect() -> Detection:
    _stage(1, tr("Find a local model server"))
    _say(
        tr(
            "Looking for Ollama, LM Studio and a llama.cpp server, and test-calling "
            "the first one that serves a model."
        )
    )
    _say(tr("A cold model can take a while to answer; this is the slow step."))
    detection = detect()
    for probe in detection.probes:
        if probe.verified:
            _say(
                tr(
                    "  {label} at {url}: verified with {model}.",
                    label=probe.candidate.label,
                    url=probe.candidate.base_url,
                    model=probe.verified_model or tr("its own model"),
                )
            )
        elif probe.reachable:
            _say(
                tr(
                    "  {label} at {url}: answers but is not verified.",
                    label=probe.candidate.label,
                    url=probe.candidate.base_url,
                )
            )
            if probe.verify_detail is not None:
                _say("    " + _render(probe.verify_detail))
        else:
            _say(
                tr(
                    "  {label} at {url}: not running.",
                    label=probe.candidate.label,
                    url=probe.candidate.base_url,
                )
            )
            # The service's own "what to install or run", never restated here.
            _say("    " + tr(probe.candidate.install_hint))
    if not detection.serving:
        _say()
        _say(
            tr(
                "Nothing answered with a model yet. Starting a server is your step — "
                "the hints above say how — and the wizard can continue without one, "
                "or be re-run once it is up."
            )
        )
    return detection


# --- stage 2: choose and verify --------------------------------------------- #


def _pick_endpoint(detection: Detection) -> tuple[str, str | None, Probe | None]:
    """A detected endpoint and its first model, or an address the user types."""
    usable = [probe for probe in detection.probes if probe.serving]
    if usable:
        _say(tr("Servers that answered with a model:"))
        for index, probe in enumerate(usable, start=1):
            _say(
                tr(
                    "  {index}. {url}  ({models})",
                    index=index,
                    url=probe.candidate.base_url,
                    models=", ".join(probe.models),
                )
            )
        other = len(usable) + 1
        _say(tr("  {index}. another address", index=other))
        while True:
            reply = _ask(tr("Choose one"), default="1")
            if reply.isdigit() and 1 <= int(reply) <= len(usable):
                chosen = usable[int(reply) - 1]
                return chosen.candidate.base_url, chosen.models[0], chosen
            if reply == str(other):
                break
            _say(tr("Enter one of the numbers listed."))
    else:
        _say(tr("No local server answered with a model, so type an address."))
    return _ask(tr("Endpoint URL (OpenAI-compatible base URL)")), None, None


def _ask_key_name(endpoint: str) -> str:
    """The environment variable's **name**, or ``""`` for a keyless endpoint.

    A local server needs no key, so it is not asked about. A hosted endpoint gets
    asked for the variable's *name* — the wizard never sees, reads or writes the
    value, which is what "BYOK, never stored" means at this surface.
    """
    if _is_local(endpoint):
        return ""
    while True:
        name = _ask(
            tr(
                "Name of the environment variable that holds this endpoint's key "
                "(the NAME only; leave empty if it needs none)"
            )
        )
        if not name:
            return ""
        if _ENV_NAME.match(name):
            _say(
                tr(
                    "The config will name {name}; you export the value in your own "
                    "shell — the wizard never stores it.",
                    name=name,
                )
            )
            return name
        _say(tr("{name} is not a usable environment-variable name.", name=name))


def stage_endpoint(detection: Detection) -> Target:
    _stage(2, tr("Choose the endpoint to record"))
    endpoint, suggested, probe = _pick_endpoint(detection)
    while True:
        endpoint = endpoint.strip()
        if not endpoint:
            _say(tr("An endpoint URL is required."))
            endpoint, suggested, probe = _pick_endpoint(detection)
            continue
        model = _ask(tr("Model name"), default=suggested or "")
        key_name = _ask_key_name(endpoint)
        _say(tr("Test-calling {endpoint} with a real request...", endpoint=endpoint))
        result = verify_endpoint(
            endpoint, model=model or None, api_key_env=key_name or None
        )
        if result.ok:
            _say(
                tr(
                    "Verified: {endpoint} answered with {model}.",
                    endpoint=endpoint,
                    model=result.model or model or tr("its own model"),
                )
            )
            return Target(
                endpoint=endpoint,
                model=result.model or model or None,
                api_key_env=key_name or None,
                probe=probe,
            )
        # A failed test call is reported, never written as if it worked.
        _say(tr("That did not work:"))
        _say("  " + (_render(result.detail) if result.detail else tr("no detail")))
        if not _confirm(tr("Try another endpoint or model?"), default=True):
            raise Cancelled
        endpoint, suggested, probe = _pick_endpoint(detection)


# --- stage 3: pull a small model -------------------------------------------- #


def stage_pull(target: Target) -> str | None:
    _stage(3, tr("Pull a small model (optional)"))
    if target.probe is None or not target.probe.pull_supported:
        _say(
            tr(
                "Only Ollama's native pull API is used here, and this endpoint does "
                "not offer it. Skipping."
            )
        )
        return None
    if not _confirm(
        tr(
            "Pull {model} from this server now? It is a download.",
            model=DEFAULT_SMALL_MODEL,
        ),
        default=False,
    ):
        return None
    model = _ask(tr("Model to pull"), default=DEFAULT_SMALL_MODEL)
    _say(tr("Pulling {model}; this can take a while.", model=model))
    pulled = pull_model(model, endpoint=target.endpoint)
    if not pulled.ok:
        _say(tr("The pull did not succeed:"))
        _say("  " + (_render(pulled.detail) if pulled.detail else tr("no detail")))
        return None
    _say(tr("Pulled {model}.", model=pulled.model))
    return pulled.model


# --- stage 4: record the endpoint ------------------------------------------- #


def stage_record(target: Target) -> Path | None:
    _stage(4, tr("Record the endpoint"))
    block = render_agent_block(
        endpoint=target.endpoint,
        model=target.model,
        api_key_env=target.api_key_env,
    )
    _say(
        tr("This is exactly what will be written into {path}:", path=str(config_path()))
    )
    print()
    print(_indent(block.rstrip("\n")))
    print()
    if target.api_key_env:
        _say(
            tr(
                "No key value is stored: the config names {name} and you export it.",
                name=target.api_key_env,
            )
        )
    if not _confirm(tr("Write it now?"), default=True):
        _say(tr("Not written; keeping whatever is already configured."))
        return None
    try:
        path = write_agent_settings(
            target.endpoint, model=target.model, api_key_env=target.api_key_env
        )
    except SetupError as exc:
        # A refusal (a hand-written [agent] table, invalid TOML) is the user's to
        # resolve — reported, never a traceback.
        _say(tr("Could not write the config:"))
        _say("  " + _render(exc.message))
        return None
    _say(tr("Recorded in {path}.", path=str(path)))
    return path


# --- stage 5: point at a harness -------------------------------------------- #


def stage_harness() -> Harness | None:
    _stage(5, tr("Point at an agent harness (MCP)"))
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


# --- stage 6: point at the MCP client config -------------------------------- #


def stage_mcp_config(harness: Harness | None) -> Path | None:
    _stage(6, tr("Point at the MCP client config"))
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


# --- stage 7: what changed, and what is still the user's -------------------- #


def stage_summary(
    target: Target,
    config_file: Path | None,
    harness: Harness | None,
    mcp_config: Path | None,
) -> None:
    _stage(7, tr("Done"))
    if config_file is not None:
        _say(tr("Agent config: {path}", path=str(config_file)))
        _say(
            tr(
                "Runners will call {endpoint} with model {model}.",
                endpoint=target.endpoint,
                model=target.model or tr("the server's default"),
            )
        )
    else:
        _say(tr("Agent config: unchanged."))
    if mcp_config is not None:
        _say(tr("MCP client config: {path}", path=str(mcp_config)))
    else:
        _say(tr("MCP client config: not written."))
    print()
    _say(tr("Steps that stay yours:"))
    _say(tr("- keep your model server running when an agent task runs;"))
    _say(
        tr(
            "- install {name} (or another MCP-capable client) if it is not on PATH "
            "yet;",
            name=PI_AGENT,
        )
    )
    if target.api_key_env:
        _say(
            tr(
                "- set {name} in the environment that starts clear-record; the "
                "wizard did not and cannot store it.",
                name=target.api_key_env,
            )
        )


# --- the session ------------------------------------------------------------ #


def _show_current() -> None:
    view = setup_view()
    if view.endpoint:
        _say(
            tr(
                "Already configured: {endpoint}{model}",
                endpoint=view.endpoint,
                model=f" ({view.model})" if view.model else "",
            )
        )
    else:
        _say(tr("No agent endpoint is configured yet."))
    for problem in view.problems:
        _say(tr("Config problem: {problem}", problem=tr(problem)))
    if view.harness:
        _say(tr("Harness already pointed at: {path}", path=view.harness))
    if view.mcp_config:
        _say(tr("MCP client config already written: {path}", path=view.mcp_config))


def main() -> int:
    i18n.install_if_unset()
    _say(tr("clear-record agent setup"))
    _say(
        tr(
            "This wizard records what a task runner needs, and points an MCP client "
            "at clear-record's tools."
        )
    )
    _say(tr("It never stores a credential."))
    _show_current()
    try:
        detection = stage_detect()
        target = stage_endpoint(detection)
        pulled = stage_pull(target)
        if pulled and pulled != target.model:
            # A newly pulled model must prove itself before it is recorded.
            result = verify_endpoint(
                target.endpoint, model=pulled, api_key_env=target.api_key_env
            )
            if result.ok:
                target.model = result.model or pulled
                _say(tr("Using the pulled model {model}.", model=target.model))
            else:
                _say(
                    tr(
                        "The pulled model did not answer a test call; keeping {model}.",
                        model=target.model or tr("the server's default"),
                    )
                )
        config_file = stage_record(target)
        harness = stage_harness()
        mcp_config = stage_mcp_config(harness)
        stage_summary(target, config_file, harness, mcp_config)
    except Cancelled:
        print()
        _say(
            tr(
                "Cancelled — nothing further was written; anything you confirmed "
                "above stands."
            )
        )
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
