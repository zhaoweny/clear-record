"""The request guard: DNS rebinding (``Host``) and CSRF (``Origin``/``Referer``).

ADR-0021: bound-to-localhost is not the same as safe-from-the-browser. These
tests drive the guard through the app with its **loopback-only default** (an
explicit empty trusted set, so the ambient test env cannot leak in) and a
loopback base URL, so "ordinary same-origin use" is genuine rather than a test
convention. The reverse-proxy escape hatch is covered by the ``CR_TRUSTED_HOSTS``
test at the bottom.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from _console import CONSOLE_PASSWORD, signed_in
from clear_record.service import Registry
from clear_record.service.lifecycle import CONSOLE
from clear_record.web import app as web_app
from clear_record.web import guard
from clear_record.web.app import NodeServer, create_app
from clear_record.web.auth import SIGN_IN_PATH
from fastapi import Request
from fastapi.testclient import TestClient

#: The console's own origin: what a real browser would put in ``Origin`` and the
#: host it would send in ``Host``.
LOOPBACK_ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(
        Registry.open(db_path=tmp_path / "registry.sqlite3"), trusted_hosts=()
    )
    return signed_in(TestClient(app, base_url=LOOPBACK_ORIGIN))


def _projects(client: TestClient) -> list[dict]:
    return client.get("/api/v1/projects").json()


# --- CSRF: Origin / Referer on state-changing requests -------------------- #
def test_cross_origin_form_post_is_rejected(client: TestClient) -> None:
    """The headline case: a hostile page's form POST to /web/ui/projects."""
    res = client.post(
        "/web/ui/projects",
        data={"name": "Evil"},
        headers={"Origin": "http://evil.example"},
    )
    assert res.status_code == 403
    assert "evil.example" in res.json()["detail"]
    assert "CR_TRUSTED_HOSTS" in res.json()["detail"]
    # The rejection happened before the handler: nothing was created.
    assert _projects(client) == []


def test_cross_origin_json_post_is_rejected(client: TestClient) -> None:
    """The machine surface is guarded too, not just the htmx forms."""
    res = client.post(
        "/api/v1/projects",
        json={"name": "Evil"},
        headers={"Origin": "http://evil.example"},
    )
    assert res.status_code == 403
    assert _projects(client) == []


def test_referer_is_used_when_origin_is_absent(client: TestClient) -> None:
    res = client.post(
        "/web/ui/projects",
        data={"name": "Evil"},
        headers={"Referer": "http://evil.example/page"},
    )
    assert res.status_code == 403
    assert _projects(client) == []


def test_opaque_null_origin_is_rejected(client: TestClient) -> None:
    res = client.post(
        "/web/ui/projects",
        data={"name": "Evil"},
        headers={"Origin": "null"},
    )
    assert res.status_code == 403
    assert _projects(client) == []


def test_same_origin_post_with_an_origin_header_is_allowed(client: TestClient) -> None:
    res = client.post(
        "/web/ui/projects",
        data={"name": "Weekly Ops"},
        headers={"Origin": LOOPBACK_ORIGIN},
    )
    assert res.status_code == 200
    assert [p["slug"] for p in _projects(client)] == ["weekly-ops"]


# --- DNS rebinding: Host on every request --------------------------------- #
def test_mismatched_host_is_rejected(client: TestClient) -> None:
    res = client.post(
        "/web/ui/projects", data={"name": "Evil"}, headers={"host": "evil.example"}
    )
    assert res.status_code == 403
    assert "Host" in res.json()["detail"]
    # Host is method-blind: a rebound GET is a disclosure, so it is rejected too.
    assert (
        client.get("/web/ui/projects", headers={"host": "evil.example"}).status_code
        == 403
    )
    assert _projects(client) == []


def test_absent_host_is_rejected(client: TestClient) -> None:
    res = client.post("/web/ui/projects", data={"name": "Evil"}, headers={"host": ""})
    assert res.status_code == 403
    assert "missing" in res.json()["detail"]
    assert _projects(client) == []


# --- Ordinary same-origin use is unaffected ------------------------------- #
def test_same_origin_gets_and_posts_still_work(client: TestClient) -> None:
    assert client.get("/web/ui/projects").status_code == 200
    # The liveness route is anonymous and is *not* under /api/v1: the guard still
    # applies to it (Host), and nothing else does.
    assert client.get("/health").json()["status"] == "ok"

    # htmx form POST with no Origin (a non-browser default) and with the browser's
    # own Origin both pass.
    assert client.post("/web/ui/projects", data={"name": "Ops"}).status_code == 200
    assert (
        client.post(
            "/web/ui/projects",
            data={"name": "Weekly"},
            headers={"Origin": LOOPBACK_ORIGIN},
        ).status_code
        == 200
    )
    assert {p["slug"] for p in _projects(client)} == {"ops", "weekly"}


