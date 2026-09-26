"""HTTP behaviour of the bundled console, through the service seam.

Two surfaces: the JSON API (``/api/*``) for machines and the server-rendered
htmx/Alpine fragments (``/ui/*``) for the browser. A temp registry and FastAPI's
test client — no network, no browser.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clear_record.core import (
    DECODER_KNOBS,
    PROFILE_CUSTOM,
    PROFILES,
    RUN_KNOBS,
    JobEvent,
    Progress,
)
from clear_record.service import AutoProbe, Registry, RunManager
from clear_record.web.app import RunCreate, WorkspaceRunCreate, create_app

#: The knobs a person sets in the console's run form: the declaration's rows that
#: are **not** decoder knobs. A preset trades the decoder — the picker offers every
#: one in ``core.PROFILES``, and the panel under the form says what it resolves —
#: so the form offers what a preset does not decide. Stated here rather than read
#: from the view module, so the test says what the rule is instead of what the code
#: happens to export.
CONSOLE_KNOBS = tuple(knob for knob in RUN_KNOBS if not knob.decoder)


def _auto_probe() -> AutoProbe:
    """A fixed machine/tape so the console's ``--auto`` tests never touch hardware."""
    return AutoProbe(
        available_backends=("apple",),
        vram_gb=8.0,
        cpu_count=16,
        models_on_disk=frozenset({"small", "medium"}),
        duration_s=600.0,
        channels=1,
        language=None,
    )


#: The address the console's own client dials — the loopback a node binds
#: (``core.node``). The suite's default ``TestClient`` speaks as ``testserver``
#: (see ``conftest.py``), which the request guard accepts through
#: ``CR_TRUSTED_HOSTS`` — but that is a *proxied* client, and a client behind a
#: published name may not name a **path** (ADR-0032): ``workspace_path``, a tape
#: set, an archive root. These fixtures are the API's ordinary caller *on the
#: node's machine*, so they address it the way every surface here does; the rule
#: and its refusal have their own module (``test_web_naming.py``).
LOCAL_ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture()
def client(tmp_path) -> TestClient:
    app = create_app(Registry.open(db_path=tmp_path / "registry.sqlite3"))
    return TestClient(app, base_url=LOCAL_ORIGIN)


