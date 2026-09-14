"""HTTP behaviour of the bundled console, through the service seam.

Two surfaces: the JSON API (``/api/*``) for machines and the server-rendered
htmx/Alpine fragments (``/ui/*``) for the browser. A temp registry and FastAPI's
test client — no network, no browser.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clear_record.core import PROFILE_CUSTOM, PROFILES, Progress
from clear_record.service import Registry, RunManager
from clear_record.web.app import create_app


@pytest.fixture()
def client(tmp_path) -> TestClient:
    app = create_app(Registry.open(db_path=tmp_path / "registry.sqlite3"))
    return TestClient(app)


@pytest.fixture()
def console(tmp_path) -> SimpleNamespace:
    """A test client over a registry with an injected, gated fake pipeline."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    gate = threading.Event()
    seen: list = []

    def fake_pipeline(directory, options, on_event) -> None:
        seen.append(options)
        progress = Progress("transcribe", 2, on_event)
        progress.start("transcribing")
        progress.advance(source="a", message="chunk 1")
        gate.wait(10)
        progress.advance(source="b", message="chunk 2")
        export = Path(directory) / "export"
        export.mkdir(parents=True, exist_ok=True)
        (export / "record.md").write_text("# record\n", encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    return SimpleNamespace(
        client=TestClient(create_app(registry, runs=manager)),
        registry=registry,
        manager=manager,
        gate=gate,
        seen=seen,
    )


def _make_meeting(console, tmp_path, title: str = "Kickoff") -> dict:
    client = console.client
    client.post("/api/projects", json={"name": "Ops"})
    res = client.post(
        "/api/projects/ops/meetings",
        json={"title": title, "workspace_path": str(tmp_path)},
    )
    assert res.status_code == 201
    return res.json()


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


# --- JSON API: meetings, tapes and runs ----------------------------------- #
def test_meeting_api_create_list_and_get(console, tmp_path) -> None:
    client = console.client
    client.post("/api/projects", json={"name": "Ops"})

    created = client.post(
        "/api/projects/ops/meetings",
        json={"title": "Kickoff", "workspace_path": str(tmp_path)},
    )
    assert created.status_code == 201
    meeting = created.json()
    assert meeting["project_slug"] == "ops"
    assert meeting["slug"] == "kickoff"
    assert meeting["status"] == "new"

    listed = client.get("/api/projects/ops/meetings").json()
    assert [m["id"] for m in listed] == [meeting["id"]]
    assert listed[0]["project_slug"] == "ops"

    fetched = client.get(f"/api/meetings/{meeting['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Kickoff"

    assert client.get("/api/projects/nope/meetings").status_code == 404
    assert client.get("/api/meetings/999").status_code == 404


def test_tapes_api_roundtrip(console, tmp_path) -> None:
    client = console.client
    meeting = _make_meeting(console, tmp_path)

    res = client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav"), str(tmp_path / "b.wav")]},
    )
    assert res.status_code == 201
    assert res.json()["paths"] == [str(tmp_path / "a.wav"), str(tmp_path / "b.wav")]

    assert (
        client.put("/api/meetings/999/tapes", json={"paths": ["x.wav"]}).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/meetings/{meeting['id']}/tapes", json={"paths": []}
        ).status_code
        == 400
    )


def test_run_api_lifecycle(console, tmp_path) -> None:
    client = console.client
    meeting = _make_meeting(console, tmp_path)
    tape = str(tmp_path / "a.wav")
    client.put(f"/api/meetings/{meeting['id']}/tapes", json={"paths": [tape]})

    started = client.post(
        f"/api/meetings/{meeting['id']}/runs", json={"backend": "apple"}
    )
    assert started.status_code == 202
    run_id = started.json()["run"]["id"]
    assert started.json()["state"]["status"] in {"queued", "running"}

    console.gate.set()
    state = console.manager.wait(run_id, timeout=10)
    assert state.status == "done"

    fetched = client.get(f"/api/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run"]["status"] == "done"
    assert fetched.json()["state"]["status"] == "done"
    assert fetched.json()["state"]["stage"] == "transcribe"

    events = client.get(f"/api/runs/{run_id}/events?after=0").json()
    assert events["next"] == 3
    assert [event["index"] for event in events["events"]] == [0, 1, 2]
    assert events["events"][0]["message"] == "transcribing"

    tail = client.get(f"/api/runs/{run_id}/events?after={events['next']}").json()
    assert tail == {"events": [], "next": 3}


def test_a_second_run_is_409(console, tmp_path) -> None:
    client = console.client
    meeting = _make_meeting(console, tmp_path)
    client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )
    first = client.post(f"/api/meetings/{meeting['id']}/runs", json={})
    assert first.status_code == 202

    conflict = client.post(f"/api/meetings/{meeting['id']}/runs", json={})
    assert conflict.status_code == 409

    console.gate.set()
    console.manager.wait(first.json()["run"]["id"], timeout=10)


