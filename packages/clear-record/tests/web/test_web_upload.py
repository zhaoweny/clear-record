"""HTTP behaviour of the managed workspace: upload, storage and delete (ADR-0024).

A temp registry, a temp managed root and FastAPI's test client — no network, no
ASR backend. The service guards are exercised through the endpoint too, so the
*status* and the user-facing *message* are both pinned.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clear_record.service import Registry, RunManager
from clear_record.service import managed
from clear_record.service import paths
from clear_record.web.app import create_app


@pytest.fixture(autouse=True)
def _managed_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "absent.toml")
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))


@pytest.fixture()
def client(tmp_path) -> TestClient:
    return TestClient(create_app(Registry.open(db_path=tmp_path / "registry.sqlite3")))


def _managed_meeting(client: TestClient, title: str = "Kickoff") -> dict:
    client.post("/api/projects", json={"name": "Ops"})
    res = client.post(
        "/api/projects/ops/meetings", json={"title": title, "managed": True}
    )
    assert res.status_code == 201, res.text
    return res.json()


def _upload(client: TestClient, meeting_id: int, name: str, payload: bytes):
    return client.post(
        f"/api/meetings/{meeting_id}/tapes",
        files={"file": (name, payload, "audio/wav")},
    )


# --- provisioning ---------------------------------------------------------- #
def test_creating_a_managed_meeting_provisions_its_workspace(client) -> None:
    meeting = _managed_meeting(client)

    assert meeting["workspace_path"] == str(
        Path(meeting["workspace_path"]).parent.parent / "ops" / "kickoff"
    )
    assert Path(meeting["workspace_path"]).is_dir()


def test_managed_ignores_a_workspace_path_but_dir_still_works(client, tmp_path) -> None:
    client.post("/api/projects", json={"name": "Ops"})
    chosen = tmp_path / "user-docs"
    chosen.mkdir()

    managed_meeting = client.post(
        "/api/projects/ops/meetings",
        json={"title": "Managed", "workspace_path": str(chosen), "managed": True},
    ).json()
    assert managed_meeting["workspace_path"] != str(chosen)

    dir_meeting = client.post(
        "/api/projects/ops/meetings",
        json={"title": "Dir", "workspace_path": str(chosen)},
    ).json()
    assert dir_meeting["workspace_path"] == str(chosen)


# --- a successful upload --------------------------------------------------- #
def test_upload_records_a_checksummed_tape(client) -> None:
    meeting = _managed_meeting(client)
    payload = b"RIFF-fake-audio"

    res = _upload(client, meeting["id"], "a.wav", payload)

    assert res.status_code == 201, res.text
    tape = res.json()
    assert tape["sha256"] == hashlib.sha256(payload).hexdigest()
    assert tape["bytes"] == len(payload)
    assert Path(tape["path"]).read_bytes() == payload
    assert Path(tape["path"]).parent == Path(meeting["workspace_path"]) / "tapes"


def test_upload_to_an_unknown_meeting_is_404(client) -> None:
    assert (
        client.post(
            "/api/meetings/999/tapes", files={"file": ("a.wav", b"x")}
        ).status_code
        == 404
    )


# --- guards ---------------------------------------------------------------- #
def test_traversal_filename_is_rejected(client) -> None:
    meeting = _managed_meeting(client)
    res = _upload(client, meeting["id"], "../escape.wav", b"x")
    assert res.status_code == 400
    assert "traversal" in res.json()["detail"]
    assert client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"] == []


def test_disallowed_extension_is_415(client) -> None:
    meeting = _managed_meeting(client)
    res = _upload(client, meeting["id"], "notes.txt", b"x")
    assert res.status_code == 415
    assert "audio tape" in res.json()["detail"]


def test_missing_file_part_is_400(client) -> None:
    meeting = _managed_meeting(client)
    res = client.post(
        f"/api/meetings/{meeting['id']}/tapes",
        files={"wrong": ("a.wav", b"x", "audio/wav")},
    )
    assert res.status_code == 400
    assert "part named 'file'" in res.json()["detail"]


def test_oversize_upload_is_413(client, monkeypatch) -> None:
    meeting = _managed_meeting(client)
    monkeypatch.setattr(managed, "max_upload_bytes", lambda: 4)
    res = _upload(client, meeting["id"], "a.wav", b"0123456789")
    assert res.status_code == 413
    assert "CR_MAX_UPLOAD_BYTES" in res.json()["detail"]
    assert client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"] == []


def test_low_disk_is_507(client, monkeypatch) -> None:
    meeting = _managed_meeting(client)
    monkeypatch.setattr(
        managed.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )
    res = _upload(client, meeting["id"], "a.wav", b"x")
    assert res.status_code == 507
    assert "CR_WORKSPACE_ROOT" in res.json()["detail"]


def test_upload_into_a_user_chosen_workspace_is_400(client, tmp_path) -> None:
    client.post("/api/projects", json={"name": "Ops"})
    chosen = tmp_path / "user-docs"
    chosen.mkdir()
    meeting = client.post(
        "/api/projects/ops/meetings",
        json={"title": "Local", "workspace_path": str(chosen)},
    ).json()

    res = _upload(client, meeting["id"], "a.wav", b"x")
    assert res.status_code == 400
    assert "user-chosen workspace" in res.json()["detail"]
    assert list(chosen.iterdir()) == []


# --- storage visibility ---------------------------------------------------- #
def test_storage_reports_size_and_tapes(client) -> None:
    meeting = _managed_meeting(client)
    _upload(client, meeting["id"], "a.wav", b"12345")
    _upload(client, meeting["id"], "b.wav", b"123")

    storage = client.get(f"/api/meetings/{meeting['id']}/storage").json()
    assert storage["managed"] is True
    assert storage["bytes"] == 8
    assert [tape["name"] for tape in storage["tapes"]] == ["a.wav", "b.wav"]


def test_storage_unknown_meeting_is_404(client) -> None:
    assert client.get("/api/meetings/999/storage").status_code == 404


def test_delete_removes_the_tape_and_names_the_archive(client) -> None:
    meeting = _managed_meeting(client)
    tape = _upload(client, meeting["id"], "a.wav", b"x").json()

    res = client.delete(f"/api/meetings/{meeting['id']}/tapes/{tape['id']}")

    assert res.status_code == 200
    assert "archive is the durable copy" in res.json()["note"]
    assert not Path(tape["path"]).exists()
    assert client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"] == []


def test_delete_unknown_tape_is_404(client) -> None:
    meeting = _managed_meeting(client)
    assert client.delete(f"/api/meetings/{meeting['id']}/tapes/999").status_code == 404
    assert client.delete("/api/meetings/999/tapes/1").status_code == 404


# --- the upload feeds the existing run path -------------------------------- #
def test_an_uploaded_tape_feeds_the_existing_run(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "absent.toml")
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    seen: list[list[str]] = []

    def fake_pipeline(directory, options, on_event) -> None:
        seen.append(list(options.audio_files))
        Path(directory, "record.json").write_text("{}", encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    client = TestClient(create_app(registry, runs=manager))
    meeting = _managed_meeting(client)
    tape = _upload(client, meeting["id"], "a.wav", b"RIFF").json()

    started = client.post(
        f"/api/meetings/{meeting['id']}/runs", json={"backend": "apple"}
    )
    assert started.status_code == 202, started.text
    state = manager.wait(started.json()["run"]["id"], timeout=10)

    assert state.status == "done"
    assert seen == [[tape["path"]]]
    assert [a.kind for a in registry.list_artifacts(meeting["id"])] == ["record"]