@pytest.fixture()
def console(tmp_path) -> SimpleNamespace:
    """A test client over a registry with an injected, gated fake pipeline.

    The fake pipeline parks inside the run until the test releases ``gate``, and
    sets ``parked`` when it gets there: a test synchronises on the run's own
    lifecycle, never on a clock, before it asks anything of the row. The teardown
    releases the gate and joins the manager, so a test that never gets there
    cannot leave a pipeline parked.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    gate = threading.Event()
    parked = threading.Event()
    seen: list = []

    def fake_pipeline(directory, options, on_event) -> None:
        seen.append(options)
        progress = Progress("transcribe", 2, on_event)
        progress.start()
        on_event(
            JobEvent(
                stage="transcribe",
                index=1,
                total=2,
                source="a",
                message="[transcribe]   a chunk 1/2 -> 1 segment(s)",
            )
        )
        progress.advance(source="a")
        parked.set()
        gate.wait()
        on_event(
            JobEvent(
                stage="transcribe",
                index=2,
                total=2,
                source="b",
                message="[transcribe]   b chunk 2/2 -> 1 segment(s)",
            )
        )
        progress.advance(source="b")
        export = Path(directory) / "export"
        export.mkdir(parents=True, exist_ok=True)
        (export / "record.md").write_text("# record\n", encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    try:
        yield SimpleNamespace(
            client=TestClient(
                create_app(registry, runs=manager), base_url=LOCAL_ORIGIN
            ),
            registry=registry,
            manager=manager,
            gate=gate,
            parked=parked,
            seen=seen,
        )
    finally:
        gate.set()
        manager.shutdown(timeout=10)


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
def test_index_is_served_with_the_compiled_bundle(client) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert "/static/app.js" in res.text
    assert "/static/app.css" in res.text
    assert "project console" in res.text


def test_compiled_assets_are_served(client) -> None:
    for path in ("/static/app.js", "/static/app.css"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert res.content


def test_the_bundle_carries_htmx_and_alpine_offline(client) -> None:
    """The compiled JS is self-contained: htmx and Alpine are bundled in.

    No CDN and no browser-side fetch — the console's offline guarantee
    (ADR-0023) depends on both libraries riding in the wheel, not a <script>
    pointing at someone else's server.
    """
    bundle = client.get("/static/app.js").content
    assert b"htmx" in bundle
    assert b"Alpine" in bundle


def test_ui_project_list_and_create(client) -> None:
    assert "No projects yet." in client.get("/ui/projects").text

    created = client.post("/ui/projects", data={"name": "Weekly Ops"})
    assert created.status_code == 200
    assert "Weekly Ops" in created.text
    assert "Weekly Ops" in client.get("/ui/projects").text


def test_ui_detail_and_glossary_roundtrip(client) -> None:
    _make_project(client)

    detail = client.get("/ui/projects/weekly-ops/glossary")
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
    # The console's delete control retires rather than removes.
    assert f'hx-delete="/ui/glossary/{term_id}"' in promoted.text

    retired = client.delete(f"/ui/glossary/{term_id}")
    assert retired.status_code == 200
    # The row survives as retired — present, not absent — so the console can
    # bring it back; a retire is a status change (ADR-0033).
    assert "Falcon" in retired.text and "retired" in retired.text
    assert "No glossary terms yet." not in retired.text
    assert "Restore" in retired.text

    # Restore puts the term back where the retire took it from — it was
    # confirmed, so it returns confirmed — rather than confirming it outright.
    restored = client.post(f"/ui/glossary/{term_id}/restore")
    assert restored.status_code == 200
    assert "confirmed" in restored.text


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

    retired = client.delete(f"/api/glossary/{term_id}")
    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"
    # The row survives with who added it and when, so the project's term count
    # does not drop and the machine API can restore it with a PATCH.
    survivors = client.get("/api/projects/weekly-ops/glossary").json()
    assert [t["term"] for t in survivors] == ["Falcon"]
    assert survivors[0]["added_by"] == "human" and survivors[0]["created_at"]
    assert client.get("/api/projects").json()[0]["term_count"] == 1
    restored = client.post(f"/api/glossary/{term_id}/restore")
    assert restored.status_code == 200
    assert restored.json()["status"] == "confirmed"


def test_a_retired_candidate_restores_as_a_candidate(client) -> None:
    """Restore is not a promotion: an un-reviewed draft comes back un-reviewed."""
    client.post("/api/projects", json={"name": "Ops"})
    draft = client.post(
        "/api/projects/ops/glossary", json={"term": "AgentTerm", "added_by": "agent"}
    ).json()
    assert draft["status"] == "candidate"

    assert client.delete(f"/api/glossary/{draft['id']}").json()["status"] == "retired"
    restored = client.post(f"/api/glossary/{draft['id']}/restore")

    assert restored.status_code == 200
    assert restored.json()["status"] == "candidate"


def test_restoring_a_term_that_is_not_retired_is_refused(client) -> None:
    """Restore returns the status a retire took the term from, so a term that is
    not retired has nothing to return to: the API answers 400 with the service's
    own message, never a 500 and never a silent status invention."""
    client.post("/api/projects", json={"name": "Ops"})
    term = client.post(
        "/api/projects/ops/glossary", json={"term": "Falcon", "status": "confirmed"}
    ).json()

    refused = client.post(f"/api/glossary/{term['id']}/restore")

    assert refused.status_code == 400
    assert "not retired" in refused.json()["detail"]
    assert client.get("/api/projects/ops/glossary").json()[0]["status"] == "confirmed"


def test_the_console_renders_a_refused_restore(client) -> None:
    """The console's own Restore reads the service's refusal, not a 500.

    htmx does not swap a 4xx, so a refusal the user can act on re-renders the
    tab with the service's message — and the term keeps the status it held.
    """
    client.post("/api/projects", json={"name": "Ops"})
    term = client.post(
        "/api/projects/ops/glossary", json={"term": "Falcon", "status": "confirmed"}
    ).json()

    refused = client.post(f"/ui/glossary/{term['id']}/restore")

    assert refused.status_code == 200
    assert "not retired" in refused.text
    assert client.get("/api/projects/ops/glossary").json()[0]["status"] == "confirmed"


def test_glossary_status_filter(client) -> None:
    client.post("/api/projects", json={"name": "Ops"})
    client.post("/api/projects/ops/glossary", json={"term": "A", "status": "confirmed"})
    client.post("/api/projects/ops/glossary", json={"term": "B"})
    confirmed = client.get("/api/projects/ops/glossary?status=confirmed").json()
    assert [t["term"] for t in confirmed] == ["A"]


def test_glossary_status_filter_rejects_an_unknown_status(client) -> None:
    """A bad client filter is a 400, not an uncaught ValueError turned 500."""
    client.post("/api/projects", json={"name": "Ops"})
    res = client.get("/api/projects/ops/glossary?status=bogus")
    assert res.status_code == 400
    assert "bogus" in res.json()["detail"]


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
    assert client.post("/api/glossary/999/restore").status_code == 404


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
    # The JSON API is not the console, and the run says so.
    assert started.json()["run"]["origin"] == "api"

    console.gate.set()
    state = console.manager.wait(run_id, timeout=10)
    assert state.status == "done"

    fetched = client.get(f"/api/runs/{run_id}")
    assert fetched.status_code == 200
    assert fetched.json()["run"]["status"] == "done"
    assert fetched.json()["state"]["status"] == "done"
    assert fetched.json()["state"]["stage"] == "transcribe"

    events = client.get(f"/api/runs/{run_id}/events?after=0").json()
    assert events["next"] == 5
    # The stream carries both halves of a stage's report: the counters a bar
    # reads, and the words the stage reported — the line's text in ``message``,
    # and none on a report that carries counters only.
    assert [event["index"] for event in events["events"]] == [0, 1, 1, 2, 2]
    assert [event["message"] for event in events["events"]] == [
        "",
        "[transcribe]   a chunk 1/2 -> 1 segment(s)",
        "",
        "[transcribe]   b chunk 2/2 -> 1 segment(s)",
        "",
    ]

    tail = client.get(f"/api/runs/{run_id}/events?after={events['next']}").json()
    assert tail == {"events": [], "next": 5}


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
    console.registry.create_project(
        "Ops",
        actor="console",
    )
    no_workspace = console.registry.create_meeting(
        "ops",
        "No workspace",
        actor="console",
    )
    console.registry.set_recording_set(
        no_workspace.id,
        [str(tmp_path / "x.wav")],
        actor="console",
    )
    res = client.post(f"/api/meetings/{no_workspace.id}/runs", json={})
    assert res.status_code == 400


def test_a_refusal_that_raced_the_pre_check_is_409(
    console, tmp_path, monkeypatch
) -> None:
    """The same condition answers the same code, when the service refuses it.

    The route reads the meeting's live run and answers 409 before it writes
    anything. A submission that races another client reads nothing there — its
    read happened first — and is refused later, by the service: the guard's own
    read, or the database's index when both reads raced. That is the same
    condition for the same user, so it answers 409 too, and the handler's re-read
    of the live state is what separates it from the 400s above.

    The race is played out in one thread: the pre-check's read misses the live run
    (it is the read that came first), the handler's is the one that sees it.
    """
    client = console.client
    meeting = _make_meeting(console, tmp_path)
    client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )
    first = client.post(f"/api/meetings/{meeting['id']}/runs", json={})
    assert first.status_code == 202

    live = console.manager.active_state
    reads: list[int] = []

    def raced(meeting_id: int):
        reads.append(meeting_id)
        return None if len(reads) == 1 else live(meeting_id)

    def refuse(*args, **kwargs):
        raise ValueError("a run is already in flight for this meeting")

    monkeypatch.setattr(console.manager, "active_state", raced)
    monkeypatch.setattr(console.manager, "start", refuse)
    conflict = client.post(f"/api/meetings/{meeting['id']}/runs", json={})

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "a run is already in flight for this meeting"

    console.gate.set()
    console.manager.wait(first.json()["run"]["id"], timeout=10)


def test_unknown_run_endpoints_are_404(console) -> None:
    client = console.client
    assert client.get("/api/runs/999").status_code == 404
    assert client.get("/api/runs/999/events").status_code == 404


# --- the knobs a client may set (one field per declaration row) ------------ #
def test_every_knob_the_command_line_takes_has_a_run_api_field() -> None:
    """The run body declares a field for every knob of ``core.RUN_KNOBS``, and no other.

    The command line derives its flags from that declaration, so this is what
    makes "what a client may set" and "what the command line may set" one set: a
    knob a person can pass to ``run`` is a field a client can send, and a row
    added to the table fails here until the body carries it. The glossary and the
    re-run scope are knobs too — the two the table does not describe with a row —
    and the fields that are *not* knobs are the ones that say how a client names
    and frames what it runs. The workspace edge adds its own subject (a
    ``directory``) and no second set of knobs.
    """
    knobs = {knob.name for knob in RUN_KNOBS} | {
        "glossary",
        "rerun_sources",
        "rerun_range",
    }
    framing = {
        "backend",
        "model",
        "language",
        "split",
        "resume",
        "profile",
        "auto",
        "origin",
    }
    assert set(RunCreate.model_fields) == knobs | framing
    assert set(WorkspaceRunCreate.model_fields) == knobs | framing | {"directory"}


def test_every_knob_a_client_sets_survives_into_the_run_record(
    console, tmp_path
) -> None:
    """One request sets every knob, and the run's own row carries each one back.

    ``run_options`` is what the console, a resumed run and every later reader
    read, so "the run record keeps what was set" is this comparison. The values
    differ from every built-in default and from every profile's, so only the
    request can account for them, and the pipeline the run executed with is
    checked beside the row — a knob that reached the row but not the decoder
    would be a record of something that did not happen.
    """
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )
    sent = {
        "chunk_seconds": 30.0,
        "overlap_seconds": 1.5,
        "jobs": 2,
        "beam_size": 4,
        "best_of": 2,
        "temperature": 0.2,
        "entropy_thold": 2.4,
        "no_speech_thold": 0.6,
        "max_context": 0,
        "threads": 3,
        "rerun_sources": ["a"],
        "rerun_range": "0:00-0:05",
    }
    started = console.client.post(f"/api/meetings/{meeting['id']}/runs", json=sent)
    assert started.status_code == 202, started.text

    console.gate.set()
    run_id = started.json()["run"]["id"]
    assert console.manager.wait(run_id, timeout=10).status == "done"

    # The row is JSON, and the seam reads it back as the options value declares:
    # a list the client sent for a tuple-typed knob comes back as the tuple.
    recorded = console.registry.get_run(run_id).run_options
    assert {name: recorded[name] for name in sent} == {
        **sent,
        "rerun_sources": ("a",),
    }

    executed = console.seen[-1]
    assert executed.chunk_seconds == 30.0
    assert executed.beam_size == 4
    assert executed.rerun_sources == ("a",)


def test_an_unknown_knob_is_refused_rather_than_ignored(console, tmp_path) -> None:
    """A body field nobody declares is refused, and nothing is written.

    A misspelled knob used to be dropped on the way in: the run started, and the
    setting the client sent was simply not in it — a run that is not the one that
    was asked for. Both run edges refuse such a body instead, before the resolver,
    the registry or the workspace resolution sees anything (the workspace edge
    must not register a meeting for a request it refuses).
    """
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )

    misspelled = console.client.post(
        f"/api/meetings/{meeting['id']}/runs", json={"chunk_second": 30}
    )
    assert misspelled.status_code == 422, misspelled.text
    assert "chunk_second" in misspelled.text
    assert console.registry.list_runs(meeting["id"]) == []

    # `--diarize` is a flag the command line has and the run request does not: a
    # client sending it is told, not handed a run without it.
    undeclared = console.client.post(
        "/api/runs", json={"directory": str(tmp_path), "diarize": True}
    )
    assert undeclared.status_code == 422, undeclared.text
    assert "diarize" in undeclared.text
    assert len(console.registry.list_meetings()) == 1, "a refused body wrote a meeting"


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


def test_an_empty_tape_set_re_renders_at_200(console, tmp_path) -> None:
    """A bad tape set is a form mistake, not an htmx-invisible 4xx.

    htmx does not swap a 4xx (base.html sets noSwap), so the refusal has to come
    back at 200 carrying the service's message for the user to see it.
    """
    meeting = _make_meeting(console, tmp_path)

    refused = console.client.post(
        f"/ui/meetings/{meeting['id']}/tapes", data={"paths": ""}
    )

    assert refused.status_code == 200
    assert "a recording set needs at least one tape" in refused.text


def test_a_blank_project_name_re_renders_at_200(client) -> None:
    """A blank name is a form mistake, not an htmx-invisible 409.

    A duplicate *name* is deduplicated by slug, so the reachable ValueError on
    this route is a whitespace-only name (the form's required flag lets it
    through). Either way the refusal must come back at 200 (base.html's noSwap).
    """
    refused = client.post("/ui/projects", data={"name": "   "})

    assert refused.status_code == 200
    assert 'class="run-error project-error"' in refused.text
    assert "must not be blank" in refused.text


def test_the_other_ui_refusals_re_render_at_200(client) -> None:
    """The remaining UI mutations obey the same noSwap rule."""

    # A blank term, an invalid status and a blank title are all fixable form
    # mistakes, so each re-renders its #detail fragment at 200 with the
    # service's message (htmx does not swap a 4xx; base.html sets noSwap).
    _make_project(client)

    blank_term = client.post("/ui/projects/weekly-ops/glossary", data={"term": "   "})
    assert blank_term.status_code == 200
    assert 'class="run-error"' in blank_term.text
    assert "term must not be blank" in blank_term.text

    added = client.post("/ui/projects/weekly-ops/glossary", data={"term": "Falcon"})
    assert added.status_code == 200
    term_id = client.get("/api/projects/weekly-ops/glossary").json()[0]["id"]

    bad_status = client.post(f"/ui/glossary/{term_id}/status", data={"status": "nope"})
    assert bad_status.status_code == 200
    assert 'class="run-error"' in bad_status.text
    assert "status must be one of" in bad_status.text

    blank_title = client.post("/ui/projects/weekly-ops/meetings", data={"title": "   "})
    assert blank_title.status_code == 200
    assert 'class="run-error"' in blank_title.text
    assert "meeting title must not be blank" in blank_title.text


def test_the_add_project_form_clears_only_on_a_real_success(client) -> None:
    """The add-project form sits outside the #projects swap target."""

    # The swap does not replace it, so it is cleared on a success signal rather
    # than on any 2xx: the refusal re-render carries no HX-Trigger.
    created = client.post("/ui/projects", data={"name": "Weekly Ops"})
    assert created.status_code == 200
    assert created.headers.get("HX-Trigger") == "project-created"

    refused = client.post("/ui/projects", data={"name": "   "})
    assert refused.status_code == 200
    assert refused.headers.get("HX-Trigger") is None
    assert "must not be blank" in refused.text


def test_run_form_offers_only_this_machines_backends(
    console, tmp_path, monkeypatch
) -> None:
    """The picker is derived from availability, not a hardcoded catalog.

    The machine that can run ``apple-speech`` must offer it; a backend this
    machine lacks (``nvidia``/``amd`` here) must not appear. ``auto`` stays.
    """
    monkeypatch.setattr(
        "clear_record.web.app.available_backend_ids", lambda: ("apple-speech",)
    )
    _make_meeting(console, tmp_path)

    detail = console.client.get("/ui/projects/ops/meetings")

    assert detail.status_code == 200
    assert '<option value="apple-speech"' in detail.text
    assert '<option value="nvidia"' not in detail.text
    assert '<option value="amd"' not in detail.text
    assert '<option value="auto"' in detail.text


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
    # The console is the surface that started it, and it is recorded.
    assert console.registry.get_run(run_id).origin == "console"
    assert "<progress" in started.text
    assert 'hx-trigger="every 1s"' in started.text
    assert f'hx-get="/ui/runs/{run_id}"' in started.text

    # The Meetings tab renders the same live fragment while the run is active.
    detail = client.get("/ui/projects/ops/meetings")
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


def test_the_run_fragment_offers_cancel_and_resume(console, tmp_path) -> None:
    """Cancel a live run, and resume one that stopped.

    The controls come from the run's row, so a run another writer started offers
    them too. Cancelling a running run is a *request* — the console's own manager
    honours it at its next report — and the fragment that comes back shows what
    the run is now: resumable, with the cache rule stated where the button is.
    """
    client = console.client
    meeting = _make_meeting(console, tmp_path)
    client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )
    started = client.post(
        f"/ui/meetings/{meeting['id']}/runs", data={"backend": "apple"}
    )
    run_id = console.registry.list_runs(meeting["id"])[0].id
    assert f'hx-post="/ui/runs/{run_id}/cancel"' in started.text

    # Wait for the run's own lifecycle before cancelling: a cancel that arrives
    # while the run is still queued is RUN-04's *other* arm (a decisive stop), not
    # the request this test asserts. The fixture parks the pipeline until the test
    # releases it, so ``parked`` is the run, not a clock.
    assert console.parked.wait(10), "the run never reached its pipeline"

    # The run is parked in its pipeline (the fixture's gate): the cancel is
    # recorded, and the fragment says so rather than pretending it stopped.
    requested = client.post(f"/ui/runs/{run_id}/cancel")
    assert requested.status_code == 200
    assert console.registry.get_run(run_id).status == "running"
    assert console.registry.get_run(run_id).cancel_requested_at is not None
    assert "asking the run to stop" in requested.text

    # Let the pipeline report: the next report stops it, terminally.
    console.gate.set()
    assert console.manager.wait(run_id, timeout=10).status == "stopped"
    stopped = client.get(f"/ui/runs/{run_id}")
    assert f'hx-post="/ui/runs/{run_id}/resume"' in stopped.text
    assert "keyed by this workspace" in stopped.text  # the cache rule, stated
    assert console.registry.meeting_by_id(meeting["id"]).status == "ready"

    # Resuming starts a new run, linked to the one it continues.
    resumed = client.post(f"/ui/runs/{run_id}/resume")
    assert resumed.status_code == 200
    new_id = console.registry.list_runs(meeting["id"])[0].id
    assert new_id != run_id
    assert console.registry.get_run(new_id).resumes_run_id == run_id
    assert f"resumed from run {run_id}" in resumed.text

    assert client.post("/ui/runs/999/cancel").status_code == 404
    assert client.post("/ui/runs/999/resume").status_code == 404


