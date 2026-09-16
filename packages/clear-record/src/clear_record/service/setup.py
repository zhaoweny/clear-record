"""Guided agent setup: detect a local endpoint, verify it, record the choice.

ADR-0018's onboarding half (ticket 20): get a user from "installed" to "a task
ran" **without hand-editing TOML**. This module owns the steps that reach a
working runner, and nothing that *runs* a task — the runner seam
(:mod:`clear_record.service.agent`) stays the only executor.

The rungs, in setup-cost order:

1. **Detect** a local OpenAI-compatible server — Ollama, LM Studio, llama.cpp —
   by asking each one's own default address for its model list. Nothing is
   assumed to be running: an unreachable candidate is a fact, not an error, and
   it carries the exact thing to install or run.
2. **Verify** with a real test call. A model list proves a server *answers*; only
   a ``/chat/completions`` round trip proves it can run a task, so the first
   server that serves a model is exercised through the same
   :class:`~clear_record.service.agent.EndpointRunner` the tasks use. A
   verification failure is reported, never swallowed.
3. **Pull a small model** where the server supports it (Ollama's native
   ``/api/pull``), so "detected but no model" has a fix that is not a shell.
4. **Record the choice** by writing a **managed block** into the config file —
   endpoint and model, so every later surface (the CLI, the console, a task run)
   reads the same plumbing through the existing
   :func:`~clear_record.service.agent.load_agent_config`.
5. **MCP rung.** pi-agent is the *default named* harness, never bundled: the
   setup finds one on ``PATH``, accepts a path the user points at, or names the
   download step, then writes the standard ``mcpServers`` entry for
   ``clear-record mcp`` into a client config the user names. Any other
   MCP-capable client works identically — the entry is a command and args, and
   nothing here knows pi-agent's own config schema.

Two rules this module holds to:

- **Never a credential.** The config carries ``api_key_env`` — the *name* of an
  environment variable — and never a value; a local endpoint writes no auth key
  at all. The MCP entry is a command and args, so it has nowhere to put one. The
  setup state file records non-secret facts only (which harness, which config
  path, which model was chosen).
- **No new dependency, no branching shell.** Every probe is stdlib
  :mod:`urllib`; the write is :mod:`json`/:mod:`tomllib` plus a deliberate,
  marker-bounded text block, so a user's own TOML comments survive.

User-facing failures are a stable message ID plus parameters
(:class:`clear_record.cli.auto.Message`, marked with
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
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from clear_record.cli.auto import Message
from clear_record.core.i18n import deferred
from clear_record.service.agent import (
    EndpointRunner,
    RunnerError,
    RunnerRequest,
    load_agent_config,
    reset_default_config,
)
from clear_record.service.paths import config_path, resolve_state_dir

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


# --- the candidates we look for --------------------------------------------- #

#: Seconds to wait for a detection probe. Short: a local server either answers
#: immediately or is not running, and detection must not stall a page.
DETECT_TIMEOUT = 1.5
#: Seconds to wait for a verification call. Generous: a cold model load on a
#: CPU-only box can take a while, and a false "does not work" is worse than a
#: slow one.
VERIFY_TIMEOUT = 120.0
#: Seconds to wait for a model pull. A small model is still a download.
PULL_TIMEOUT = 1800.0

#: The small model the Ollama rung offers to pull. Small enough to be a
#: reasonable default download and strong enough to follow the task contracts'
#: strict JSON, which is the one thing a tiny model tends to fail.
DEFAULT_SMALL_MODEL = "qwen2.5:1.5b"


@dataclasses.dataclass(frozen=True)
class EndpointCandidate:
    """One local server we know how to look for, and how to get it.

    ``install_hint`` is a message ID (marked with
    :func:`~clear_record.core.i18n.deferred`), so "nothing is listening" reads
    as a translated instruction rather than a bare connection error.
    """

    slug: str
    label: str
    base_url: str
    install_hint: str

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "label": self.label,
            "base_url": self.base_url,
            "install_hint": self.install_hint,
        }


#: The local OpenAI-compatible servers the setup knows, in probe order. Each
#: address is the server's own documented default, and each hint says the one
#: thing that makes it answer.
LOCAL_CANDIDATES: tuple[EndpointCandidate, ...] = (
    EndpointCandidate(
        slug="ollama",
        label="Ollama",
        base_url="http://127.0.0.1:11434/v1",
        install_hint=deferred(
            "install Ollama from https://ollama.com, then run `ollama serve`"
        ),
    ),
    EndpointCandidate(
        slug="lm_studio",
        label="LM Studio",
        base_url="http://127.0.0.1:1234/v1",
        install_hint=deferred(
            "open LM Studio, load a model, and start its local server"
        ),
    ),
    EndpointCandidate(
        slug="llama_cpp",
        label="llama.cpp server",
        base_url="http://127.0.0.1:8080/v1",
        install_hint=deferred(
            "run the llama.cpp OpenAI-compatible server, e.g. "
            "`llama-server -m <model.gguf>`"
        ),
    ),
)

#: Probe key for an endpoint the user supplied (the console's "I have my own").
CUSTOM_SLUG = "custom"


def _custom_candidate(endpoint: str) -> EndpointCandidate:
    url = endpoint.strip().rstrip("/")
    return EndpointCandidate(
        slug=CUSTOM_SLUG,
        label=url,
        base_url=url,
        install_hint=deferred("start the server this address belongs to"),
    )


# --- probing ---------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Probe:
    """What one candidate's address turned out to be.

    A probe is **observational**: an unreachable address is a normal result with
    :attr:`detail` explaining it, never an exception. ``models`` is what the
    server says it serves; ``verified`` is set only by a real test call.
    """

    candidate: EndpointCandidate
    reachable: bool
    models: tuple[str, ...] = ()
    pull_supported: bool = False
    detail: Message | None = None
    verified: bool = False
    verified_model: str | None = None
    verify_detail: Message | None = None

    @property
    def slug(self) -> str:
        return self.candidate.slug

    @property
    def serving(self) -> bool:
        """Reachable *and* declaring at least one model to run it with."""
        return self.reachable and bool(self.models)

    def as_dict(self) -> dict:
        return {
            "slug": self.slug,
            "label": self.candidate.label,
            "base_url": self.candidate.base_url,
            "reachable": self.reachable,
            "models": list(self.models),
            "pull_supported": self.pull_supported,
            "verified": self.verified,
            "verified_model": self.verified_model,
            "detail": str(self.detail) if self.detail is not None else None,
            "verify_detail": (
                str(self.verify_detail) if self.verify_detail is not None else None
            ),
            "install_hint": self.candidate.install_hint,
        }


@dataclasses.dataclass(frozen=True)
class Detection:
    """Every candidate probed, in order, with the first usable one findable."""

    probes: tuple[Probe, ...]

    @property
    def reachable(self) -> tuple[Probe, ...]:
        return tuple(probe for probe in self.probes if probe.reachable)

    @property
    def verified(self) -> tuple[Probe, ...]:
        return tuple(probe for probe in self.probes if probe.verified)

    @property
    def serving(self) -> tuple[Probe, ...]:
        return tuple(probe for probe in self.probes if probe.serving)

    @property
    def best(self) -> Probe | None:
        """The endpoint setup would offer: verified first, then serving, then up.

        Ordered by how much is *proven*, not by list order: a verified server
        beats one that merely lists a model, which beats one that only answers.
        """
        for group in (self.verified, self.serving, self.reachable):
            if group:
                return group[0]
        return None

    def as_dict(self) -> dict:
        return {"probes": [probe.as_dict() for probe in self.probes]}


def _models_url(base_url: str) -> str:
    """The OpenAI-compatible model-list URL for a base URL."""
    return base_url.rstrip("/") + "/models"


def _native_base(base_url: str) -> str:
    """The server origin, with an OpenAI ``/v1`` suffix stripped.

    Ollama's native API (``/api/tags``, ``/api/pull``) lives at the origin, not
    under the OpenAI-compatible ``/v1`` prefix, so the pull rung needs it.
    """
    origin = base_url.rstrip("/")
    if origin.endswith("/v1"):
        origin = origin[: -len("/v1")]
    return origin.rstrip("/")


def _model_ids(document: object) -> tuple[str, ...]:
    """Model names from either shape: OpenAI ``data[].id`` or Ollama ``models[].name``."""
    if not isinstance(document, dict):
        return ()
    rows = document.get("data")
    if rows is None:
        rows = document.get("models")
    if not isinstance(rows, list):
        return ()
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("id") or row.get("name") or row.get("model")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(dict.fromkeys(names))


def _body_snippet(body: bytes, limit: int = 200) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    return text[:limit] if text else "(empty body)"


def _request_json(
    url: str,
    *,
    opener: Callable[..., object],
    timeout: float,
    method: str = "GET",
    payload: Mapping | None = None,
) -> object:
    """One HTTP call, decoded as JSON, with every failure a :class:`SetupError`.

    ``opener`` is injectable (the :class:`EndpointRunner` seam, and how the
    tests drive a fake endpoint); the message carries the URL and the transport
    detail, never a response body in full.
    """
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        response = opener(request, timeout=timeout)
        try:
            raw = response.read()  # type: ignore[attr-defined]
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
    except urllib.error.HTTPError as exc:
        snippet = _body_snippet(exc.read())
        exc.close()
        raise SetupError(
            Message(
                deferred("could not reach {url}: HTTP {status} ({detail})"),
                (("url", url), ("status", exc.code), ("detail", snippet)),
            )
        ) from exc
    except SetupError:
        raise
    except Exception as exc:  # noqa: BLE001 - any transport failure is one error
        raise SetupError(
            Message(
                deferred("could not reach {url}: {detail}"),
                (("url", url), ("detail", f"{type(exc).__name__}: {exc}")),
            )
        ) from exc
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise SetupError(
            Message(
                deferred("{url} returned a response that is not JSON ({detail})"),
                (("url", url), ("detail", exc.msg)),
            )
        ) from exc


def probe_endpoint(
    candidate: EndpointCandidate,
    *,
    opener: Callable[..., object] | None = None,
    timeout: float = DETECT_TIMEOUT,
) -> Probe:
    """Ask one candidate whether it is running and what it serves.

    Never raises for an unreachable address (that is the common case, and the
    answer this rung exists to give). Ollama is additionally asked its native
    ``/api/tags``, which both lists models when the OpenAI shim is off and is the
    positive signal that the pull rung will work.
    """
    open_url = opener or urllib.request.urlopen
    models: tuple[str, ...] = ()
    reachable = False
    detail: Message | None = None
    try:
        document = _request_json(
            _models_url(candidate.base_url), opener=open_url, timeout=timeout
        )
    except SetupError as exc:
        detail = exc.message
    else:
        reachable = True
        models = _model_ids(document)

    pull_supported = False
    if candidate.slug == "ollama":
        try:
            native = _request_json(
                _native_base(candidate.base_url) + "/api/tags",
                opener=open_url,
                timeout=timeout,
            )
        except SetupError:
            pull_supported = False
        else:
            pull_supported = True
            if not models:
                models = _model_ids(native)
            if not reachable:
                reachable = True
                detail = None

    return Probe(
        candidate=candidate,
        reachable=reachable,
        models=models,
        pull_supported=pull_supported,
        detail=detail,
    )


@dataclasses.dataclass(frozen=True)
class Verification:
    """The result of one real ``/chat/completions`` test call."""

    ok: bool
    endpoint: str
    model: str | None = None
    detail: Message | None = None


def verify_endpoint(
    endpoint: str,
    *,
    model: str | None = None,
    api_key_env: str | None = None,
    opener: Callable[..., object] | None = None,
    timeout: float = VERIFY_TIMEOUT,
    environ: Mapping[str, str] | None = None,
) -> Verification:
    """Prove an endpoint can answer a task-shaped call, with a real request.

    This goes through :class:`~clear_record.service.agent.EndpointRunner` — the
    exact runner the tasks use — so verification exercises the real protocol,
    the real model name and the real fail-closed key handling (a named but unset
    ``api_key_env`` refuses rather than calling unauthenticated). The credential
    is still only a name: nothing here reads or stores a value.
    """
    endpoint = endpoint.strip()
    if not endpoint:
        return Verification(
            ok=False,
            endpoint=endpoint,
            model=model,
            detail=Message(deferred("an endpoint URL is required"), ()),
        )
    try:
        runner = EndpointRunner(
            endpoint,
            model=model or None,
            api_key_env=api_key_env or None,
            timeout=timeout,
            opener=opener,
            environ=environ,
        )
    except Exception as exc:  # noqa: BLE001 - a bad endpoint is a result, not a crash
        return Verification(
            ok=False,
            endpoint=endpoint,
            model=model,
            detail=Message(
                deferred("{endpoint} could not be used: {detail}"),
                (("endpoint", endpoint), ("detail", str(exc))),
            ),
        )
    request = RunnerRequest(
        step="verify",
        prompt=TEST_CALL_PROMPT,
        input_json={},
        project="",
        meeting="",
        kind="setup",
        output_file=Path("setup-verify.json"),
    )
    try:
        output = runner.run(request)
    except RunnerError as exc:
        return Verification(
            ok=False,
            endpoint=endpoint,
            model=model,
            detail=Message(
                deferred("{endpoint} did not answer the test call: {detail}"),
                (("endpoint", endpoint), ("detail", str(exc))),
            ),
        )
    return Verification(ok=True, endpoint=endpoint, model=output.model or model)


#: The test call's prompt. It is sent to the model, never shown to a user, so it
#: is deliberately not a translated string: the model's job is a completion, not
#: a label.
TEST_CALL_PROMPT = "Reply with the single word: ok"


def _verifying_probe(
    probe: Probe,
    *,
    opener: Callable[..., object] | None,
    timeout: float,
    environ: Mapping[str, str] | None,
) -> Probe:
    result = verify_endpoint(
        probe.candidate.base_url,
        model=probe.models[0],
        opener=opener,
        timeout=timeout,
        environ=environ,
    )
    return dataclasses.replace(
        probe,
        verified=result.ok,
        verified_model=result.model if result.ok else None,
        verify_detail=result.detail,
    )


def detect(
    *,
    candidates: Sequence[EndpointCandidate] | None = None,
    endpoint: str | None = None,
    opener: Callable[..., object] | None = None,
    timeout: float = DETECT_TIMEOUT,
    environ: Mapping[str, str] | None = None,
    verify: bool = True,
) -> Detection:
    """Probe the known local servers, and optionally test-call the first usable.

    An explicit ``endpoint`` (the user's own address, or the configured one) is
    probed **first**, so the setup never offers to replace a working endpoint
    with a discovered one. Exactly one verification call is made — the first
    probe that is reachable and serves a model — so detection stays bounded even
    when several servers are running.
    """
    chosen = list(candidates) if candidates is not None else list(LOCAL_CANDIDATES)
    if endpoint and endpoint.strip():
        chosen.insert(0, _custom_candidate(endpoint))

    probes = [
        probe_endpoint(candidate, opener=opener, timeout=timeout)
        for candidate in chosen
    ]
    if verify:
        verified = False
        tested: list[Probe] = []
        for probe in probes:
            if not verified and probe.serving:
                probe = _verifying_probe(
                    probe, opener=opener, timeout=timeout, environ=environ
                )
                verified = True
            tested.append(probe)
        probes = tested
    return Detection(probes=tuple(probes))


# --- pulling a small model -------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ModelPull:
    """The outcome of asking a server to fetch a model."""

    ok: bool
    model: str
    detail: Message | None = None


def pull_model(
    model: str,
    *,
    endpoint: str,
    opener: Callable[..., object] | None = None,
    timeout: float = PULL_TIMEOUT,
) -> ModelPull:
    """Ask an Ollama server to pull ``model`` (its native, non-streaming API).

    Only Ollama supports this; a server that does not is told so plainly rather
    than sent a request it will not understand. The response is checked for an
    ``error`` field, because Ollama reports a failed pull as a 200 with an error
    body rather than an HTTP status.
    """
    model = model.strip()
    if not model:
        return ModelPull(
            ok=False,
            model=model,
            detail=Message(deferred("a model name is required to pull one"), ()),
        )
    url = _native_base(endpoint) + "/api/pull"
    try:
        document = _request_json(
            url,
            opener=opener or urllib.request.urlopen,
            timeout=timeout,
            method="POST",
            payload={"name": model, "stream": False},
        )
    except SetupError as exc:
        return ModelPull(ok=False, model=model, detail=exc.message)
    if isinstance(document, dict) and document.get("error"):
        return ModelPull(
            ok=False,
            model=model,
            detail=Message(
                deferred("the endpoint refused to pull {model}: {detail}"),
                (("model", model), ("detail", str(document["error"]))),
            ),
        )
    return ModelPull(ok=True, model=model)


# --- recording the choice (config, never a credential) ---------------------- #

#: The managed block's markers. Everything between them is written by setup and
#: replaced wholesale on the next run; everything outside them is the user's and
#: is byte-for-byte untouched — which is why the write is marker-bounded rather
#: than a TOML round trip that would discard the user's comments.
MANAGED_BEGIN = "# >>> clear-record agent setup >>>"
MANAGED_END = "# <<< clear-record agent setup <<<"


def _toml_string(value: str) -> str:
    """A TOML basic string for ``value`` (JSON quoting is TOML-compatible)."""
    return json.dumps(value, ensure_ascii=False)


def render_agent_block(
    *,
    endpoint: str,
    model: str | None = None,
    api_key_env: str | None = None,
) -> str:
    """The ``[agent]`` table setup would write, markers included.

    The block is exactly what :func:`write_agent_settings` persists; showing it
    is how the console and the wizard can say what they are about to do. It
    contains an environment **variable name** at most — there is no field a
    secret could occupy.
    """
    lines = [
        MANAGED_BEGIN,
        "[agent]",
        f"endpoint = {_toml_string(endpoint)}",
    ]
    if model:
        lines.append(f"model = {_toml_string(model)}")
    if api_key_env:
        lines.append(
            f"api_key_env = {_toml_string(api_key_env)}  # a NAME; never the value"
        )
    lines.append(MANAGED_END)
    return "\n".join(lines) + "\n"


def _replace_managed_block(text: str, block: str) -> str:
    """Replace the marked block with ``block``, leaving every other line alone."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == MANAGED_BEGIN and not skipping:
            skipping = True
            out.append(block)
            continue
        if skipping:
            if stripped == MANAGED_END:
                skipping = False
            continue
        out.append(line)
    return "".join(out)


def write_agent_settings(
    endpoint: str,
    *,
    model: str | None = None,
    api_key_env: str | None = None,
    config_file: str | Path | None = None,
) -> Path:
    """Write the endpoint/model into the config's managed ``[agent]`` block.

    Refuses, rather than guesses, when the config file already carries an
    ``[agent]`` table outside the managed block: that table is the user's own
    hand-written plumbing and silently appending a second one would produce an
    invalid TOML file. The process-wide default config is dropped afterwards, so
    the very next task run reads what was just written.

    ``api_key_env`` is the **name** of an environment variable. No value is
    accepted here and none is written anywhere; a local endpoint passes nothing.
    """
    endpoint = endpoint.strip()
    if not endpoint:
        raise SetupError(Message(deferred("an endpoint URL is required"), ()))
    model = model.strip() if model and model.strip() else None
    api_key_env = api_key_env.strip() if api_key_env and api_key_env.strip() else None
    path = Path(config_file) if config_file is not None else config_path()

    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    managed = MANAGED_BEGIN in existing and MANAGED_END in existing
    if path.is_file() and not managed:
        try:
            document = tomllib.loads(existing)
        except tomllib.TOMLDecodeError as exc:
            raise SetupError(
                Message(
                    deferred("the config file {path} is not valid TOML ({detail})"),
                    (("path", str(path)), ("detail", exc.msg)),
                )
            ) from exc
        if "agent" in document:
            raise SetupError(
                Message(
                    deferred(
                        "the config file {path} already has an [agent] table; "
                        "edit it by hand, or remove it so setup can manage it"
                    ),
                    (("path", str(path)),),
                )
            )

    block = render_agent_block(endpoint=endpoint, model=model, api_key_env=api_key_env)
    if managed:
        text = _replace_managed_block(existing, block)
    elif existing.strip():
        text = existing.rstrip("\n") + "\n\n" + block
    else:
        text = block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    update_setup_state(endpoint=endpoint, model=model)
    reset_default_config()
    return path


# --- the MCP rung: point at a harness, write its MCP config ----------------- #

#: The MCP server entry's name inside the client's ``mcpServers`` table.
MCP_SERVER_NAME = "clear-record"
#: The command an MCP client should run: the installed console script.
MCP_SERVER_COMMAND = "clear-record"
#: Its arguments: the stdio MCP subcommand (ADR-0017).
MCP_SERVER_ARGS: tuple[str, ...] = ("mcp",)

#: pi-agent is ADR-0018's **default named** harness, not a dependency. Detection
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
    (ADR-0017's BYOK note: the *agent* brings its own model).
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


# --- the setup record (non-secret state, app-owned) ------------------------- #

#: Setup's own record: which harness was pointed at and which client config was
#: written. **Config** holds the operative endpoint/model (the runner reads it);
#: this file holds facts about the *setup* that no runner needs, and it is
#: app-owned state rather than a user document.
SETUP_FILENAME = "agent-setup.json"
#: The only keys setup ever writes. A key is an allow-list so a future caller
#: cannot quietly persist something that is not a setup fact — and so no field
#: here could ever be a credential.
SETUP_STATE_KEYS = ("endpoint", "model", "harness", "mcp_config", "seen_version")


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


# --- the version marker (first run vs after an update, ticket 04) ----------- #


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

    This is deliberately **not** "no runner is configured". A returning user
    whose agent is set up must still see Setup after an upgrade, and a first-run
    user must see it before any probe has run. Visiting or skipping records
    nothing; only :func:`record_seen_version` writes the marker.
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

#: The setup states a surface branches on. ``ready`` means a runner exists;
#: ``not_configured`` is the plain "nothing is set up" the console must show;
#: ``problem`` is a config that exists but cannot be used.
STATE_READY = "ready"
STATE_NOT_CONFIGURED = "not_configured"
STATE_PROBLEM = "problem"


@dataclasses.dataclass(frozen=True)
class SetupView:
    """Everything a surface needs to show the setup state — and nothing secret.

    ``api_key_env`` is a variable **name**; there is no field holding a value.
    ``detection`` is ``None`` when no probe has been run, which is what lets the
    home page render instantly and the "Detect" action do the waiting.
    """

    state: str
    endpoint: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    commands: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()
    detection: Detection | None = None
    harness: str | None = None
    mcp_config: str | None = None

    @property
    def configured(self) -> bool:
        return self.state == STATE_READY

    @property
    def has_runner(self) -> bool:
        return bool(self.endpoint or self.commands)

    def as_dict(self) -> dict:
        """The JSON-safe view (machine-read, so the English strings stay English)."""
        return {
            "state": self.state,
            "configured": self.configured,
            "endpoint": self.endpoint,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "commands": list(self.commands),
            "problems": list(self.problems),
            "harness": self.harness,
            "mcp_config": self.mcp_config,
            "detection": (
                self.detection.as_dict() if self.detection is not None else None
            ),
        }


def setup_view(
    *,
    detection: Detection | None = None,
    environ: Mapping[str, str] | None = None,
    config_file: str | Path | None = None,
    state: Mapping | None = None,
) -> SetupView:
    """Resolve the current setup state (never raises; never reads a key's value).

    ``load_agent_config`` is the one source of what the runners will use, so the
    console cannot show a different endpoint from the one a task would call. A
    malformed config is a state *with problems*, not an exception — the config
    surface is the service's own, surfaced verbatim, exactly as the console's
    webhook panel does.
    """
    config = load_agent_config(environ=environ, config_file=config_file)
    record = dict(state) if state is not None else read_setup_state()
    has_runner = bool(config.endpoint or config.commands)
    if has_runner:
        status = STATE_READY
    elif config.problems:
        status = STATE_PROBLEM
    else:
        status = STATE_NOT_CONFIGURED
    return SetupView(
        state=status,
        endpoint=config.endpoint,
        model=config.model,
        api_key_env=config.api_key_env,
        commands=tuple(sorted(config.commands)),
        problems=tuple(config.problems),
        detection=detection,
        harness=record.get("harness"),
        mcp_config=record.get("mcp_config"),
    )


__all__ = [
    "CUSTOM_SLUG",
    "DEFAULT_HARNESSES",
    "DEFAULT_SMALL_MODEL",
    "DETECT_TIMEOUT",
    "Detection",
    "EndpointCandidate",
    "Harness",
    "LOCAL_CANDIDATES",
    "MANAGED_BEGIN",
    "MANAGED_END",
    "MCP_SERVER_ARGS",
    "MCP_SERVER_COMMAND",
    "MCP_SERVER_NAME",
    "ModelPull",
    "PI_AGENT",
    "PULL_TIMEOUT",
    "Probe",
    "SETUP_FILENAME",
    "SETUP_STATE_KEYS",
    "STATE_NOT_CONFIGURED",
    "STATE_PROBLEM",
    "STATE_READY",
    "SetupError",
    "SetupView",
    "TEST_CALL_PROMPT",
    "VERIFY_TIMEOUT",
    "Verification",
    "clear_seen_version",
    "current_version",
    "detect",
    "find_harness",
    "mcp_client_config",
    "mcp_server_entry",
    "probe_endpoint",
    "pull_model",
    "read_setup_state",
    "record_seen_version",
    "remember_harness",
    "render_agent_block",
    "resolve_harness",
    "seen_version",
    "setup_incomplete",
    "setup_state_path",
    "setup_view",
    "update_setup_state",
    "verify_endpoint",
    "write_agent_settings",
    "write_mcp_config",
]