def test_non_browser_client_without_origin_is_allowed(client: TestClient) -> None:
    """curl / a script sends neither Origin nor Referer; it is not the threat."""
    res = client.post("/api/v1/projects", json={"name": "Scripted"})
    assert res.status_code == 201
    assert res.json()["slug"] == "scripted"


# --- The escape hatch: CR_TRUSTED_HOSTS ----------------------------------- #
def test_trusted_hosts_env_allows_a_proxys_public_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy hostname in CR_TRUSTED_HOSTS is trusted without opening loopback."""
    monkeypatch.setenv("CR_TRUSTED_HOSTS", "console.example.com")
    app = create_app(Registry.open(db_path=tmp_path / "registry.sqlite3"))
    client = signed_in(TestClient(app, base_url=LOOPBACK_ORIGIN))

    res = client.post(
        "/web/ui/projects",
        data={"name": "Proxied"},
        headers={
            "host": "console.example.com",
            "Origin": "https://console.example.com",
        },
    )
    assert res.status_code == 200

    # A different, unnamed host is still refused: the hatch names hosts, it does
    # not switch the guard off.
    assert (
        client.post(
            "/web/ui/projects",
            data={"name": "Evil"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )


# --- Trusted proxies: forwarded headers, from declared peers only ---------- #
#: A peer that is not this machine. ``TestClient`` puts it in the request's
#: socket address, so a test can say which side of the declaration a request is
#: on without a real network — and the header a client sends is never what
#: decides that.
OUTSIDE_PEER = ("203.0.113.7", 47110)

#: The public name the proxy serves (and the browser puts in ``Host``).
PROXY_HOST = "console.example.com"

#: The headers a proxy adds for the browser it serves: the name and scheme the
#: browser saw, over a request the console receives on plain loopback HTTP.
_FORWARDED = {
    "host": "127.0.0.1:8765",
    "X-Forwarded-Host": PROXY_HOST,
    "X-Forwarded-Proto": "https",
}


def _proxied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    declared: str | None = None,
    peer: tuple[str, int] = OUTSIDE_PEER,
    trusted_hosts: tuple[str, ...] = (),
) -> TestClient:
    """A client from ``peer``, on a console that declares ``declared`` a proxy.

    The declaration is put where a deployment puts it — ``CR_TRUSTED_PROXIES`` in
    the environment — so the test arrives through the same door an operator uses;
    ``declared=None`` removes it, which is a stock install: no forwarded header
    is believed at all.
    """
    if declared is None:
        monkeypatch.delenv("CR_TRUSTED_PROXIES", raising=False)
    else:
        monkeypatch.setenv("CR_TRUSTED_PROXIES", declared)
    app = create_app(
        Registry.open(db_path=tmp_path / "registry.sqlite3"),
        trusted_hosts=trusted_hosts,
    )
    return TestClient(
        app, base_url=LOOPBACK_ORIGIN, client=peer, follow_redirects=False
    )


def test_a_forwarded_loopback_name_cannot_make_a_client_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declared peer's forwarded name drives URLs, never the path-local rule.

    ``X-Forwarded-Host`` is what the console's absolute URLs are built from, and
    it is a value the *client* puts in its own request — so it must never be the
    name the path-local rule reads. One declared-peer request here forwards
    ``127.0.0.1`` while its own ``Host`` names the public host: the same request
    reports ``https://127.0.0.1/…`` and is refused by the rule, and the route
    that takes a path on this node answers the one ``PATH_IS_LOCAL`` sentence and
    creates nothing.
    """
    monkeypatch.setenv("CR_TRUSTED_PROXIES", OUTSIDE_PEER[0])
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    app = create_app(registry, trusted_hosts=(PROXY_HOST,))

    @app.get("/api/v1/url-probe")
    def _probe(request: Request) -> dict[str, object]:
        # The same request, read both ways: the URL the app builds (the guard's
        # forwarded resolution) and the rule the path routes ask.
        return {"url": str(request.url), "local": web_app._local_client(request)}

    client = TestClient(
        app, base_url=LOOPBACK_ORIGIN, client=OUTSIDE_PEER, follow_redirects=False
    )
    minted, _row = app.state.auth.mint_token("forwarded loopback", actor=CONSOLE)
    headers = {
        "host": PROXY_HOST,
        "X-Forwarded-Host": "127.0.0.1",
        "X-Forwarded-Proto": "https",
        "Authorization": f"Bearer {minted}",
    }

    answer = client.get("/api/v1/url-probe", headers=headers)
    refused = client.post(
        "/api/v1/projects",
        json={"name": "Elsewhere", "default_archive_root": "/srv/tapes"},
        headers=headers,
    )

    assert answer.json() == {
        "url": "https://127.0.0.1/api/v1/url-probe",
        "local": False,
    }
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == web_app.PATH_IS_LOCAL
    assert registry.list_projects() == []


