"""A record cannot *silently* mix Han scripts (ticket 224).

Nothing in the pipeline rewrites a script, and the transcription paths do not
agree on one: whisper.cpp's ``-l zh`` writes Mandarin in Traditional characters
where Apple's on-device transcriber writes Simplified. A workspace that changes
backend between sources (``--rerun-source``), or re-decodes part of a source
(``--rerun-range``, an interrupted run), therefore used to interleave the two
with no marker at all. The stage now records the scripts each source's text
actually shows in the record and names them on the console whenever that is not
uniform — a difference between two sources or inside one.

The Mandarin below is hand-written, never field content (ADR-0006).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from clear_record.core import JobEvent, Segment, TranscriptionResult
from clear_record.pipeline import stages
from clear_record.pipeline.workspace import Workspace
from clear_record.providers import BackendBase, BackendInfo

TRADITIONAL = "這個對象規則標籤"
SIMPLIFIED = "这个对象规则标签"


def _pass_rows(events: list[JobEvent]) -> list[str]:
    """The pass's per-source lines (the rows under its summary line)."""
    return [event.message for event in events if event.message.startswith("  ")]


def _write_tone(path: Path, *, seconds: float = 6.0, freq: float = 220.0) -> None:
    sample_rate = 16000
    t = np.arange(int(seconds * sample_rate), dtype=np.float64) / sample_rate
    sf.write(
        str(path),
        (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32),
        sample_rate,
    )


def _install_backend(monkeypatch, text_by_source: dict[str, str]) -> None:
    """Point the stage at a backend that answers each source with its own text.

    The audio path a chunk is handed lies under the source's own chunk-cache
    directory, which is how a fake backend tells two sources apart; the mapping
    is read per call, so a test can change what a re-decode returns.
    """

    class _ScriptedBackend(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
        )

        def available(self) -> bool:
            return True

        def transcribe(self, audio_path, **kwargs):
            source_id = Path(audio_path).parent.name
            data, sample_rate = sf.read(audio_path)
            duration = len(data) / sample_rate
            return TranscriptionResult(
                source=source_id,
                segments=(
                    Segment(
                        0.0,
                        round(duration, 3),
                        text_by_source[source_id],
                        source_id,
                    ),
                ),
                language="zh",
                backend="fake",
                model="fake",
                audio_duration=duration,
            )

    monkeypatch.setattr(stages, "get_backend", lambda _id: _ScriptedBackend())


def _ingest_two(tmp_path, text_by_source: dict[str, str], monkeypatch):
    """Ingest two 6 s sources and install the scripted backend over them."""
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav")
    _write_tone(wd / "b.wav", freq=300.0)
    stages.ingest(str(wd), split="mix")
    _install_backend(monkeypatch, text_by_source)
    return wd


def _transcribe(tmp_path, text_by_source: dict[str, str], monkeypatch):
    """Ingest two 6 s sources, then transcribe them, and return the events."""
    wd = _ingest_two(tmp_path, text_by_source, monkeypatch)
    events: list[JobEvent] = []
    stages.transcribe(
        str(wd),
        "fake",
        chunk_seconds=3.0,
        overlap_seconds=1.0,
        jobs=1,
        on_event=events.append,
    )
    return wd, events


def test_two_sources_in_different_scripts_are_named(tmp_path, monkeypatch) -> None:
    """The mixed case: what each source shows is in the record, and the console
    names them — a column on the per-source rows and one warning."""
    wd, events = _transcribe(tmp_path, {"a": TRADITIONAL, "b": SIMPLIFIED}, monkeypatch)

    _per_source, meta = Workspace.at(wd).load_segments()
    assert meta["sources"]["a"]["scripts"] == ["traditional"]
    assert meta["sources"]["b"]["scripts"] == ["simplified"]

    rows = _pass_rows(events)
    (row_a,) = [row for row in rows if row.strip().startswith("a ")]
    (row_b,) = [row for row in rows if row.strip().startswith("b ")]
    assert row_a.endswith("script=traditional"), row_a
    assert row_b.endswith("script=simplified"), row_b

    (warning,) = [
        event for event in events if "Han script is not uniform" in event.message
    ]
    assert warning.level == "warn"
    assert "a=traditional" in warning.message
    assert "b=simplified" in warning.message
    # The line is the newest on the run's stream, so it carries the pass's tally
    # (two sources of three chunks): the console's bar must not step back.
    assert (warning.index, warning.total, warning.reused) == (6, 6, 0)


