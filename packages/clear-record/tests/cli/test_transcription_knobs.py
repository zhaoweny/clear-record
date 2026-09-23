"""Decoder knobs through the resumable transcription seam.

The knobs travel ``TranscriptionOptions`` → backend command, and a backend that
cannot honour one must fail loudly. The chunk cache keys on them, so switching
profile re-decodes instead of reusing the other profile's chunks.
"""

from __future__ import annotations

import inspect

import numpy as np
import pytest
import soundfile as sf

from clear_record.pipeline import stages
from clear_record.pipeline.transcription import TranscriptionOptions, transcribe
from clear_record.pipeline.workspace import Workspace, chunk_cache_key
from clear_record.core import (
    DECODER_KNOB_FIELDS,
    DECODER_KNOBS,
    Segment,
    Source,
    TranscriptionResult,
)
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


class _EchoAllKnobsBackend(BackendBase):
    """Advertises every declared decoder knob, so a test can prove the whole
    declaration reaches the backend rather than the one knob other tests here
    happen to set."""

    info = BackendInfo(
        id="echo-all",
        vendor="test",
        frameworks=(),
        description="echoes every declared decoder knob",
        decoder_knobs=DECODER_KNOB_FIELDS,
    )
    seen: dict = {}

    def available(self) -> bool:
        return True

    def transcribe(self, audio_path, **kwargs):
        type(self).seen = kwargs
        return TranscriptionResult(
            source="echo-all",
            segments=(Segment(0.0, 0.5, "hello", "echo-all"),),
            language="en",
            backend="echo-all",
            model="echo-all",
            audio_duration=1.0,
        )


def test_the_stage_takes_every_declared_decoder_knob() -> None:
    """The command hands the stage each declared knob by name; the stage's
    keyword list is its own public API (a caller may set a knob directly), so it
    cannot be derived — this is the pin that keeps the two in step."""
    signature = inspect.signature(stages.transcribe)

    assert {knob.name for knob in DECODER_KNOBS} <= set(signature.parameters)


def test_transcription_options_carry_every_declared_decoder_knob() -> None:
    """The stage's options type restates none of the knobs: it inherits the
    declaration's decoder fields, and each one round-trips through the converter
    the row names."""
    requested = {knob.name: knob.convert("3") for knob in DECODER_KNOBS}

    options = TranscriptionOptions(**requested)

    assert options.decoder_knobs() == requested
    assert TranscriptionOptions().decoder_knobs() == {}


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


def test_the_stage_turns_an_unsupported_knob_into_a_systemexit(
    tmp_path, monkeypatch
) -> None:
    """The CLI must name the unsupported knob, not traceback out of the stage."""
    wd = tmp_path / "rec"
    wd.mkdir()
    _wav(wd / "a.wav")
    stages.ingest(str(wd), split="mix")
    monkeypatch.setattr(stages, "get_backend", lambda _id: _NoKnobBackend())

    with pytest.raises(SystemExit, match="cannot honour"):
        stages.transcribe(str(wd), "noknob", beam_size=4)


def test_the_unsupported_knob_message_is_translated(tmp_path, monkeypatch) -> None:
    """The frame is translated; the backend id and knob names stay verbatim."""
    from clear_record.core import i18n

    wd = tmp_path / "rec"
    wd.mkdir()
    _wav(wd / "a.wav")
    stages.ingest(str(wd), split="mix")
    monkeypatch.setattr(stages, "get_backend", lambda _id: _NoKnobBackend())

    i18n.install("zh_CN")
    with pytest.raises(SystemExit) as err:
        stages.transcribe(str(wd), "noknob", beam_size=4)

    message = str(err.value)
    assert "无法支持解码器选项" in message
    assert "noknob" in message and "beam_size" in message
    assert "cannot honour" not in message
    assert "It supports:" not in message


def test_every_declared_decoder_knob_reaches_the_backend_through_the_stage(
    tmp_path, monkeypatch
) -> None:
    """Generalizes ``test_supported_decoder_knob_reaches_the_backend`` (which
    only ever set ``beam_size``) to the whole declaration: ``stages.transcribe``
    builds its own ``TranscriptionOptions`` from ``DECODER_KNOB_FIELDS`` rather
    than restating each name, so every knob the declaration lists — not just
    the one this file happened to name first — reaches the backend."""
    wd = tmp_path / "rec"
    wd.mkdir()
    _wav(wd / "a.wav")
    stages.ingest(str(wd), split="mix")
    monkeypatch.setattr(stages, "get_backend", lambda _id: _EchoAllKnobsBackend())

    values = {knob.name: knob.convert("3") for knob in DECODER_KNOBS}
    _EchoAllKnobsBackend.seen = {}
    stages.transcribe(str(wd), "echo-all", **values)

    assert {name: _EchoAllKnobsBackend.seen[name] for name in values} == values


def test_a_decoder_knob_set_through_run_options_reaches_the_stage(
    tmp_path, monkeypatch
) -> None:
    """``run``/``calibrate`` hand a decoder knob to the stage through
    ``PipelineOptions`` and ``_run_transcribe`` (not through ``transcribe``'s own
    keyword arguments) — a path no test exercised with a decoder knob set
    before this one. ``_run_transcribe`` derives its forwarding from
    ``options.decoder_knobs()`` rather than restating each name, so this proves
    the whole declaration reaches the stage from that side too."""
    wd = tmp_path / "rec"
    wd.mkdir()
    _wav(wd / "a.wav")

    seen: dict = {}

    def spy_transcribe(directory, backend_id, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(stages, "transcribe", spy_transcribe)
    monkeypatch.setattr(stages, "align", lambda *a, **k: None)
    monkeypatch.setattr(stages, "diarize", lambda *a, **k: None)
    monkeypatch.setattr(stages, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(stages, "export", lambda *a, **k: None)

    values = {knob.name: knob.convert("3") for knob in DECODER_KNOBS}
    stages.run(str(wd), stages.PipelineOptions(backend="echo-all", **values))

    assert {name: seen[name] for name in values} == values


def test_a_declared_knob_the_stage_signature_lacks_fails_loudly(
    tmp_path, monkeypatch
) -> None:
    """The declaration is the one source, so a knob it grows that the stage's
    own signature has not followed must fail here rather than be dropped.

    This is the property the ticket exists for: before the stage derived its
    forwarding from ``DECODER_KNOB_FIELDS``, a knob declared but not spelled in
    ``stages.transcribe``'s keyword list simply never reached ``TranscriptionOptions``
    — the value was accepted, resolved, and silently lost. Now the stage raises.
    """
    wd = tmp_path / "rec"
    wd.mkdir()
    _wav(wd / "a.wav")
    stages.ingest(str(wd), split="mix")

    monkeypatch.setattr(
        stages, "DECODER_KNOB_FIELDS", (*DECODER_KNOB_FIELDS, "synthetic_knob")
    )

    with pytest.raises(KeyError) as raised:
        stages.transcribe(str(wd), "echo-all")

    assert "synthetic_knob" in str(raised.value)
