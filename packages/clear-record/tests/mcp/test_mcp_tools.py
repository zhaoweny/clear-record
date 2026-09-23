"""The MCP tool surface, exercised through a real in-process MCP client.

The SDK ships an in-process transport (``Client(server)``), so these tests drive
the same stdio code path a user's agent would — list the tools, call them, read
structured results and ``is_error`` failures — with no subprocess and no
network. The pipeline callable is injected, so a full run is started and watched
with no ASR backend and no GPU.

The tool logic itself is a thin translation of ``clear_record.service``; the
service's own behaviour is covered by ``tests/service/``. These tests assert the
*adapter*: the surface exists, results are JSON-serializable, and failures come
back actionable rather than as crashes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clear_record.core import (
    PipelineOptions,
    Progress,
    RecordDocument,
    Segment,
    write_json,
)
from clear_record.mcp.server import TOOL_NAMES, ServiceTools, build_server
from clear_record.service import (
    AutoProbe,
    Meeting,
    ModelNotOnDisk,
    Registry,
    RunManager,
    project_snapshot,
    resolve_run,
)
from mcp_client import error_text as _error_text
from mcp_client import names as _names
from mcp_client import payload as _payload


def _registry(tmp_path: Path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def test_tool_surface_is_registered(tmp_path: Path) -> None:
    server = build_server(_registry(tmp_path))
    assert _names(server) == set(TOOL_NAMES)
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES))  # no accidental duplicate


def test_project_and_glossary_roundtrip(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Weekly Ops", notes="sync")
    server = build_server(registry)

    projects = _payload(server, "list_projects")
    assert [p["slug"] for p in projects] == ["weekly-ops"]
    assert projects[0]["term_count"] == 0

    project = _payload(server, "get_project", {"slug": "weekly-ops"})
    assert project["name"] == "Weekly Ops"
    assert project["notes"] == "sync"

    created = _payload(
        server,
        "add_glossary_term",
        {"project": "weekly-ops", "term": "Falcon", "definition": "the project"},
    )
    assert created["term"] == "Falcon"
    assert created["status"] == "candidate"
    assert created["added_by"] == "human"

    agent_term = _payload(
        server,
        "add_glossary_term",
        {"project": "weekly-ops", "term": "Aero", "added_by": "agent"},
    )
    assert agent_term["added_by"] == "agent"

    terms = _payload(server, "list_glossary_terms", {"project": "weekly-ops"})
    assert {t["term"] for t in terms} == {"Falcon", "Aero"}

    confirmed = _payload(
        server,
        "update_glossary_term",
        {"term_id": created["id"], "status": "confirmed"},
    )
    assert confirmed["status"] == "confirmed"

    filtered = _payload(
        server, "list_glossary_terms", {"project": "weekly-ops", "status": "candidate"}
    )
    assert [t["term"] for t in filtered] == ["Aero"]

    # The glossary changed, so the project's term count reflects it.
    assert _payload(server, "get_project", {"slug": "weekly-ops"})["term_count"] == 2


def test_meeting_tape_set_run_and_artifacts_roundtrip(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")

    def fake_pipeline(directory, options, on_event) -> None:
        assert options.audio_files == (str(tape),)
        progress = Progress("transcribe", 2, on_event)
        progress.start()
        progress.advance(source="a")
        progress.advance(source="a")
        export = Path(directory) / "export"
        export.mkdir(parents=True, exist_ok=True)
        (export / "record.md").write_text("# record\n", encoding="utf-8")
        (Path(directory) / "record.json").write_text("{}", encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    server = build_server(registry, manager)

    meeting = _payload(
        server,
        "create_meeting",
        {"project": "ops", "title": "Kickoff", "workspace_path": str(workspace)},
    )
    assert meeting["slug"] == "kickoff"
    assert meeting["status"] == "new"

    tapes = _payload(
        server,
        "set_meeting_tapes",
        {"project": "ops", "meeting": "kickoff", "paths": [str(tape)]},
    )
    assert tapes["paths"] == [str(tape)]

    read = _payload(server, "get_meeting", {"project": "ops", "meeting": "kickoff"})
    assert read["tapes"] == [str(tape)]
    assert [
        m["slug"] for m in _payload(server, "list_meetings", {"project": "ops"})
    ] == ["kickoff"]

    run = _payload(server, "start_run", {"project": "ops", "meeting": "kickoff"})
    # RUN-02: an MCP-started run says so, so the console's status data can show
    # where the work came from.
    assert run["origin"] == "mcp"
    manager.wait(run["id"], timeout=10)

    status = _payload(server, "run_status", {"run_id": run["id"]})
    assert status["status"] == "done"
    assert status["progress"]["stage"] == "transcribe"
    assert status["progress"]["total"] == 2

    events = _payload(server, "run_events", {"run_id": run["id"]})
    assert events["status"] == "done"
    assert len(events["events"]) == 3
    assert events["events"][-1]["done"] is True
    assert events["next"] == 3
    assert (
        _payload(server, "run_events", {"run_id": run["id"], "after": 3})["events"]
        == []
    )

    runs = _payload(server, "list_runs", {"project": "ops", "meeting": "kickoff"})
    assert [r["id"] for r in runs] == [run["id"]]

    artifacts = _payload(
        server, "list_artifacts", {"project": "ops", "meeting": "kickoff"}
    )
    assert {a["kind"] for a in artifacts} == {"record", "export"}
    assert all(a["sha256"] for a in artifacts)


def test_unknown_project_and_meeting_are_actionable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    server = build_server(registry)

    assert "unknown project 'nope'" in _error_text(
        server, "get_project", {"slug": "nope"}
    )
    assert "unknown project 'nope'" in _error_text(
        server, "list_glossary_terms", {"project": "nope"}
    )
    message = _error_text(
        server, "get_meeting", {"project": "ops", "meeting": "missing"}
    )
    assert "unknown meeting 'missing'" in message
    assert "known meetings: (none)" in message


def test_start_run_without_tapes_is_actionable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    server = build_server(registry, RunManager(registry, pipeline=lambda *a: None))

    message = _error_text(server, "start_run", {"project": "ops", "meeting": "kickoff"})
    assert "tape set" in message


def test_backend_failure_is_reported_by_run_status(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    registry.set_recording_set(meeting.id, [str(tape)])

    def no_backend(directory, options, on_event) -> None:
        raise RuntimeError("backend unavailable")

    manager = RunManager(registry, pipeline=no_backend)
    server = build_server(registry, manager)
    run = _payload(server, "start_run", {"project": "ops", "meeting": "kickoff"})
    manager.wait(run["id"], timeout=10)

    status = _payload(server, "run_status", {"run_id": run["id"]})
    assert status["status"] == "failed"
    assert "backend unavailable" in status["progress"]["error"]


def test_unknown_run_ids_are_actionable(tmp_path: Path) -> None:
    server = build_server(_registry(tmp_path))
    assert "unknown run id 99" in _error_text(server, "run_status", {"run_id": 99})
    assert "unknown run id 99" in _error_text(server, "run_events", {"run_id": 99})


def test_update_project_and_meeting_notes(tmp_path: Path) -> None:
    """The story persists: project notes and meeting notes are agent-writable."""
    registry = _registry(tmp_path)
    registry.create_project("Ops", notes="old")
    meeting = registry.create_meeting("ops", "Kickoff")
    assert meeting.notes == ""
    server = build_server(registry)

    project = _payload(
        server, "update_project", {"slug": "ops", "notes": "the project story"}
    )
    assert project["notes"] == "the project story"
    assert _payload(server, "get_project", {"slug": "ops"})["notes"] == (
        "the project story"
    )

    updated = _payload(
        server,
        "update_meeting",
        {"project": "ops", "meeting": "kickoff", "notes": "the meeting story"},
    )
    assert updated["notes"] == "the meeting story"
    assert (
        _payload(server, "get_meeting", {"project": "ops", "meeting": "kickoff"})[
            "notes"
        ]
        == "the meeting story"
    )

    assert "unknown project 'nope'" in _error_text(
        server, "update_project", {"slug": "nope", "notes": "x"}
    )
    assert "unknown meeting 'missing'" in _error_text(
        server,
        "update_meeting",
        {"project": "ops", "meeting": "missing", "notes": "x"},
    )


def test_the_agent_flow_advertises_a_transcript_tool_the_server_exposes() -> None:
    """The hello-world check's MCP leg names a tool that really exists.

    The service layer (below the MCP adapter, so it may not import it) advertises
    the transcript read as a string; this pins that string to the real surface, so
    a rename cannot leave the console promising a tool the server does not expose.
    """
    from clear_record.service.agent_flow import TRANSCRIPT_TOOL

    assert TRANSCRIPT_TOOL in TOOL_NAMES


def test_read_transcript_as_text_with_a_slice(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    write_json(
        workspace / "record.json",
        RecordDocument(
            sources=(),
            alignment=None,
            segments=tuple(
                Segment(
                    start=float(i), end=float(i) + 0.5, text=f"line {i}", source="a"
                )
                for i in range(4)
            ),
        ),
    )
    server = build_server(registry)

    page = _payload(
        server, "read_transcript", {"project": "ops", "meeting": "kickoff", "limit": 2}
    )
    assert page["source"] == "record"
    assert page["total"] == 4
    assert (page["offset"], page["returned"], page["next"]) == (0, 2, 2)
    assert page["text"].splitlines() == [
        "00:00:00.000 [a] line 0",
        "00:00:01.000 [a] line 1",
    ]

    following = _payload(
        server,
        "read_transcript",
        {"project": "ops", "meeting": "kickoff", "offset": page["next"]},
    )
    assert (following["offset"], following["next"]) == (2, None)
    assert following["text"].splitlines() == [
        "00:00:02.000 [a] line 2",
        "00:00:03.000 [a] line 3",
    ]


def test_read_transcript_without_a_run_is_actionable(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    server = build_server(registry)
    assert "no transcript" in _error_text(
        server, "read_transcript", {"project": "ops", "meeting": "kickoff"}
    )


def test_start_run_carries_explicit_options(tmp_path: Path) -> None:
    """A re-run can set intent: profile, backend, model and language."""
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    registry.set_recording_set(meeting.id, [str(tape)])

    seen = []

    def fake_pipeline(directory, options, on_event) -> None:
        seen.append(options)

    manager = RunManager(registry, pipeline=fake_pipeline)
    server = build_server(registry, manager)

    run = _payload(
        server,
        "start_run",
        {
            "project": "ops",
            "meeting": "kickoff",
            "profile": "balanced",
            "backend": "whisper",
            "model": "small",
            "language": "zh",
            "glossary": "/tmp/tuned-glossary.txt",
        },
    )
    manager.wait(run["id"], timeout=10)

    options = seen[0]
    assert options.backend == "whisper"
    assert options.model == "small"
    assert options.language == "zh"
    assert options.profile == "balanced"
    assert options.beam_size == 5  # the profile's preset
    assert options.glossary == "/tmp/tuned-glossary.txt"


def test_start_run_rejects_unknown_profile(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    server = build_server(registry, RunManager(registry, pipeline=lambda *a: None))
    message = _error_text(
        server,
        "start_run",
        {"project": "ops", "meeting": "kickoff", "profile": "turbo"},
    )
    assert "unknown profile 'turbo'" in message


def _probe(**over: object) -> AutoProbe:
    """A deterministic ``--auto`` probe (mirrors ``tests/service/test_auto.py``)."""
    base: dict = dict(
        available_backends=("apple",),
        vram_gb=8.0,
        cpu_count=16,
        models_on_disk=frozenset({"small", "medium"}),
        duration_s=600.0,
        channels=1,
        language=None,
    )
    base.update(over)
    return AutoProbe(**base)


def _runnable_meeting(tmp_path: Path) -> tuple[Registry, Meeting]:
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    registry.set_recording_set(meeting.id, [str(tape)])
    return registry, meeting


def test_start_run_backend_auto_resolves_and_reports_the_choice(
    tmp_path: Path, monkeypatch
) -> None:
    """``backend='auto'`` runs the resolved backend and returns the resolver's why."""
    monkeypatch.setattr(
        "clear_record.service.auto.available_backend_ids", lambda: ("amd", "nvidia")
    )
    registry, _ = _runnable_meeting(tmp_path)

    seen: list = []

    def fake_pipeline(directory, options, on_event) -> None:
        seen.append(options)

    manager = RunManager(registry, pipeline=fake_pipeline)
    server = build_server(registry, manager)
    run = _payload(
        server,
        "start_run",
        {"project": "ops", "meeting": "kickoff", "backend": "auto"},
    )
    manager.wait(run["id"], timeout=10)

    assert seen[0].backend == "nvidia"  # first available in the preference order
    assert run["backend"] == "nvidia"  # the resolved options are what ran
    assert run["options"]["backend_auto"]["backend"] == "nvidia"
    assert any("--backend auto" in line for line in run["explanations"])