def test_sources_in_one_script_are_recorded_without_a_column(
    tmp_path, monkeypatch
) -> None:
    """The common case stays quiet: what each source shows is in the record, no
    row grows a column and no warning is raised — only a *difference* is news."""
    wd, events = _transcribe(
        tmp_path, {"a": SIMPLIFIED, "b": "这个规则对象"}, monkeypatch
    )

    _per_source, meta = Workspace.at(wd).load_segments()
    assert meta["sources"]["a"]["scripts"] == ["simplified"]
    assert meta["sources"]["b"]["scripts"] == ["simplified"]

    rows = _pass_rows(events)
    assert rows and all("script=" not in row for row in rows)
    assert not [
        event for event in events if "Han script is not uniform" in event.message
    ]


def test_a_source_whose_script_is_unknown_is_not_a_disagreement(
    tmp_path, monkeypatch
) -> None:
    """A source the classifier cannot settle (Latin text, shared characters)
    records nothing rather than being counted as the other script: the stage only
    claims a difference it can show, and the record carries what it does know."""
    wd, events = _transcribe(
        tmp_path, {"a": TRADITIONAL, "b": "hello world"}, monkeypatch
    )

    _per_source, meta = Workspace.at(wd).load_segments()
    assert meta["sources"]["a"]["scripts"] == ["traditional"]
    assert "scripts" not in meta["sources"]["b"]
    assert not [
        event for event in events if "Han script is not uniform" in event.message
    ]


def test_a_scoped_rerun_that_changes_the_script_names_the_reused_source(
    tmp_path, monkeypatch
) -> None:
    """``--rerun-source a`` re-decodes one source (a newer backend/firmware
    writes the other script) while the chunks outside the scope are carried over
    untouched. The run then holds both scripts, so the pass must name them — the
    record says which script each source shows, the reused one included."""
    text = {"a": TRADITIONAL, "b": TRADITIONAL}
    wd = _ingest_two(tmp_path, text, monkeypatch)
    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0, jobs=1)

    # A glossary edit leaves every cached chunk stale, and the scope re-decodes
    # only source ``a``; that decode now comes out in the other script.
    text["a"] = SIMPLIFIED
    Workspace.at(wd).glossary_path.write_text("ACME\n", encoding="utf-8")
    events: list[JobEvent] = []
    stages.transcribe(
        str(wd),
        "fake",
        chunk_seconds=3.0,
        overlap_seconds=1.0,
        jobs=1,
        rerun_sources=("a",),
        on_event=events.append,
    )

    _per_source, meta = Workspace.at(wd).load_segments()
    assert meta["sources"]["a"]["scripts"] == ["simplified"]
    assert meta["sources"]["b"]["scripts"] == ["traditional"]
    (warning,) = [
        event for event in events if "Han script is not uniform" in event.message
    ]
    assert "a=simplified" in warning.message
    assert "b=traditional" in warning.message


def test_one_source_holding_both_scripts_is_named_on_its_own(
    tmp_path, monkeypatch
) -> None:
    """The in-source mix, with no second source to disagree with.

    A range scope re-decodes part of one source (any partial decode does: an
    interrupted run, a ``--rerun-range`` scope), so that source's own text holds
    chunks from before and after the re-decode. The record carries *both* scripts
    for it and the run says so — one label would hide half of what the source
    holds."""
    text = {"a": TRADITIONAL, "b": TRADITIONAL}
    wd = _ingest_two(tmp_path, text, monkeypatch)
    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0, jobs=1)

    # Stale digests (the glossary) + a range scope: only source a's first chunk
    # is re-decoded, and it now comes out Simplified.
    text["a"] = SIMPLIFIED
    Workspace.at(wd).glossary_path.write_text("ACME\n", encoding="utf-8")
    events: list[JobEvent] = []
    stages.transcribe(
        str(wd),
        "fake",
        chunk_seconds=3.0,
        overlap_seconds=1.0,
        jobs=1,
        rerun_sources=("a",),
        rerun_range="0-2",
        on_event=events.append,
    )

    _per_source, meta = Workspace.at(wd).load_segments()
    assert meta["sources"]["a"]["scripts"] == ["simplified", "traditional"]
    assert meta["sources"]["b"]["scripts"] == ["traditional"]

    (warning,) = [
        event for event in events if "Han script is not uniform" in event.message
    ]
    assert "a=simplified+traditional" in warning.message