# Pins, not discriminates: nothing in the console read a forwarded header at
# 9b37fbe either, and ``TestClient`` never runs the server under the app, so this
# half is green at 9b37fbe and 897fd71 on purpose. What discriminates the rule is
# its other half —
# `test_a_declared_proxys_forwarded_host_is_honoured_and_checked` above — and,
# for the ignored side, the real-socket
# `test_the_server_does_not_honour_a_forwarded_scheme_by_itself`, which fails at
# 9b37fbe because the server trusted a loopback peer's `X-Forwarded-Proto` by
# default.
def test_an_undeclared_peers_forwarded_host_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket peer decides who is believed; a header cannot nominate itself.

    The peer is not a declared proxy, so its ``X-Forwarded-Host`` is not read:
    the request is judged by the ``Host`` it carries, and a *trusted* name in the
    forwarded header does not save it. That is the whole point of the
    declaration — forwarded headers are a claim, and only a declared peer's
    claims are read.
    """
    client = _proxied(tmp_path, monkeypatch, trusted_hosts=(PROXY_HOST,))

    res = client.get(
        "/health", headers={"host": "evil.example", "X-Forwarded-Host": PROXY_HOST}
    )

    assert res.status_code == 403
    assert "evil.example" in res.json()["detail"]


def test_a_declared_proxys_forwarded_host_is_honoured_and_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same request from a declared peer is judged by the *forwarded* name.

    ``X-Forwarded-Host`` is what drove the browser's request, so it is the name
    the guard checks — which is why the operator still has to name it in
    ``CR_TRUSTED_HOSTS``: a declaration of the peer is not a declaration of the
    host, and a forwarded name nobody named is refused exactly like a direct one.
    """
    client = _proxied(
        tmp_path,
        monkeypatch,
        declared="203.0.113.7",
        trusted_hosts=(PROXY_HOST,),
    )

    served = client.get(
        "/health", headers={"host": "127.0.0.1:8765", "X-Forwarded-Host": PROXY_HOST}
    )
    forwarded_but_unnamed = client.get(
        "/health",
        headers={"host": "127.0.0.1:8765", "X-Forwarded-Host": "evil.example"},
    )

    assert served.status_code == 200
    assert forwarded_but_unnamed.status_code == 403
    assert "evil.example" in forwarded_but_unnamed.json()["detail"]


# Pins too (green at 9b37fbe and 897fd71): it is the criterion's own "still", a
# regression guard on the guard, and the discriminating tests for the same rule
# are the declared/undeclared halves above.
def test_a_spoofed_host_from_an_undeclared_peer_is_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's own check is untouched: a hostile ``Host`` is a hostile ``Host``.

    Nothing is declared at all here, so nothing about the request is read from a
    header: the rebound name is checked as it always was, and the
    ``X-Forwarded-For`` claiming a loopback client cannot make the peer look
    local either.
    """
    client = _proxied(tmp_path, monkeypatch)

    res = client.post(
        "/web/ui/projects",
        data={"name": "Evil"},
        headers={"host": "evil.example", "X-Forwarded-For": "127.0.0.1"},
    )

    assert res.status_code == 403
    assert "Host" in res.json()["detail"]


def _sign_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    declared: str | None,
    headers: dict[str, str],
) -> httpx.Response:
    """Sign in from :data:`OUTSIDE_PEER` with ``headers``; return the response."""
    client = _proxied(tmp_path, monkeypatch, declared=declared)
    client.app.state.auth.set_password(CONSOLE_PASSWORD, actor=CONSOLE)
    return client.post(
        SIGN_IN_PATH, data={"password": CONSOLE_PASSWORD}, headers=headers
    )


def _free_port() -> int:
    """A port nothing is listening on (bound and released, as tests do)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _set_cookie(response) -> dict[str, str]:
    """The session cookie's attributes, read off the header rather than the jar.

    The jar is the wrong instrument for this question: a browser (and httpx)
    refuses to *store* a ``Secure`` cookie that arrived over plain HTTP, so
    "the app set ``Secure``" is only visible in the header the app sent.
    """
    raw = response.headers["set-cookie"]
    _, _, rest = raw.partition("=")
    attributes = {}
    for part in rest.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        attributes[key.lower()] = value
    return attributes


