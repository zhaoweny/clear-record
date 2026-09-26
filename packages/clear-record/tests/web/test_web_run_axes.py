"""The run fragment renders the service's four axes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from clear_record.pipeline.workspace import Workspace
from clear_record.core import RecordDocument, Segment, Source
from clear_record.service import Registry, RunManager
from clear_record.web.app import create_app

COST = {
    "audio_seconds": 3600.0,
    "total_wall_seconds": 1200.0,
    "jobs": 4,
    "chunk_seconds": 30.0,
    "machine": "Mac14,1",
}


def _console(registry: Registry) -> TestClient:
    """A console over ``registry`` whose run queue is stopped before seeding.

    These tests are about what the run fragment renders, not about the queue:
    they write run rows straight into the registry and read them back. The
    app's own :class:`RunManager` would otherwise claim a hand-seeded ``queued``
    row on its next 1 s rescan and fail it — the row has no tape set — turning
    the row terminal underneath an assertion that expects a live run.
    ``start_queue=False`` leaves every seeded row exactly as written, and leaves
    it so **by construction**: no drain thread is started, so nothing is decided
    by whether the first pass or the shutdown gets there first.
    """
    manager = RunManager(registry, start_queue=False)
    return TestClient(create_app(registry, runs=manager, trusted_hosts=("testserver",)))


def _documents(
    target: Workspace, *, memory: dict | None, confidence: float | None
) -> None:
    """One source's manifest, one segment and record, in *target*."""
    source = Source(id="a", path=str(target.root / "a.wav"), label="mic")
    target.write_manifest([source])
    segments = [
        Segment(
            start=0.0, end=4.0, text="hello there", source="a", confidence=confidence
        )
    ]
    target.write_segments(
        {"a": segments},
        {"sources": {"a": {"duration": 10.0}}, **(memory or {})},
    )
    target.write_record(
        RecordDocument(sources=(source,), alignment=None, segments=tuple(segments))
    )


def _seeded(
    tmp_path,
    *,
    memory: dict | None = None,
    cost: dict | None = None,
    confidence: float | None = 0.9,
):
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    registry.create_project(
        "Ops",
        actor="console",
    )
    directory = tmp_path / "ws"
    directory.mkdir()
    workspace = Workspace.at(directory)
    _documents(workspace, memory=memory, confidence=confidence)
    meeting = registry.create_meeting(
        "ops",
        "Kickoff",
        workspace_path=str(directory),
        actor="console",
    )
    run = registry.create_run(
        meeting.id,
        backend="apple",
        model="small",
        language="en",
        options={
            "profile": "accurate",
            "auto": {"explanation": "chose profile (a short tape)", "chose": ["model"]},
        },
        actor="console",
    )
    # The run's own copy (ADR-0033): the accuracy axis reads it, not the
    # workspace's published copy.
    _documents(workspace.begin_scope(run.id), memory=memory, confidence=confidence)
    if cost is not None:
        registry.update_run(
            run.id,
            status="done",
            progress={"cost": cost},
            actor="console",
        )
    return registry, client, run


def test_a_finished_run_renders_the_four_axes(tmp_path) -> None:
    _registry, client, run = _seeded(
        tmp_path,
        # The run's own cost carries the peak; the workspace holds a later
        # run's larger one, which must not be shown (F1).
        memory={"peak_rss_bytes": 1024 * 1024 * 1024},
        cost={**COST, "peak_rss_bytes": 512 * 1024 * 1024},
    )

    fragment = client.get(f"/ui/runs/{run.id}")

    assert fragment.status_code == 200
    text = fragment.text
    assert "run-axes" in text
    assert "coverage" in text and "0.4" in text  # accuracy, from the record
    assert "3.00x" in text  # speed, as x-realtime (the CLI's rounding)
    assert "512.0 MiB" in text  # peak decoder-worker memory
    assert "auto chose" in text and "model" in text  # fit, from the run meta
    assert "unknown" not in text

    # The project meetings tab renders the same fragment for the latest run.
    tab = client.get("/projects/ops/meetings")
    assert tab.status_code == 200
    assert "512.0 MiB" in tab.text


def test_an_unmeasured_run_says_unknown_with_the_records_reason(tmp_path) -> None:
    registry, client, run = _seeded(tmp_path)  # queued: no cost, no memory

    live = client.get(f"/ui/runs/{run.id}").text
    assert "run-axes" not in live  # a live run stays a progress fragment

    registry.update_run(
        run.id,
        status="done",
        actor="console",
    )
    finished = client.get(f"/ui/runs/{run.id}").text
    assert "run-axes" in finished
    assert "unknown" in finished
    assert "the run recorded no cost, so this axis is unknown" in finished
    assert "the run recorded no worker memory" in finished


def test_a_null_accuracy_figure_renders_a_dash_not_none(tmp_path) -> None:
    """F2: a missing confidence is a dash, and figures round like the CLI's."""
    _registry, client, run = _seeded(tmp_path, cost=dict(COST), confidence=None)

    text = client.get(f"/ui/runs/{run.id}").text

    assert "None" not in text
    assert "mean confidence -" in text
    assert "coverage 0.4000" in text
