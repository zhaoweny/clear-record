"""What the console and the JSON API record themselves as (ADR-0033).

One FastAPI app serves both surfaces, and they are told apart by the actor each
one supplies: every ``/web/ui/`` route calls the service as ``console``, every
``/api/v1/`` route as ``api``. The actor is not a request field, so the record says
which *surface* did a thing — including on the run edges, where a body word does
choose the run's own ``origin`` and is recorded in that column, never as the
actor (a client that sends ``{"origin": "console"}`` is still audited as ``api``).

The registry here is the app's own, so the rows these tests read are the rows the
routes wrote: the audit record is the service's, and these are its two web
callers.
"""

from __future__ import annotations

import pytest
from _console import signed_in
from clear_record.service import API, CONSOLE, MCP, MeetingAgent, Registry, RunManager
from clear_record.web.app import create_app
from fastapi.testclient import TestClient

#: The address the console's own client dials (see ``test_web_api``): a client on
#: the node's machine, which is what a route taking a local path requires.
LOCAL_ORIGIN = "http://127.0.0.1:8765"


class App:
    """The app under test, plus the registry whose record its routes wrote."""

    def __init__(self, *, client: TestClient, registry: Registry) -> None:
        self.client = client
        self.registry = registry

    def rows(self) -> list[tuple[str, str, str, str]]:
        """**Every** row the registry holds, oldest first — nothing filtered out.

        The fixture's own sign-in writes one ``credential.set`` row (the act a
        real first run performs), so it is the first row of every expected list
        here: a filter would hide a spurious or misplaced credential row written
        by the route under test, which is a row this surface is meant to see.
        """
        return [
            (row.actor, row.action, row.target, row.outcome)
            for row in self.registry.list_audit_events()
        ]


@pytest.fixture()
def app(tmp_path) -> App:
    """A console over a temp registry, with the queue stopped.

    The queue is built stopped (``start_queue=False``) and shut down at teardown:
    nothing here runs a pipeline, and a drain thread left behind would be a
    mutation of the registry this suite did not ask for.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    manager = RunManager(registry, pipeline=lambda *a, **k: None, start_queue=False)
    try:
        yield App(
            client=signed_in(
                TestClient(create_app(registry, runs=manager), base_url=LOCAL_ORIGIN)
            ),
            registry=registry,
        )
    finally:
        manager.shutdown(timeout=10)


def test_the_console_records_itself_as_the_actor(app: App) -> None:
    """A console form submit is one ``console`` row, naming what it touched."""
    res = app.client.post("/web/ui/projects", data={"name": "Ops"})

    assert res.status_code == 200
    assert app.rows() == [
        (CONSOLE, "credential.set", "credential:console", "ok"),
        (CONSOLE, "project.create", "project:Ops", "ok"),
    ]


def test_the_json_api_records_itself_as_the_actor(app: App) -> None:
    """The same mutation through ``/api/v1/`` is the API's row, not the console's."""
    res = app.client.post("/api/v1/projects", json={"name": "Ops"})

    assert res.status_code == 201
    assert app.rows() == [
        (CONSOLE, "credential.set", "credential:console", "ok"),
        (API, "project.create", "project:Ops", "ok"),
    ]


def test_a_client_declared_origin_is_not_the_audit_actor(app: App) -> None:
    """A body word names the run's ``origin``; the record holds the transport.

    The forgery this closes: a client posted ``{"origin": "console"}`` and the
    enqueue was filed as the console's work. ``origin`` is the run's own
    provenance column and stays the caller's word; the actor is the surface that
    carried the request (ADR-0033).
    """
    workspace = app.registry.db_path.parent / "ws"
    workspace.mkdir(exist_ok=True)
    app.registry.create_project("Ops", actor=CONSOLE)
    meeting = app.registry.create_meeting(
        "ops", "Kickoff", workspace_path=str(workspace), actor=CONSOLE
    )
    app.registry.set_recording_set(meeting.id, ["a.wav"], actor=CONSOLE)
    before = len(app.registry.list_audit_events())

    res = app.client.post(
        f"/api/v1/meetings/{meeting.id}/runs", json={"origin": "console"}
    )

    assert res.status_code == 202
    assert res.json()["run"]["origin"] == "console"  # the caller's word, on the row
    assert [
        (row.actor, row.action, row.target, row.outcome)
        for row in app.registry.list_audit_events()[before:]
        if row.action == "run.enqueue"
    ] == [(API, "run.enqueue", f"meeting:{meeting.id}", "ok")]


def test_a_console_acceptance_records_console_as_the_reviewer(app: App) -> None:
    """The human's half: the console decides, and the decision says so.

    The harness writes the draft — as ``mcp`` in ``tests/mcp/`` — and the
    acceptance here is the console's, so the version's reviewer and every
    registry write the promotion makes carry the console's word, never the
    writer's (ADR-0033).
    """
    workspace = app.registry.db_path.parent / "ws"
    workspace.mkdir()
    app.registry.create_project("Ops", actor=CONSOLE)
    meeting = app.registry.create_meeting(
        "ops", "Kickoff", workspace_path=str(workspace), actor=CONSOLE
    )
    agent = MeetingAgent(app.registry, meeting)
    draft = agent.write(
        "glossary_collection", {"terms": [{"term": "Falcon"}]}, actor=MCP
    )

    res = app.client.post(
        f"/web/ui/meetings/{meeting.id}/agent/drafts/{draft.draft_id}/accept",
        data={"version": draft.version},
    )

    assert res.status_code == 200
    decided = agent.draft(draft.draft_id)
    assert decided is not None
    assert decided.versions[-1].reviewed_by == CONSOLE
    assert decided.versions[-1].provenance.author == MCP
    # The write, the promotion the decision produced, and the decision's own row:
    # the promotion runs inside the decision's hold on the chain, so the terms it
    # adds are recorded before the decision that caused them is (ADR-0031).
    assert app.rows() == [
        (CONSOLE, "credential.set", "credential:console", "ok"),
        (CONSOLE, "project.create", "project:Ops", "ok"),
        (CONSOLE, "meeting.create", "meeting:Kickoff", "ok"),
        (MCP, "draft.write", f"draft:{draft.draft_id}", "ok"),
        (CONSOLE, "term.add", "term:Falcon", "ok"),
        (CONSOLE, "draft.accept", f"draft:{draft.draft_id}", "ok"),
    ]
