"""The vendor-neutral ASR backend interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from clear_record.core import TranscriptionResult

from clear_record.providers.process import ProcessRunner

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
    # True when independent `transcribe()` calls run in separate OS processes
    # (e.g. a subprocess CLI) and may therefore be executed concurrently.
    # In-process backends that share model state keep this False so the
    # pipeline stays sequential and thread-safe.
    parallelizable: bool = False


class Backend(Protocol):
    """A live, callable ASR backend.

    ``available()`` performs a cheap runtime probe (platform + import). It must
    stay cheap enough to call on every CLI invocation and must **never** import a
    heavy GPU framework on module import.
    """

    info: BackendInfo

    def available(self) -> bool: ...

    def prepare(self, model: str | None, model_dir: str | None) -> str | None:
        """Prepare this backend's model before transcription (or no-op).

        A backend that owns model selection/resolution/download — e.g. the
        ``whisper-cli`` adapters' ggml resolve/download — implements this and
        returns the resolved model path. One with nothing to prepare inherits
        the no-op default in :class:`BackendBase` and returns ``None``. Callers
        invoke it once, single-threaded, before fanning out so workers never
        race a first-use download.
        """
        ...

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
        initial_prompt: str | None = None,
        process_runner: ProcessRunner | None = None,
    ) -> TranscriptionResult:
        """Transcribe ``audio_path`` and return timestamped segments.

        ``language`` is the BCP-47-ish whisper hint, or ``None``/"auto" to
        auto-detect. ``model`` selects the checkpoint name/size (or a path).
        ``model_dir`` is where to download/read model weights. ``initial_prompt``
        biases decoding toward a glossary of names/terms. ``process_runner``
        optionally overrides how the backend launches its CLI, so a caller can
        scope cancellation to its own children (see ``clear_record.providers.process``).
        """
        ...


class BackendBase:
    """Concrete no-op defaults for the optional parts of :class:`Backend`.

    ``Backend`` is a structural ``Protocol``, so a backend that only satisfies
    ``info`` / ``available()`` / ``transcribe()`` would still be missing
    ``prepare`` at the call site. Inherit this for the no-op default, and
    override ``prepare`` when the backend owns a downloadable model.
    """

    def prepare(self, model: str | None, model_dir: str | None) -> str | None:
        return None


__all__ = ["Backend", "BackendBase", "BackendId", "BackendInfo", "DEFAULT_MODEL"]
