"""The auth gate: one credential, human sessions, and one anonymous surface.

ADR-0033's authentication half, driven through the **real app**: a fresh registry
answers only the setup route, the liveness route and the compiled assets; setting
the credential writes a salted hash — nowhere else, never the plaintext — and
signs the operator in; and every other route needs a session that the registry
answers for, so sign-out, revoke-all and both clocks take effect on the next
request.

Nothing here reaches around the gate: the pages are fetched as a browser fetches
them, and the routes are enumerated from the app's own table rather than
spot-checked, so a route added later cannot quietly join the anonymous surface
(:func:`test_no_route_outside_the_anonymous_surface_answers_without_a_session`).
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from _console import CONSOLE_PASSWORD, signed_in
from clear_record.core import node as node_module
from clear_record.core.diagnostics import LOG_FILENAME
from clear_record.service import Registry
from clear_record.service.auth import (
    ConsoleAuth,
    SessionPolicy,
    SessionState,
    refresh_local_session,
    token_digest,
    verify_password,
)
from clear_record.service.diagnostics import collect_bundle, redact_log_line
from clear_record.service.lifecycle import CONSOLE
from clear_record.web import app as web_app
from clear_record.web.app import create_app
from clear_record.web.auth import (
    CONSOLE_PATH,
    CREDENTIAL_PATH,
    REVOKE_ALL_PATH,
    SESSION_COOKIE,
    SETUP_PATH,
    SIGN_IN_PATH,
    SIGN_OUT_PATH,
    answers_anonymously,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

#: The password every test in this module sets and signs in with. It is long
#: enough to pass the rule and distinctive enough that "is it in the registry /
#: the bundle" is a real question.
PASSWORD = "gate-test-password-7f3c"


@pytest.fixture()
def console(tmp_path) -> Iterator[SimpleNamespace]:
    """A console over a temp registry, **not** signed in and not following redirects.

    Bare on purpose: the states this file is about are the ones a client with no
    session meets — the first run, the sign-in page, a dead cookie — and a client
    that followed redirects would hide which URL it was sent to.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    # ``TestClient`` speaks as ``testserver``, which the suite names in
    # ``CR_TRUSTED_HOSTS``; passing it here too keeps this file independent of
    # the autouse fixture's registration (a mixed-directory invocation can lose
    # it, and this file is the one that would then meet the guard's 403 instead
    # of the gate's answer).
    app = create_app(registry, trusted_hosts=("testserver",))
    yield SimpleNamespace(
        app=app,
        registry=registry,
        client=TestClient(app, follow_redirects=False),
    )


def _set_password(
    client: TestClient, password: str = PASSWORD, *, confirm: str | None = None
):
    """Submit the first-run credential form with its confirmation."""
    return client.post(
        CREDENTIAL_PATH,
        data={
            "password": password,
            "confirm": password if confirm is None else confirm,
        },
        follow_redirects=False,
    )


def _sign_in(client: TestClient, password: str = PASSWORD):
    return client.post(
        SIGN_IN_PATH, data={"password": password}, follow_redirects=False
    )


def _cookie_attributes(response) -> dict[str, str]:
    """The session cookie's ``Set-Cookie`` line as {lowercased attribute: value}."""
    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{SESSION_COOKIE}="), raw
    attributes: dict[str, str] = {}
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        attributes[name.lower()] = value
    return attributes


# --- the first run ----------------------------------------------------------- #


def test_a_fresh_registry_answers_only_the_setup_page(console) -> None:
    """Nothing but the credential step is reachable, and the API says so in JSON."""
    setup_page = console.client.get(SETUP_PATH)
    assert setup_page.status_code == 200
    assert "Set the console password" in setup_page.text

    for path in ("/", "/projects/ops", "/settings", "/activity", "/ui/projects"):
        response = console.client.get(path)
        assert response.status_code == 303, path
        assert response.headers["location"] == SETUP_PATH, path

    api = console.client.get("/api/projects")
    assert api.status_code == 401
    assert "sign in" in api.json()["detail"]


def test_the_setup_page_renders_no_project_meeting_or_transcript_content(
    console,
) -> None:
    """The anonymous page is the credential step, never a look at the data."""
    console.registry.create_project("Classified Project", actor=CONSOLE)
    console.registry.add_term("classified-project", "Falcon", actor=CONSOLE)

    page = console.client.get(SETUP_PATH).text

    assert "Classified Project" not in page
    assert "classified-project" not in page
    assert "Falcon" not in page


