"""The console's auth gate: one credential, one cookie, one anonymous surface.

ADR-0033's authentication half, as this edge implements it. The *policy* — the
credential's hash, the two session clocks, what a sign-in does — lives in
:mod:`clear_record.service.auth`; this module is the HTTP half: the cookie's
name and attributes, the paths that may be answered without a session, and which
refusal a request gets when it may not.

**The anonymous surface is a list, and this module is that list.** Every request
is gated by :func:`clear_record.web.app.create_app`'s auth middleware, which lets
exactly these through:

- the **setup route** — the one page that renders without a session: the
  first run's credential step, and, once a credential exists, the sign-in form.
  Its two POST targets (:data:`CREDENTIAL_PATH`, :data:`SIGN_IN_PATH`) are
  anonymous too, because they are that page's own forms;
- the **liveness route** (:data:`~clear_record.core.node.HEALTH_PATH`) — a probe
  must not need a credential, and its answer is exactly ``{"status": "ok"}``,
  which is why it is not under the machine prefix and why the old ``/api/health``
  — which named the registry — is gone;
- the **compiled assets** under ``/static`` — the pages above are unreadable
  without them, they carry no project data, and they are the same bytes for
  everyone.

Everything else needs a credential: pages and fragments — the console, which a
token can never open — redirect to the setup route, and the machine surface
(``/api/v1/…``) accepts **either** a live session cookie or a machine token
(:func:`bearer_token`, ``Authorization: Bearer …``), answering ``401`` with
:data:`AUTH_REQUIRED` in ``detail`` when it has neither, so a script is told what
a browser is shown. A token is consulted **only** for a :func:`machine_request`:
it authorizes the machine surface and nothing on the console's side of the app,
which is the whole of its reach.
:func:`answers_anonymously` is the single rule both the middleware and the
route-table test read, so the list cannot grow by a route somebody forgot to add
to a second copy of it. The console's own prefix is :data:`CONSOLE_PATH`, one
constant: every console route is built on it, and the setup route, its forms
and revoke-all are derived from it.
"""

from __future__ import annotations

from fastapi import Request, Response

from clear_record.core.node import HEALTH_PATH, SESSION_COOKIE
from clear_record.web.guard import SAFE_METHODS

#: The console's own URL prefix: the pages, their ``/ui`` fragments and the setup
#: route all live under it, and the machine API under :data:`MACHINE_PREFIX`. It
#: deliberately carries **no** trailing slash, so it is a prefix of every console
#: path (``/web/settings``, ``/web/ui/projects``) rather than a sibling of them.
#:
#: It is also the session cookie's ``Path``: a human session rides the console's
#: own requests and **never** the machine API's, whose credential is a token of
#: its own (ADR-0033) — so a console route that acts on the server (the header's
#: Quit control is the one) is a console route, not a call into ``/api/v1``.
CONSOLE_PATH = "/web"

#: The console's home — the prefix *as a directory*, which is the URL a reader
#: types, a link shows and a sign-in lands on. Built from :data:`CONSOLE_PATH` so
#: the two cannot disagree; ``/web`` (no slash) is the same page by the router's
#: own slash redirect.
CONSOLE_HOME = f"{CONSOLE_PATH}/"

#: The anonymous page: first run's credential step, then the sign-in form.
SETUP_PATH = f"{CONSOLE_PATH}/setup"

#: The setup page's own form targets, and the two ways a session begins or ends.
CREDENTIAL_PATH = f"{SETUP_PATH}/credential"
SIGN_IN_PATH = f"{SETUP_PATH}/sign-in"
SIGN_OUT_PATH = f"{SETUP_PATH}/sign-out"

#: Ending every session — the borrowed-browser remedy, from Settings → Status.
REVOKE_ALL_PATH = f"{CONSOLE_PATH}/ui/sessions/revoke-all"

#: Minting a machine token, and revoking one: Settings → Status's token list.
#: Both are **console** routes — the operator is the only one who mints or
#: revokes, so a token itself can never reach them (a token authenticates the
#: machine surface alone).
TOKENS_PATH = f"{CONSOLE_PATH}/ui/tokens"


def token_revoke_path(token_id: int) -> str:
    """The console route that revokes one token, built from the one prefix.

    A function rather than a format string a caller completes, so the list
    template and the route cannot disagree about the path: both build it from
    :data:`TOKENS_PATH`.
    """
    return f"{TOKENS_PATH}/{token_id}/revoke"


#: The compiled assets' mount, and the machine surface's prefix. Both are facts of
#: the route table; they are named here because the gate reads them.
STATIC_PREFIX = "/static"
MACHINE_PREFIX = "/api/v1/"

#: The GET paths answered without a session.
ANONYMOUS_PATHS = (SETUP_PATH, HEALTH_PATH)

#: The POST paths answered without a session: the setup page's own two forms.
ANONYMOUS_POSTS = (CREDENTIAL_PATH, SIGN_IN_PATH)

#: The scheme word a machine token is presented with: ``Authorization: Bearer
#: <token>`` (the HTTP authentication grammar's own word, and the one the MCP
#: best-practices guidance asks a local HTTP server for). One declaration, so the
#: refusal below and the gate's parser cannot disagree about it.
BEARER_SCHEME = "Bearer"