def test_a_run_without_a_cost_record_renders_unknown(console, tmp_path) -> None:
    """A run recorded before the cost record existed renders as unknown.

    The row has no ``progress`` at all. The fragment must still render, with no
    ETA and no exception — a missing record is not an error.
    """
    registry = console.registry
    meeting = _make_meeting(console, tmp_path)
    run = registry.create_run(
        meeting["id"],
        backend="apple",
        model="small",
        actor="console",
    )
    registry.update_run(
        run.id,
        status="done",
        ended_at="2026-01-01T00:01:00+00:00",
        actor="console",
    )

    fragment = console.client.get(f"/ui/runs/{run.id}")
    assert fragment.status_code == 200
    assert f'id="run-{meeting["id"]}"' in fragment.text
    assert "ETA" not in fragment.text


# --- HTML surface: the transcription-profile picker ----------------------- #
def test_profile_picker_lists_the_shared_profiles(console, tmp_path) -> None:
    """The picker's options are `core.options.PROFILES`, with `custom` the default.

    Reading ``PROFILES`` here (not a literal list) is the point: a profile added
    to the shared table shows up in the console with no web change.
    """
    _make_meeting(console, tmp_path)
    detail = console.client.get("/ui/projects/ops/meetings")
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


def test_the_console_run_form_exposes_the_user_facing_knobs_and_no_more(
    console, tmp_path
) -> None:
    """The form offers the declaration's non-decoder rows, and no other knob.

    What a person sets in the browser is what a **profile** does not decide — how
    the tape is chunked and how many workers decode it; the decoder block is the
    preset picker's (the panel under the form shows which knobs the chosen preset
    resolves). The glossary and the re-run scope are the machine surface's: a
    browser names no path on the node, and re-decoding a range is a transcript
    move the console already serves with *resume*. Asserting both directions is
    what keeps a knob from being offered and then dropped, or settable and
    invisible.
    """
    _make_meeting(console, tmp_path)
    form = console.client.get("/ui/projects/ops/meetings").text

    for knob in CONSOLE_KNOBS:
        assert f'name="{knob.name}"' in form, knob.name
    for knob in DECODER_KNOBS:
        assert f'name="{knob.name}"' not in form, knob.name
    for name in ("glossary", "rerun_sources", "rerun_range"):
        assert f'name="{name}"' not in form, name