def test_start_run_auto_resolves_and_reports_the_explanation(
    tmp_path: Path, monkeypatch
) -> None:
    """``auto=True`` fills the unset profile/model and returns the explanation."""
    monkeypatch.setattr(
        "clear_record.service.auto.probe_auto", lambda *a, **k: _probe()
    )
    registry, _ = _runnable_meeting(tmp_path)

    seen: list = []

    def fake_pipeline(directory, options, on_event) -> None:
        seen.append(options)

    manager = RunManager(registry, pipeline=fake_pipeline)
    server = build_server(registry, manager)
    run = _payload(
        server,
        "start_run",
        {"project": "ops", "meeting": "kickoff", "auto": True},
    )
    manager.wait(run["id"], timeout=10)

    assert seen[0].profile == "accurate"  # short tape, roomy machine
    assert seen[0].model == "medium"  # the largest checkpoint already on disk
    auto_meta = run["options"]["auto"]
    assert auto_meta["chose"] == ["model", "profile"]
    assert run["explanations"] == [auto_meta["explanation"]]


def test_start_run_backend_auto_without_a_backend_is_actionable(
    tmp_path: Path, monkeypatch
) -> None:
    """No backend for ``backend='auto'``: a usable error, not a traceback."""
    monkeypatch.setattr("clear_record.service.auto.available_backend_ids", lambda: ())
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    server = build_server(registry, RunManager(registry, pipeline=lambda *a: None))

    message = _error_text(
        server,
        "start_run",
        {"project": "ops", "meeting": "kickoff", "backend": "auto"},
    )
    assert "no ASR backend is available" in message
    assert "--backend auto" in message