#: What a machine client is told when it has neither credential. Plain English on
#: purpose: it answers a script (the JSON API's ``detail``), not a person reading
#: a translated console — the same shape the request guard's and the naming
#: rules' sentences have. Both ways in are named — a session cookie, which opens
#: the console and the machine surface alike, and a bearer token, which opens the
#: machine surface only — because a client that is refused needs to know what
#: would have been accepted, and the whole anonymous surface is named for the
#: same reason.
AUTH_REQUIRED = (
    f"request refused: this node needs a signed-in console session or a machine "
    f"token, and this request carried neither. Present a {BEARER_SCHEME} token "
    f"(mint one in the console under {CONSOLE_PATH}/settings/status), or open "
    f"{SETUP_PATH} in a browser and sign in; only {SETUP_PATH}, {HEALTH_PATH} "
    f"and the compiled assets under {STATIC_PREFIX} answer without a credential."
)


def answers_anonymously(method: str, path: str) -> bool:
    """Whether this request is let through without a session.

    A request with no session and no place on the list is refused — a redirect for
    a browser, ``401`` for the machine surface — so this function is the whole of
    "what is public" and a route added to the table is private by default.
    """
    if path == STATIC_PREFIX or path.startswith(f"{STATIC_PREFIX}/"):
        return True
    if method in SAFE_METHODS:
        return path in ANONYMOUS_PATHS
    return method == "POST" and path in ANONYMOUS_POSTS


def machine_request(request: Request) -> bool:
    """Whether this request is addressed to the JSON API rather than a page."""
    return request.url.path.startswith(MACHINE_PREFIX)


def bearer_token(request: Request) -> str | None:
    """The machine token an ``Authorization: Bearer …`` header carries, or ``None``.

    Exactly one shape is read: the scheme word — case-insensitively, as the HTTP
    authentication grammar has it — and one value after it, with no second word
    and no empty token. Anything else is **no credential**, which is what lets
    the gate refuse it exactly as it refuses an absent one instead of guessing at
    what a malformed header meant.

    Reading the header is this function's whole job: whether the value names a
    live token is the registry's answer
    (:meth:`clear_record.service.auth.ConsoleAuth.authenticate_token`), and a
    token presented here is only ever consulted for a
    :func:`machine_request` — the console's pages have no bearer branch at all.
    """
    header = request.headers.get("authorization")
    if header is None:
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME.lower():
        return None
    return parts[1]


def secure_request(request: Request) -> bool:
    """Whether this request arrived over HTTPS, so the cookie may be ``Secure``.

    ``request.url.scheme`` — the **request's own** scheme, which is the socket's
    except where a declared proxy says otherwise: the request guard resolves a
    declared peer's ``X-Forwarded-Proto`` into the request before this module
    reads it (:func:`clear_record.web.guard.apply_forwarded`), and the server
    under the console is deliberately told to do no such thing itself. So a
    TLS-terminating proxy the operator declared (``CR_TRUSTED_PROXIES``) gets the
    ``Secure`` cookie a proxied console needs, while an undeclared peer's
    forwarded scheme is never read and the socket's plain ``http`` stands.
    """
    return request.url.scheme == "https"


def set_session_cookie(
    response: Response, token: str, *, secure: bool, max_age: int
) -> None:
    """Put the session token on ``response`` as the console's cookie.

    Every attribute is a decision: ``httponly`` keeps a script on the page from
    reading it, ``samesite="lax"`` keeps a cross-site form post from *sending* it
    (the console's own mutations are same-origin, while a top-level GET
    navigation still carries it, which is what makes a bookmark work), ``path`` scopes it to
    the console, and ``secure`` follows the scheme the request arrived by. The
    token is opaque and high-entropy, so the cookie needs no signature.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        path=CONSOLE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    """Remove the session cookie, with the attributes it was set with.

    A browser matches a deletion's path (and domain), so the path must be the one
    the cookie was set with; the flags are stated for the same reason, and a
    signed-out browser is left with no cookie at all rather than a dead value.
    """
    response.delete_cookie(
        SESSION_COOKIE,
        path=CONSOLE_PATH,
        httponly=True,
        samesite="lax",
        secure=secure,
    )


__all__ = [
    "ANONYMOUS_PATHS",
    "ANONYMOUS_POSTS",
    "AUTH_REQUIRED",
    "BEARER_SCHEME",
    "CONSOLE_HOME",
    "CONSOLE_PATH",
    "CREDENTIAL_PATH",
    "HEALTH_PATH",
    "MACHINE_PREFIX",
    "REVOKE_ALL_PATH",
    "SESSION_COOKIE",
    "SETUP_PATH",
    "SIGN_IN_PATH",
    "SIGN_OUT_PATH",
    "STATIC_PREFIX",
    "TOKENS_PATH",
    "answers_anonymously",
    "bearer_token",
    "clear_session_cookie",
    "machine_request",
    "secure_request",
    "set_session_cookie",
    "token_revoke_path",
]