@pytest.mark.parametrize("knob", CONSOLE_KNOBS, ids=lambda knob: knob.name)
def test_a_console_run_sets_each_knob_the_form_offers(console, tmp_path, knob) -> None:
    """Every knob the form renders is one the route reads, and it reaches the run.

    Parametrized over the declaration, so a row the form shows but a submission
    drops — or one the route reads and the form never shows — fails here, named.
    ``7`` is a value no built-in default and no preset produces, so only the
    submission can account for it.
    """
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )

    options = _start_ui_run(console, meeting["id"], backend="apple", **{knob.name: "7"})

    assert getattr(options, knob.name) == knob.convert("7")
    row = console.registry.list_runs(meeting["id"])[0].run_options
    assert row[knob.name] == knob.convert("7")


def test_the_console_reads_no_knob_its_form_does_not_offer(console, tmp_path) -> None:
    """The submission half of the form's subset: a knob it does not show is not read.

    A decoder knob reaches the console through the profile picker, and the
    glossary is a path on the node — so a submission carrying either is not a
    setting the console can make, and the run is started without it. Only a
    submission can prove this: the form's own markup cannot say what the route
    would accept.
    """
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )

    options = _start_ui_run(
        console,
        meeting["id"],
        backend="apple",
        beam_size="7",
        glossary=str(tmp_path / "glossary.txt"),
    )

    assert options.beam_size is None, "a decoder knob the form does not offer"
    assert options.glossary is None, "a path the console cannot name"