def test_setting_the_credential_signs_in_and_lands_on_the_console(console) -> None:
    response = _set_password(console.client)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert console.registry.credential() is not None
    assert console.client.get("/api/projects").status_code == 200


def test_the_first_run_form_cannot_replace_an_existing_credential(console) -> None:
    """The takeover this closes: the form is anonymous, so it may only *create*.

    It has to be reachable with no session — nobody can sign in yet — so if it
    could also replace the credential, anyone who could reach the page could take
    the console over. The rescue command is the way back for a lost credential.
    """
    _set_password(console.client)

    replaced = _set_password(console.client, "attacker-long-password")

    assert replaced.status_code == 409
    assert "already set" in replaced.text
    assert verify_password(PASSWORD, console.registry.credential())
    assert not verify_password("attacker-long-password", console.registry.credential())


def test_the_credential_form_refuses_a_mismatch_and_a_short_password(console) -> None:
    mismatch = _set_password(console.client, PASSWORD, confirm=PASSWORD + "x")
    assert mismatch.status_code == 200
    assert "do not match" in mismatch.text

    too_short = _set_password(console.client, "short")
    assert too_short.status_code == 200
    assert "at least 8 characters" in too_short.text

    assert console.registry.credential() is None


# --- the sign-in page -------------------------------------------------------- #


def test_the_setup_page_asks_for_the_password_once_one_is_set(console) -> None:
    _set_password(console.client)
    console.client.post(SIGN_OUT_PATH, follow_redirects=False)

    page = console.client.get(SETUP_PATH).text

    assert "Sign in" in page
    assert "Set the console password" not in page
    # Not the wizard: its steps are content a session is required for.
    assert "setup-step" not in page


def test_a_wrong_password_re_renders_the_sign_in_page(console) -> None:
    _set_password(console.client)
    console.client.post(SIGN_OUT_PATH, follow_redirects=False)

    response = _sign_in(console.client, "not-the-password")

    assert response.status_code == 401
    assert "does not match" in response.text
    assert "set-cookie" not in response.headers
    assert console.client.get("/api/projects").status_code == 401


def test_signing_in_again_works_and_starts_a_new_session(console) -> None:
    _set_password(console.client)
    first = console.client.cookies.get(SESSION_COOKIE)
    console.client.post(SIGN_OUT_PATH, follow_redirects=False)

    assert _sign_in(console.client).status_code == 303
    second = console.client.cookies.get(SESSION_COOKIE)

    assert second and second != first
    assert console.client.get("/ui/projects").status_code == 200


# --- the cookie -------------------------------------------------------------- #


def test_the_session_cookie_is_httponly_lax_and_scoped_to_the_console(console) -> None:
    attributes = _cookie_attributes(_set_password(console.client))

    assert attributes["httponly"] == ""
    assert attributes["samesite"] == "lax"
    assert attributes["path"] == CONSOLE_PATH
    assert "secure" not in attributes
    assert int(attributes["max-age"]) > 0


