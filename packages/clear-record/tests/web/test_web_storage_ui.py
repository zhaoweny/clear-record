"""The console's managed-storage UI: upload, sizes and delete (ADR-0024).

The upload endpoint has its own tests (``test_web_upload.py``); these exercise
the *console* over it — the storage panel a meeting renders, the upload control
with the service's guards surfaced as text, the delete controls, and the
manual-only retention wording. English is the source locale, so most assertions
read the English strings; a ``zh_CN`` case proves the new strings go through
``tr``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clear_record.service import Registry, managed, paths
from clear_record.web.app import AUDIO_ACCEPT, LANG_COOKIE, create_app


@pytest.fixture(autouse=True)
def _managed_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "absent.toml")
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))


@pytest.fixture()
def client(tmp_path) -> TestClient:
    return TestClient(create_app(Registry.open(db_path=tmp_path / "registry.sqlite3")))


def _project(client: TestClient, name: str = "Ops") -> None:
    client.post("/api/projects", json={"name": name})


def _managed_meeting(client: TestClient, title: str = "Kickoff") -> dict:
    """A managed meeting, via the JSON API (the UI creation route is tested too)."""
    created = client.post(
        "/api/projects/ops/meetings", json={"title": title, "managed": True}
    )
    assert created.status_code == 201, created.text
    return created.json()


def _user_chosen_meeting(client: TestClient, path: Path, title: str = "Local") -> dict:
    created = client.post(
        "/api/projects/ops/meetings", json={"title": title, "workspace_path": str(path)}
    )
    assert created.status_code == 201, created.text
    return created.json()


def _upload(client: TestClient, meeting_id: int, name: str, payload: bytes):
    return client.post(
        f"/ui/meetings/{meeting_id}/tapes/upload",
        files={"file": (name, payload, "audio/wav")},
    )


# --- managed by default, path as the override ------------------------------ #
def test_the_console_creates_a_managed_meeting_by_default(client, tmp_path) -> None:
    _project(client)

    created = client.post("/ui/projects/ops/meetings", data={"title": "Kickoff"})

    assert created.status_code == 200
    meeting = client.get("/api/projects/ops/meetings").json()[0]
    assert Path(meeting["workspace_path"]).is_dir()
    assert Path(meeting["workspace_path"]).is_relative_to(tmp_path / "managed")
    # The resolved path is on the project view itself (htmx may not have run).
    assert meeting["workspace_path"] in created.text
    assert "status-managed" in created.text


def test_a_workspace_path_is_still_a_user_chosen_override(client, tmp_path) -> None:
    _project(client)
    chosen = tmp_path / "user-docs"
    chosen.mkdir()

    created = client.post(
        "/ui/projects/ops/meetings",
        data={"title": "Local", "workspace_path": str(chosen)},
    )

    assert created.status_code == 200
    meeting = client.get("/api/projects/ops/meetings").json()[0]
    assert meeting["workspace_path"] == str(chosen)
    assert not Path(meeting["workspace_path"]).is_relative_to(tmp_path / "managed")


# --- the storage panel ------------------------------------------------------ #
def test_the_panel_shows_the_resolved_path_sizes_and_tapes(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    payload = b"12345"
    assert _upload(client, meeting["id"], "a.wav", payload).status_code == 200

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")

    assert panel.status_code == 200
    assert meeting["workspace_path"] in panel.text  # resolved path
    assert "managed workspace" in panel.text
    assert "a.wav" in panel.text
    assert "5 B" in panel.text  # the tape's and the workspace's reported size
    assert hashlib.sha256(payload).hexdigest()[:12] in panel.text
    assert "Free on the managed disk" in panel.text
    assert "Managed root" in panel.text


def test_the_panel_offers_an_upload_built_from_the_service_allow_list(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")

    assert f'hx-post="/ui/meetings/{meeting["id"]}/tapes/upload"' in panel.text
    assert 'hx-encoding="multipart/form-data"' in panel.text
    # The picker reuses the guard's own list, never a restated one.
    assert f'accept="{AUDIO_ACCEPT}"' in panel.text
    assert ".wav" in AUDIO_ACCEPT and ".txt" not in AUDIO_ACCEPT
    # htmx 4 sends the body with fetch(), which reports no upload progress, so
    # the control shows an indeterminate busy state instead. The removed XHR
    # progress event must not come back, and the chosen file must survive the
    # storage re-render.
    assert "htmx:finally:request" in panel.text
    assert "htmx:xhr:progress" not in panel.text
    assert "hx-preserve" in panel.text


def test_a_user_chosen_meeting_has_no_upload_and_says_why(client, tmp_path) -> None:
    _project(client)
    chosen = tmp_path / "user-docs"
    chosen.mkdir()
    meeting = _user_chosen_meeting(client, chosen)

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")

    assert "user-chosen workspace" in panel.text
    assert str(chosen) in panel.text
    assert "/tapes/upload" not in panel.text
    # The service's own refusal, not a restatement of it.
    assert "user-chosen workspace, which has no managed place" in panel.text


def test_storage_of_an_unknown_meeting_is_404(client) -> None:
    assert client.get("/ui/meetings/999/storage").status_code == 404


# --- uploading through the panel -------------------------------------------- #
def test_upload_lands_a_tape_and_rerenders_the_panel(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    payload = b"RIFF-fake-audio"

    panel = _upload(client, meeting["id"], "a.wav", payload)

    assert panel.status_code == 200
    assert "a.wav" in panel.text
    storage = client.get(f"/api/meetings/{meeting['id']}/storage").json()
    assert [tape["name"] for tape in storage["tapes"]] == ["a.wav"]
    assert Path(storage["tapes"][0]["path"]).read_bytes() == payload


def test_a_refusal_is_a_rendered_panel_not_a_dead_end(client) -> None:
    """A guard failure comes back as the panel with the message in place."""
    _project(client)
    meeting = _managed_meeting(client)

    panel = _upload(client, meeting["id"], "../escape.wav", b"x")

    assert panel.status_code == 200
    assert "Filename refused" in panel.text
    assert "traversal" in panel.text
    assert client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"] == []


def test_the_non_audio_guard_is_surfaced(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)

    panel = _upload(client, meeting["id"], "notes.txt", b"x")

    assert "Not an audio tape" in panel.text
    assert "allowed extensions" in panel.text


def test_the_size_guard_is_surfaced(client, monkeypatch) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    monkeypatch.setattr(managed, "max_upload_bytes", lambda: 4)

    panel = _upload(client, meeting["id"], "a.wav", b"0123456789")

    assert "Upload too large" in panel.text
    assert "CR_MAX_UPLOAD_BYTES" in panel.text
    assert client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"] == []


def test_the_disk_guard_is_surfaced(client, monkeypatch) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    monkeypatch.setattr(
        managed.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )

    panel = _upload(client, meeting["id"], "a.wav", b"x")

    assert "Not enough disk space" in panel.text
    assert "CR_WORKSPACE_ROOT" in panel.text


def test_the_panel_renders_the_services_free_space(client, monkeypatch) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    monkeypatch.setattr(managed, "root_free_bytes", lambda root=None: 5 * 1024**3)

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")

    assert "Free on the managed disk: 5.0 GiB" in panel.text


def test_the_panel_and_the_guard_share_one_free_space_accounting(
    client, monkeypatch
) -> None:
    """Patching the one seam moves both the panel's number and the guard."""
    _project(client)
    meeting = _managed_meeting(client)
    monkeypatch.setattr(managed, "root_free_bytes", lambda root=None: 0)

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")
    assert "Free on the managed disk: 0 B" in panel.text

    refused = _upload(client, meeting["id"], "a.wav", b"x")
    assert "Not enough disk space" in refused.text
    assert "CR_WORKSPACE_ROOT" in refused.text