def test_a_run_without_tapes_or_workspace_is_400(console, tmp_path) -> None:
    client = console.client
    # A workspace but no tape set.
    meeting = _make_meeting(console, tmp_path)
    assert (
        client.post(f"/api/meetings/{meeting['id']}/runs", json={}).status_code == 400
    )

    # A tape set but no workspace (created through the store, not the API).
    console.registry.create_project("Ops")
    no_workspace = console.registry.create_meeting("ops", "No workspace")
    console.registry.set_recording_set(no_workspace.id, [str(tmp_path / "x.wav")])
    res = client.post(f"/api/meetings/{no_workspace.id}/runs", json={})
    assert res.status_code == 400


def test_unknown_run_endpoints_are_404(console) -> None:
    client = console.client
    assert client.get("/api/runs/999").status_code == 404
    assert client.get("/api/runs/999/events").status_code == 404


# --- HTML surface: meetings and the live run fragment --------------------- #
def test_ui_create_meeting_and_save_tapes(console, tmp_path) -> None:
    client = console.client
    client.post("/ui/projects", data={"name": "Ops"})

    created = client.post(
        "/ui/projects/ops/meetings",
        data={"title": "Kickoff", "workspace_path": str(tmp_path)},
    )
    assert created.status_code == 200
    assert "Kickoff" in created.text
    assert "No run yet." in created.text

    meeting_id = console.registry.list_meetings("ops")[0].id
    assert f'hx-post="/ui/meetings/{meeting_id}/runs"' in created.text

    saved = client.post(
        f"/ui/meetings/{meeting_id}/tapes",
        data={"paths": f"{tmp_path}/a.wav\n{tmp_path}/b.wav"},
    )
    assert saved.status_code == 200
    assert f"{tmp_path}/a.wav" in saved.text
    assert f"{tmp_path}/b.wav" in saved.text


def test_ui_run_fragment_polls_while_running(console, tmp_path) -> None:
    client = console.client
    meeting = _make_meeting(console, tmp_path)
    client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )

    started = client.post(
        f"/ui/meetings/{meeting['id']}/runs", data={"backend": "apple"}
    )
    assert started.status_code == 200
    run_id = console.registry.list_runs(meeting["id"])[0].id
    assert "<progress" in started.text
    assert 'hx-trigger="every 1s"' in started.text
    assert f'hx-get="/ui/runs/{run_id}"' in started.text

    # The project detail renders the same live fragment while the run is active.
    detail = client.get("/ui/projects/ops")
    assert f'hx-get="/ui/runs/{run_id}"' in detail.text

    console.gate.set()
    state = console.manager.wait(run_id, timeout=10)
    assert state.status == "done"

    done = client.get(f"/ui/runs/{run_id}")
    assert done.status_code == 200
    assert "done" in done.text
    assert "<progress" in done.text
    assert 'hx-trigger="every 1s"' not in done.text

    assert client.get("/ui/runs/999").status_code == 404


# --- HTML surface: the transcription-profile picker ----------------------- #
def test_profile_picker_lists_the_shared_profiles(console, tmp_path) -> None:
    """The picker's options are `core.options.PROFILES`, with `custom` the default.

    Reading ``PROFILES`` here (not a literal list) is the point: a profile added
    to the shared table shows up in the console with no web change.
    """
    _make_meeting(console, tmp_path)
    detail = console.client.get("/ui/projects/ops")
    assert detail.status_code == 200
    for profile in PROFILES:
        assert f'<option value="{profile}"' in detail.text
    assert f'<option value="{PROFILE_CUSTOM}" selected>' in detail.text
    assert 'hx-get="/ui/profile-options"' in detail.text
    assert "re-transcri" in detail.text


def test_profile_preview_resolves_the_knobs(client, monkeypatch) -> None:
    """The preview shows what the selected profile resolves to, via core."""
    for variable in (
        "CR_CHUNK_SECONDS",
        "CR_OVERLAP_SECONDS",
        "CR_JOBS",
        "CR_BEAM_SIZE",
        "CR_BEST_OF",
        "CR_TEMPERATURE",
        "CR_ENTROPY_THOLD",
        "CR_NO_SPEECH_THOLD",
        "CR_MAX_CONTEXT",
        "CR_THREADS",
    ):
        monkeypatch.delenv(variable, raising=False)

    accurate = client.get("/ui/profile-options", params={"profile": "accurate"})
    assert accurate.status_code == 200
    assert "beam_size" in accurate.text
    assert "beam_size=8" in accurate.text

    fast = client.get("/ui/profile-options", params={"profile": "fast"})
    assert fast.status_code == 200
    assert "best_of=1" in fast.text

    custom = client.get("/ui/profile-options", params={"profile": PROFILE_CUSTOM})
    assert custom.status_code == 200
    assert "no preset" in custom.text
    assert "beam_size" not in custom.text

    assert (
        client.get("/ui/profile-options", params={"profile": "turbo"}).status_code
        == 400
    )