def test_a_declared_proxys_forwarded_proto_secures_the_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TLS-terminating proxy the operator declared gets a ``Secure`` session.

    The console itself is spoken to over plain HTTP by the proxy, so the scheme
    the browser saw can only come from the declared peer's header — which is what
    keeps the cookie off a plain-HTTP hop.
    """
    response = _sign_in(
        tmp_path,
        monkeypatch,
        declared="203.0.113.7",
        headers={"X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 303
    assert _set_cookie(response)["secure"] == ""


# Pins at this level (green at 9b37fbe and 897fd71): this client talks to the app
# object, so the server whose default it would otherwise inherit never runs. The
# discriminating property is the same one over a real socket, in
# `test_the_server_does_not_honour_a_forwarded_scheme_by_itself` below.
def test_an_undeclared_peers_forwarded_proto_does_not_secure_the_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _sign_in(
        tmp_path, monkeypatch, declared=None, headers={"X-Forwarded-Proto": "https"}
    )

    assert response.status_code == 303
    assert "secure" not in _set_cookie(response)


def test_a_declared_peer_that_forwards_no_scheme_stays_plain_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declaration opens the door; it does not decide the scheme itself."""
    response = _sign_in(tmp_path, monkeypatch, declared="203.0.113.7", headers={})

    assert response.status_code == 303
    assert "secure" not in _set_cookie(response)


