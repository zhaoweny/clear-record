"""The vendor-neutral ASR backend interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cr_core import TranscriptionResult

BackendId = str

# Multilingual checkpoint used unless overridden. Multilingual (non-`.en`) models
# handle Chinese/English mixed speech (owner voice: mixed-language meetings).
DEFAULT_MODEL = "small"


@dataclass(frozen=True)
class BackendInfo:
    """Static metadata describing a backend capability (not a live backend)."""

    id: BackendId
    vendor: str
    frameworks: tuple[str, ...]
    description: str
    default_model: str = DEFAULT_MODEL


class Backend(Protocol):
    """A live, callable ASR backend.

    ``available()`` performs a cheap runtime probe (platform + import). It must
    stay cheap enough to call on every CLI invocation and must **never** import a
    heavy GPU framework on module import.
    """

    info: BackendInfo

    def available(self) -> bool: ...

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
    ) -> TranscriptionResult:
        """Transcribe ``audio_path`` and return timestamped segments.

        ``language`` is the BCP-47-ish whisper hint, or ``None``/"auto" to
        auto-detect. ``model`` selects the checkpoint name/size (or a path).
        ``model_dir`` is where to download/read model weights.
        """
        ...


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


__all__ = ["Backend", "BackendId", "BackendInfo", "DEFAULT_MODEL", "_importable"]