def _start_ui_run(console, meeting_id: int, **data):
    res = console.client.post(f"/ui/meetings/{meeting_id}/runs", data=data)
    assert res.status_code == 200, res.text
    console.gate.set()
    run_id = console.registry.list_runs(meeting_id)[0].id
    console.manager.wait(run_id, timeout=10)
    return console.seen[-1]


def test_ui_run_carries_the_chosen_profile(console, tmp_path) -> None:
    """The resolved profile reaches the run's options (not just the form)."""
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )

    options = _start_ui_run(console, meeting["id"], backend="apple", profile="accurate")

    assert options.profile == "accurate"
    assert options.beam_size == 8  # the preset, applied by resolve_options


def test_ui_run_custom_sets_no_profile_knobs(console, tmp_path) -> None:
    """`custom` is the escape hatch: every knob stays the user's."""
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )

    options = _start_ui_run(console, meeting["id"], backend="apple", profile="custom")

    assert options.profile == "custom"
    assert options.beam_size is None
    assert options.best_of is None


def test_run_api_accepts_a_profile(console, tmp_path) -> None:
    """The JSON surface offers the same choice as the form."""
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )

    started = console.client.post(
        f"/api/meetings/{meeting['id']}/runs",
        json={"backend": "apple", "profile": "fast"},
    )
    assert started.status_code == 202
    console.gate.set()
    console.manager.wait(started.json()["run"]["id"], timeout=10)

    assert console.seen[-1].profile == "fast"
    assert console.seen[-1].best_of == 1


# --- JSON API: archives --------------------------------------------------- #
def _seed_archivable(client, tmp_path, *, default_root: bool = True):
    """A project (optionally with a default archive root) and a meeting with one tape."""
    root = tmp_path / "archive"
    project = {"name": "Ops"}
    if default_root:
        project["default_archive_root"] = str(root)
    client.post("/api/projects", json=project)
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake-audio")
    meeting = client.post(
        "/api/projects/ops/meetings",
        json={"title": "Kickoff", "workspace_path": str(tmp_path)},
    ).json()
    client.put(f"/api/meetings/{meeting['id']}/tapes", json={"paths": [str(tape)]})
    return root, tape, meeting


def test_archive_api_lifecycle(client, tmp_path) -> None:
    root, _tape, meeting = _seed_archivable(client, tmp_path)

    created = client.post(f"/api/meetings/{meeting['id']}/archives", json={})
    assert created.status_code == 201
    archive = created.json()
    assert archive["meeting_id"] == meeting["id"]
    assert archive["project_id"] == meeting["project_id"]
    assert Path(archive["root_path"]).parent == root.resolve() / "ops"
    assert Path(archive["manifest_path"]).is_file()
    assert archive["manifest_sha256"]

    project_archives = client.get("/api/projects/ops/archives").json()
    assert [a["id"] for a in project_archives] == [archive["id"]]
    meeting_archives = client.get(f"/api/meetings/{meeting['id']}/archives").json()
    assert [a["id"] for a in meeting_archives] == [archive["id"]]

    verified = client.post(f"/api/archives/{archive['id']}/verify")
    assert verified.status_code == 200
    assert verified.json()["ok"] is True
    assert verified.json()["checked"] == 1
    assert verified.json()["mismatched"] == []

    # A same-length tamper of a copied tape is caught by the digest alone.
    copy = Path(archive["root_path"]) / "tapes" / "a.wav"
    copy.write_bytes(bytes(copy.stat().st_size))
    tampered = client.post(f"/api/archives/{archive['id']}/verify").json()
    assert tampered["ok"] is False
    assert tampered["mismatched"] == ["tapes/a.wav"]


def test_archive_api_accepts_an_explicit_root(client, tmp_path) -> None:
    _seed_archivable(client, tmp_path, default_root=False)
    meeting_id = client.get("/api/projects/ops/meetings").json()[0]["id"]
    override = tmp_path / "somewhere-else"

    refused = client.post(f"/api/meetings/{meeting_id}/archives", json={})
    assert refused.status_code == 400
    assert "no archive root" in refused.json()["detail"]

    created = client.post(
        f"/api/meetings/{meeting_id}/archives", json={"root": str(override)}
    )
    assert created.status_code == 201
    assert Path(created.json()["root_path"]).parent == override.resolve() / "ops"


