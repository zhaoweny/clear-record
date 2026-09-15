"""The service layer's seam to the text-to-speech provider.

The layering DAG (ADR-0012) lets :mod:`clear_record.service` import
:mod:`clear_record.cli` but not :mod:`clear_record.providers`, so the provider
is reached the same way :mod:`clear_record.service.auto` reaches backend
availability: through a thin re-export in the ``cli`` layer. This module adds no
behaviour of its own; it is the one bridge from the setup service to the system
TTS engines.
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