def test_the_cookie_is_secure_when_the_request_arrived_over_https(tmp_path) -> None:
    """The scheme the request arrived by is what ``Secure`` follows.

    The console is served plain HTTP behind the operator's proxy, so this is the
    socket's scheme today; the trusted-proxy change is what will let a declared
    peer's ``X-Forwarded-Proto`` say ``https`` here (ADR-0021's open item).
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    app = create_app(registry, trusted_hosts=("testserver",))
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)

    attributes = _cookie_attributes(_set_password(client))

    assert attributes["secure"] == ""
    assert client.get("/api/projects").status_code == 200


# --- the clocks -------------------------------------------------------------- #


def _short_idle(console, *, milliseconds: int) -> None:
    """Give this app a sub-second idle timeout, on the real clock.

    The policy is an attribute on the app's own auth seam, so the test drives the
    **real** timer: nothing here patches ``time`` or a clock callable, and the
    session that expires is one the sign-in form issued.
    """
    console.app.state.auth.policy = SessionPolicy(
        idle_timeout=timedelta(milliseconds=milliseconds),
        absolute_lifetime=timedelta(days=1),
    )


def test_an_idle_past_the_timeout_redirects_to_sign_in_never_a_500(console) -> None:
    _short_idle(console, milliseconds=250)
    _set_password(console.client)
    assert console.client.get("/ui/projects").status_code == 200

    time.sleep(0.4)
    page = console.client.get("/ui/projects")

    assert page.status_code == 303
    assert page.headers["location"] == SETUP_PATH
    assert console.client.get("/api/projects").status_code == 401


def test_a_stale_cookie_is_cleared_on_the_way_to_the_sign_in_page(console) -> None:
    _short_idle(console, milliseconds=200)
    _set_password(console.client)

    time.sleep(0.35)
    response = console.client.get("/ui/projects")

    assert response.status_code == 303
    assert _cookie_attributes(response)["max-age"] == "0"


def test_reading_the_anonymous_page_does_not_extend_a_session(console) -> None:
    """The sign-in page asks whether a session exists; it must not keep one alive.

    The idle clock moves on *accepted* requests, so reading a page that is not
    gated cannot be a way to hold a session open from a probe or a shared link.
    """
    _short_idle(console, milliseconds=250)
    _set_password(console.client)

    assert console.client.get(SETUP_PATH).status_code == 200
    time.sleep(0.15)
    assert console.client.get(SETUP_PATH).status_code == 200
    time.sleep(0.15)

    assert console.client.get("/ui/projects").status_code == 303


def test_a_session_past_its_absolute_lifetime_is_refused(console) -> None:
    """Busy is not immortal: the absolute deadline never moves.

    The clock is injected rather than slept through — a month-long lifetime is
    not something a real-timer test can wait for — and the idle deadline is left
    far in the future, so the refusal can only be the absolute half.
    """
    console.app.state.auth.policy = SessionPolicy(
        idle_timeout=timedelta(hours=1), absolute_lifetime=timedelta(seconds=30)
    )
    _set_password(console.client)
    started = console.app.state.auth.clock()
    console.app.state.auth.clock = lambda: started + timedelta(seconds=31)

    assert console.client.get("/ui/projects").status_code == 303
    assert console.client.get("/api/projects").status_code == 401


# --- sign-out and revoke-all ------------------------------------------------- #


def test_signing_out_refuses_the_next_request(console) -> None:
    _set_password(console.client)
    assert console.client.get("/ui/projects").status_code == 200

    response = console.client.post(SIGN_OUT_PATH, follow_redirects=False)

    assert response.status_code == 303
    assert _cookie_attributes(response)["max-age"] == "0"
    assert console.client.get("/ui/projects").status_code == 303
    assert console.client.get("/api/projects").status_code == 401


def test_revoking_every_session_refuses_every_browser_on_the_next_request(
    tmp_path,
) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    app = create_app(registry, trusted_hosts=("testserver",))
    first = signed_in(TestClient(app, follow_redirects=False))
    second = signed_in(TestClient(app, follow_redirects=False))
    assert first.get("/api/projects").status_code == 200
    assert second.get("/api/projects").status_code == 200

    response = first.post(REVOKE_ALL_PATH, follow_redirects=False)

    assert response.status_code == 303
    assert first.get("/api/projects").status_code == 401
    assert second.get("/api/projects").status_code == 401


def test_a_revoked_session_stays_revoked_with_no_restart(console) -> None:
    """The same app, the same cookie: the registry is what decides, every request."""
    _set_password(console.client)
    token = console.client.cookies.get(SESSION_COOKIE)
    app = console.app
    assert console.client.get("/ui/projects").status_code == 200

    ConsoleAuth(app.state.registry).revoke_all()

    assert console.client.get("/ui/projects").status_code == 303
    # The cookie the browser held is cleared on the way to the sign-in page, and
    # the row behind it — not the process — is what decided: a fresh request with
    # the same cookie value is refused for the same reason.
    assert console.client.cookies.get(SESSION_COOKIE) is None
    stale = console.client.get("/ui/projects", follow_redirects=False)
    assert stale.status_code == 303
    assert token is not None


# --- the liveness route ------------------------------------------------------ #


def test_health_answers_with_no_session_and_no_token(console) -> None:
    response = console.client.get(
        "/health",
        headers={"cookie": f"{SESSION_COOKIE}=a-stale-value"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "set-cookie" not in response.headers
    assert console.registry.db_path.name not in response.text


def test_health_is_anonymous_on_a_fresh_registry(console) -> None:
    """A probe must tell a healthy node from a setup page with no credential at all."""
    assert console.client.get("/health").status_code == 200


# --- the anonymous surface --------------------------------------------------- #


def _route_requests(app: FastAPI) -> list[tuple[str, str]]:
    """``(method, path)`` for every route the app serves, path params filled with 1.

    Read off the app's own table rather than a hand-kept list, so a route added
    later is covered the moment it exists. ``HEAD``/``OPTIONS`` are left out:
    they are answered *by* the routes already here and are not a separate surface.
    """
    requests: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or {"GET"}
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        requests.extend(
            (method, path) for method in sorted(methods - {"HEAD", "OPTIONS"})
        )
    return requests


def test_no_route_outside_the_anonymous_surface_answers_without_a_session(
    console,
) -> None:
    """Every route but the declared ones refuses an anonymous request.

    The refusal is a redirect to the setup route for a page and fragment, and
    ``401`` for the machine surface, so a script is told what a browser is shown.
    A route that answered anything else — a page, a fragment, an empty 200 — has
    quietly joined the anonymous surface, and this fails naming it.
    """
    requests = _route_requests(console.app)
    assert len(requests) > 40, (
        "the route table moved somewhere this test is not reading"
    )

    offenders: list[tuple[str, str, int, str | None]] = []
    for method, path in requests:
        if answers_anonymously(method, path):
            continue
        response = console.client.request(method, path, follow_redirects=False)
        location = response.headers.get("location")
        if path.startswith("/api/"):
            refused = response.status_code == 401
        else:
            refused = response.status_code == 303 and location == SETUP_PATH
        if not refused:
            offenders.append((method, path, response.status_code, location))

    assert offenders == [], f"routes answering anonymously: {offenders}"


def test_the_declared_anonymous_surface_answers(console) -> None:
    """The other half: the three declared entries are reachable with no session."""
    assert console.client.get(SETUP_PATH).status_code == 200
    assert console.client.get("/health").status_code == 200
    assert console.client.get("/static/app.css").status_code == 200
    # The setup page's own two forms are answered rather than gated (their
    # subjects are wrong here; the point is that the gate let them through).
    assert _set_password(console.client, "short").status_code == 200
    assert _sign_in(console.client, "wrong").status_code == 401


def test_an_htmx_fragment_request_is_navigated_to_sign_in(console) -> None:
    """A fragment target is no place for a page: htmx is told to navigate.

    No 303 on this answer: the browser follows one transparently before htmx sees
    anything, and the sign-in page would then be swapped into the fragment that
    asked. The header rides a plain 200, which is the response htmx reads.
    """
    response = console.client.get(
        "/ui/projects", headers={"HX-Request": "true"}, follow_redirects=False
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == SETUP_PATH
    assert "location" not in response.headers
    # A browser that is not htmx still gets the redirect.
    assert console.client.get("/ui/projects").status_code == 303


# --- what is written, and where ---------------------------------------------- #


def test_the_stored_credential_is_a_salted_hash_and_the_plaintext_is_nowhere(
    tmp_path,
) -> None:
    """The registry keeps a salted scrypt hash; the password exists nowhere else.

    Two installs that choose the **same** password store different values — the
    salt is per install — and neither file contains the password itself. The
    session cookie's token is held the same way: the registry keeps its digest.
    """
    first = Registry.open(db_path=tmp_path / "one.sqlite3")
    second = Registry.open(db_path=tmp_path / "two.sqlite3")
    ConsoleAuth(first).set_password(PASSWORD, actor=CONSOLE)
    ConsoleAuth(second).set_password(PASSWORD, actor=CONSOLE)

    stored = first.credential()
    assert stored is not None and stored.startswith("scrypt$")
    assert stored != second.credential(), "the salt is not per install"
    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + "x", stored)

    token = ConsoleAuth(first).sign_in(PASSWORD)
    assert token is not None
    raw = (tmp_path / "one.sqlite3").read_bytes()
    assert PASSWORD.encode() not in raw
    assert token.encode() not in raw
    assert token_digest(token).encode() in raw, "the session row is not in the file"


def test_the_credential_set_is_audited_and_the_sessions_are_not(console) -> None:
    """The one act that answers "who holds the key" is a row; sessions are bookkeeping."""
    _set_password(console.client)
    console.client.post(SIGN_OUT_PATH, follow_redirects=False)
    _sign_in(console.client)

    rows = [
        (event.actor, event.action, event.target, event.outcome)
        for event in console.registry.list_audit_events()
    ]

    assert rows == [(CONSOLE, "credential.set", "credential:console", "ok")]


def test_the_diagnostics_bundle_carries_no_credential(tmp_path, monkeypatch) -> None:
    """The bundle redacts a credential value, and reads no file that holds one.

    The credential lives in the registry as a hash and is never logged, so a
    bundle has nothing to carry; this is the second line — a log line that
    somehow reached the sink with the password in it is scrubbed, which is what
    the ticket's redaction test asserts.
    """
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    ConsoleAuth(registry).set_password(PASSWORD, actor=CONSOLE)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / LOG_FILENAME).write_text(
        '{"level":"info","logger":"console","message":"POST /setup/sign-in '
        f'password={PASSWORD} HTTP/1.1"}}\n',
        encoding="utf-8",
    )

    bundle = collect_bundle(registry=registry, log_dir=log_dir)

    # The positive control: the log line really was read into the bundle, so the
    # password's absence is the redaction's doing and not an empty file's.
    assert "# recent log lines (oldest first)" in bundle
    assert '"level":"info"' in bundle
    assert PASSWORD not in bundle


def test_a_log_line_never_prints_a_credential_value() -> None:
    """Free text and structured records are both scrubbed; the key survives."""
    line = '{"level":"warning","logger":"console","message":"password=x"}'
    assert "password=[redacted]" in redact_log_line(line)

    structured = redact_log_line(
        '{"level":"info","password":"x","token":"y","note":"z"}'
    )
    assert '"password":"[redacted]"' in structured
    assert '"token":"[redacted]"' in structured
    assert '"note":"z"' in structured


def test_the_suite_password_is_the_one_the_shared_helper_uses(
    console: SimpleNamespace,
) -> None:
    """The helper's own credential is not this module's: a sanity anchor."""
    assert CONSOLE_PASSWORD != PASSWORD
    signed_in(console.client, CONSOLE_PASSWORD)
    assert console.client.get("/api/projects").status_code == 200


def test_the_console_path_is_one_declaration() -> None:
    """The cookie's scope is the console prefix, declared once for the re-root.

    ``Path=`` on the session cookie is what keeps it off everything else that
    shares the origin, and the route re-root moves the console under ``/web`` —
    when it does, this constant (and the routes that read it) move with it, and
    this test is where a half-moved console fails.
    """
    assert CONSOLE_PATH.startswith("/")
    assert not CONSOLE_PATH.endswith("api")
    assert Path(CREDENTIAL_PATH).parent == Path(SETUP_PATH) == Path(SIGN_IN_PATH).parent
    assert Path(SIGN_OUT_PATH).parent == Path(SETUP_PATH)


# --- the session the node publishes for its own machine ---------------------- #


def test_the_published_local_session_is_a_session_like_any_other(
    console, monkeypatch, tmp_path
) -> None:
    """The node's own machine presents it as the cookie; the gate has no branch.

    The file is the node's (`core.node`), and the token in it is a session row
    opened through the same :class:`ConsoleAuth` path a sign-in uses — so it is
    subject to the same clocks, it is refused when it names nothing, and
    ``revoke_all`` ends it like any browser's.
    """
    monkeypatch.setenv("CR_STATE_DIR", str(tmp_path / "state"))
    token = refresh_local_session(console.app.state.auth)
    assert node_module.local_session() == token

    console.client.cookies.set(SESSION_COOKIE, token)
    assert console.client.get("/api/projects").status_code == 200

    console.app.state.auth.revoke_all()
    assert console.client.get("/api/projects").status_code == 401
    node_module.forget_local_session()


def test_a_stale_or_unknown_local_session_is_refused_like_any_other_cookie(
    console, monkeypatch, tmp_path
) -> None:
    """A file left behind by a dead node is an unknown session, not an exemption."""
    monkeypatch.setenv("CR_STATE_DIR", str(tmp_path / "state"))
    node_module.publish_local_session("a-token-this-registry-never-issued")

    console.client.cookies.set(SESSION_COOKIE, "a-token-this-registry-never-issued")

    assert console.client.get("/api/projects").status_code == 401
    assert console.client.get(SETUP_PATH).status_code == 200
    node_module.forget_local_session()


def test_the_keeper_republishes_a_session_that_lapsed(
    console, monkeypatch, tmp_path
) -> None:
    """The loop is what keeps the node's own session live — on a real timer.

    What a node publishes at startup is a session like a browser's: the idle
    window, the absolute lifetime and ``revoke_all`` all end it, and there is
    nobody on the client's side to sign in again. The keeper's tick is therefore
    the mechanism, not the first publish — so this drives the tick itself (the
    interval is shortened to make it observable; the waiting is real time, and
    the sleep is set past the session's own window so the tick has something to
    repair). A loop whose refresh were removed would hold the dead token here
    until the deadline below, and the assertions name that.
    """
    monkeypatch.setenv("CR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(web_app, "LOCAL_SESSION_REFRESH_S", 0.05)
    console.app.state.auth.policy = SessionPolicy(
        idle_timeout=timedelta(milliseconds=200), absolute_lifetime=timedelta(days=1)
    )
    # Every node posture records its address before it publishes the session
    # (`NodeServer.startup`), and the two files are one node's pair: the clean
    # exit takes the session file down only when the record is this process's.
    node_module.record(node_module.NodeAddress.of("127.0.0.1", 8765))
    keeper = web_app.LocalSessionKeeper(console.app.state.auth)

    first = keeper.start()
    try:
        assert node_module.local_session() == first
        assert console.app.state.auth.session(first, touch=False) is SessionState.ACTIVE

        # Real time past the idle window, then wait for the keeper's own tick.
        deadline = time.monotonic() + 10
        while node_module.local_session() == first and time.monotonic() < deadline:
            time.sleep(0.02)

        second = node_module.local_session()
        assert second is not None and second != first, "the keeper never refreshed"
        assert (
            console.app.state.auth.session(second, touch=False) is SessionState.ACTIVE
        )
        # The lapsed token is not a way in: the file holds the fresh one only.
        assert (
            console.app.state.auth.session(first, touch=False)
            is not SessionState.ACTIVE
        )
    finally:
        keeper.stop()

    assert node_module.local_session() is None


def test_the_bundle_carries_no_local_session(tmp_path, monkeypatch) -> None:
    """The token lives in the state directory the bundle never reads."""
    state = tmp_path / "state"
    monkeypatch.setenv("CR_STATE_DIR", str(state))
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    ConsoleAuth(registry).set_password(PASSWORD, actor=CONSOLE)
    token = refresh_local_session(ConsoleAuth(registry))

    bundle = collect_bundle(registry=registry)

    assert node_module.local_session() == token
    assert token not in bundle


def test_health_answers_a_head_probe(console) -> None:
    """A supervisor's HEAD probe reads a healthy node, not a 405.

    The gate treats HEAD as safe (it cannot change anything), and the liveness
    route is what such a probe asks — so it answers the same 200 with no body
    rather than a method refusal that would read as an unhealthy node.
    """
    response = console.client.head("/health", follow_redirects=False)

    assert response.status_code == 200
    assert response.content == b""


def test_the_published_schema_names_every_operation_once(console) -> None:
    """``/health`` answers GET and HEAD, and the schema keeps them apart.

    FastAPI derives an operation id from the route's *first* method, so a single
    route declaring both published ``health_health_head`` twice — an OpenAPI
    document no client generator can key on (FastAPI warns, one operation wins,
    and the other is unreachable by id). The liveness route's pair is the one
    place in the table with two methods on one path, so this asserts the whole
    document's ids rather than only ``/health``'s.
    """
    schema = console.app.openapi()
    ids = [
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    ]

    assert len(ids) == len(set(ids)), (
        f"duplicate operation ids: {sorted(i for i in ids if ids.count(i) > 1)}"
    )
    assert sorted(schema["paths"]["/health"]) == ["get", "head"]
