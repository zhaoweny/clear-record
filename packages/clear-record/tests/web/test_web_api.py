"""HTTP behaviour of the bundled console, through the service seam.

Two surfaces: the JSON API (``/api/*``) for machines and the server-rendered
htmx/Alpine fragments (``/ui/*``) for the browser. A temp registry and FastAPI's
test client — no network, no browser.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clear_record.service import Registry
from clear_record.web.app import create_app


@pytest.fixture()
def client(tmp_path) -> TestClient:
    app = create_app(Registry.open(db_path=tmp_path / "registry.sqlite3"))
    return TestClient(app)


def _make_project(client, name: str = "Weekly Ops") -> dict:
    res = client.post("/api/projects", json={"name": name})
    assert res.status_code == 201
    return res.json()


# --- HTML surface (htmx + Alpine, server-rendered) ------------------------ #
def test_index_is_served_with_the_vendored_libraries(client) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "/static/htmx.min.js" in res.text
    assert "/static/alpine.min.js" in res.text
    assert "project console" in res.text


def test_vendored_assets_are_served(client) -> None:
    for path in ("/static/htmx.min.js", "/static/alpine.min.js", "/static/app.css"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert res.content


def test_ui_project_list_and_create(client) -> None:
    assert "No projects yet." in client.get("/ui/projects").text

    created = client.post("/ui/projects", data={"name": "Weekly Ops"})
    assert created.status_code == 200
    assert "Weekly Ops" in created.text
    assert "Weekly Ops" in client.get("/ui/projects").text


def test_ui_detail_and_glossary_roundtrip(client) -> None:
    _make_project(client)

    detail = client.get("/ui/projects/weekly-ops")
    assert detail.status_code == 200
    assert "No glossary terms yet." in detail.text

    added = client.post(
        "/ui/projects/weekly-ops/glossary",
        data={"term": "Falcon", "definition": "the project"},
    )
    assert added.status_code == 200
    assert "Falcon" in added.text

    term_id = client.get("/api/projects/weekly-ops/glossary").json()[0]["id"]
    promoted = client.post(
        f"/ui/glossary/{term_id}/status", data={"status": "confirmed"}
    )
    assert promoted.status_code == 200
    assert "confirmed" in promoted.text

    removed = client.delete(f"/ui/glossary/{term_id}")
    assert removed.status_code == 200
    assert "No glossary terms yet." in removed.text


def test_ui_unknown_project_is_404(client) -> None:
    assert client.get("/ui/projects/nope").status_code == 404


# --- JSON API ------------------------------------------------------------- #
def test_health_reports_the_registry(client) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["registry"].endswith("registry.sqlite3")


def test_project_and_glossary_flow(client) -> None:
    created = client.post("/api/projects", json={"name": "Weekly Ops"})
    assert created.status_code == 201
    assert created.json()["slug"] == "weekly-ops"

    listed = client.get("/api/projects").json()
    assert [p["slug"] for p in listed] == ["weekly-ops"]
    assert listed[0]["term_count"] == 0

    added = client.post(
        "/api/projects/weekly-ops/glossary",
        json={"term": "Falcon", "definition": "the project"},
    )
    assert added.status_code == 201
    term_id = added.json()["id"]
    assert added.json()["status"] == "candidate"

    terms = client.get("/api/projects/weekly-ops/glossary").json()
    assert [t["term"] for t in terms] == ["Falcon"]

    patched = client.patch(f"/api/glossary/{term_id}", json={"status": "confirmed"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "confirmed"

    assert client.get("/api/projects").json()[0]["term_count"] == 1

    assert client.delete(f"/api/glossary/{term_id}").status_code == 204
    assert client.get("/api/projects/weekly-ops/glossary").json() == []


def test_glossary_status_filter(client) -> None:
    client.post("/api/projects", json={"name": "Ops"})
    client.post("/api/projects/ops/glossary", json={"term": "A", "status": "confirmed"})
    client.post("/api/projects/ops/glossary", json={"term": "B"})
    confirmed = client.get("/api/projects/ops/glossary?status=confirmed").json()
    assert [t["term"] for t in confirmed] == ["A"]


def test_unknown_project_is_404(client) -> None:
    assert client.get("/api/projects/nope").status_code == 404
    assert client.get("/api/projects/nope/glossary").status_code == 404
    assert (
        client.post("/api/projects/nope/glossary", json={"term": "A"}).status_code
        == 404
    )


def test_duplicate_slug_is_409(client) -> None:
    client.post("/api/projects", json={"name": "Ops", "slug": "ops"})
    assert (
        client.post("/api/projects", json={"name": "Other", "slug": "ops"}).status_code
        == 409
    )


def test_duplicate_term_is_409(client) -> None:
    client.post("/api/projects", json={"name": "Ops"})
    client.post("/api/projects/ops/glossary", json={"term": "A"})
    assert (
        client.post("/api/projects/ops/glossary", json={"term": "A"}).status_code == 409
    )


def test_invalid_status_is_400(client) -> None:
    client.post("/api/projects", json={"name": "Ops"})
    term_id = client.post("/api/projects/ops/glossary", json={"term": "A"}).json()["id"]
    res = client.patch(f"/api/glossary/{term_id}", json={"status": "bogus"})
    assert res.status_code == 400


def test_unknown_term_is_404(client) -> None:
    assert (
        client.patch("/api/glossary/999", json={"status": "confirmed"}).status_code
        == 404
    )
    assert client.delete("/api/glossary/999").status_code == 404


def test_shutdown_is_refused_without_a_managed_server(client) -> None:
    """The Quit button only works under the real server (not a test client)."""
    assert client.post("/api/shutdown").status_code == 409
