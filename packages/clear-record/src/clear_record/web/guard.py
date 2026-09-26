"""A thin request guard for the localhost console (ADR-0021).

The console binds ``127.0.0.1`` and gates what it serves behind one credential
(ADR-0033), but "bound to localhost" bounds **who can connect**, not **who can
act**: a hostile page open in the same browser can be induced to POST to
``127.0.0.1`` (CSRF), and DNS rebinding can point a remote name at localhost.
This module rejects the two browser-borne shapes — and it runs *outside* the auth
gate, so both are answered before a session is looked at:

- a ``Host`` that is not a trusted host — the DNS-rebinding defence, checked on
  **every** request because a rebound request is no less readable for being a
  ``GET``;
- an ``Origin``/``Referer`` that is not a trusted host — the CSRF defence,
  checked only on **state-changing** requests, where those headers carry meaning.

A state-changing request with neither ``Origin`` nor ``Referer`` (``curl``, a
script, the MCP client) is allowed: browsers attach ``Origin`` to cross-origin
state-changing requests, so its absence is not the attack shape, and rejecting
it would break the machine surface the console exists to offer.

Trusted hosts are the loopback names — ``127.0.0.1``, ``::1``, ``localhost``
(and ``*.localhost``) — plus any comma-separated hostnames in the
``CR_TRUSTED_HOSTS`` environment variable. That variable is the documented
escape hatch for a console reached through a reverse proxy: the proxy's public
hostname (and the name the browser puts in ``Origin``) goes there.

A second list lives here too: the peers in ``CR_TRUSTED_PROXIES``, whose
forwarded headers this console honours — **and only theirs**. A request from any
other peer is judged by the socket it arrived on and the ``Host`` it carries;
its ``X-Forwarded-*`` headers are ignored rather than merged in, so a client
cannot nominate its own scheme, host or address. For a declared peer the three
headers are resolved into the request *before* the checks below and before the
auth gate reads the scheme (:func:`forwarded_facts`, :func:`apply_forwarded`),
which is what makes a TLS-terminating proxy's ``X-Forwarded-Proto`` the session
cookie's ``Secure`` and its ``X-Forwarded-Host`` the console's absolute URLs. The
forwarded name drives those URLs and the ``Host`` check above; it never drives the
path-local rule, which reads the ``Host`` the *client* itself sent
(:func:`client_named_host`) — so a client's own ``X-Forwarded-Host: 127.0.0.1``
does not make it local.

That declaration names a **peer**, so it is not a trust source for the ``Host``
check: the name a proxy forwards still has to be in ``CR_TRUSTED_HOSTS``, which
is also the one declaration that admits a non-loopback bind
(:func:`names_a_trusted_host`) — a bind admitted on the peer alone would start a
console that refuses every request, which is what the startup refusal exists to
prevent.

No domain logic lives here and nothing is stored; the guard is a function of the
request headers, the socket's peer and the configured host and peer sets.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, NamedTuple
from urllib.parse import urlsplit

#: Methods that cannot change server state, and so are exempt from the CSRF
#: (``Origin``/``Referer``) half of the guard. ``Host`` is checked regardless.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Comma-separated extra hostnames the console may be reached by. Empty by
#: default, so a stock install trusts loopback only.
TRUSTED_HOSTS_ENV = "CR_TRUSTED_HOSTS"

#: Comma-separated peers whose forwarded headers the console honours. Empty by
#: default, so a stock install believes **no** forwarded header: every request is
#: judged by the socket it arrived on and the ``Host`` it carries. Naming a peer
#: here is the whole declaration a TLS-terminating proxy needs — its
#: ``X-Forwarded-Proto`` then drives the session cookie's ``Secure``, its
#: ``X-Forwarded-Host`` the console's absolute URLs (never the name the
#: path-local rule reads, which is the ``Host`` the client itself sent), and its
#: ``X-Forwarded-For`` the address the app attributes the request to (which no
#: reader consumes yet). It is deliberately
#: **not** a trust source for the ``Host`` check: the name a proxy forwards still
#: has to be in :data:`TRUSTED_HOSTS_ENV`.
TRUSTED_PROXIES_ENV = "CR_TRUSTED_PROXIES"

#: The forwarded headers a declared peer's requests may carry, lower-cased the way
#: a request's own header mapping spells them.
FORWARDED_PROTO = "x-forwarded-proto"
FORWARDED_HOST = "x-forwarded-host"
FORWARDED_FOR = "x-forwarded-for"

#: The ASGI scope key :func:`apply_forwarded` stashes the ``Host`` the **client**
#: sent under, just before a declared peer's forwarded name replaces it in the
#: scope. :func:`client_named_host` reads it, and it is what the path-local rule
#: (:func:`clear_record.web.app._local_client`) is written on, so a forwarded name
#: can drive the console's URLs and never its path-local decision. Namespaced
#: because the scope is shared with every other middleware and the router.
CLIENT_HOST_KEY = "clear_record.client_host"

#: The schemes ``X-Forwarded-Proto`` may name — what a browser can be behind.
#: Anything else (a ``ws`` hop, a whole URL, a typo) is ignored rather than
#: written into the request, so the scheme stays one of the two the console's own
#: cookie and URL decisions are written for.
_FORWARDED_SCHEMES = frozenset({"http", "https"})

_LOOPBACK_HINT = "127.0.0.1, localhost, ::1 or a name in CR_TRUSTED_HOSTS"


def host_name(value: str | None) -> str | None:
    """The bare hostname of a ``Host``/authority header, or None.

    Strips a ``:port`` suffix and IPv6 brackets, lowercases, and drops a
    trailing FQDN dot, so ``[::1]:8765``, ``127.0.0.1:8765`` and ``LOCALHOST.``
    all normalize to the names the guard compares.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        return text[1:end].lower() or None
    # A single colon is a port suffix; a bare IPv6 literal has more than one.
    if text.count(":") == 1:
        text = text.rsplit(":", 1)[0]
    name = text.lower().rstrip(".")
    return name or None


