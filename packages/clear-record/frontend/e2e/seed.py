"""Seed a deterministic console for the Playwright suite.

`just e2e` runs this before `playwright test`: it wipes `CR_DATA_DIR` and writes
one realistic project — meetings with and without a managed workspace, a
reconciled transcript, artifacts, a finished run, an archive, and glossary terms
in every status — so the visual specs capture real content instead of empty
states.

The seed uses the service layer directly (the same `Registry` the console opens),
so it cannot drift from the app's own schema.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

from clear_record.core import RecordDocument, Segment, write_json
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


def main() -> int:
    data = Path(os.environ["CR_DATA_DIR"]).expanduser().resolve()
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
    )
    registry.update_run(run.id, status="done")

    # A meeting that has not produced anything yet: the empty states, the run
    # form and the profile preview.
    registry.create_meeting("q3-sync", "Weekly standup")

    # A managed meeting with a workspace but no transcript yet.
    retro = registry.create_meeting("q3-sync", "Retro")
    retro = registry.set_meeting_workspace(
        retro.id, str(workspace_path_for(root, retro))
    )
    Path(retro.workspace_path).mkdir(parents=True, exist_ok=True)

    # An archive whose manifest is gone: the lazy status cell's "missing" hue.
    registry.add_archive(
        kickoff.id,
        q3.id,
        root_path=str(data / "archives" / "kickoff"),
        manifest_path=str(data / "archives" / "kickoff" / "manifest.json"),
        manifest_sha256="0" * 64,
    )

    print(f"seeded {data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
