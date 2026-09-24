"""Where the running node is: one record, one resolver, one answer.

The API server — the **node** — is the centre of the v0.4.0 direction: the
surfaces reach *it* rather than running the pipeline in their own process — the
command line and the tray complete a request against the address it names, the
console answers with it in process, and the MCP adapter dials it only to ask
whether it is there — and the tray **attaches first**: the node that answers the
recorded address is the node it becomes a client of, and only when nothing
answers does it start one of its own. Reaching a node means knowing where it is,
and this module is the one place that knows.

The address is **recorded, never discovered**. A node writes it where the app's
own path resolution already keeps state
(:func:`clear_record.core.paths.node_address_path`, ADR-0025's state directory)
when it starts listening, and removes it when it stops; the tray's supervisor
publishes it the same way when it starts one, and probes *that* node at the socket
it bound rather than through the record — a node the tray only joined is probed
through the address the record gave it, as every surface reaches it. The surfaces
resolve that one file through :func:`recorded`: the command line, the MCP adapter
and the tray complete their request with :func:`ask`, and the console answers
with the record its own socket vouches for, with no request of its own. No surface
therefore scans a port range or guesses a port. A port the node did not choose
itself (``--port 0``) is recorded as the port its socket actually bound, because
the writer records *after* the bind, not the request.

A record nothing answers is answered as an absent one is. :class:`NoNodeError`
carries :data:`NO_NODE_MESSAGE`, so the command line, the console's
``GET /api/node`` refusal and the MCP adapter's instructions state **one**
sentence — never a hang, and never a different error per surface. (The tray's
status line is the node's own health, so it keeps its own two strings.)
The English source *is* the message ID (marked with
:func:`~clear_record.core.i18n.deferred`): a person reads ``tr(NO_NODE_MESSAGE)``
in their locale, and a machine surface (the JSON API's ``detail``, the MCP
adapter's instructions) states the English source, exactly as ``docs/i18n.md``
requires of machine-read text.

``core`` may import no third-party package (ADR-0012, ``tests/test_layering.py``),
so the one request the client makes is stdlib :mod:`urllib` — the same choice the
tray's supervisor already made for its health probe, which now goes through
:func:`reach` too, so the node is probed the one way it is reached.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

from clear_record.core.i18n import deferred, tr
from clear_record.core.paths import node_address_path

#: The node's default bind — **the** declaration of where a node listens by
#: default. The console's and the tray's ``--host``/``--port`` options, and the
#: servers every posture starts, read these and nothing else.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: The path the client asks, and the node answers, "are you there?" on. One
#: path, so the two sides cannot disagree about what answering means.
HEALTH_PATH = "/api/health"

#: A wildcard bind is not an address a client can dial; loopback is where the
#: local user reaches it. Kept here so a recorded ``0.0.0.0`` is never handed to
#: a surface as a URL it cannot use.
_DIALABLE = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}


def _is_int(value: object) -> bool:
    """Whether ``value`` is a JSON integer — ``bool`` is one to Python, not here."""
    return isinstance(value, int) and not isinstance(value, bool)


#: The one answer when no address is recorded, or nothing answers the one that
#: is. The English source is the message ID: ``tr`` renders it for a person, and
#: the machine surfaces (the JSON API, the MCP adapter) state it as it stands.
#: The two ways it names to start a node are the ones both audiences can act on:
#: ``serve`` is a command a terminal user has, and the tray app is the
#: double-click target of the desktop bundle, whose CLI is **not** on ``PATH``
#: (``packaging/pyinstaller/README.md``) — so ``serve`` alone would be a sentence
#: that audience cannot follow.
NO_NODE_MESSAGE = deferred(
    "no clear-record node is listening; start one with `clear-record serve`, or "
    "from the tray app"
)


class NoNodeError(RuntimeError):
    """Nothing answers at the node's address — absent and stale are one answer.

    Raised by :func:`address`, :func:`reach` and :func:`ask`, so every surface
    reports the one sentence :data:`NO_NODE_MESSAGE` rather than the different
    error each of them would otherwise produce (a missing file, a refused
    connection, a timeout).
    """

    def __init__(self) -> None:
        super().__init__(tr(NO_NODE_MESSAGE))


@dataclass(frozen=True)
class NodeAddress:
    """Where a node is listening, and which process recorded it.

    ``pid`` is the process that wrote the record — the node's own. It is ``None``
    for an address a caller assembled without reading one (the tray probing the
    node it started at the socket that node bound), and it is what :func:`forget`
    compares to decide whether a record is this process's to remove.
    """

    host: str
    port: int
    pid: int | None = None

    @classmethod
    def of(cls, host: str, port: int) -> NodeAddress:
        """This process's address on ``host:port``."""
        return cls(host=host, port=port, pid=os.getpid())

    @property
    def url(self) -> str:
        """The base URL a client dials; a wildcard bind is dialled on loopback."""
        host = _DIALABLE.get(self.host, self.host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # an IPv6 literal must be bracketed in a URL
        return f"http://{host}:{self.port}/"

    def url_for(self, path: str) -> str:
        """The absolute URL of ``path`` on this node."""
        return f"{self.url}{path.lstrip('/')}"

    def as_dict(self) -> dict[str, Any]:
        """The record's fields, as they are written to the address file.

        ``url`` is carried as well as the parts it is built from, so a reader
        outside this module — a shell script, another language, a human opening
        the file — does not have to know the wildcard/IPv6 dialling rule to use
        the address.
        """
        return {
            "host": self.host,
            "port": self.port,
            "pid": self.pid,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, data: object) -> NodeAddress | None:
        """The address a record describes, or ``None`` when it does not describe one.

        Total by construction: every field is *checked*, never coerced, so a
        truncated, hand-edited or foreign file — a ``pid`` that is a word, a port
        that is a list, a JSON array — is simply no address, rather than an
        exception raised out of a surface that only wanted to find the node.
        """
        if not isinstance(data, Mapping):
            return None
        host, port, pid = data.get("host"), data.get("port"), data.get("pid")
        if not isinstance(host, str) or not host:
            return None
        if not _is_int(port) or not 0 < port < 65536:
            return None
        if pid is not None and not _is_int(pid):
            return None
        return cls(host=host, port=int(port), pid=None if pid is None else int(pid))


# --- the record ------------------------------------------------------------ #


def recorded() -> NodeAddress | None:
    """The address a node recorded, or ``None`` when none is recorded.

    Read-only and cheap, and **total**: a file that is missing, truncated,
    hand-edited or written by something else is no address, never an exception.
    The console's route is its caller; :func:`address` and :func:`ask` start here.
    """
    try:
        data = json.loads(node_address_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return NodeAddress.from_dict(data)


def record(address: NodeAddress) -> Path:
    """Record *address* where every surface resolves it; return the file written.

    Written beside itself and moved into place, so a reader that runs while a
    node is starting sees the previous address or the new one — never half a
    file it would have to guess at.
    """
    path = node_address_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    pending.write_text(json.dumps(address.as_dict()), encoding="utf-8")
    os.replace(pending, path)
    return path


def forget() -> None:
    """Remove the record **this process** wrote; another node's is left alone.

    A shutting-down node must not erase the address of a node that replaced it,
    so the recorded pid is the test — the file is unlinked only when it is ours.
    """
    current = recorded()
    if current is None or current.pid != os.getpid():
        return
    try:
        node_address_path().unlink()
    except OSError:
        pass


# --- the client every surface reaches the node through --------------------- #


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Answer with the 3xx response instead of following it.

    ``urlopen`` follows redirects by default, so a health route that answered
    307 to another page would read as a node that is answering.
    """

    def http_error_301(self, req, fp, code, msg, headers):
        return fp

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301


#: One opener per process for the health request — never a redirect follower.
_OPENER = urllib.request.build_opener(_NoRedirect)


def address() -> NodeAddress:
    """The recorded address, or :class:`NoNodeError` when none is recorded."""
    current = recorded()
    if current is None:
        raise NoNodeError()
    return current


def _send(
    target: NodeAddress,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    timeout: float,
) -> tuple[int, bytes]:
    """One request to the node: its status line and its body's bytes.

    The transport both readers share. Every failure that means *nothing answered*
    — a refused connection, a timeout, a listener that does not speak HTTP — is
    :class:`NoNodeError`; an **HTTP** answer is never one of them, whatever its
    status, because that is the node talking and its words are the caller's to
    render.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    sent = urllib.request.Request(target.url_for(path), data=data, method=method)
    if data is not None:
        sent.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(sent, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:  # an answer, not a transport failure
        with exc:
            return int(exc.code), exc.read()
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
        ValueError,
    ) as exc:
        raise NoNodeError() from exc


def reach(
    target: NodeAddress,
    path: str = HEALTH_PATH,
    *,
    timeout: float = 1.0,
) -> bytes:
    """GET ``path`` from ``target``; :class:`NoNodeError` unless it answers 200.

    Only an ``HTTP 200`` counts: a redirect, a refusal, a timeout, a guard
    rejection and a listener that does not answer as HTTP at all (something else
    holds the port the record names) are all "nothing is there", which is the same
    answer as no record at all. Returning the body keeps the one client useful to
    a caller that wants the node's own answer rather than only its existence.
    """
    status, body = _send(target, "GET", path, None, timeout)
    if status != HTTPStatus.OK:
        raise NoNodeError()
    return body


@dataclass(frozen=True)
class Answer:
    """One answer from the node: its status and its decoded JSON body.

    What a caller needs when it is not asking whether the node exists but doing
    something with it: ``status`` is the HTTP status (``202`` for work accepted,
    a ``4xx`` for the node's own refusal) and ``body`` is the decoded JSON — the
    shape the API's own schema declares, or ``None`` for an empty body.
    """

    status: int
    body: Any

    @property
    def ok(self) -> bool:
        """Whether the node accepted the request (any 2xx)."""
        return 200 <= self.status < 300

    def detail(self) -> str:
        """The node's own sentence for a refusal, if its body carries one.

        The JSON API answers a refusal with ``{"detail": ...}``: a string for the
        refusals it raises deliberately, a list for a request that does not match
        its declared shape. Anything else is rendered verbatim rather than guessed
        at, so a caller always has *something* of the node's to show.
        """
        if isinstance(self.body, Mapping):
            detail = self.body.get("detail")
            if isinstance(detail, str):
                return detail
            if detail is not None:
                return json.dumps(detail)
            return json.dumps(self.body)
        return "" if self.body is None else str(self.body)


def request(
    target: NodeAddress,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> Answer:
    """Send one JSON ``method`` request to ``target`` and return its answer.

    The client a **writing** surface needs, beside :func:`reach`: the health read
    asks only whether 200 came back, while a submission has to be told *what* the
    node answered — accepted, or refused in its own words — so the status and the
    decoded body come back to the caller. A transport that never answered is still
    :class:`NoNodeError`, so every surface reports the one sentence for a node
    that is not there.

    The timeout defaults to the generous end: a request that resolves a model or
    reads a workspace before it answers is doing real work, unlike the probe.
    """
    status, payload = _send(target, method, path, body, timeout)
    parsed: Any = None
    if payload:
        try:
            parsed = json.loads(payload)
        except ValueError:
            # A body that is not JSON is still an answer: keep its text under the
            # API's own refusal key, so one reader covers both.
            parsed = {"detail": payload.decode("utf-8", "replace").strip()}
    return Answer(status=status, body=parsed)


def ask(path: str = HEALTH_PATH, *, timeout: float = 1.0) -> NodeAddress:
    """Resolve the node's address and complete one request against it.

    The one call a surface makes: the recorded address is read and the node is
    asked for ``path``. A recorded-but-stale address therefore fails exactly as
    an absent one does — one error, from every surface, with no port scanned and
    no second candidate tried (a node elsewhere on the machine is not the node
    that was recorded).
    """
    target = address()
    reach(target, path, timeout=timeout)
    return target


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "HEALTH_PATH",
    "NO_NODE_MESSAGE",
    "Answer",
    "NoNodeError",
    "NodeAddress",
    "address",
    "ask",
    "forget",
    "reach",
    "record",
    "recorded",
    "request",
]
