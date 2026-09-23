"""Guided agent setup: point at a harness, register the MCP server, remember it.

ADR-0031's onboarding half: get a user from "installed" to "a harness holds the
tools", **without hand-editing JSON or TOML**. This module owns the rungs that
reach a working harness:

1. **Harness.** pi-agent is the *default named* client, never bundled: the setup
   finds one on ``PATH``, accepts a path the user points at, or names the
   download step. Any other MCP-capable client works identically — the entry is a
   command and args, and nothing here knows pi-agent's own config schema.
2. **MCP client config.** Write the standard ``mcpServers`` entry for
   ``clear-record mcp`` into a config file the user names.

Two rules this module holds to:

- **No credential, and nothing to configure.** The MCP entry is a command and
  args; it has nowhere to put a key, and the server needs none. The setup state
  file records non-secret facts only (which harness, which config path).
- **The removed agent config is ignored, not migrated.** ``[agent]`` tables and
  ``CR_AGENT_*`` variables written for the 0.2 in-process path are reported (see
  :func:`ignored_agent_config`) and otherwise left exactly where they are: an
  existing config file keeps working and nothing writes to it.

User-facing failures are a stable message ID plus parameters
(:class:`clear_record.pipeline.auto.Message`, marked with
:func:`clear_record.core.i18n.deferred`), so a presentation boundary renders
them in the user's locale while :func:`str` stays the English form for the JSON
API and the logs — the :mod:`clear_record.service.managed` pattern.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from clear_record.pipeline.auto import Message
from clear_record.core.i18n import deferred
from clear_record.core.paths import config_path, resolve_state_dir
from clear_record.service.schemas import Shape

# --- errors ----------------------------------------------------------------- #


class SetupError(RuntimeError):
    """A setup step refused or failed.

    ``message`` is the stable ID plus parameters; a boundary renders it with
    ``tr`` (``exc.message.render(tr)``) and ``str(exc)`` stays the English form
    for machine surfaces. The rule lives with the service; the console only
    translates — the same split :mod:`clear_record.service.managed` uses.
    """

    def __init__(self, message: Message) -> None:
        self.message = message
        super().__init__(str(message))


# --- the harness rung: point at an MCP-capable client ----------------------- #

#: The MCP server entry's name inside the client's ``mcpServers`` table.
MCP_SERVER_NAME = "clear-record"
#: The command an MCP client should run: the installed console script.
MCP_SERVER_COMMAND = "clear-record"
#: Its arguments: the stdio MCP subcommand (ADR-0017).
MCP_SERVER_ARGS: tuple[str, ...] = ("mcp",)

#: pi-agent is ADR-0031's **default named** harness, not a dependency. Detection
#: is a ``PATH`` lookup of this one name; any other MCP-capable client works
#: identically, and callers may pass their own names to :func:`find_harness`.
PI_AGENT = "pi-agent"
DEFAULT_HARNESSES: tuple[str, ...] = (PI_AGENT,)


@dataclasses.dataclass(frozen=True)
class Harness:
    """An MCP-capable client, found on ``PATH`` or pointed at by the user."""

    name: str
    path: str | None

    @property
    def found(self) -> bool:
        return self.path is not None

    def as_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "found": self.found}


def find_harness(
    names: Sequence[str] = DEFAULT_HARNESSES,
    *,
    which: Callable[[str], str | None] | None = None,
) -> tuple[Harness, ...]:
    """Look each name up on ``PATH`` (``which`` injectable for tests).

    A missing harness is a **result** — the setup then offers the download rung
    — not an error.
    """
    lookup = which or shutil.which
    return tuple(Harness(name=name, path=lookup(name)) for name in names)


def resolve_harness(path: str | Path) -> Harness:
    """A harness the user **points at**, checked to be an executable file.

    This is the "point at an existing pi-agent" rung: setup never guesses a
    location, and it refuses a path that is not something it could run.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise SetupError(
            Message(
                deferred("the agent harness {path} is not a file"),
                (("path", str(candidate)),),
            )
        )
    if not os.access(candidate, os.X_OK):
        raise SetupError(
            Message(
                deferred("the agent harness {path} is not executable"),
                (("path", str(candidate)),),
            )
        )
    return Harness(name=candidate.stem or str(candidate), path=str(candidate))