def test_start_run_auto_model_not_on_disk_is_actionable(
    tmp_path: Path, monkeypatch
) -> None:
    """``--auto`` never downloads: an absent model is a usable error."""
    monkeypatch.setattr(
        "clear_record.service.auto.probe_auto",
        lambda *a, **k: _probe(models_on_disk=frozenset()),
    )
    registry, _ = _runnable_meeting(tmp_path)
    server = build_server(registry, RunManager(registry, pipeline=lambda *a: None))

    # The refusal is the service's ModelNotOnDisk; its structured fields are the
    # stable contract, so assert those rather than the service's sentence. The
    # adapter only translates it, and the model it names is still a fact.
    with pytest.raises(ModelNotOnDisk) as excinfo:
        resolve_run(PipelineOptions(), auto=True, directory=str(tmp_path))
    assert excinfo.value.model == "medium"

    message = _error_text(
        server,
        "start_run",
        {"project": "ops", "meeting": "kickoff", "auto": True},
    )
    assert "'medium'" in message  # names the model; the sentence is the service's


def test_rerun_options_key_the_chunk_cache(tmp_path: Path) -> None:
    """The tuning loop is cheap to repeat: the cache key follows the intent.

    The transcription chunk cache is keyed on backend/model/language/glossary
    (``clear_record.pipeline.workspace.chunk_cache_key``). Two runs with the same
    options produce the same key — a re-run is served from cache, no needless
    re-decode — while a glossary edit changes the key and re-decodes.
    """
    from clear_record.pipeline.workspace import chunk_cache_key

    registry = _registry(tmp_path)
    registry.create_project("Ops")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    registry.set_recording_set(meeting.id, [str(tape)])

    seen = []

    def fake_pipeline(directory, options, on_event) -> None:
        seen.append(options)

    manager = RunManager(registry, pipeline=fake_pipeline)
    server = build_server(registry, manager)

    def start(glossary: str) -> None:
        run = _payload(
            server,
            "start_run",
            {"project": "ops", "meeting": "kickoff", "glossary": glossary},
        )
        manager.wait(run["id"], timeout=10)

    def cache_key(options) -> dict:
        return chunk_cache_key(
            backend=options.backend,
            model=options.model,
            language=options.language,
            glossary=options.glossary or "",
            chunk_seconds=options.chunk_seconds,
            overlap_seconds=options.overlap_seconds,
            n_chunks=3,
            decoders=options.decoder_knobs(),
        )

    start("/tmp/glossary-v1.txt")
    start("/tmp/glossary-v2.txt")  # the glossary was tuned
    start("/tmp/glossary-v2.txt")  # a deliberate repeat

    assert cache_key(seen[0]) != cache_key(seen[1])
    assert cache_key(seen[1]) == cache_key(seen[2])


