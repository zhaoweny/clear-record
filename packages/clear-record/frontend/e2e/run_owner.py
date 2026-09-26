"""Keep one seeded run genuinely ``running`` for the whole e2e suite (RUN-03).

The console reconciles a ``running`` row it cannot see a live owner for into
``interrupted`` at startup — correctly, because that is exactly what a killed
process leaves behind. So a *live* row cannot simply be written into the seeded
registry: the suite would render an interrupted run where the status page needs
a running one. This process is the owner the console has to find alive.

It starts a real :class:`RunManager` over the seeded registry — the same manager
the console uses, claim and heartbeat included — with a pipeline that reports
progress and then never returns, so the row stays ``running`` with a fresh
heartbeat and a growing progress stream for as long as the suite runs. The
queue's one-run-at-a-time rule does the rest: while this run executes, nothing
may claim the fixture's queued row, so the page shows both.

``seed.py`` starts this and waits until the run is claimed before it returns
(so the console it precedes can never claim the row itself), and
``e2e/teardown.ts`` ends it after the last test. It also stops on its own when
the row is no longer this process's running run — a reseeded registry, or an
owner that took it over, both mean there is nothing left to hold.
"""

from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path

from clear_record.core import EventSink, JobEvent, PipelineOptions
from clear_record.service import Meeting, Registry, RunManager

#: The meeting whose run this process keeps in flight: created by ``seed.py``
#: with a managed workspace and one tape, so a run against it is claimable.
PROJECT_SLUG = "field-interviews"
MEETING_TITLE = "Interview 04"

#: The chunk plan the stand-in transcribe reports. It is never reached — the
#: loop caps below ``total``, so the run cannot finish and the suite always sees
#: one live run — and it is large enough that the progress bar looks like a real
#: tape rather than a stub.
CHUNKS = 240
#: The chunk length this fixture's runs execute with. The tool's default is 600
#: seconds (ten minutes), which would let a run report one chunk in a whole
#: suite and show no progress at all; 30 seconds is the plan the seeded finished
#: runs' cost records also name, so the console speaks one economy throughout.
CHUNK_SECONDS = 30.0

#: How long one of those chunks "takes". At the ~2.4x realtime this machine's
#: own recorded runs sustain (the seeded cost records), 30 seconds of audio is
#: ~12.5 s of wall clock — and the arithmetic has to stay consistent, because
#: the console derives a live run's rate from chunks x chunk length over the
#: event's elapsed seconds: a faster loop would make the seeded page claim a
#: decode speed no machine has.
SECONDS_PER_CHUNK = 12.5

#: How long to wait for the manager's own drain thread to claim the run before
#: giving up. The claim is a single conditional update; this is a stuck-detector,
#: not a race with anything.
CLAIM_TIMEOUT_S = 30.0


def _endless_transcribe(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> None:
    """The stand-in pipeline: it reports progress, and it never returns.

    What the seeded console needs is a run that is *genuinely* in flight, with
    the stage and progress a real one would have. Nothing here decodes anything:
    the progress is the only work, and the sleep is what keeps the run live for
    the length of the suite.
    """
    assert on_event is not None  # RunManager always attaches its own sink
    started = time.monotonic()
    # The stage opens at once, so a page rendered the moment after the claim
    # still shows the stage and its plan; the chunks then land at the cadence a
    # real decoder would finish them at.
    on_event(JobEvent(stage="transcribe", index=0, total=CHUNKS))
    index = 0
    while True:
        time.sleep(SECONDS_PER_CHUNK)
        index = min(index + 1, CHUNKS - 1)
        elapsed = time.monotonic() - started
        on_event(
            JobEvent(
                stage="transcribe",
                index=index,
                total=CHUNKS,
                elapsed_s=round(elapsed, 3),
                eta_s=round(elapsed / index * (CHUNKS - index), 3),
            )
        )


def _claimed_owner(registry: Registry, run_id: int) -> str | None:
    """This process's owner string once its run is running, else ``None``."""
    row = registry.get_run(run_id)
    if row is None or row.status != "running" or not row.owner:
        return None
    return row.owner


def _meeting(registry: Registry) -> Meeting | None:
    return next(
        (
            meeting
            for meeting in registry.list_meetings(PROJECT_SLUG)
            if meeting.title == MEETING_TITLE
        ),
        None,
    )


def main() -> int:
    data = Path(os.environ["CR_DATA_DIR"]).expanduser().resolve()
    registry = Registry.open(data_dir=data)
    meeting = _meeting(registry)
    if meeting is None:
        print(
            f"run-owner: no {MEETING_TITLE!r} in {PROJECT_SLUG!r}; run the seed first",
            file=sys.stderr,
        )
        return 1
    manager = RunManager(registry, pipeline=_endless_transcribe)
    enqueued = manager.start(
        meeting,
        dataclasses.replace(
            PipelineOptions(),
            backend="apple-speech",
            model="whisper-large-v3",
            chunk_seconds=CHUNK_SECONDS,
        ),
        origin="cli",
        actor="cli",
    )
    deadline = time.monotonic() + CLAIM_TIMEOUT_S
    owner = None
    while owner is None and time.monotonic() < deadline:
        owner = _claimed_owner(registry, enqueued.id)
        if owner is None:
            time.sleep(0.05)
    if owner is None:
        print(f"run-owner: run {enqueued.id} was never claimed", file=sys.stderr)
        return 1
    print(f"run-owner: run {enqueued.id} running as {owner}", flush=True)

    # The claim is what makes this process the run's owner; the loop is what
    # keeps the process alive while the console is up. It ends when the row is
    # no longer this process's running run — reseeded, reaped, or taken over.
    while _claimed_owner(registry, enqueued.id) == owner:
        time.sleep(5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
