"""Stages report structured progress, and the CLI's text output is unchanged.

The stages run for real against synthetic tones (no ASR backend needed), so the
event sequence is observed end to end rather than mocked.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from clear_record.pipeline import stages
from clear_record.pipeline.workspace import Workspace
from clear_record.core import JobEvent, RunCancelled, Segment, Step, log_path
from clear_record.pipeline.stages import format_timestamp


def _write_tone(
    path,
    sr: int = 8000,
    seconds: float = 6.0,
    freq: float = 220.0,
    gain: float = 0.4,
) -> None:
    """One device's recording of the chirp.

    A second source is the *same* chirp at another ``gain``: two recordings of
    one scene correlate (so ``align`` places them) while their bytes differ, so
    ingest does not fold the pair into one source (ticket 218 — byte-identical
    inputs are one source).
    """
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    f1 = freq * 6.0
    phase = 2 * np.pi * (freq * t + (f1 - freq) * t * t / (2.0 * seconds))
    sf.write(str(path), (gain * np.sin(phase)).astype(np.float32), sr)


def test_stages_emit_progress_and_leave_stdout_to_the_command_surface(
    tmp_path, capsys
) -> None:
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    _write_tone(wd / "b.wav", gain=0.25)

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources
    stages.align(str(wd), on_event=events.append)

    # `transcribe` is backend-gated, so seed segments by hand (as the CLI tests do).
    Workspace.at(wd).write_segments(
        {
            source.id: [Segment(start=0.0, end=1.0, text="hello", source=source.id)]
            for source in sources
        },
        {"backend": "none", "model": "none"},
    )
    stages.reconcile(str(wd), on_event=events.append)
    stages.export(str(wd), on_event=events.append)

    by_stage: dict[str, list[JobEvent]] = {}
    for event in events:
        by_stage.setdefault(event.stage, []).append(event)

    assert set(by_stage) == {"ingest", "align", "reconcile", "export"}
    for stage, stage_events in by_stage.items():
        assert stage_events[-1].done, stage
        assert stage_events[-1].index == stage_events[-1].total, stage
        indices = [event.index for event in stage_events]
        assert indices == sorted(indices), stage

    assert by_stage["ingest"][-1].total == 2
    assert by_stage["export"][-1].total == 4

    # The stages write nothing to stdout themselves: every word a *stage* reports
    # is a message on the sink — its mid-stage lines, its summary, its data rows
    # (the pin for the command's bytes is ``test_stage_stdout``; the surface's own
    # end lines are not the stage's).
    assert capsys.readouterr().out == ""


def test_each_ingest_decode_line_arrives_before_its_own_work(
    tmp_path, monkeypatch
) -> None:
    """An ingest decode line is reported where that decode begins.

    The command surface printed each line *before* normalizing that input; once
    the stage stopped printing and returned its inputs as report data instead,
    the whole batch of lines came back with the report and was rendered after
    the pass. A stage's own line travels on its sink as an event whose
    ``message`` is the text — the one channel the command surface prints from —
    one per decode, in file order — so a stage that holds them until it returns,
    or a surface that renders them afterwards, fails the interleaving pinned
    here.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    for index, name in enumerate(("a.wav", "b.wav", "c.wav")):
        # A different tone each: ingest folds byte-identical inputs into one
        # source (ticket 218), and this pins one decode line *per file*.
        _write_tone(wd / name, freq=220.0 + 110.0 * index)

    timeline: list[str] = []
    work = stages.prepare_16k_wav

    def announcing(source, dest, **kwargs):
        timeline.append(f"work {Path(source).name}")
        return work(source, dest, **kwargs)

    monkeypatch.setattr(stages, "prepare_16k_wav", announcing)

    events: list[JobEvent] = []

    class Sink:
        def __call__(self, event: JobEvent) -> None:
            events.append(event)
            # What the command surface does with the event: print its message.
            if event.message:
                timeline.append(event.message)

    stages.ingest(str(wd), on_event=Sink())

    # Every decode line precedes its own input's work, as it did when the
    # command surface printed it.
    assert timeline == [
        "[ingest] decode a.wav -> a.wav",
        "work a.wav",
        "[ingest] decode b.wav -> b.wav",
        "work b.wav",
        "[ingest] decode c.wav -> c.wav",
        "work c.wav",
        # And the pass's summary and source table close the stage, in that order.
        f"[ingest] 3 source(s) -> {wd / 'manifest.json'}",
        f"  a                        {wd / 'audio' / 'a.wav'}",
        f"  b                        {wd / 'audio' / 'b.wav'}",
        f"  c                        {wd / 'audio' / 'c.wav'}",
    ]
    # The decode lines are the stage's events, with their source and counters —
    # not a print beside them: the console's run row and a client reading the
    # stream read the same payload the surface prints.
    decode_events = [e for e in events if e.message.startswith("[ingest] decode")]
    assert [e.message for e in decode_events] == [
        "[ingest] decode a.wav -> a.wav",
        "[ingest] decode b.wav -> b.wav",
        "[ingest] decode c.wav -> c.wav",
    ]
    assert [e.source for e in decode_events] == ["a", "b", "c"]
    # A progress report carries the counters and no words, so a client that
    # prints every message prints exactly the stage's lines and never a blank
    # one: the pass's four reports (the opening 0, one per file) are wordless.
    assert [e.index for e in events if not e.message] == [0, 1, 2, 3]


