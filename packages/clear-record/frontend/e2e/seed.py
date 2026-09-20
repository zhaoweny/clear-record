"""Seed a deterministic console for the Playwright suite.

`just e2e` runs this before `playwright test`: it wipes `CR_DATA_DIR` and writes
one realistic project — meetings with and without a managed workspace, a
reconciled transcript, artifacts, a finished run, an archive, and glossary terms
in every status — so the visual specs capture real content instead of empty
states. A second project carries the status page's live run (RUN-03).

Two things this seed guarantees that the rows alone cannot:

* **one run is genuinely in flight.** A `running` row written straight into the
  registry is reconciled to `interrupted` by the console at startup, correctly —
  that is what a killed owner leaves behind. So the seed starts
  `e2e/run_owner.py`, which claims the run through a real `RunManager` and holds
  it, and waits for that claim before it returns: the console that starts next
  finds a live owner and leaves the run running;
* **one run is queued behind it.** Created after the claim, so the owner's drain
  cannot pick it up, and unclaimable while the node's one run executes — which
  is what the queue's one-at-a-time rule means.

`e2e/teardown.ts` ends the run owner after the last test; the seed records its
pid beside the data directory for it.

The seed uses the service layer directly (the same `Registry` the console opens),
so it cannot drift from the app's own schema.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from clear_record.core import PipelineOptions, RecordDocument, Segment, write_json
from clear_record.service import Meeting, setup
from clear_record.service.diagnostics import machine_description
from clear_record.service.managed import workspace_path_for
from clear_record.service.store import Registry

#: ``(start, end, speaker, source, text)`` — a plausible review conversation, long
#: enough that the reading column is exercised at a realistic measure.
SEGMENTS: tuple[tuple[float, float, str, str, str], ...] = (
    (
        3.0,
        7.4,
        "Alex",
        "macbook",
        "so the recorder was running from the top of the hour",
    ),
    (
        8.1,
        13.9,
        "Bo",
        "h4n",
        "right, and I lined the second mic up to the laptop track afterwards",
    ),
    (
        14.6,
        19.2,
        "Alex",
        "macbook",
        "there is still a couple of hundred milliseconds of drift by the end",
    ),
    (
        19.8,
        25.1,
        "Bo",
        "h4n",
        "that is the room, not the clocks. the fan kicks in about twenty minutes in",
    ),
    (
        25.9,
        31.4,
        "Alex",
        "macbook",
        "do we want to lift that before or after reconciliation",
    ),
    (
        32.0,
        38.8,
        "Bo",
        "h4n",
        "after. the aligner should see the raw tracks, then the record gets one timeline",
    ),
    (
        39.5,
        44.0,
        "Alex",
        "macbook",
        "agreed. and the glossary is doing a lot of work already",
    ),
    (
        44.6,
        50.2,
        "Bo",
        "h4n",
        "it is. h4n comes through as aitch four en in three places",
    ),
    (
        51.0,
        56.3,
        "Alex",
        "macbook",
        "add it as confirmed so the next run stops guessing",
    ),
    (57.1, 63.9, "Bo", "h4n", "done. what about pre roll, do we keep that term"),
    (
        64.5,
        70.0,
        "Alex",
        "macbook",
        "retire it, we settled on lead in two sessions ago",
    ),
    (
        70.8,
        76.2,
        "Bo",
        "h4n",
        "fine. the minutes from last week are already in the project view",
    ),
    (
        77.0,
        82.6,
        "Alex",
        "macbook",
        "good. let us accept the draft once the check comes back clean",
    ),
    (
        83.4,
        89.9,
        "Bo",
        "h4n",
        "one more thing, the archive is the durable copy, so we can drop the uploaded tapes",
    ),
    (
        90.6,
        95.1,
        "Alex",
        "macbook",
        "manually, though. nothing deletes the tapes on its own",
    ),
    (95.9, 101.4, "Bo", "h4n", "understood. I will note that in the wrap up"),
)

TERMS: tuple[tuple[str, str, str | None, str, str], ...] = (
    (
        "falcon",
        "FAL-kun",
        "Falcon, falcon-1",
        "Internal codename for the reconciliation pass.",
        "confirmed",
    ),
    (
        "lead in",
        "LEED in",
        "pre-roll, preroll",
        "Room tone kept at the head of a take.",
        "confirmed",
    ),
    (
        "H4n",
        "aitch four en",
        "H4N, h4",
        "The field recorder used as the second source.",
        "candidate",
    ),
    ("pre-roll", None, None, "Superseded by lead in.", "retired"),
    (
        "reconciliation",
        None,
        "reconcile",
        "Merging several sources onto one attributed timeline.",
        "candidate",
    ),
)


def _cost_record(
    *,
    stages: dict,
    audio_seconds: float,
    chunks: int,
    chunks_reused: int,
    wall_seconds: float,
    peak_rss_bytes: int | None,
    peak_rss_reason: str | None = None,
) -> dict:
    """A finished run's cost record, in the service's own shape (RUN-01).

    Key for key what ``RunManager`` writes when a run stops: the per-stage
    wall-clock, the audio seconds processed, the chunk economy, the resolved
    decoder facts, the transcribe workers' peak memory (``None`` and no reason
    when nothing sampled one), the total wall clock and the machine. ``stages``
    therefore names **every** stage, ``None`` for one the run never reached —
    the builder's shape, which a reader is entitled to index. The figures are
    the fixture's, chosen so the ratios a reader derives are believable; nothing
    here is stored as a ratio, because the service derives those.
    """
    return {
        "stages": stages,
        "audio_seconds": audio_seconds,
        "chunks": chunks,
        "chunks_reused": chunks_reused,
        "chunks_redecoded": chunks - chunks_reused,
        "backend": "apple-speech",
        "model": "whisper-large-v3",
        "jobs": 4,
        "chunk_seconds": 30.0,
        "peak_rss_bytes": peak_rss_bytes,
        "peak_rss_reason": peak_rss_reason,
        "total_wall_seconds": wall_seconds,
        "machine": machine_description(),
    }


def _managed_meeting(registry: Registry, root: Path, slug: str, title: str) -> Meeting:
    """A meeting with a managed workspace, ready to be run against."""
    meeting = registry.create_meeting(slug, title)
    meeting = registry.set_meeting_workspace(
        meeting.id, str(workspace_path_for(root, meeting))
    )
    Path(meeting.workspace_path).mkdir(parents=True, exist_ok=True)
    return meeting


def _upload_tape(registry: Registry, meeting: Meeting, name: str) -> None:
    """One small uploaded tape, so a run against ``meeting`` is claimable.

    A run needs a tape set to execute at all (``RunManager.start``); a queued
    row whose meeting could never run would be a state no surface produces.
    """
    tape_bytes = b"RIFF" + b"\x00" * 4092
    path = Path(meeting.workspace_path) / name
    path.write_bytes(tape_bytes)
    registry.register_tape(
        meeting.id,
        path=str(path),
        sha256=hashlib.sha256(tape_bytes).hexdigest(),
        bytes=len(tape_bytes),
    )


def _throwaway_env(data: Path) -> dict[str, str]:
    """The ``CR_*`` directories that keep every seeded process in throwaway dirs.

    ``just e2e`` names the data and state directories on the recipe line, so
    those are already ours; the log, cache and models directories still default
    to the **operator's** real ones. A run owner that resolved those would
    append the fixture's run events to the log a person is reading, and
    playwright.config.ts sets all three for the server it boots for exactly this
    reason. Deriving them from a sibling of the data directory keeps the whole
    suite inside one throwaway tree, whatever the caller named the data dir.
    """
    root = data.parent
    return {
        "CR_LOG_DIR": str(root / "logs"),
        "CR_CACHE_DIR": str(root / "cache"),
        "CR_MODELS_DIR": str(root / "models"),
    }


def _start_run_owner(registry: Registry, data: Path) -> None:
    """Start the process that holds one run in flight, and wait for its claim.

    The child is started in **its own session**, so it outlives this seed and
    the console it precedes still sees its pid alive. Its pid is written beside
    the data directory for ``e2e/teardown.ts``, which ends it after the last
    test; the child also stops on its own if its run stops being its own.

    Waiting for the claim is the point of the handshake: the console reconciles
    and drains the queue at startup, and a run that is not already owned by a
    live process would either be reaped as an orphan or claimed and executed by
    the console itself.
    """
    helper = Path(__file__).with_name("run_owner.py")
    # The child outlives this process, so it must not hold this process's
    # stdout: a runner that reads the seed's output to end-of-file would wait
    # for a log line that never comes. Its own log is beside the pid file.
    log = (data.parent / "run-owner.log").open("w", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, str(helper)],
        # The child must not inherit the operator's log/cache/model directories
        # (see _throwaway_env), and it must hold its own descriptors rather than
        # this process's.
        env={**os.environ, **_throwaway_env(data)},
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()  # the child holds its own descriptor now
    (data.parent / "run-owner.pid").write_text(f"{child.pid}\n", encoding="utf-8")
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise SystemExit(
                f"run-owner exited before claiming a run ({child.returncode}):\n"
                f"{(data.parent / 'run-owner.log').read_text(encoding='utf-8')}"
            )
        if registry.runs_with_status("running"):
            return
        time.sleep(0.1)
    raise SystemExit("run-owner did not claim a run within 60s")


def main() -> int:
    data = Path(os.environ["CR_DATA_DIR"]).expanduser().resolve()
    # Before anything opens the registry: this process logs as it seeds, and so
    # does the run owner it starts, so both have to be inside the throwaway tree
    # rather than the operator's real log/cache/model directories.
    os.environ.update(_throwaway_env(data))
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)

    registry = Registry.open(data_dir=data)
    # The server's managed root defaults to `<data>/workspaces`; build the same
    # absolute path here so the workspace path stored in the registry never
    # depends on either process's cwd.
    root = data / "workspaces"

    q3 = registry.create_project(
        "Q3 sync", notes="Quarterly planning and review recordings"
    )
    registry.create_project(
        "Field interviews", notes="On-location interviews, one speaker each"
    )

    for term, reading, aliases, definition, status in TERMS:
        registry.add_term(
            "q3-sync",
            term,
            reading=reading,
            aliases=aliases,
            definition=definition,
            status=status,
        )

    # The meeting the visual specs review: a managed workspace with a real
    # reconciled record, an artifact row, accepted minutes and a finished run.
    kickoff = registry.create_meeting("q3-sync", "Kickoff")
    kickoff = registry.set_meeting_workspace(
        kickoff.id, str(workspace_path_for(root, kickoff))
    )
    workspace = Path(kickoff.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)
    write_json(
        workspace / "record.json",
        RecordDocument(
            sources=(),
            alignment=None,
            segments=tuple(
                Segment(start=s, end=e, text=text, source=source, speaker=speaker)
                for s, e, speaker, source, text in SEGMENTS
            ),
        ),
    )
    registry.add_artifact(
        kickoff.id,
        kind="record",
        path=str(workspace / "record.json"),
        produced_by="pipeline",
        review_state="final",
    )
    # One uploaded tape: the Media tab's inventory and the storage panel need a
    # real file to size, and the screenshot is empty without one.
    tape_bytes = b"RIFF" + b"\x00" * 4092
    tape_path = workspace / "kickoff-mic.wav"
    tape_path.write_bytes(tape_bytes)
    registry.register_tape(
        kickoff.id,
        path=str(tape_path),
        sha256=hashlib.sha256(tape_bytes).hexdigest(),
        bytes=len(tape_bytes),
    )
    minutes = workspace / "minutes.md"
    minutes.write_text(
        "# Kickoff\n\n- Align on the raw tracks, reconcile after.\n"
        "- Confirm H4n; retire pre-roll.\n- The archive is the durable copy.\n",
        encoding="utf-8",
    )
    registry.add_artifact(
        kickoff.id,
        kind="minutes",
        path=str(minutes),
        produced_by="agent",
        review_state="accepted",
    )
    run = registry.create_run(
        kickoff.id,
        backend="apple-speech",
        model="whisper-large-v3",
        language="en",
        options={
            "profile": "balanced",
            "decoder_knobs": {"beam_size": 5, "temperature": 0.0},
        },
        origin="console",
    )
    # Finished with the cost record a real run measures (RUN-01): the console
    # derives its speed and duration from these primitives, so without one the
    # run would read unknown wherever it is shown.
    registry.update_run(
        run.id,
        status="done",
        progress={
            "cost": _cost_record(
                stages={
                    "ingest": 42.0,
                    "align": 8.0,
                    "transcribe": 1330.0,
                    "reconcile": 60.0,
                    "export": 50.0,
                },
                audio_seconds=3600.0,
                chunks=120,
                chunks_reused=112,
                wall_seconds=1490.0,
                peak_rss_bytes=3_435_597_824,
            )
        },
    )

    # A meeting that has not produced anything yet: the empty states, the run
    # form and the profile preview.
    registry.create_meeting("q3-sync", "Weekly standup")

    # A managed meeting with a workspace but no transcript yet.
    retro = registry.create_meeting("q3-sync", "Retro")
    retro = registry.set_meeting_workspace(
        retro.id, str(workspace_path_for(root, retro))
    )
    Path(retro.workspace_path).mkdir(parents=True, exist_ok=True)

    # A run the user cancelled (RUN-04): a terminal row with the options it ran
    # with, so the console offers to resume it and states what a resume re-uses.
    cancelled = registry.create_run(
        retro.id,
        backend="apple-speech",
        model="whisper-large-v3",
        language="en",
        options={"profile": "balanced"},
        run_options=dataclasses.asdict(
            PipelineOptions(
                backend="apple-speech",
                model="whisper-large-v3",
                language="en",
                resume=True,
            )
        ),
        origin="console",
    )
    # It stopped mid-transcribe (RUN-04), so its record covers the stages it
    # reached and leaves the rest to nothing — never a guess.
    registry.update_run(
        cancelled.id,
        status="stopped",
        progress={
            "cost": _cost_record(
                stages={
                    "ingest": 40.0,
                    "align": 8.0,
                    "transcribe": 206.0,
                    "reconcile": None,
                    "export": None,
                },
                audio_seconds=612.0,
                chunks=24,
                chunks_reused=16,
                wall_seconds=254.0,
                peak_rss_bytes=None,
            )
        },
    )

    # An archive whose manifest is gone: the lazy status cell's "missing" hue.
    registry.add_archive(
        kickoff.id,
        q3.id,
        root_path=str(data / "archives" / "kickoff"),
        manifest_path=str(data / "archives" / "kickoff" / "manifest.json"),
        manifest_sha256="0" * 64,
    )

    # The status page's live rows (RUN-03). The queue runs one thing at a time,
    # so only the first of these can execute: `e2e/run_owner.py` holds it, and
    # the second stays queued behind it — which is exactly what the page has to
    # show, in two different projects. The running one is started by the owner
    # (through a real RunManager, which is what makes it a *live* run); the
    # queued one is started here, by an agent's surface.
    interview = _managed_meeting(registry, root, "field-interviews", "Interview 04")
    _upload_tape(registry, interview, "interview-04-mic.wav")
    _start_run_owner(registry, data)
    review = _managed_meeting(registry, root, "q3-sync", "Design review")
    _upload_tape(registry, review, "review-mic.wav")
    registry.create_run(
        review.id,
        backend="apple-speech",
        model="whisper-large-v3",
        language="en",
        options={"profile": "balanced"},
        run_options=dataclasses.asdict(
            PipelineOptions(
                backend="apple-speech",
                model="whisper-large-v3",
                language="en",
                resume=True,
            )
        ),
        origin="mcp",
    )

    # A returning user (ticket 04): record the current version so `/` lands on
    # Projects and no update notice shows. The update spec writes a stale marker
    # deliberately, then dismisses it back to current.
    setup.record_seen_version()

    print(f"seeded {data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
