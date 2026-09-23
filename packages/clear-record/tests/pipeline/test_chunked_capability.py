"""A ``chunked=False`` backend is handed each source once (ADR-0019).

A whole-file/streaming OS service is not the whisper chunk pipeline: the seam's
``BackendInfo.chunked`` lets it say so, and the pipeline reuses the per-source
chunk cache as the coarse progress/resume unit rather than splitting a tape.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from clear_record.pipeline.transcription import TranscriptionOptions, transcribe
from clear_record.pipeline.workspace import Workspace
from clear_record.core import Segment, Source, TranscriptionResult
from clear_record.providers import BackendInfo


class _WholeFileBackend:
    """A streaming backend that must see the whole source, once."""

    calls = 0
    info = BackendInfo(
        id="apple-speech",
        vendor="Apple",
        frameworks=("Speech",),
        description="fake system backend",
        default_model="system",
        runtime="system",
        parallelizable=False,
        chunked=False,
    )

    def transcribe(self, audio_path, **kwargs):
        type(self).calls += 1
        data, sr = sf.read(audio_path)
        duration = len(data) / sr
        return TranscriptionResult(
            source="apple-speech",
            segments=(Segment(0.0, round(duration, 3), "whole", "a"),),
            language="en",
            backend="apple-speech",
            model="system",
            audio_duration=duration,
        )


def test_unchunked_backend_receives_the_source_once(tmp_path) -> None:
    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    wav = wd / "a.wav"
    sf.write(str(wav), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr)

    source = Source(id="a", path=str(wav))
    # A 3 s chunk plan would split this 8 s tape; the capability overrides it.
    result = transcribe(
        [source],
        _WholeFileBackend(),
        TranscriptionOptions(chunk_seconds=3.0, overlap_seconds=1.0),
        workspace=Workspace.at(wd),
    )

    assert _WholeFileBackend.calls == 1
    assert result.source_meta["a"]["chunks"] == 1
    assert result.per_source["a"]
