"""cr-engine: audio signal processing for clear-record (no vendor/ASR code)."""

from __future__ import annotations

from cr_engine.align import align_sources, cross_correlate, estimate_offset
from cr_engine.audio import (
    ASR_SAMPLE_RATE,
    AudioDecodeError,
    channel_count,
    prepare_16k_wav,
    read_audio,
    rms,
)
from cr_engine.chunk import DEFAULT_CHUNK_S, DEFAULT_OVERLAP_S, plan_chunks, write_chunk
from cr_engine.diarize import diarize, logmel_stats
from cr_engine.merge import reconcile
from cr_engine.synth import SR as SYNTH_SR
from cr_engine.synth import make_scene, record
from cr_engine.text import clean_segments, collapse_repetitions, is_non_speech

__all__ = [
    "ASR_SAMPLE_RATE",
    "AudioDecodeError",
    "DEFAULT_CHUNK_S",
    "DEFAULT_OVERLAP_S",
    "SYNTH_SR",
    "align_sources",
    "channel_count",
    "clean_segments",
    "collapse_repetitions",
    "cross_correlate",
    "diarize",
    "estimate_offset",
    "is_non_speech",
    "logmel_stats",
    "make_scene",
    "plan_chunks",
    "prepare_16k_wav",
    "read_audio",
    "reconcile",
    "record",
    "rms",
    "write_chunk",
]
