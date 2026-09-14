"""Decoder knobs through the resumable transcription seam.

The knobs travel ``TranscriptionOptions`` → backend command, and a backend that
cannot honour one must fail loudly. The chunk cache keys on them, so switching
profile re-decodes instead of reusing the other profile's chunks.
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from clear_record.cli.transcription import TranscriptionOptions, transcribe
from clear_record.cli.workspace import Workspace, chunk_cache_key
from clear_record.core import Segment, Source, TranscriptionResult
from clear_record.providers import BackendBase, BackendInfo


def _wav(path) -> str:
    sr = 16000
    t = np.arange(sr, dtype=np.float64) / sr
    sf.write(str(path), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr)
    return str(path)


class _NoKnobBackend(BackendBase):
    info = BackendInfo(
        id="noknob", vendor="test", frameworks=(), description="no decoder knobs"
    )

    def available(self) -> bool:
        return True

    def transcribe(self, audio_path, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the backend must not be called")


class _EchoBackend(BackendBase):
    info = BackendInfo(
        id="echo",
        vendor="test",
        frameworks=(),
        description="echoes its kwargs",
        decoder_knobs=("beam_size",),
    )
    seen: dict = {}

    def available(self) -> bool:
        return True

    def transcribe(self, audio_path, **kwargs):
        type(self).seen = kwargs
        return TranscriptionResult(
            source="echo",
            segments=(Segment(0.0, 0.5, "hello", "echo"),),
            language="en",
            backend="echo",
            model="echo",
            audio_duration=1.0,
        )


def test_unsupported_decoder_knob_fails_loudly(tmp_path) -> None:
    with pytest.raises(ValueError, match="cannot honour"):
        transcribe(
            [],
            _NoKnobBackend(),
            TranscriptionOptions(beam_size=4),
            workspace=Workspace.at(tmp_path),
        )


def test_supported_decoder_knob_reaches_the_backend(tmp_path) -> None:
    _EchoBackend.seen = {}
    wav = _wav(tmp_path / "a.wav")
    source = Source(id="a", path=wav)

    result = transcribe(
        [source],
        _EchoBackend(),
        TranscriptionOptions(beam_size=4, chunk_seconds=600.0),
        workspace=Workspace.at(tmp_path),
    )

    assert result.per_source["a"]
    assert _EchoBackend.seen["beam_size"] == 4
    # An unset knob is not forwarded at all (the backend default applies).
    assert "best_of" not in _EchoBackend.seen


def test_chunk_cache_key_splits_on_a_set_decoder_knob() -> None:
    base = dict(
        backend="amd",
        model="small",
        language=None,
        glossary="",
        chunk_seconds=600.0,
        overlap_seconds=5.0,
        n_chunks=3,
    )
    # Unset: exactly the key the pipeline produced before tunable decoding.
    assert chunk_cache_key(**base) == chunk_cache_key(**base, decoders={})
    # Set: a different key, and different values are different keys.
    assert chunk_cache_key(**base, decoders={"beam_size": 5}) != chunk_cache_key(**base)
    assert chunk_cache_key(**base, decoders={"beam_size": 5}) != chunk_cache_key(
        **base, decoders={"beam_size": 8}
    )