def test_run_threads_the_sink_to_every_stage(tmp_path, monkeypatch) -> None:
    received = []

    def fake_runner(directory, options, on_event, cancel=None) -> None:
        received.append(on_event)

    monkeypatch.setattr(stages, "_STAGE_RUNNERS", {step: fake_runner for step in Step})
    sink = lambda event: None  # noqa: E731 - trivial sink

    stages.run(str(tmp_path), on_event=sink)

    assert len(received) == len(list(Step))
    assert all(handler is sink for handler in received)


def test_a_sink_that_stops_the_run_stops_the_pipeline(tmp_path, monkeypatch) -> None:
    """A sink raising ``RunCancelled`` unwinds the pipeline where it was called.

    This is how a run's cancel reaches the work (RUN-04): every stage announces
    itself through the sink, and the run queue's sink raises there when the run's
    signal is set. The stage announcing itself is the last thing that runs — the
    stages after it are never entered — and the exception reaches the caller
    rather than being recorded as a failure.
    """
    ran: list[str] = []
    events: list[JobEvent] = []

    def fake_runner(directory, options, on_event, cancel=None) -> None:
        ran.append("ran")
        on_event(JobEvent(stage="ingest", index=0, total=1))

    monkeypatch.setattr(stages, "_STAGE_RUNNERS", {step: fake_runner for step in Step})

    def stopping_sink(event: JobEvent) -> None:
        events.append(event)
        raise RunCancelled("the run was cancelled")

    with pytest.raises(RunCancelled):
        stages.run(str(tmp_path), on_event=stopping_sink)

    assert ran == ["ran"], "the announcing stage ran; the rest never started"
    assert events and events[0].stage == "ingest"

    # A cancellation is an outcome, not a failed pipeline: the log says the run
    # stopped, and nothing reports the pipeline as having failed.
    log = log_path().read_text(encoding="utf-8")
    assert "cli.run.stopped" in log
    assert "cli.run.failed" not in log


