"""Signing a test's console client in: the one helper the web suite shares.

The console has a credential (ADR-0033), so a page or an API call needs a
**session**. Every test that drives the console does it through :func:`signed_in`,
which sets the credential through the service seam the app itself uses and then
signs in through the real ``POST /setup/sign-in`` form — nothing here reaches
around the gate, and the cookie under test is the cookie the app issues. The
gate's own states (first run, a wrong password, an idle expiry, a revoked
session) are driven with a bare client instead, in ``test_web_auth.py``.

A module of its own rather than a ``conftest`` import: this suite has a
``conftest.py`` per test directory, and importing one of them by name from a test
module depends on which of them pytest imported first.
"""

from __future__ import annotations

from clear_record.service.auth import SessionState
from clear_record.service.lifecycle import CONSOLE
from clear_record.web.auth import SESSION_COOKIE, SIGN_IN_PATH
from fastapi.testclient import TestClient

#: The password every signed-in client in this suite uses. Not a secret: it is
#: set through the same service call the app's first-run route makes, and it
#: exists so a test can sign in through the real form rather than arranging a
#: session of its own.
CONSOLE_PASSWORD = "console-password-for-the-suite"


def signed_in(client: TestClient, password: str = CONSOLE_PASSWORD) -> TestClient:
    """Give ``client`` a live console session and return it.

    A fresh registry has no credential, by design, so one is set first — through
    :meth:`clear_record.service.auth.ConsoleAuth.set_password`, whose ``actor`` is
    the console's own word — and the session then comes from the **sign-in form**,
    which is what makes the cookie the app's own answer rather than a value a test
    minted. The assertion is the registry's verdict on that cookie, not the
    sign-in's status code: a client that is not really signed in fails here,
    where the reason is legible, instead of in whichever assertion follows.
    """
    console = client.app.state.auth
    if not console.configured():
        console.set_password(password, actor=CONSOLE)
    client.post(SIGN_IN_PATH, data={"password": password})
    token = client.cookies.get(SESSION_COOKIE)
    assert console.session(token, touch=False) is SessionState.ACTIVE, (
        "the suite's client is not signed in: the sign-in form refused it"
    )
    return client


__all__ = ["CONSOLE_PASSWORD", "signed_in"]