def test_a_console_knob_that_is_not_a_number_re_renders(console, tmp_path) -> None:
    """A mistyped knob is a form mistake: it re-renders, in the console's own words.

    htmx swaps no 4xx, so a refusal a person can fix has to come back as the run
    fragment with the message — the rule the tape and archive forms already
    follow — and the run must not be queued behind it. The message names the box
    the person filled (**chunk seconds**, the console's own word for that knob),
    not the spelling the command line gives the knob.
    """
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )

    answered = console.client.post(
        f"/ui/meetings/{meeting['id']}/runs",
        data={"backend": "apple", "chunk_seconds": "half"},
    )

    assert answered.status_code == 200, answered.text
    assert "chunk seconds needs a number" in answered.text, answered.text
    assert "--chunk-seconds" not in answered.text, answered.text
    assert console.registry.list_runs(meeting["id"]) == []


def test_a_slow_run_resolution_does_not_stall_the_node(
    console, tmp_path, monkeypatch
) -> None:
    """The console resolves a run off the event loop, so other clients keep answering.

    Resolving a run is real work — an ``auto`` run probes the machine and the tape
    before anything is written — and the console's route is the one edge that has
    to read a form before it can do it. If that work ran on the event loop, every
    other client of this node would wait for it: the probe here is slowed to a
    visible pause, a concurrent request is made while it runs, and that request
    must come back long before the probe does. Entering the client is what puts
    both requests on one loop (``with console.client``), so what is measured is
    the loop's own wait, not two loops' independence.
    """
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )
    pause = 0.75
    probing = threading.Event()

    def slow_probe(*args, **kwargs):
        probing.set()
        time.sleep(pause)
        return _auto_probe()

    monkeypatch.setattr("clear_record.service.auto.probe_auto", slow_probe)

    with console.client:
        answered: list = []
        submitted = threading.Thread(
            target=lambda: answered.append(
                console.client.post(
                    f"/ui/meetings/{meeting['id']}/runs",
                    data={"backend": "apple", "auto": "1"},
                )
            )
        )
        submitted.start()
        assert probing.wait(10), "the run was never resolved"
        began = time.monotonic()
        health = console.client.get("/api/health")
        waited = time.monotonic() - began
        submitted.join(30)
        assert not submitted.is_alive(), "the submission never returned"
        console.gate.set()

    assert [response.status_code for response in answered] == [200]
    assert health.status_code == 200
    assert waited < pause / 2, (
        f"a concurrent request waited {waited:.2f}s on a {pause}s resolution"
    )


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


