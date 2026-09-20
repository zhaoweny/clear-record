"""RUN-03 in the console: the pipeline status page and the header chip.

The page answers "what is clear-record doing right now" from the shared registry
— running and queued runs across every project, then the newest finished ones —
and the header chip is fed from the same reads, so the two cannot disagree.

These tests write run rows straight into the registry, so the queue is stopped
before seeding (issue 19 / b8f975e): the app's own manager would otherwise claim
a hand-seeded ``queued`` row on its next 1 s rescan and fail it, turning the row
terminal underneath an assertion that expects a live run.
"""

from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from clear_record.core import JobEvent, PipelineOptions
from clear_record.service import Registry, RunManager
from clear_record.web.app import create_app

#: The claiming owner a live run is seeded with. A host this machine is not, so
#: "the row names its owner's host" cannot pass by accident.
OWNER = "run-owner.example:4242"

#: A finished run's raw cost primitives (RUN-01): 3600 s of audio in 1200 s of
#: wall clock is 3.00x realtime, derived at display time and never stored.
COST = {
    "audio_seconds": 3600.0,
    "total_wall_seconds": 1200.0,
    "machine": "Mac14,1 (Darwin-25.3.0; arm64)",
}


def _console(registry: Registry) -> TestClient:
    """A console over ``registry`` whose run queue is stopped (see the module)."""
    manager = RunManager(registry)
    manager.shutdown(timeout=5.0)
    return TestClient(create_app(registry, runs=manager, trusted_hosts=("testserver",)))


def _chip(page: str) -> str:
    """The header chip's rendered markup, which carries its own live label."""
    return next(line.strip() for line in page.splitlines() if 'id="status"' in line)


def _id_mark(run_id: int) -> str:
    """The run's own id as the page prints it (exact, never a substring match)."""
    return f">#{run_id}</span>"


def _seeded(registry: Registry):
    """Two projects with one live run each and one finished run behind them."""
    registry.create_project("Ops")
    registry.create_project("Field interviews")
    on_air = registry.create_meeting("ops", "Kickoff")
    later = registry.create_meeting("field-interviews", "Interview 04")

    # A finished run: the history section's own row, with a cost record.
    finished = registry.create_run(
        on_air.id, backend="apple", model="small", language="en", origin="cli"
    )
    registry.update_run(finished.id, status="done", progress={"cost": COST})

    # The run in flight: claimed by a live owner (a host:pid string) and with a
    # persisted progress event — the only place a live run's stage and progress
    # live, because the terminal summary is written when it stops. Its queued
    # options resolve the chunk length, so 3 chunks of 30 s in 36 s of its own
    # transcribe stage is a 2.50x rate so far.
    running = registry.create_run(
        on_air.id,
        backend="apple",
        model="small",
        language="en",
        origin="console",
        run_options=dataclasses.asdict(
            PipelineOptions(backend="apple", model="small", chunk_seconds=30.0)
        ),
    )
    registry.claim_run(running.id, owner=OWNER)
    registry.add_run_event(
        running.id,
        JobEvent(stage="transcribe", index=3, total=12, eta_s=42.0, elapsed_s=36.0),
    )

    # And the queue behind it, started by an agent.
    queued = registry.create_run(
        later.id, backend="apple", model="small", language="en", origin="mcp"
    )
    return running, queued, finished


