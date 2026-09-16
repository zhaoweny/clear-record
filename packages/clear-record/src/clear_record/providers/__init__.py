"""clear_record.providers: per-vendor ASR backend adapters for clear-record.

Vendor stacks are selected behind a single ``Backend`` interface and are never
imported by ``clear_record.core``. A backend is *available* only if its optional
extra is installed and its runtime probe succeeds; otherwise the CLI reports it
as unavailable rather than failing the whole pipeline.
"""

from __future__ import annotations

from clear_record.providers.apple_speech import (
    AppleSpeechBackend,
    AppleSpeechError,
    AppleSpeechHelper,
    AppleSpeechUnavailable,
    SpeechProbe,
)
from clear_record.providers.backends import (
    BACKENDS,
    PluginLoadProbe,
    available_backend_ids,
    backend_availability,
    download_ggml_model,
    get_backend,
    probe_ggml_plugin_load,
)
from clear_record.providers.base import (
    APPLE_SPEECH_BACKEND_ID,
    Availability,
    Backend,
    BackendBase,
    BackendId,
    BackendInfo,
    DEFAULT_MODEL,
    RUNTIME_SYSTEM,
    RUNTIME_WHISPER_CLI,
    WINDOWS_AI_BACKEND_ID,
)
from clear_record.providers.paths import resolve_models_dir
from clear_record.providers.process import (
    CancellableProcessRunner,
    ProcessCancelled,
    ProcessRunner,
    SubprocessRunner,
)
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
    "APPLE_SPEECH_BACKEND_ID",
    "BACKENDS",
    "AppleSpeechBackend",
    "AppleSpeechError",
    "AppleSpeechHelper",
    "AppleSpeechUnavailable",
    "Availability",
    "Backend",
    "BackendBase",
    "BackendId",
    "BackendInfo",
    "CancellableProcessRunner",
    "DEFAULT_MODEL",
    "PluginLoadProbe",
    "ProcessCancelled",
    "ProcessRunner",
    "RUNTIME_SYSTEM",
    "RUNTIME_WHISPER_CLI",
    "SpeechProbe",
    "SubprocessRunner",
    "Synthesis",
    "TtsEngine",
    "TtsError",
    "TtsUnavailable",
    "TtsVoice",
    "WINDOWS_AI_BACKEND_ID",
    "available_backend_ids",
    "backend_availability",
    "detect",
    "download_ggml_model",
    "get_backend",
    "probe_ggml_plugin_load",
    "resolve_models_dir",
    "synthesize",
    "synthesize_clip",
]