def mcp_server_entry(
    *,
    command: str = MCP_SERVER_COMMAND,
    args: Sequence[str] = MCP_SERVER_ARGS,
) -> dict:
    """The client-side entry that launches clear-record's stdio MCP server.

    A command and its arguments, and nothing else — in particular no
    environment block and no credential, because the MCP server needs none
    (ADR-0031: the *harness* brings the model).
    """
    return {"command": command, "args": list(args)}


def mcp_client_config(
    existing: Mapping | None = None,
    *,
    command: str = MCP_SERVER_COMMAND,
    args: Sequence[str] = MCP_SERVER_ARGS,
    name: str = MCP_SERVER_NAME,
) -> dict:
    """``existing`` with clear-record registered under ``mcpServers``.

    The shape is the de-facto MCP client convention (Claude Desktop, Cursor and
    most clients read it), which is what keeps this rung harness-agnostic: the
    entry names a command, so pointing at pi-agent or any other client is the
    same operation.
    """
    document = dict(existing) if isinstance(existing, Mapping) else {}
    servers = document.get("mcpServers")
    writable = dict(servers) if isinstance(servers, Mapping) else {}
    writable[name] = mcp_server_entry(command=command, args=args)
    document["mcpServers"] = writable
    return document


def write_mcp_config(
    path: str | Path,
    *,
    command: str = MCP_SERVER_COMMAND,
    args: Sequence[str] = MCP_SERVER_ARGS,
    name: str = MCP_SERVER_NAME,
) -> Path:
    """Register the clear-record MCP server in the client config at ``path``.

    ``path`` is always the user's choice (the console and the wizard ask for it):
    a client's config location is the client's business, and inventing one for an
    external harness would be a guess. An existing config is merged — other
    servers survive — and a file that is not a JSON object is refused rather than
    overwritten.
    """
    target = Path(path).expanduser()
    existing: object = {}
    if target.is_file():
        text = target.read_text(encoding="utf-8").strip()
        if text:
            try:
                existing = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SetupError(
                    Message(
                        deferred("{path} is not valid JSON ({detail})"),
                        (("path", str(target)), ("detail", exc.msg)),
                    )
                ) from exc
            if not isinstance(existing, dict):
                raise SetupError(
                    Message(
                        deferred(
                            "{path} must contain a JSON object to hold mcpServers"
                        ),
                        (("path", str(target)),),
                    )
                )
    document = mcp_client_config(existing, command=command, args=args, name=name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    update_setup_state(mcp_config=str(target))
    return target


# --- the removed agent config: reported, never fatal ------------------------ #

#: The env prefix the 0.2 in-process agent path read. A variable with this prefix
#: is now **ignored**: nothing in this version consults an endpoint, a model or a
#: key, and an existing shell keeps working with them set.
AGENT_ENV_PREFIX = "CR_AGENT_"


def _agent_table(config_file: str | Path | None) -> bool:
    """Whether the config file carries an ``[agent]`` table (never raises)."""
    path = Path(config_file) if config_file is not None else config_path()
    if not path.is_file():
        return False
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return isinstance(document.get("agent"), dict)


def ignored_agent_config(
    *,
    environ: Mapping[str, str] | None = None,
    config_file: str | Path | None = None,
) -> tuple[str, ...]:
    """The 0.2 agent configuration this version **ignores**, as messages.

    The least destructive option (the release's decision): a user's ``[agent]``
    table and ``CR_AGENT_*`` variables are neither migrated nor fatal — an
    existing config file keeps working, and this version calls nothing. The
    messages are English with the concrete path or variable names embedded, so a
    boundary shows them verbatim (the same treatment a config problem gets).
    """
    found: list[str] = []
    if _agent_table(config_file):
        path = Path(config_file) if config_file is not None else config_path()
        found.append(
            f"The [agent] section in {path} is ignored: this version does not call "
            "a model itself. Point an agent harness at the MCP server instead."
        )
    env = os.environ if environ is None else environ
    names = sorted(name for name in env if name.startswith(AGENT_ENV_PREFIX))
    if names:
        found.append(
            f"The environment variable(s) {', '.join(names)} are ignored: this "
            "version reads no model endpoint or key. Point an agent harness at the "
            "MCP server instead."
        )
    return tuple(found)


# --- the setup record (non-secret state, app-owned) ------------------------- #

#: Setup's own record: which harness was pointed at and which client config was
#: written. It holds facts about the *setup* that nothing else needs, and it is
#: app-owned state rather than a user document.
SETUP_FILENAME = "agent-setup.json"
#: The only keys setup ever writes. A key is an allow-list so a future caller
#: cannot quietly persist something that is not a setup fact — and so no field
#: here could ever be a credential.
SETUP_STATE_KEYS = ("harness", "mcp_config", "seen_version")


def setup_state_path() -> Path:
    """The setup record's path (``<state>/agent-setup.json``)."""
    return resolve_state_dir() / SETUP_FILENAME


def read_setup_state(*, path: str | Path | None = None) -> dict:
    """The recorded setup facts, or ``{}`` when there are none or it is unreadable."""
    source = Path(path) if path is not None else setup_state_path()
    if not source.is_file():
        return {}
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(document, dict):
        return {}
    return {
        key: document[key]
        for key in SETUP_STATE_KEYS
        if isinstance(document.get(key), str) and document[key]
    }


def update_setup_state(*, path: str | Path | None = None, **fields: object) -> dict:
    """Merge non-secret setup facts into the record and return the new state."""
    document = read_setup_state(path=path)
    for key, value in fields.items():
        if key not in SETUP_STATE_KEYS:
            raise SetupError(
                Message(
                    deferred("setup does not record {key!r}"),
                    (("key", key),),
                )
            )
        if isinstance(value, str) and value.strip():
            document[key] = value.strip()
    target = Path(path) if path is not None else setup_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return document


def remember_harness(harness: Harness) -> dict:
    """Record which harness the user pointed at (a path; never a credential)."""
    return update_setup_state(harness=harness.path)


# --- the version marker (first run vs after an update) ---------------------- #


def current_version() -> str:
    """The installed version, from package metadata — never a hand-copied literal.

    The same value Status shows (:func:`clear_record.service.archive.tool_version`),
    so a release cannot leave the wizard's marker behind: the marker is compared
    with the version the process actually is.
    """
    from clear_record.service.archive import tool_version

    return tool_version()


def seen_version(*, state: Mapping | None = None) -> str | None:
    """The version the user last dismissed or completed setup for, if any."""
    record = dict(state) if state is not None else read_setup_state()
    value = record.get("seen_version")
    return value if isinstance(value, str) and value else None


def setup_incomplete(
    *, state: Mapping | None = None, version: str | None = None
) -> bool:
    """Whether the Setup link shows: no marker recorded, or a different version.

    This is deliberately **not** "no harness is set up". A returning user whose
    agent is set up must still see Setup after an upgrade, and a first-run user
    must see it before any probe has run. Visiting or skipping records nothing;
    only :func:`record_seen_version` writes the marker.
    """
    seen = seen_version(state=state)
    current = version if version is not None else current_version()
    return seen is None or seen != current


def record_seen_version(
    *, path: str | Path | None = None, version: str | None = None
) -> dict:
    """Mark this version as seen — the one write DISMISS and COMPLETE share."""
    return update_setup_state(
        path=path, seen_version=version if version is not None else current_version()
    )


def clear_seen_version(*, path: str | Path | None = None) -> dict:
    """Forget the marker, so the Setup link returns and the wizard re-opens.

    The Settings -> Status walk-setup-again action. No other key changes, and
    the write is the same JSON record DISMISS and COMPLETE use. A record that
    does not exist is left absent: forgetting a marker writes no file.
    """
    target = Path(path) if path is not None else setup_state_path()
    if not target.is_file():
        return {}
    document = read_setup_state(path=path)
    document.pop("seen_version", None)
    target.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return document


# --- the view every surface renders ----------------------------------------- #

#: The setup states a surface branches on. ``ready`` means a harness **and** its
#: MCP client config are both recorded and still on disk; ``not_configured`` is
#: the plain "nothing is set up" the console must show; ``problem`` is a recorded
#: path that is no longer there.
STATE_READY = "ready"
STATE_NOT_CONFIGURED = "not_configured"
STATE_PROBLEM = "problem"


class SetupStatusOut(Shape):
    """The machine-readable agent setup state: what is recorded, and nothing secret.

    There is no ``endpoint``, no ``model`` and no ``api_key_env`` field, because
    this version has none of them; ``ignored`` names the 0.2 configuration that
    nothing reads any more.
    """

    state: str
    ready: bool
    harness: str | None
    mcp_config: str | None
    problems: list[str]
    ignored: list[str]


@dataclasses.dataclass(frozen=True)
class SetupView:
    """Everything a surface needs to show the agent setup state — and nothing secret.

    There is no key field and no endpoint field: this version asks for neither.
    ``ignored`` carries the 0.2 configuration this version deliberately ignores.
    """

    state: str
    harness: str | None = None
    mcp_config: str | None = None
    problems: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == STATE_READY

    def as_dict(self) -> SetupStatusOut:
        """The declared view (machine-read, so the English strings stay English)."""
        return SetupStatusOut(
            state=self.state,
            ready=self.ready,
            harness=self.harness,
            mcp_config=self.mcp_config,
            problems=list(self.problems),
            ignored=list(self.ignored),
        )


def _recorded_path_problem(label: str, value: str | None) -> str | None:
    """A recorded path that is not there any more, named as the reader sees it."""
    if not value:
        return None
    if Path(value).expanduser().is_file():
        return None
    return f"The recorded {label} {value} is not a file any more; point at one again."


def setup_view(
    *,
    environ: Mapping[str, str] | None = None,
    config_file: str | Path | None = None,
    state: Mapping | None = None,
) -> SetupView:
    """Resolve the current agent setup state (never raises; reads no key).

    The readiness it reports is what this path can actually check: the harness
    the user pointed at and the client config that was written are both still
    files on disk. Anything the 0.2 path left behind is reported through
    ``ignored`` rather than acted on.
    """
    record = dict(state) if state is not None else read_setup_state()
    harness = record.get("harness")
    mcp_config = record.get("mcp_config")
    problems = tuple(
        problem
        for problem in (
            _recorded_path_problem("agent harness", harness),
            _recorded_path_problem("MCP client config", mcp_config),
        )
        if problem is not None
    )
    if problems:
        status = STATE_PROBLEM
    elif harness and mcp_config:
        status = STATE_READY
    else:
        status = STATE_NOT_CONFIGURED
    return SetupView(
        state=status,
        harness=harness,
        mcp_config=mcp_config,
        problems=problems,
        ignored=ignored_agent_config(environ=environ, config_file=config_file),
    )


__all__ = [
    "AGENT_ENV_PREFIX",
    "DEFAULT_HARNESSES",
    "Harness",
    "MCP_SERVER_ARGS",
    "MCP_SERVER_COMMAND",
    "MCP_SERVER_NAME",
    "PI_AGENT",
    "SETUP_FILENAME",
    "SETUP_STATE_KEYS",
    "STATE_NOT_CONFIGURED",
    "STATE_PROBLEM",
    "STATE_READY",
    "SetupError",
    "SetupStatusOut",
    "SetupView",
    "clear_seen_version",
    "current_version",
    "find_harness",
    "ignored_agent_config",
    "mcp_client_config",
    "mcp_server_entry",
    "read_setup_state",
    "record_seen_version",
    "remember_harness",
    "resolve_harness",
    "seen_version",
    "setup_incomplete",
    "setup_state_path",
    "setup_view",
    "update_setup_state",
    "write_mcp_config",
]