def test_run_form_offers_the_opt_in_auto_and_backend_auto(console, tmp_path) -> None:
    """`--auto` is a choice in the form, never the silent default; and the
    backend's own `auto` sentinel is offered beside the concrete ids."""
    _make_meeting(console, tmp_path)
    detail = console.client.get("/ui/projects/ops/meetings").text

    assert 'name="auto"' in detail
    assert 'name="auto" value="1" checked' not in detail  # opt-in, not default
    assert '<option value="auto"' in detail


def test_ui_run_auto_records_and_shows_the_resolver_explanation(
    console, tmp_path, monkeypatch
) -> None:
    """Checking `auto` resolves the run and surfaces the resolver's own explanation."""
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )
    monkeypatch.setattr(
        "clear_record.service.auto.probe_auto", lambda *a, **k: _auto_probe()
    )

    options = _start_ui_run(console, meeting["id"], backend="apple", auto="1")

    assert options.profile == "accurate"  # short tape on a roomy machine
    assert options.model == "medium"
    run = console.registry.list_runs(meeting["id"])[0]
    assert run.options["profile"] == "accurate"
    assert run.options["decoder_knobs"] == {"beam_size": 8}
    assert "--auto: chose" in run.options["auto"]["explanation"]
    # Visible in the run fragment (and again on a reload, from the registry).
    assert "--auto: chose" in console.client.get(f"/ui/runs/{run.id}").text


