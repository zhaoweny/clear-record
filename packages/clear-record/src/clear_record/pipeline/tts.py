"""The pipeline layer's seam to the text-to-speech provider.

The layering DAG (ADR-0012) lets :mod:`clear_record.service` import
:mod:`clear_record.pipeline` but not :mod:`clear_record.providers`, so the TTS
engines -- the setup flow's hello-world tape and the ``tts`` leg its acceptance
check reports -- are reached through this layer, beside the model provisioning
in :mod:`clear_record.pipeline.stages`. This module adds no behaviour of its
own; it is the one bridge from the setup service to the system TTS engines, and
the names it re-exports are the provider's own, so the ``TtsUnavailable`` a
caller catches is the exception the provider raised.
"""

from __future__ import annotations

from clear_record.providers.tts import (
    Synthesis,
    TtsEngine,
    TtsError,
    TtsUnavailable,
    TtsVoice,
    detect,
    synthesize,
    synthesize_clip,
)

__all__ = [
    "Synthesis",
    "TtsEngine",
    "TtsError",
    "TtsUnavailable",
    "TtsVoice",
    "detect",
    "synthesize",
    "synthesize_clip",
]