def test_the_ui_upload_accepts_an_upload_id(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)

    panel = client.post(
        f"/ui/meetings/{meeting['id']}/tapes/upload?upload_id=take-01",
        files={"file": ("a.wav", b"RIFF", "audio/wav")},
    )

    assert panel.status_code == 200
    assert "a.wav" in panel.text
    storage = client.get(f"/api/meetings/{meeting['id']}/storage").json()
    assert [tape["name"] for tape in storage["tapes"]] == ["a.wav"]


def test_the_ui_upload_surfaces_a_malformed_upload_id(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)

    panel = client.post(
        f"/ui/meetings/{meeting['id']}/tapes/upload?upload_id=not%20a%20token",
        files={"file": ("a.wav", b"x", "audio/wav")},
    )

    assert panel.status_code == 200
    assert "Invalid upload id" in panel.text
    assert client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"] == []


def test_the_user_chosen_workspace_guard_is_surfaced(client, tmp_path) -> None:
    _project(client)
    chosen = tmp_path / "user-docs"
    chosen.mkdir()
    meeting = _user_chosen_meeting(client, chosen)

    panel = _upload(client, meeting["id"], "a.wav", b"x")

    assert panel.status_code == 200
    assert "This meeting cannot take an upload" in panel.text
    assert "user-chosen workspace" in panel.text
    assert list(chosen.iterdir()) == []


