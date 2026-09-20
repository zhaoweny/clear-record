"""The request guard: DNS rebinding (``Host``) and CSRF (``Origin``/``Referer``).

ADR-0021: bound-to-localhost is not the same as safe-from-the-browser. These
tests drive the guard through the app with its **loopback-only default** (an
explicit empty trusted set, so the ambient test env cannot leak in) and a
loopback base URL, so "ordinary same-origin use" is genuine rather than a test
convention. The reverse-proxy escape hatch is covered by the ``CR_TRUSTED_HOSTS``
test at the bottom.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clear_record.service import Registry
from clear_record.web import guard
from clear_record.web.app import create_app

#: The console's own origin: what a real browser would put in ``Origin`` and the
#: host it would send in ``Host``.
LOOPBACK_ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(
        Registry.open(db_path=tmp_path / "registry.sqlite3"), trusted_hosts=()
    )
    return TestClient(app, base_url=LOOPBACK_ORIGIN)


def _projects(client: TestClient) -> list[dict]:
    return client.get("/api/projects").json()


# --- CSRF: Origin / Referer on state-changing requests -------------------- #
def test_cross_origin_form_post_is_rejected(client: TestClient) -> None:
    """The headline case: a hostile page's form POST to /ui/projects."""
    res = client.post(
        "/ui/projects",
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
        "/api/projects",
        json={"name": "Evil"},
        headers={"Origin": "http://evil.example"},
    )
    assert res.status_code == 403
    assert _projects(client) == []


def test_referer_is_used_when_origin_is_absent(client: TestClient) -> None:
    res = client.post(
        "/ui/projects",
        data={"name": "Evil"},
        headers={"Referer": "http://evil.example/page"},
    )
    assert res.status_code == 403
    assert _projects(client) == []


def test_opaque_null_origin_is_rejected(client: TestClient) -> None:
    res = client.post(
        "/ui/projects",
        data={"name": "Evil"},
        headers={"Origin": "null"},
    )
    assert res.status_code == 403
    assert _projects(client) == []


def test_same_origin_post_with_an_origin_header_is_allowed(client: TestClient) -> None:
    res = client.post(
        "/ui/projects",
        data={"name": "Weekly Ops"},
        headers={"Origin": LOOPBACK_ORIGIN},
    )
    assert res.status_code == 200
    assert [p["slug"] for p in _projects(client)] == ["weekly-ops"]


# --- DNS rebinding: Host on every request --------------------------------- #
def test_mismatched_host_is_rejected(client: TestClient) -> None:
    res = client.post(
        "/ui/projects", data={"name": "Evil"}, headers={"host": "evil.example"}
    )
    assert res.status_code == 403
    assert "Host" in res.json()["detail"]
    # Host is method-blind: a rebound GET is a disclosure, so it is rejected too.
    assert (
        client.get("/ui/projects", headers={"host": "evil.example"}).status_code == 403
    )
    assert _projects(client) == []


def test_absent_host_is_rejected(client: TestClient) -> None:
    res = client.post("/ui/projects", data={"name": "Evil"}, headers={"host": ""})
    assert res.status_code == 403
    assert "missing" in res.json()["detail"]
    assert _projects(client) == []


# --- Ordinary same-origin use is unaffected ------------------------------- #
def test_same_origin_gets_and_posts_still_work(client: TestClient) -> None:
    assert client.get("/ui/projects").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"

    # htmx form POST with no Origin (a non-browser default) and with the browser's
    # own Origin both pass.
    assert client.post("/ui/projects", data={"name": "Ops"}).status_code == 200
    assert (
        client.post(
            "/ui/projects",
            data={"name": "Weekly"},
            headers={"Origin": LOOPBACK_ORIGIN},
        ).status_code
        == 200
    )
    assert {p["slug"] for p in _projects(client)} == {"ops", "weekly"}


def test_non_browser_client_without_origin_is_allowed(client: TestClient) -> None:
    """curl / a script sends neither Origin nor Referer; it is not the threat."""
    res = client.post("/api/projects", json={"name": "Scripted"})
    assert res.status_code == 201
    assert res.json()["slug"] == "scripted"


# --- The escape hatch: CR_TRUSTED_HOSTS ----------------------------------- #
def test_trusted_hosts_env_allows_a_proxys_public_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy hostname in CR_TRUSTED_HOSTS is trusted without opening loopback."""
    monkeypatch.setenv("CR_TRUSTED_HOSTS", "console.example.com")
    app = create_app(Registry.open(db_path=tmp_path / "registry.sqlite3"))
    client = TestClient(app, base_url=LOOPBACK_ORIGIN)

    res = client.post(
        "/ui/projects",
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
            "/ui/projects",
            data={"name": "Evil"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )


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
