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

__all__ = [
    "ASR_SAMPLE_RATE",
    "AudioDecodeError",
    "align_sources",
    "cross_correlate",
    "estimate_offset",
    "prepare_16k_wav",
    "read_audio",
    "reconcile",
    "rms",
]
