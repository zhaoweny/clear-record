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
  which is why it is not under ``/api`` and why the old ``/api/health`` — which
  named the registry — is gone;
- the **compiled assets** under ``/static`` — the pages above are unreadable
  without them, they carry no project data, and they are the same bytes for
  everyone.

Everything else needs a live session: pages and fragments redirect to the setup
route, and the machine surface (``/api/…``) answers ``401`` with
:data:`AUTH_REQUIRED` in ``detail``, so a script is told what a browser is shown.
:func:`answers_anonymously` is the single rule both the middleware and the
route-table test read, so the list cannot grow by a route somebody forgot to add
to a second copy of it. The console's own prefix is :data:`CONSOLE_PATH`, one
constant: the cookie is scoped to it, and the route re-root moves it.
"""

from __future__ import annotations

from fastapi import Request, Response

from clear_record.core.node import HEALTH_PATH, SESSION_COOKIE
from clear_record.web.guard import SAFE_METHODS

#: The console's own URL prefix, and therefore the cookie's ``Path``: a session
#: cookie rides on console requests and nowhere else. The console is rooted at
#: the root today, so this is ``/``; the route re-root moves the console under
#: ``/web`` and this constant moves with it, which is what keeps the cookie
#: scoped to the console rather than to whatever else shares the origin.
CONSOLE_PATH = "/"

#: The anonymous page: first run's credential step, then the sign-in form.
SETUP_PATH = "/setup"

#: The setup page's own form targets, and the two ways a session begins or ends.
CREDENTIAL_PATH = f"{SETUP_PATH}/credential"
SIGN_IN_PATH = f"{SETUP_PATH}/sign-in"
SIGN_OUT_PATH = f"{SETUP_PATH}/sign-out"

#: Ending every session — the borrowed-browser remedy, from Settings → Status.
REVOKE_ALL_PATH = "/ui/sessions/revoke-all"

#: The compiled assets' mount, and the machine surface's prefix. Both are facts of
#: the route table; they are named here because the gate reads them.
STATIC_PREFIX = "/static"
MACHINE_PREFIX = "/api/"

#: The GET paths answered without a session.
ANONYMOUS_PATHS = (SETUP_PATH, HEALTH_PATH)

#: The POST paths answered without a session: the setup page's own two forms.
ANONYMOUS_POSTS = (CREDENTIAL_PATH, SIGN_IN_PATH)

#: What a machine client is told when it has no session. Plain English on
#: purpose: it answers a script (the JSON API's ``detail``), not a person reading
#: a translated console — the same shape the request guard's and the naming
#: rules' sentences have. The whole anonymous surface is named, because a client
#: that is refused needs to know whether it asked one of the routes that would
#: have answered.
AUTH_REQUIRED = (
    f"request refused: this node needs a signed-in console session, and this "
    f"request carried none. Open {SETUP_PATH} in a browser and sign in; only "
    f"{SETUP_PATH}, {HEALTH_PATH} and the compiled assets under {STATIC_PREFIX} "
    f"answer without one."
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


def secure_request(request: Request) -> bool:
    """Whether this request arrived over HTTPS, so the cookie may be ``Secure``.

    ``request.url.scheme``, and **this module reads no forwarded header**. What
    the scheme *is* depends on the server under it: uvicorn's own default already
    rewrites the scheme from ``X-Forwarded-Proto`` for a **loopback peer**
    (``proxy_headers=True`` with ``forwarded_allow_ips="127.0.0.1,::1"``), so a
    proxy running on this machine is believed today — which is the ``Secure``
    cookie a proxied console needs, and also the shape the trusted-proxy change
    narrows: honouring the header only from the peers the operator *declares*
    (``CR_TRUSTED_PROXIES``) is that change's, and this function is where a
    narrowing the server itself does not make would land.
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
    "answers_anonymously",
    "clear_session_cookie",
    "machine_request",
    "secure_request",
    "set_session_cookie",
]
