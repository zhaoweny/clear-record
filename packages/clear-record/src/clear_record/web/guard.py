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

A second list lives here too and is **not** a check: the peers in
``CR_TRUSTED_PROXIES``, whose forwarded headers the console may honour. The
console's own code reads no ``X-Forwarded-*`` header today (ADR-0021's open item,
the trusted-proxy change) — the server under it, uvicorn, does rewrite the scheme
from a **loopback** peer's ``X-Forwarded-Proto`` by default, which is the shape
that change narrows — so nothing consults this list yet. What admits a
non-loopback bind is a hostname the guard will actually trust
(:func:`names_a_trusted_host`), because the startup refusal exists to refuse a
bind that would answer ``403`` to every request it received.

No domain logic lives here and nothing is stored; the guard is a pure function
of the request headers and the configured host set.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Iterable, Mapping
from urllib.parse import urlsplit

#: Methods that cannot change server state, and so are exempt from the CSRF
#: (``Origin``/``Referer``) half of the guard. ``Host`` is checked regardless.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Comma-separated extra hostnames the console may be reached by. Empty by
#: default, so a stock install trusts loopback only.
TRUSTED_HOSTS_ENV = "CR_TRUSTED_HOSTS"

#: Comma-separated peers whose forwarded headers the console may honour. Empty by
#: default. **Declared, not yet honoured**: the console's own code reads no
#: ``X-Forwarded-*`` header (ADR-0021's open item; the trusted-proxy change is
#: what reads a declared peer's headers and lets them drive the session cookie's
#: scheme and the console's absolute URLs). The server under the console does
#: rewrite the scheme from a **loopback** peer's ``X-Forwarded-Proto`` by default,
#: which is the shape that change narrows. Nothing consults this list yet, and it
#: is deliberately **not** a trust source for the ``Host`` check: the name a proxy
#: forwards still has to be in :data:`TRUSTED_HOSTS_ENV`.
TRUSTED_PROXIES_ENV = "CR_TRUSTED_PROXIES"

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
    peer comparison the trusted-proxy change will make. Nothing reads a forwarded
    header from these peers yet, and this list is **not** consulted by the
    ``Host`` check (see :func:`names_a_trusted_host`).
    """
    source = os.environ if env is None else env
    return normalize_hosts(source.get(TRUSTED_PROXIES_ENV, "").split(","))


def names_a_trusted_host(env: Mapping[str, str] | None = None) -> bool:
    """Whether the operator named a host this console will answer to.

    The one declaration a not-loopback bind needs, because it is the one the
    ``Host`` check below reads: a name in :data:`TRUSTED_HOSTS_ENV`. A declared
    proxy peer (:data:`TRUSTED_PROXIES_ENV`) is *not* one — the guard answers
    ``403`` to any ``Host`` that is neither loopback nor named, whatever peer
    forwarded the request — so a bind admitted on the proxy alone would serve
    nobody, which is exactly what the startup refusal exists to prevent.

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
