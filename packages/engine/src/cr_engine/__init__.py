"""cr-engine: audio signal processing for clear-record (no vendor/ASR code)."""

from __future__ import annotations

from cr_engine.align import align_sources, cross_correlate, estimate_offset
from cr_engine.audio import (
    ASR_SAMPLE_RATE,
    AudioDecodeError,
    prepare_16k_wav,
    read_audio,
    rms,
)
from cr_engine.merge import reconcile
from cr_engine.synth import SR as SYNTH_SR
from cr_engine.synth import make_scene, record

__all__ = [
    "ASR_SAMPLE_RATE",
    "AudioDecodeError",
    "SYNTH_SR",
    "align_sources",
    "cross_correlate",
    "estimate_offset",
    "make_scene",
    "prepare_16k_wav",
    "read_audio",
    "reconcile",
    "record",
    "rms",
]