def test_archive_api_unknown_ids_are_404(client, tmp_path) -> None:
    _seed_archivable(client, tmp_path)
    assert client.post("/api/meetings/999/archives", json={}).status_code == 404
    assert client.get("/api/meetings/999/archives").status_code == 404
    assert client.get("/api/projects/nope/archives").status_code == 404
    assert client.post("/api/archives/999/verify").status_code == 404


def test_archive_api_verify_without_a_manifest_is_404(client, tmp_path) -> None:
    _root, _tape, meeting = _seed_archivable(client, tmp_path)
    archive = client.post(f"/api/meetings/{meeting['id']}/archives", json={}).json()
    (Path(archive["root_path"]) / "archive.json").unlink()
    assert client.post(f"/api/archives/{archive['id']}/verify").status_code == 404


# --- HTML surface: archiving and verification ----------------------------- #
def test_ui_archive_a_meeting_and_list_it(client, tmp_path) -> None:
    _root, _tape, meeting = _seed_archivable(client, tmp_path)
    assert f'hx-post="/ui/meetings/{meeting["id"]}/archives"' in (
        client.get("/ui/projects/ops").text
    )

    archived = client.post(f"/ui/meetings/{meeting['id']}/archives", data={"root": ""})
    assert archived.status_code == 200
    assert "Kickoff" in archived.text
    assert "Archives" in archived.text
    archive = client.get("/api/projects/ops/archives").json()[0]
    assert archive["root_path"] in archived.text
    # The status is fetched lazily by the row, never hashed during the render.
    assert f'hx-get="/ui/archives/{archive["id"]}/verify"' in archived.text
    assert 'hx-trigger="load"' in archived.text
    assert '<span class="badge">ok</span>' not in archived.text

    # The status endpoint produces the verification fragment on demand.
    status = client.get(f"/ui/archives/{archive['id']}/verify")
    assert status.status_code == 200
    assert '<span class="badge">ok</span>' in status.text
    assert "file verified" in status.text

    # The project view renders the same archive (with the lazy wiring).
    detail = client.get("/ui/projects/ops")
    assert archive["root_path"] in detail.text
    assert f'hx-get="/ui/archives/{archive["id"]}/verify"' in detail.text


def test_project_detail_does_not_hash_archives(client, tmp_path, monkeypatch) -> None:
    _root, _tape, meeting = _seed_archivable(client, tmp_path)
    client.post(f"/ui/meetings/{meeting['id']}/archives", data={"root": ""})
    archive = client.get("/api/projects/ops/archives").json()[0]

    def boom(_path):
        raise AssertionError("the detail render must not verify archives")

    monkeypatch.setattr("clear_record.web.app.verify_archive", boom)
    detail = client.get("/ui/projects/ops")
    assert detail.status_code == 200
    assert archive["root_path"] in detail.text
    assert f'hx-get="/ui/archives/{archive["id"]}/verify"' in detail.text


def test_ui_verify_reports_a_tampered_archive_inline(client, tmp_path) -> None:
    _root, _tape, meeting = _seed_archivable(client, tmp_path)
    client.post(f"/ui/meetings/{meeting['id']}/archives", data={"root": ""})
    archive = client.get("/api/projects/ops/archives").json()[0]
    copy = Path(archive["root_path"]) / "tapes" / "a.wav"
    copy.write_bytes(bytes(copy.stat().st_size))

    verified = client.post(f"/ui/archives/{archive['id']}/verify")
    assert verified.status_code == 200
    assert '<span class="badge">failed</span>' in verified.text
    assert "1 mismatched" in verified.text
    assert "tapes/a.wav" in verified.text

    # The lazy GET resolves the same fragment the button would swap in.
    lazy = client.get(f"/ui/archives/{archive['id']}/verify")
    assert '<span class="badge">failed</span>' in lazy.text

    assert client.post("/ui/archives/999/verify").status_code == 404
    assert client.get("/ui/archives/999/verify").status_code == 404


def test_ui_verify_reports_a_missing_manifest(client, tmp_path) -> None:
    _root, _tape, meeting = _seed_archivable(client, tmp_path)
    client.post(f"/ui/meetings/{meeting['id']}/archives", data={"root": ""})
    archive = client.get("/api/projects/ops/archives").json()[0]
    (Path(archive["root_path"]) / "archive.json").unlink()

    status = client.get(f"/ui/archives/{archive['id']}/verify")
    assert status.status_code == 200
    assert '<span class="badge">missing</span>' in status.text
    assert "no manifest" in status.text


def test_ui_archive_without_a_root_explains_itself(client, tmp_path) -> None:
    _seed_archivable(client, tmp_path, default_root=False)
    meeting_id = client.get("/api/projects/ops/meetings").json()[0]["id"]

    refused = client.post(f"/ui/meetings/{meeting_id}/archives", data={"root": ""})
    assert refused.status_code == 200
    assert "no archive root" in refused.text
    assert "No archives yet." in refused.text