def test_the_channel_carries_the_lines_and_the_data_items(tmp_path) -> None:
    """One payload: every line a stage reports, and every item a pass produced.

    The backend-free stages are driven end to end over one workspace, and the
    messages a client renders are collected. A pass's mid-stage lines come first,
    then its summary and the rows its own report holds — the text and the order
    the command surface prints, from the same events (the bytes themselves are
    pinned by ``tests/cli/test_stage_stdout.py``). Each expected line is built
    from the value the stage returned, so this asserts the *carriage*: a line, or
    a data item that stays on the returned report, fails here.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    _write_tone(wd / "b.wav", gain=0.25)

    events: list[JobEvent] = []
    ingest_report = stages.ingest(str(wd), on_event=events.append)
    sources = ingest_report.sources
    align = stages.align(str(wd), on_event=events.append)
    glossary_report = stages.glossary(
        str(wd), add=["Clear Record"], on_event=events.append
    )
    # `transcribe` is backend-gated, so seed segments by hand (as the CLI tests do).
    Workspace.at(wd).write_segments(
        {
            source.id: [Segment(start=0.0, end=1.0, text="hello", source=source.id)]
            for source in sources
        },
        {"backend": "none", "model": "none"},
    )
    stages.diarize(str(wd), on_event=events.append)
    attribute_report = stages.attribute(str(wd), on_event=events.append)
    record = stages.reconcile(str(wd), on_event=events.append)
    written = stages.export(str(wd), on_event=events.append)

    assert [event.message for event in events if event.message] == [
        # ingest: the decode lines where they happened, then the summary and the
        # source table the report holds.
        "[ingest] decode a.wav -> a.wav",
        "[ingest] decode b.wav -> b.wav",
        f"[ingest] {len(ingest_report.sources)} source(s) -> {wd / 'manifest.json'}",
        *[f"  {source.id:24s} {source.path}" for source in ingest_report.sources],
        # align: where every source landed, and the reference.
        f"[align] reference={align.reference} method={align.method} "
        f"conf={align.confidence} unresolved={len(align.unresolved)}",
        *[
            f"  {sid:24s} offset={offset:+.4f}s"
            + (" (ref)" if sid == align.reference else "")
            for sid, offset in align.offsets.items()
        ],
        # glossary: the file, and its terms in order.
        f"[glossary] {glossary_report.path} ({len(glossary_report.terms)} term(s))",
        *[f"  {term}" for term in glossary_report.terms],
        # diarize: one line per source, reported as each is decided.
        "[diarize] a: 1 speaker(s) over 1 segment(s)",
        "[diarize] b: 1 speaker(s) over 1 segment(s)",
        # attribute: what the pass measured.
        f"[attribute] {attribute_report.segments} segment(s), "
        f"{attribute_report.speakers} speaker(s), "
        f"{attribute_report.changed} re-attributed",
        # reconcile: the record, then its opening segments.
        f"[reconcile] {len(record.segments)} segment(s), "
        f"{len({seg.speaker for seg in record.segments})} attributed speaker(s) -> "
        f"{wd / 'record.json'}",
        *[
            f"  {format_timestamp(seg.start)} [{seg.speaker or seg.source}] {seg.text}"
            for seg in record.segments
        ],
        # export: one line per artifact, where it is written.
        *[f"[export] {fmt:4s} -> {path}" for fmt, path in written.items()],
    ]
    # A row carries the source it is about, so a client can group them.
    assert {event.source for event in events if event.message.startswith("  b ")} == {
        "b"
    }


def test_a_row_carries_the_source_it_is_about(tmp_path) -> None:
    """A row carries the source it is about.

    The channel's rows are per item: the segment's source for the record's
    preview, the format for each exported artifact. The event states it, so a
    client can group the rows without parsing their text. The export rows are
    also paired with the wordless advance beside them — both carry the format,
    which is ``_report_written``'s own pairing — while the preview's closing
    report speaks for the whole pass and carries no source, so there the row's
    ``source`` is the segment's alone.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    _write_tone(wd / "b.wav", gain=0.25)

    events: list[JobEvent] = []
    sources = stages.ingest(str(wd), on_event=events.append).sources
    Workspace.at(wd).write_segments(
        {
            source.id: [Segment(start=0.0, end=1.0, text="hello", source=source.id)]
            for source in sources
        },
        {"backend": "none", "model": "none"},
    )

    events.clear()
    record = stages.reconcile(str(wd), on_event=events.append)
    stages.export(str(wd), on_event=events.append)

    # The record's preview rows are the record's own segments, and each carries
    # the source that segment came from.
    preview = [
        event
        for event in events
        if event.stage == "reconcile" and event.message.startswith("  ")
    ]
    assert [event.source for event in preview] == [
        seg.source for seg in record.segments[: len(preview)]
    ]
    # Each format's line rides the same source as the advance that closed it:
    # the wordless report, then the row, both naming that format.
    assert [
        (event.source, bool(event.message))
        for event in events
        if event.stage == "export"
    ] == [
        (None, False),
        ("md", False),
        ("md", True),
        ("srt", False),
        ("srt", True),
        ("vtt", False),
        ("vtt", True),
        ("json", False),
        ("json", True),
    ]