def test_ui_run_backend_auto_is_orthogonal_to_the_profile(
    console, tmp_path, monkeypatch
) -> None:
    """`--backend auto` picks the backend; the chosen profile is untouched."""
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )
    monkeypatch.setattr(
        "clear_record.service.auto.available_backend_ids", lambda: ("amd",)
    )

    options = _start_ui_run(console, meeting["id"], backend="auto", profile="fast")

    assert options.backend == "amd"
    assert options.profile == "fast"
    assert options.best_of == 1
    run = console.registry.list_runs(meeting["id"])[0]
    assert run.options["backend_auto"]["backend"] == "amd"
    assert "--backend auto: chose 'amd'" in run.options["backend_auto"]["explanation"]
    assert run.options["profile"] == "fast"


def test_run_api_accepts_auto(console, tmp_path, monkeypatch) -> None:
    """The JSON surface offers the same opt-in as the form."""
    meeting = _make_meeting(console, tmp_path)
    console.client.put(
        f"/api/meetings/{meeting['id']}/tapes",
        json={"paths": [str(tmp_path / "a.wav")]},
    )
    monkeypatch.setattr(
        "clear_record.service.auto.probe_auto", lambda *a, **k: _auto_probe()
    )

    started = console.client.post(
        f"/api/meetings/{meeting['id']}/runs",
        json={"backend": "apple", "auto": True},
    )
    assert started.status_code == 202
    console.gate.set()
    console.manager.wait(started.json()["run"]["id"], timeout=10)

    assert console.seen[-1].profile == "accurate"
    assert console.seen[-1].model == "medium"
    assert started.json()["run"]["options"]["profile"] == "accurate"


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
        client.get("/ui/projects/ops/meetings").text
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

    # The Meetings tab renders the same archive (with the lazy wiring).
    detail = client.get("/ui/projects/ops/meetings")
    assert archive["root_path"] in detail.text
    assert f'hx-get="/ui/archives/{archive["id"]}/verify"' in detail.text


def test_project_detail_does_not_hash_archives(client, tmp_path, monkeypatch) -> None:
    _root, _tape, meeting = _seed_archivable(client, tmp_path)
    client.post(f"/ui/meetings/{meeting['id']}/archives", data={"root": ""})
    archive = client.get("/api/projects/ops/archives").json()[0]

    def boom(_path):
        raise AssertionError("the detail render must not verify archives")

    monkeypatch.setattr("clear_record.web.app.verify_archive", boom)
    detail = client.get("/ui/projects/ops/meetings")
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


def test_ui_verify_reports_an_unreadable_manifest_as_unverifiable(
    client, tmp_path
) -> None:
    """A manifest that is there but cannot be read is not a file that is gone.

    The fragment says what the service answered: *unverifiable*, with its
    reason — never the "missing" it used to print for a manifest sitting right
    there.
    """
    _root, _tape, meeting = _seed_archivable(client, tmp_path)
    client.post(f"/ui/meetings/{meeting['id']}/archives", data={"root": ""})
    archive = client.get("/api/projects/ops/archives").json()[0]
    (Path(archive["root_path"]) / "archive.json").write_text(
        "{not json", encoding="utf-8"
    )

    status = client.get(f"/ui/archives/{archive['id']}/verify")
    assert status.status_code == 200
    # Its own chip, carrying the service's reason — never the failure chip,
    # which would name a file as the thing that went wrong.
    assert '<span class="badge">unverifiable</span>' in status.text
    assert '<span class="badge">failed</span>' not in status.text
    assert "archive.json" in status.text
    assert "missing" not in status.text

    api = client.post(f"/api/archives/{archive['id']}/verify").json()
    assert api["ok"] is False
    assert api["missing"] == []
    assert api["unverifiable"]


