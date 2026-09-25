"""clear_record.engine: audio signal processing for clear-record (no vendor/ASR
code)."""

from __future__ import annotations

from clear_record.engine.align import align_sources, estimate_offset
from clear_record.engine.attribute import (
    attribute_by_source,
    attribute_segments,
    attribute_segments_windowed,
)
from clear_record.engine.audio import (
    ASR_SAMPLE_RATE,
    AudioDecodeError,
    channel_count,
    prepare_16k_wav,
    read_audio,
)
from clear_record.engine.chunk import (
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    plan_chunks,
    write_chunk,
)
from clear_record.engine.diarize import diarize
from clear_record.engine.merge import reconcile, source_speaker_names, unplaced_sources
from clear_record.engine.synth import DEFAULT_F0_HZ
from clear_record.engine.synth import SR as SYNTH_SR
from clear_record.engine.synth import (
    make_scene,
    mix_crosstalk,
    record,
)
from clear_record.engine.text import (
    changed_terms,
    clean_segments,
    collapse_repetitions,
    glossary_terms,
    is_non_speech,
    term_could_affect,
)

__all__ = [
    "ASR_SAMPLE_RATE",
    "AudioDecodeError",
    "DEFAULT_CHUNK_S",
    "DEFAULT_F0_HZ",
    "DEFAULT_OVERLAP_S",
    "SYNTH_SR",
    "align_sources",
    "attribute_by_source",
    "attribute_segments",
    "attribute_segments_windowed",
    "changed_terms",
    "channel_count",
    "clean_segments",
    "collapse_repetitions",
    "diarize",
    "estimate_offset",
    "glossary_terms",
    "is_non_speech",
    "make_scene",
    "mix_crosstalk",
    "plan_chunks",
    "prepare_16k_wav",
    "read_audio",
    "reconcile",
    "record",
    "source_speaker_names",
    "term_could_affect",
    "unplaced_sources",
    "write_chunk",
]
