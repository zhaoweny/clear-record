"""The console against a registry this build did not write.

Two rows earn a test here, both of them whole-registry questions rather than
page ones:

* the row a **released build** of this application left behind (its
  ``PipelineOptions`` carried ``formats``, which this build does not have) — the
  console has to open that registry and show it, because an upgrade must not
  break the runs the release queued;
* a row **no build** wrote — a hand edit or a corrupt column — which the console
  cannot read and must say so about, instead of answering 500.

The refusal is one exception (``MalformedRunOptions``) raised from the registry,
so these also pin where the web boundary turns it into an HTTP answer.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import closing

from fastapi.testclient import TestClient

from clear_record.core import PipelineOptions
from clear_record.service import Registry, RunManager
from clear_record.web.app import create_app

#: The key the released line wrote and this build does not have (see
#: ``core.options.SUPERSEDED_KEYS``).
_RELEASED_KEY = "formats"


def _console(tmp_path) -> tuple[TestClient, Registry, int]:
    """A console over one registry holding one queued run, and that run's id.

    The run queue is stopped **by construction** (``start_queue=False`` starts no
    drain thread): a live one would claim a hand-written row on its next rescan
    (see ``test_web_activity``), and a queue stopped after the fact would leave
    that to timing. These tests are about the read.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff", workspace_path=str(tmp_path))
    run = registry.create_run(
        meeting.id,
        backend="apple",
        origin="cli",
        run_options=dataclasses.asdict(PipelineOptions(backend="apple")),
    )
    manager = RunManager(registry, start_queue=False)
    client = TestClient(
        create_app(registry, runs=manager, trusted_hosts=("testserver",))
    )
    return client, registry, run.id


def _released_row() -> str:
    """The JSON a released build wrote: this build's options plus its ``formats``."""
    row = dataclasses.asdict(PipelineOptions(backend="apple"))
    row[_RELEASED_KEY] = ["md", "srt"]
    return json.dumps(row)


def _store(registry: Registry, run_id: int, text: str) -> None:
    with closing(sqlite3.connect(str(registry.db_path))) as conn, conn:
        conn.execute(
            "UPDATE pipeline_run SET run_options = ? WHERE id = ?", (text, run_id)
        )


def test_the_console_starts_against_a_registry_a_release_wrote(tmp_path) -> None:
    """An upgrade opens the registry the release left, runs and all.

    The released row was *reduced* before this: the key the release wrote was
    dropped and the fields this build declares but the row did not carry silently
    took built-in defaults. The strict read is what refuses that row, which is why
    the settle step runs first — without it, every page that reads a run would
    raise on a registry an earlier release had written.
    """
    client, registry, run_id = _console(tmp_path)
    _store(registry, run_id, _released_row())

    assert client.get("/").status_code == 200
    answered = client.get(f"/api/runs/{run_id}")
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["run"]["id"] == run_id
    assert _RELEASED_KEY not in body["run"]["run_options"]


def test_a_row_this_build_cannot_read_is_a_409_not_a_500(tmp_path) -> None:
    """The API answers the reader's own message, not a traceback.

    ``detail`` is the exception's text, which names the run and the field: a
    script or an integration can act on that, and a 500 told it nothing.
    """
    client, registry, run_id = _console(tmp_path)
    _store(registry, run_id, '{"backend": "apple", "jobs": "many"}')

    answered = client.get(f"/api/runs/{run_id}")

    assert answered.status_code == 409, answered.text
    detail = answered.json()["detail"]
    assert f"run {run_id}" in detail
    assert "jobs" in detail


def test_a_page_route_that_refuses_says_so_in_the_console(tmp_path) -> None:
    """A page answers the same refusal as a page, not as JSON.

    A page route answers the same refusal on a page of its own, not in the
    console's chrome — the header chip reads the same registry, so a chrome render
    would meet the same refusal — so a user reading the Projects landing gets the
    message, without the console around it. The page renders the refusal's *frame*
    through ``tr`` and appends the reader's field detail untranslated; that split
    is pinned in ``tests/test_i18n_boundaries.py``.
    """
    client, registry, run_id = _console(tmp_path)
    _store(registry, run_id, "not json at all")

    page = client.get("/")

    assert page.status_code == 409, page.text
    assert f"run {run_id}" in page.text
    assert "not JSON" in page.text