def origin_host(value: str | None) -> str | None:
    """The hostname of an ``Origin``/``Referer`` URL, or None if unusable."""
    if value is None:
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ("http", "https"):
        return None
    return (parsed.hostname or "").lower() or None


def is_loopback_host(name: str | None) -> bool:
    """Whether ``name`` is a loopback name (the defaults, independent of config)."""
    if name is None:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def normalize_hosts(values: Iterable[str]) -> frozenset[str]:
    """Normalize configured hostnames to the form :func:`is_trusted` compares."""
    return frozenset(name for value in values if (name := host_name(value)))


def trusted_extra_hosts(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The extra trusted hosts from ``CR_TRUSTED_HOSTS`` (empty when unset)."""
    source = os.environ if env is None else env
    return normalize_hosts(source.get(TRUSTED_HOSTS_ENV, "").split(","))


def trusted_proxies(env: Mapping[str, str] | None = None) -> frozenset[str]:
    """The peers declared in ``CR_TRUSTED_PROXIES`` (empty when unset).

    Normalized the way the trusted hosts are — a bare address or hostname, ports
    and brackets stripped — so one spelling serves both this declaration and the
    peer comparison :func:`is_declared_proxy` makes. Nothing here is a ``Host``
    the guard trusts: this list names the peers whose forwarded headers are
    believed (see :func:`names_a_trusted_host`).
    """
    source = os.environ if env is None else env
    return normalize_hosts(source.get(TRUSTED_PROXIES_ENV, "").split(","))


def is_declared_proxy(peer: str | None, declared: frozenset[str]) -> bool:
    """Whether ``peer`` is one of the peers the operator declared a proxy.

    ``peer`` is the address the **socket** delivered (``scope["client"]``), never
    one a header claims: a header is exactly what a client would use to nominate
    itself, so the socket stays the truth about who is speaking.
    """
    name = host_name(peer)
    return name is not None and name in declared


class Forwarded(NamedTuple):
    """What a declared peer's forwarded headers say about one request.

    Each field is ``None`` when nothing usable was forwarded — which is also what
    any request from an undeclared peer resolves to, since its headers are not
    read at all. A caller then keeps the socket's own scheme, ``Host`` and
    client, and judges the request as the peer sent it.
    """

    scheme: str | None = None
    host: str | None = None
    client: str | None = None


def _forwarded_token(value: str | None) -> str | None:
    """The first value of a comma-separated forwarded header, or None.

    Forwarded headers accumulate as a request passes through proxies, and each
    proxy appends its own hop: the first value is the one furthest from *this*
    server, so it is the one describing the browser's own connection.
    """
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _forwarded_header(headers: Mapping[str, str], name: str) -> str | None:
    """One forwarded header, from a mapping keyed with or without its case.

    A request's own header mapping answers case-insensitively, as HTTP requires
    (``request.headers``, the only caller's); a plain mapping does not, and a
    header silently ignored over a capital letter is how a security decision goes
    missing. The exact key is tried first, so the usual path costs one lookup.
    """
    value = headers.get(name)
    if value is not None:
        return value
    for key, candidate in headers.items():
        if key.lower() == name:
            return candidate
    return None


def _forwarded_scheme(value: str | None) -> str | None:
    """``http``/``https`` when a declared peer forwarded one, else None."""
    token = _forwarded_token(value)
    scheme = token.lower() if token is not None else None
    return scheme if scheme in _FORWARDED_SCHEMES else None


def _forwarded_host(value: str | None) -> str | None:
    """The authority a declared peer forwarded, or None if it is not one.

    An authority and nothing else — ``console.example.com`` or
    ``console.example.com:8443``, port kept because it is part of the name the
    browser addressed. A value carrying a path, whitespace, userinfo or anything
    outside ASCII is refused, so a misconfigured proxy cannot write a scheme or a
    path into the console's absolute URLs; the request's own ``Host`` then stays
    in force and is checked as usual.
    """
    authority = _forwarded_token(value)
    if authority is None or not authority.isascii():
        return None
    if any(char in authority for char in "/\\@ \t"):
        return None
    return authority


def _forwarded_client(value: str | None, declared: frozenset[str]) -> str | None:
    """The client address a declared peer's ``X-Forwarded-For`` chain leads to.

    The chain is read **right to left** — each hop appends, so the rightmost
    entry is the one this server's own peer saw — and the first address that is
    not itself a declared proxy is the client. A chain of nothing but declared
    peers means the request came through the trusted chain from its far end, so
    the leftmost entry is the client. An entry the guard cannot read as a name is
    skipped: the declaration is what makes a header believable, and one
    unreadable hop does not turn the rest of the chain into a guess.
    """
    names = [name for token in (value or "").split(",") if (name := host_name(token))]
    for name in reversed(names):
        if name not in declared:
            return name
    return names[0] if names else None


def forwarded_facts(
    headers: Mapping[str, str],
    peer: str | None,
    declared: frozenset[str],
) -> Forwarded:
    """What this request's forwarded headers say, if its peer may say anything.

    ``headers`` is the request's own header mapping and ``peer`` the address the
    socket delivered. The answer is empty — every field ``None`` — unless
    ``peer`` is a declared proxy, and then only for the headers it actually sent:
    an undeclared peer's ``X-Forwarded-*`` is never read, which is what keeps the
    socket the truth for everyone else.
    """
    if not is_declared_proxy(peer, declared):
        return Forwarded()
    return Forwarded(
        scheme=_forwarded_scheme(_forwarded_header(headers, FORWARDED_PROTO)),
        host=_forwarded_host(_forwarded_header(headers, FORWARDED_HOST)),
        client=_forwarded_client(_forwarded_header(headers, FORWARDED_FOR), declared),
    )


def _host_header(scope: Mapping[str, Any]) -> str | None:
    """The scope's ``Host`` header, as the ASGI headers list spells it.

    The bytes are decoded as latin-1 — the ASGI convention for header values,
    and what Starlette's own ``Headers`` does — so a value survives the round
    trip as the wire sent it.
    """
    for name, value in scope.get("headers", ()):
        if name.lower() == b"host":
            return value.decode("latin-1")
    return None


def client_named_host(scope: Mapping[str, Any]) -> str | None:
    """The ``Host`` the **client** named, never a declared peer's forwarded one.

    :func:`apply_forwarded` stashes the header it is about to overwrite under
    :data:`CLIENT_HOST_KEY`, and this returns that. The scope's own ``Host`` —
    the forwarded name, once a declared peer's header was resolved — is what URL
    building and the guard's ``Host`` check read; the path-local rule
    (:func:`clear_record.web.app._local_client`) reads this instead, because
    "which name did the client address" is the fact that rule is written on, and
    a forwarded name is a name the client itself put in the request.

    A request the guard never rewrote has no stash, so the scope's own ``Host``
    is read directly — the same header, unchanged.
    """
    if CLIENT_HOST_KEY in scope:
        return scope[CLIENT_HOST_KEY]
    return _host_header(scope)


def apply_forwarded(scope: MutableMapping[str, Any], facts: Forwarded) -> None:
    """Write a declared peer's forwarded facts into an ASGI request scope.

    The guard's middleware calls this **first**, before it reads the ``Host`` and
    before the auth gate — registered inside it, so running after it — reads the
    scheme. Every reader downstream then sees the request as the proxy describes
    it: the guard's own ``Host``/``Origin`` checks, the cookie's ``Secure``
    (:func:`clear_record.web.auth.secure_request`), and the absolute URLs the
    router builds from the scope. What a forwarded name may **not** become is the
    name the path-local rule reads: the header it replaces is stashed under
    :data:`CLIENT_HOST_KEY` first, so a peer's ``X-Forwarded-Host: 127.0.0.1``
    never satisfies :func:`clear_record.web.app._local_client`.

    The scope's ``client`` is written too, but nothing consumes it: no code in
    the repo reads it after this write, and uvicorn's access log prints the
    transport peer the socket delivered rather than this value. A field the peer
    did not forward is left exactly as the socket delivered it.
    """
    if facts.scheme is not None:
        scope["scheme"] = facts.scheme
    if facts.host is not None:
        # The scope's ``Host`` is what Starlette builds a request's URL from and
        # what the guard's own ``Host`` check reads, so the forwarded name goes
        # there — but the name the client itself sent is stashed first (once: a
        # second pass must not stash the name the first one rewrote it to),
        # because the path-local rule reads that one and never this one
        # (:func:`client_named_host`).
        if CLIENT_HOST_KEY not in scope:
            scope[CLIENT_HOST_KEY] = _host_header(scope)
        scope["headers"] = [
            (name, value)
            for name, value in scope.get("headers", ())
            if name.lower() != b"host"
        ] + [(b"host", facts.host.encode("ascii"))]
    if facts.client is not None:
        # A forwarded chain names an address, not a connection, so there is no
        # port to state and none is invented: 0 is what Starlette's
        # ``Request.client`` carries for a scope that has none, and no decision
        # here reads the port.
        scope["client"] = (facts.client, 0)


def names_a_trusted_host(env: Mapping[str, str] | None = None) -> bool:
    """Whether the operator named a host this console will answer to.

    The one declaration a not-loopback bind needs, because it is the one the
    ``Host`` check below reads: a name in :data:`TRUSTED_HOSTS_ENV`. A declared
    proxy peer (:data:`TRUSTED_PROXIES_ENV`) is *not* one — a declared peer may
    speak for the browser, but the name it forwards is still checked, because the
    guard answers ``403`` to any ``Host`` that is neither loopback nor named,
    whatever peer forwarded the request — so a bind admitted on the proxy alone
    would serve nobody, which is exactly what the startup refusal exists to
    prevent.

    The startup refusal asks this of a non-loopback bind; ``--tailscale`` does
    not, because it resolves the tailnet name and passes it to
    :func:`clear_record.web.app.create_app` in-process (and refuses a
    non-loopback bind on its own).
    """
    return bool(trusted_extra_hosts(env))


def is_trusted(name: str | None, extra: frozenset[str]) -> bool:
    """Whether ``name`` is loopback or was explicitly named by the operator."""
    return is_loopback_host(name) or (name is not None and name in extra)


def host_problem(host_header: str | None, extra: frozenset[str]) -> str | None:
    """Reject a ``Host`` the console does not answer to (DNS rebinding)."""
    if is_trusted(host_name(host_header), extra):
        return None
    shown = host_header if host_header else "missing"
    return (
        f"request rejected: Host {shown!r} is not a trusted host for this "
        f"console (expected {_LOOPBACK_HINT})."
    )


def source_problem(
    origin_header: str | None,
    referer_header: str | None,
    extra: frozenset[str],
) -> str | None:
    """Reject a cross-origin ``Origin``/``Referer`` (CSRF).

    ``Origin`` wins when both are present, as it is the header the browser sets
    deliberately; ``Referer`` is the fallback for the older/edge cases that omit
    it. Neither present means a non-browser client, which is allowed.
    """
    source = origin_header if origin_header is not None else referer_header
    if source is None:
        return None
    if source.strip().lower() == "null":
        return (
            "request rejected: a state-changing request carried the opaque "
            "origin 'null', which this console does not accept."
        )
    if is_trusted(origin_host(source), extra):
        return None
    return (
        f"request rejected: Origin/Referer {source!r} is not a trusted host "
        f"for this console. If the console is behind a reverse proxy, set "
        f"CR_TRUSTED_HOSTS to the public hostname (expected {_LOOPBACK_HINT})."
    )