# --- deleting --------------------------------------------------------------- #
def test_each_tape_has_a_confirmed_delete_that_names_the_archive(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    _upload(client, meeting["id"], "a.wav", b"x")
    stored = client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"][0]

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")
    assert (
        f'hx-delete="/ui/meetings/{meeting["id"]}/tapes/{stored["id"]}"' in panel.text
    )
    assert "archive is the durable copy" in panel.text

    removed = client.delete(f"/ui/meetings/{meeting['id']}/tapes/{stored['id']}")

    assert removed.status_code == 200
    assert "a.wav" not in removed.text
    assert not Path(stored["path"]).exists()
    assert client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"] == []


def test_a_meeting_can_delete_all_its_tapes_at_once(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    for name in ("a.wav", "b.wav"):
        _upload(client, meeting["id"], name, b"x")

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")
    assert f'hx-delete="/ui/meetings/{meeting["id"]}/tapes"' in panel.text
    assert "Delete all of this meeting" in panel.text
    assert "archive is the durable copy" in panel.text

    removed = client.delete(f"/ui/meetings/{meeting['id']}/tapes")

    assert removed.status_code == 200
    assert "No tapes uploaded yet." in removed.text
    assert client.get(f"/api/meetings/{meeting['id']}/storage").json()["tapes"] == []


def test_retention_is_manual_only(client) -> None:
    """The panel never implies the node cleans up on its own."""
    _project(client)
    meeting = _managed_meeting(client)

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")

    assert "never deletes tapes on its own" in panel.text
    assert "deleting here is manual" in panel.text
    assert "archive is the durable copy" in panel.text


# --- the new strings are translated ----------------------------------------- #
def test_the_panel_strings_go_through_tr(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    client.cookies.set(LANG_COOKIE, "zh_CN")

    panel = client.get(f"/ui/meetings/{meeting['id']}/storage")

    assert "托管工作区" in panel.text  # managed workspace
    assert "上传录音" in panel.text  # Upload tape
    assert "托管磁盘可用空间" in panel.text  # Free on the managed disk


def test_the_upload_id_refusal_is_translated(client) -> None:
    _project(client)
    meeting = _managed_meeting(client)
    client.cookies.set(LANG_COOKIE, "zh_CN")

    panel = client.post(
        f"/ui/meetings/{meeting['id']}/tapes/upload?upload_id=not%20a%20token",
        files={"file": ("a.wav", b"x", "audio/wav")},
    )

    assert "上传 ID 无效" in panel.text  # Invalid upload id