def test_the_page_lists_the_live_queue_across_projects(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    running, queued, _finished = _seeded(registry)

    page = client.get("/activity")

    assert page.status_code == 200
    text = page.text
    # Both live runs, from two different projects, with the stage and progress
    # the running one's own event stream last reported.
    assert "Kickoff" in text and "Interview 04" in text
    assert ">Ops<" in text and ">Field interviews<" in text
    assert "transcribe" in text and "3 / 12" in text
    # The queue's place in the FIFO, and the origin each run was started from.
    assert "position 1" in text
    assert "mcp" in text and "console" in text and "cli" in text
    # The machine column shows the host its claiming owner named; the queued
    # run has none, so its column is unknown rather than this node.
    assert "run-owner.example" in text
    assert ">unknown<" in text
    assert _id_mark(running.id) in text and _id_mark(queued.id) in text


def test_a_running_run_shows_the_rate_so_far(tmp_path) -> None:
    """A live run has no cost record, but its own events carry the rate so far."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    running, queued, _finished = _seeded(registry)

    rows = client.get("/activity").text.split('class="run status-')
    live = next(row for row in rows if _id_mark(running.id) in row)
    waiting = next(row for row in rows if _id_mark(queued.id) in row)

    # 3 chunks x 30 s over the transcribe stage's own 36 s: 2.50x so far.
    assert "2.50x" in live and "so far" in live
    # Nothing has measured the queued run, and it is not transcribing.
    assert "unknown" in waiting and "x realtime" not in waiting


def test_a_run_past_transcribe_reports_no_rate_so_far(tmp_path) -> None:
    """The chunk length is the transcribe stage's economy, not every stage's.

    A run whose last event belongs to another stage has no chunk primitive to
    derive a rate from; indexing it as one would report a figure for units that
    are not chunks.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    running, _queued, _finished = _seeded(registry)
    registry.add_run_event(
        running.id, JobEvent(stage="reconcile", index=9, total=10, elapsed_s=3.0)
    )

    rows = client.get("/activity").text.split('class="run status-')
    live = next(row for row in rows if _id_mark(running.id) in row)

    assert "x realtime" not in live
    assert "unknown" in live


def test_the_newest_outcome_is_the_last_finish_not_the_last_row(tmp_path) -> None:
    """The chip and the history rank on when a run ended, not on its id.

    A run created later can finish earlier — the queue is FIFO, a cancel is not
    — so ranking by id would leave the chip saying idle while the page it links
    to lists a failure under "Recently finished".
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")
    # Created first — and the run that finished last, by failing.
    created_first = registry.create_run(
        meeting.id, backend="apple", model="small", language="en", origin="api"
    )
    registry.update_run(
        created_first.id,
        status="failed",
        ended_at="2026-09-17T11:00:00+00:00",
        error="no ggml model on disk",
    )
    # Created second, finished an hour earlier: the row written last is not the
    # newest outcome.
    created_second = registry.create_run(
        meeting.id, backend="apple", model="small", language="en", origin="console"
    )
    registry.update_run(
        created_second.id,
        status="done",
        ended_at="2026-09-17T10:00:00+00:00",
        progress={"cost": COST},
    )

    text = client.get("/activity").text
    history = text.split("Recently finished")[1]

    # The failure is the newest *outcome*, so the chip points at it — and at the
    # page that lists it...
    assert ">needs attention</a>" in _chip(text)
    # ...and it leads that history.
    assert history.index("no ggml model on disk") < history.index(
        _id_mark(created_second.id)
    )


def _seeded_running(registry: Registry, *, index: int, reused: int, elapsed_s: float):
    """A running run whose last transcribe event says what it has done."""
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")
    run = registry.create_run(
        meeting.id,
        backend="apple",
        model="small",
        language="en",
        origin="console",
        run_options=dataclasses.asdict(
            PipelineOptions(backend="apple", model="small", chunk_seconds=30.0)
        ),
    )
    registry.claim_run(run.id, owner=OWNER)
    registry.add_run_event(
        run.id,
        JobEvent(
            stage="transcribe",
            index=index,
            reused=reused,
            total=240,
            elapsed_s=elapsed_s,
        ),
    )
    return run


def test_a_resumed_run_rates_only_the_chunks_it_decoded(tmp_path) -> None:
    """A cached chunk is not work the decoder's clock paid for (RUN-03).

    8 chunks finished, 4 of them served from the cache: the rate divides the
    **decoded** 4 x 30 s by the stage's own elapsed seconds. Counting all 8 would
    double it — the shape that made the seed's 112-of-120 reuse print ~260x.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    run = _seeded_running(registry, index=8, reused=4, elapsed_s=4.4)

    text = client.get("/activity").text
    row = next(
        part for part in text.split('class="run status-') if f"#{run.id}</span>" in part
    )

    assert "27.27x" in row and "so far" in row  # 4 x 30 / 4.4
    assert "54.55x" not in row  # what counting the cached chunks would claim


def test_a_fully_reused_run_reports_no_rate(tmp_path) -> None:
    """Nothing was decoded, so there is no decode rate to report."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    run = _seeded_running(registry, index=6, reused=6, elapsed_s=0.02)

    text = client.get("/activity").text
    row = next(
        part for part in text.split('class="run status-') if f"#{run.id}</span>" in part
    )

    assert "x realtime" not in row
    assert "unknown" in row


def test_a_finished_run_shows_its_recorded_speed_and_duration(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    _running, _queued, finished = _seeded(registry)

    text = client.get("/activity").text

    # The speed is the record's own ratio, derived here (RUN-01), and the
    # duration is the wall clock the same record measured.
    assert "3.00x" in text and "realtime" in text
    assert "1200.0s" in text
    assert COST["machine"] in text
    assert _id_mark(finished.id) in text


def test_a_failed_run_shows_its_outcome_and_why(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    _seeded(registry)
    registry.create_project("Retro")
    meeting = registry.create_meeting("retro", "Weekly")
    failed = registry.create_run(
        meeting.id, backend="apple", model="small", language="en", origin="api"
    )
    registry.update_run(failed.id, status="failed", error="no ggml model on disk")

    text = client.get("/activity").text

    assert "failed" in text and "no ggml model on disk" in text
    assert "api" in text


def test_history_leads_with_the_newest_run(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")
    older = registry.create_run(
        meeting.id, backend="apple", model="small", language="en"
    )
    registry.update_run(older.id, status="done", progress={"cost": COST})
    newer = registry.create_run(
        meeting.id, backend="apple", model="large", language="en"
    )
    registry.update_run(newer.id, status="failed", error="the tape was unreadable")

    history = client.get("/activity").text.split("Recently finished")[1]

    assert history.index("the tape was unreadable") < history.index(_id_mark(older.id))


def test_an_idle_node_says_so(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)

    text = client.get("/activity").text

    assert "No runs queued or running." in text
    assert "No finished runs yet." in text


def test_the_chip_reports_the_live_queue_and_the_newest_outcome(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")

    # Nothing has run yet: the chip is idle, and says so in its own class.
    idle = client.get("/").text
    assert ">idle</a>" in idle
    assert "status-neutral" in _chip(idle)

    # A queued run the node has not picked up yet is queued, not running.
    run = registry.create_run(meeting.id, backend="apple", model="small", language="en")
    queued = client.get("/").text
    assert ">queued 1</a>" in queued
    assert "status-queued" in _chip(queued)

    # In flight: the chip counts the running run, and links to the page.
    registry.claim_run(run.id, owner=OWNER)
    running = client.get("/").text
    assert 'href="/activity">running 1</a>' in running
    assert "status-running" in _chip(running)

    # The newest outcome decides once nothing is in flight: a failure needs
    # attention, a deliberate stop does not.
    registry.update_run(run.id, status="failed", error="the tape was unreadable")
    failed = client.get("/").text
    assert ">needs attention</a>" in failed
    assert "status-failed" in _chip(failed)

    registry.update_run(run.id, status="stopped")
    stopped = client.get("/").text
    assert ">idle</a>" in stopped


def test_every_in_flight_status_has_a_chip_label() -> None:
    """The chip's labels cover the service's in-flight declaration exactly.

    The chip reads ``ACTIVE_RUN_STATUSES`` for *which* runs are in flight and
    ``ACTIVE_RUN_LABELS`` for how to say it, so the two are one pair of
    declarations: a third in-flight status must bring a label with it instead of
    being counted under another state's name — or raising a ``KeyError`` on the
    next page render. Compared as sets, in both directions.
    """
    from clear_record.service import ACTIVE_RUN_STATUSES
    from clear_record.web.app import ACTIVE_RUN_LABELS

    assert set(ACTIVE_RUN_LABELS) == set(ACTIVE_RUN_STATUSES)


def test_the_chip_reports_the_heaviest_in_flight_state(tmp_path) -> None:
    """A node executing work says so, even with work waiting behind it.

    The chip's label table orders the states it reports (``running`` before
    ``queued``), and the row order must not decide: the older row here is the
    *waiting* one, so a chip that ranked by insertion would say "queued". It
    reports the first label whose status has rows, and counts the rows in that
    state.
    """
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    registry.create_project("Ops")
    waiting = registry.create_meeting("ops", "Kickoff")
    executing = registry.create_meeting("ops", "Retro")
    registry.create_run(waiting.id, backend="apple", model="small", language="en")
    claimed = registry.create_run(
        executing.id, backend="apple", model="small", language="en"
    )
    registry.claim_run(claimed.id, owner=OWNER)

    chip = _chip(client.get("/").text)

    assert "status-running" in chip
    assert ">running 1</a>" in chip
    assert "queued" not in chip


def test_the_chip_and_the_page_are_translated(tmp_path) -> None:
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    _seeded(registry)
    client.cookies.set("cr_lang", "zh_CN")

    text = client.get("/activity").text

    assert ">运行中 1 个</a>" in _chip(text)
    assert "活动" in text  # the nav entry and the page heading
    assert "进行中" in text and "最近完成" in text
    assert "来源" in text and "机器" in text and "耗时" in text
    # The figures are not translated: they are the run's own measurements.
    assert "3.00x" in text and "1200.0s" in text


def test_a_queued_row_names_no_machine(tmp_path) -> None:
    """Nothing has claimed the job yet, so its machine is unknown, not this node."""
    registry = Registry.open(db_path=tmp_path / "registry.sqlite3")
    client = _console(registry)
    running, queued, _finished = _seeded(registry)

    rows = client.get("/activity").text.split('class="run status-')
    live = next(row for row in rows if _id_mark(running.id) in row)
    waiting = next(row for row in rows if _id_mark(queued.id) in row)

    assert "run-owner.example" in live
    assert "run-owner.example" not in waiting