def test_start_run_defaults_to_the_project_glossary_snapshot(tmp_path: Path) -> None:
    """No explicit glossary: the MCP run applies the project's confirmed terms.

    The tuning loop closes here — an agent edits the glossary over MCP, then a
    plain ``start_run`` picks the edit up, writing it to the workspace
    ``glossary.txt`` and recording the snapshot hash. Candidates are ignored.
    """
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    registry.add_term("ops", "Falcon", status="confirmed")
    registry.add_term("ops", "Draft", added_by="agent")  # candidate
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(workspace))
    registry.set_recording_set(meeting.id, [str(tape)])

    seen: dict = {}

    def fake_pipeline(directory, options, on_event) -> None:
        seen["options"] = options
        seen["text"] = Path(options.glossary).read_text(encoding="utf-8")

    manager = RunManager(registry, pipeline=fake_pipeline)
    server = build_server(registry, manager)

    run = _payload(server, "start_run", {"project": "ops", "meeting": "kickoff"})
    manager.wait(run["id"], timeout=10)

    assert seen["text"] == "Falcon\n"  # the candidate term is excluded
    snapshot = project_snapshot(registry, "ops")
    assert run["options"]["glossary"] == seen["options"].glossary
    assert run["options"]["glossary_sha256"] == snapshot.sha256
    assert (workspace / "glossary.txt").read_text(encoding="utf-8") == "Falcon\n"

    # The recorded identity is still readable after the run finishes.
    status = _payload(server, "run_status", {"run_id": run["id"]})
    assert status["options"]["glossary_sha256"] == snapshot.sha256