def test_ui_archive_without_a_root_explains_itself(client, tmp_path) -> None:
    _seed_archivable(client, tmp_path, default_root=False)
    meeting_id = client.get("/api/projects/ops/meetings").json()[0]["id"]

    refused = client.post(f"/ui/meetings/{meeting_id}/archives", data={"root": ""})
    assert refused.status_code == 200
    assert "no archive root" in refused.text
    assert "No archives yet." in refused.text


def test_a_meeting_run_is_never_refused_by_the_workspaces_declaration(
    console, tmp_path
) -> None:
    """The folder's declaration governs discovery, not a meeting's registry tapes.

    A directory run resolves its tapes through the walk, so the folder's
    ``.clear-record-ignore`` governs it (``service.runs.workspace_run_meeting``,
    the directory edge). A **meeting** run — this route, the console's own button,
    an agent's tool — is handed the registry's tape set and never walks, so the
    declaration does not govern it. Answering it with the declaration's refusal
    would be false about a run that does have inputs (measured: 400 with the
    sentence while the same submission ran to ``done`` with the check bypassed),
    and reading the file at all would turn an **unreadable** declaration into a
    500 for a submission that never needed it.

    The run's own stream is checked too: naming one of these tapes "excluded"
    while the pipeline decoded it would be the same misdescription from the other
    side (``service.runs.report_declaration_exclusions``).
    """
    client = console.client
    workspace = tmp_path / "ws"
    tapes = workspace / "tapes"
    tapes.mkdir(parents=True)
    for name in ("a.wav", "b.wav"):
        (tapes / name).write_bytes(b"RIFF" + name.encode())
    meeting = _make_meeting(console, workspace)
    taken = [str(tapes / "a.wav"), str(tapes / "b.wav")]
    assert (
        client.put(
            f"/api/meetings/{meeting['id']}/tapes", json={"paths": taken}
        ).status_code
        == 201
    )
    declaration = workspace / ".clear-record-ignore"
    declaration.write_text("tapes/*.wav\n", encoding="utf-8")

    started = client.post(f"/api/meetings/{meeting['id']}/runs", json={})
    assert started.status_code == 202
    run_id = started.json()["run"]["id"]
    console.gate.set()
    assert console.manager.wait(run_id, timeout=10).status == "done"
    assert list(console.seen[-1].audio_files) == taken, "the registry's tapes ran"
    events = client.get(f"/api/runs/{run_id}/events?after=0").json()
    assert [
        event["message"]
        for event in events["events"]
        if "excluded" in (event["message"] or "")
    ] == []

    # A declaration that cannot be read is nobody's business on this edge either:
    # the submission is answered and the run finishes exactly as it did above.
    declaration.chmod(0o000)
    second = client.post(f"/api/meetings/{meeting['id']}/runs", json={})
    assert second.status_code == 202
    second_id = second.json()["run"]["id"]
    console.gate.set()
    assert console.manager.wait(second_id, timeout=10).status == "done"


@pytest.mark.parametrize("shape", ["permission", "not-utf8"])
def test_an_auto_run_reads_the_declaration_and_says_when_it_cannot(
    console, tmp_path, shape
) -> None:
    """``auto`` measures the folder, so it reads the declaration — and answers it.

    The probe needs the workspace's tapes to choose a profile, so every run that
    asks for ``auto`` walks the folder (this route, the console's button, an
    agent's tool) and the walk reads the folder's own ``.clear-record-ignore``.
    That read is a decision — the probe cannot pick a profile without it — and the
    submission is answered with the declaration's own sentence, the id the
    directory path answers with too, with nothing written. (The only other reader
    on this path is ``service.runs.report_declaration_exclusions``, which
    *stands down* rather than decide: it is a report, and the run's inputs were
    already settled by the registry.) Both ways a file refuses to be read are the
    same answer: a mode nothing may read (measured: 500 with the raw
    ``PermissionError`` before this) and a declaration saved in the machine's own
    encoding (which reached the client as the codec's own text).
    """
    from clear_record.pipeline.workspace import CANNOT_READ_DECLARATION

    client = console.client
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.wav").write_bytes(b"RIFFa")
    meeting = _make_meeting(console, workspace)
    assert (
        client.put(
            f"/api/meetings/{meeting['id']}/tapes",
            json={"paths": [str(workspace / "a.wav")]},
        ).status_code
        == 201
    )
    declaration = workspace / ".clear-record-ignore"
    declaration.write_bytes(b"caf\xe9\n" if shape == "not-utf8" else b"*.wav\n")
    if shape == "permission":
        declaration.chmod(0o000)

    refused = client.post(f"/api/meetings/{meeting['id']}/runs", json={"auto": True})

    assert refused.status_code == 400
    assert refused.json()["detail"] == CANNOT_READ_DECLARATION
    assert console.registry.list_runs(meeting["id"]) == [], (
        "a refused run wrote nothing"
    )
