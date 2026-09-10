"""End-to-end tests for the non-ASR pipeline stages on synthetic audio.

These exercise ingest -> align -> reconcile -> export + calibrate_report without
requiring an ASR backend, so they run in any environment (including CI without a
GPU framework). The ASR-requiring `transcribe` stage is tested separately when a
backend is available (see the @skip conditions in test_transcriber).
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from cr_cli import stages


def _write_tone(
    path, sr: int = 8000, seconds: float = 6.0, freq: float = 220.0
) -> None:
    # aperiodic chirp so cross-correlation alignment is unambiguous
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    f1 = freq * 6.0
    phase = 2 * np.pi * (freq * t + (f1 - freq) * t * t / (2.0 * seconds))
    sig = (0.4 * np.sin(phase)).astype(np.float32)
    sf.write(str(path), sig, sr)


def _workspace(tmp_path) -> str:
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", freq=220.0)
    _write_tone(wd / "b.wav", freq=220.0)
    return str(wd)


def test_ingest_align_reconcile_export(tmp_path) -> None:
    wd = _workspace(tmp_path)
    sources = stages.ingest(wd)
    assert len(sources) == 2
    alignment = stages.align(wd)
    assert alignment.reference == sources[0].id

    # Seed segments "by hand" (transcribe stage is backend-gated).
    from cr_core import Segment

    per_source = {
        sources[0].id: [
            Segment(
                start=0.0,
                end=1.5,
                text="first line",
                source=sources[0].id,
                confidence=0.9,
            )
        ],
        sources[1].id: [
            Segment(
                start=0.0,
                end=1.5,
                text="first line",
                source=sources[1].id,
                confidence=0.6,
            )
        ],
    }
    from cr_cli import workspace as ws

    ws.write_segments(
        tmp_path / "rec", per_source, {"backend": "none", "model": "none"}
    )

    record = stages.reconcile(wd)
    assert record.segments and record.segments[0].speaker == sources[0].label

    written = stages.export(wd)
    assert "md" in written and written["md"].exists()

    report = stages.calibrate_report(wd)
    assert report["coverage"] is not None


def test_ingest_splits_multichannel_sources(tmp_path) -> None:
    """A 4-channel meeting/DJI capture becomes four per-channel sources (auto),
    and `--mix-down` collapses it to one."""
    sr = 8000
    t = np.arange(int(3.0 * sr), dtype=np.float64) / sr
    chans = [np.sin(2 * np.pi * f * t).astype(np.float32) for f in (200, 300, 400, 500)]
    quad = tmp_path / "meeting.wav"
    sf.write(str(quad), np.stack(chans, axis=1), sr)

    wd = tmp_path / "sess"
    wd.mkdir()
    (wd / "meeting.wav").unlink(missing_ok=True)
    import shutil

    shutil.copy(str(quad), str(wd / "meeting.wav"))

    split_sources = stages.ingest(str(wd), split="auto")
    assert len(split_sources) == 4
    assert all(s.id.endswith(("ch1", "ch2", "ch3", "ch4")) for s in split_sources)

    mixed = stages.ingest(str(wd), split="mix")
    assert len(mixed) == 1
    assert not mixed[0].id.endswith("ch1")


def test_transcribe_chunks_resume_and_glossary_invalidation(
    tmp_path, monkeypatch
) -> None:
    """Chunked transcription is resumable, and adding a glossary invalidates the
    chunk cache so a background first pass can be corrected."""
    import soundfile as sf

    from cr_core import Segment, TranscriptionResult
    from cr_providers import BackendInfo

    from cr_cli import stages

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    class Fake:
        calls = 0
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
        )

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
        ):
            type(self).calls += 1
            data, file_sr = sf.read(audio_path)
            dur = len(data) / file_sr
            return TranscriptionResult(
                source="fake",
                segments=(
                    Segment(
                        start=0.0,
                        end=round(dur, 3),
                        text="chunk",
                        source="fake",
                        confidence=0.5,
                    ),
                ),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=dur,
            )

    fake = Fake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fake)

    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    first = Fake.calls
    assert first > 1  # the 8 s tape was chunked

    # resume: every chunk cached -> no backend calls
    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    assert Fake.calls == first

    # a new glossary invalidates the cache -> re-transcribes
    stages.glossary(str(wd), add=["Zhaoweny"])
    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    assert Fake.calls > first