def _sign_in_over_a_real_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    declared: str | None,
    headers: dict[str, str],
):
    """Sign in through a **real** socket, with ``declared`` in the environment.

    ``TestClient`` speaks to the app object directly, so it never runs the server
    under the console — and that server's own forwarded-header handling is
    exactly what the console's declaration replaces. A real socket is what shows
    the difference: the peer here is genuinely loopback (the one address a test
    can arrive from), which is the address the server's handling trusts by
    *default*.
    """
    monkeypatch.setenv("CR_STATE_DIR", str(tmp_path / "state"))
    if declared is None:
        monkeypatch.delenv("CR_TRUSTED_PROXIES", raising=False)
    else:
        monkeypatch.setenv("CR_TRUSTED_PROXIES", declared)
    app = create_app(
        Registry.open(db_path=tmp_path / "registry.sqlite3"),
        trusted_hosts=("testserver",),
    )
    app.state.auth.set_password(CONSOLE_PASSWORD, actor=CONSOLE)
    port = _free_port()
    server = NodeServer(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    app.state.server = server
    thread = threading.Thread(target=server.run, name="guard-test-node", daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20.0
        while True:
            try:
                if (
                    httpx.get(
                        f"{base}/health", timeout=1.0, trust_env=False
                    ).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("the console never answered")
            time.sleep(0.02)
        # ``trust_env=False`` on both calls: this is a loopback request, and the
        # ambient ``*_proxy`` variables have no business in it.
        return httpx.post(
            f"{base}{SIGN_IN_PATH}",
            data={"password": CONSOLE_PASSWORD},
            headers=headers,
            follow_redirects=False,
            trust_env=False,
        )
    finally:
        server.should_exit = True
        thread.join(20.0)
        assert not thread.is_alive(), "the console did not stop"


def test_the_server_does_not_honour_a_forwarded_scheme_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over a real socket, with no peer declared, uvicorn's default is not the rule.

    A loopback peer's ``X-Forwarded-Proto`` is what the server under the console
    used to believe whatever the operator said; with the declaration landed it is
    believed only when the declaration names the peer, so this request gets a
    plain cookie and the browser's own scheme stays the truth.
    """
    response = _sign_in_over_a_real_socket(
        tmp_path, monkeypatch, declared=None, headers={"X-Forwarded-Proto": "https"}
    )

    assert response.status_code == 303
    assert "secure" not in _set_cookie(response)


# Declares 127.0.0.1, the peer a real socket always delivers here, so it would
# pass at 9b37fbe too — for the wrong reason (the server's own loopback default
# believed that header whatever the operator said). It pins the shape
# `--tailscale` declares; what discriminates the declaration is its sibling
# above, the *same* socket with nothing declared.
def test_a_declared_loopback_proxy_secures_the_cookie_over_a_real_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape ``--tailscale`` declares: Serve's hop is loopback.

    ``tailscale serve`` proxies to ``http://127.0.0.1:<port>`` and forwards the
    tailnet's ``https``, so the peer the console sees is loopback and the flag
    declares exactly that peer — with no second variable for the operator.
    """
    response = _sign_in_over_a_real_socket(
        tmp_path,
        monkeypatch,
        declared="127.0.0.1",
        headers={"X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 303
    assert _set_cookie(response)["secure"] == ""


def _url_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, declared: str | None
) -> tuple[TestClient, dict[str, str]]:
    """A client holding a machine token, and an app that reports its own URLs.

    No console route builds an absolute URL today (its links are relative), so
    the route added here asks the framework for the ones a template or a
    ``url_for`` *would* build — the request's own URL and the static mount's —
    which is exactly the URL the guard's resolution decides.
    """
    if declared is None:
        monkeypatch.delenv("CR_TRUSTED_PROXIES", raising=False)
    else:
        monkeypatch.setenv("CR_TRUSTED_PROXIES", declared)
    app = create_app(
        Registry.open(db_path=tmp_path / "registry.sqlite3"),
        trusted_hosts=(PROXY_HOST,),
    )

    @app.get("/api/v1/url-probe")
    def _probe(request: Request) -> dict[str, str]:
        return {
            "url": str(request.url),
            "static": str(request.url_for("static", path="app.css")),
        }

    client = TestClient(app, base_url=LOOPBACK_ORIGIN, client=OUTSIDE_PEER)
    minted, _row = app.state.auth.mint_token("url probe", actor=CONSOLE)
    return client, {"Authorization": f"Bearer {minted}"}


def test_a_declared_proxys_forwarded_host_and_scheme_drive_absolute_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, bearer = _url_probe(tmp_path, monkeypatch, declared="203.0.113.7")

    answer = client.get("/api/v1/url-probe", headers={**bearer, **_FORWARDED}).json()

    assert answer == {
        "url": f"https://{PROXY_HOST}/api/v1/url-probe",
        "static": f"https://{PROXY_HOST}/static/app.css",
    }


# Pins (green at 9b37fbe and 897fd71 — no console code read these headers then):
# the other half of the URL pair above, and the one that would silently change if
# the resolved name ever leaked into URL building from an undeclared peer.
def test_an_undeclared_peers_forwarded_headers_leave_the_socket_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: the same headers, believed by nobody."""
    client, bearer = _url_probe(tmp_path, monkeypatch, declared=None)

    answer = client.get("/api/v1/url-probe", headers={**bearer, **_FORWARDED}).json()

    assert answer == {
        "url": "http://127.0.0.1:8765/api/v1/url-probe",
        "static": "http://127.0.0.1:8765/static/app.css",
    }


def test_an_in_process_declaration_is_the_one_that_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--tailscale`` declares Serve's hop in-process, so the parameter leads.

    The environment names a *different* peer here on purpose: a caller that knows
    the hop — the flag that just set Serve up — passes it, and the declaration is
    not a fallback the environment can override.
    """
    monkeypatch.setenv("CR_TRUSTED_PROXIES", "198.51.100.4")
    app = create_app(
        Registry.open(db_path=tmp_path / "registry.sqlite3"),
        trusted_hosts=(),
        trusted_proxies=("203.0.113.7",),
    )
    client = TestClient(
        app, base_url=LOOPBACK_ORIGIN, client=OUTSIDE_PEER, follow_redirects=False
    )
    client.app.state.auth.set_password(CONSOLE_PASSWORD, actor=CONSOLE)

    response = client.post(
        SIGN_IN_PATH,
        data={"password": CONSOLE_PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 303
    assert _set_cookie(response)["secure"] == ""


def test_the_forwarded_facts_are_read_from_a_declared_peer_alone() -> None:
    """The rule the app tests above exercise, at the resolution itself."""
    headers = {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "console.example.com:8443",
        "X-Forwarded-For": "198.51.100.9, 203.0.113.7",
    }
    declared = frozenset({"203.0.113.7"})

    assert guard.forwarded_facts(headers, "203.0.113.7", declared) == guard.Forwarded(
        scheme="https", host="console.example.com:8443", client="198.51.100.9"
    )
    # One address away from the declaration and nothing is read: not the scheme,
    # not the host, not the client.
    assert guard.forwarded_facts(headers, "203.0.113.8", declared) == guard.Forwarded()
    assert guard.forwarded_facts(headers, None, declared) == guard.Forwarded()


def test_a_forwarded_chain_is_read_right_to_left() -> None:
    """Each hop appends, so the entry nearest this server is the last one.

    A declared hop inside the chain is skipped and the first address that is not
    a declared proxy is the client; a chain of nothing but declared proxies came
    from the far end, so its leftmost entry is.
    """
    declared = frozenset({"203.0.113.7", "10.0.0.7"})

    def client(chain: str) -> str | None:
        headers = {"X-Forwarded-For": chain}
        return guard.forwarded_facts(headers, "203.0.113.7", declared).client

    assert client("198.51.100.9, 203.0.113.7") == "198.51.100.9"
    assert client("203.0.113.7") == "203.0.113.7"
    assert client("198.51.100.9:5555, 10.0.0.7") == "198.51.100.9"
    assert client("198.51.100.9, 203.0.113.7, 10.0.0.7") == "198.51.100.9"
    assert client("10.0.0.7, 203.0.113.7") == "10.0.0.7"


def test_a_forwarded_value_that_is_not_usable_is_ignored() -> None:
    """A declared peer's junk is not written into the request either.

    Each field has a shape the console's own decisions are written for — a
    scheme of two, an authority, a chain of names — and a value outside it leaves
    the socket's own fact in force rather than a half-understood one.
    """
    declared = frozenset({"203.0.113.7"})

    def facts(header: str, value: str) -> guard.Forwarded:
        return guard.forwarded_facts({header: value}, "203.0.113.7", declared)

    assert facts("X-Forwarded-Proto", "javascript") == guard.Forwarded()
    assert facts("X-Forwarded-Proto", "https, http") == guard.Forwarded(scheme="https")
    assert facts("X-Forwarded-Host", "console.example.com/evil") == guard.Forwarded()
    assert facts("X-Forwarded-Host", "user@console.example.com") == guard.Forwarded()
    assert facts("X-Forwarded-Host", " ") == guard.Forwarded()
    # Whitespace is more than the space and tab the separators cover: a control
    # character inside the value (a newline, a carriage return, a NUL, DEL) is not
    # part of any authority, and letting one through would write it into the scope
    # ``Host`` the guard then checks and the URLs built from it.
    assert facts("X-Forwarded-Host", "console.example.com\nx") == guard.Forwarded()
    assert facts("X-Forwarded-Host", "console.example.com\rx") == guard.Forwarded()
    assert facts("X-Forwarded-Host", "console.example.com\tx") == guard.Forwarded()
    assert facts("X-Forwarded-Host", "console.example.com\x00x") == guard.Forwarded()
    assert facts("X-Forwarded-Host", "console.example.com\x7fx") == guard.Forwarded()
    assert facts("X-Forwarded-For", "") == guard.Forwarded()


def test_the_host_a_declared_peer_forwards_keeps_its_port() -> None:
    """The authority is the name the browser addressed, port and all."""
    headers = {"X-Forwarded-Host": "console.example.com:8443"}

    assert guard.forwarded_facts(
        headers, "203.0.113.7", frozenset({"203.0.113.7"})
    ) == (guard.Forwarded(host="console.example.com:8443"))


# --- The pure helpers, at their edges ------------------------------------- #
def test_host_name_normalizes_ports_brackets_and_case() -> None:
    assert guard.host_name("127.0.0.1:8765") == "127.0.0.1"
    assert guard.host_name("[::1]:8765") == "::1"
    assert guard.host_name("LOCALHOST.") == "localhost"
    assert guard.host_name("") is None
    assert guard.host_name(None) is None


def test_loopback_detection_covers_names_and_ip_literals() -> None:
    assert guard.is_loopback_host("localhost")
    assert guard.is_loopback_host("app.localhost")
    assert guard.is_loopback_host("127.0.0.1")
    assert guard.is_loopback_host("::1")
    assert not guard.is_loopback_host("evil.example")
    assert not guard.is_loopback_host(None)