def test_list_glossary_terms_rejects_an_unknown_status(tmp_path: Path) -> None:
    """The status enum is validated by the service (store.list_terms)."""
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    server = build_server(registry)

    message = _error_text(
        server, "list_glossary_terms", {"project": "ops", "status": "bogus"}
    )
    assert "status must be one of" in message
    assert "bogus" in message


def test_run_events_replay_for_a_later_process(tmp_path: Path) -> None:
    """A fresh manager (the next server process) replays the persisted stream."""
    registry = _registry(tmp_path)
    registry.create_project("Ops")
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    registry.set_recording_set(meeting.id, [str(tape)])

    def fake_pipeline(directory, options, on_event) -> None:
        progress = Progress("transcribe", 1, on_event)
        progress.start()
        progress.advance(source="a")

    first = RunManager(registry, pipeline=fake_pipeline)
    run = _payload(
        build_server(registry, first),
        "start_run",
        {"project": "ops", "meeting": "kickoff"},
    )
    first.wait(run["id"], timeout=10)

    # A second manager over the same registry models the next server process:
    # the events are persisted, so run_events still pages them.
    second = RunManager(registry, pipeline=fake_pipeline)
    events = _payload(
        build_server(registry, second), "run_events", {"run_id": run["id"]}
    )
    assert events["status"] == "done"
    assert events["events"]


def test_an_injected_manager_is_kept_even_when_falsy(tmp_path: Path) -> None:
    """A falsy injected dependency must not be silently replaced."""

    class FalsyManager:
        def __bool__(self) -> bool:
            return False

    injected = FalsyManager()
    tools_obj = ServiceTools(_registry(tmp_path), injected)  # type: ignore[arg-type]
    assert tools_obj.manager is injected
