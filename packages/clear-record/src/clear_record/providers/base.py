"""The vendor-neutral ASR backend interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from clear_record.core import TranscriptionResult
from clear_record.core.message import Message
from clear_record.core.process import ProcessRunner

BackendId = str

# Multilingual checkpoint used unless overridden. Multilingual (non-`.en`) models
# handle Chinese/English mixed speech (owner voice: mixed-language meetings).
DEFAULT_MODEL = "small"

# --- runtime families ------------------------------------------------------- #
# Which substrate a backend drives. The shipped ``apple``/``nvidia``/``amd``
# adapters all drive the system ``whisper-cli`` plus a ggml plugin; a
# system-native backend (Apple ``SpeechTranscriber``, Windows
# ``Microsoft.Windows.AI.Speech``) drives an OS service instead. The runtime
# decides which probes apply: only a ``whisper-cli`` backend exposes ggml plugin
# probes. See ADR-0005's 2026-09-14 Update and ADR-0019.
RUNTIME_WHISPER_CLI = "whisper-cli"
RUNTIME_SYSTEM = "system"

# System-native (non-``whisper-cli``) backend ids. The shipped ids are
# ``apple``/``nvidia``/``amd`` (all ``whisper-cli``), so the native family keeps
# distinct, self-describing names to avoid a silent collision when it registers.
# These are the single definition of the ids; ``pipeline.auto.BACKEND_PREFERENCE``
# consumes them. ``apple-speech`` now names the registered Apple adapter; the
# Windows id is defined but unregistered (deferred, ADR-0019).
APPLE_SPEECH_BACKEND_ID = "apple-speech"
WINDOWS_AI_BACKEND_ID = "windows-ai"


@dataclass(frozen=True)
class Availability:
    """Whether a backend can run here, and (when not) the concrete reason.

    ``available()`` stays the cheap boolean probe; this carries the same verdict
    with the *why* attached, so ``clear-record backends`` can say a backend is
    missing an OS version, a capability, or a provisioned asset rather than only
    that it is unavailable. ``reason`` is a
    :class:`~clear_record.core.message.Message` (an ID plus parameters) the
    boundary renders: the failing check when unavailable, or a summary of how it
    is available (the shipped adapters both do this).
    """

    available: bool
    reason: Message | None = None


@dataclass(frozen=True)
class BackendInfo:
    """Static metadata describing a backend capability (not a live backend)."""

    id: BackendId
    vendor: str
    frameworks: tuple[str, ...]
    description: str
    default_model: str = DEFAULT_MODEL
    # True when independent `transcribe()` calls may run concurrently. This is a
    # per-backend statement, not a property of any one adapter: a process-isolated
    # `whisper-cli` backend sets True; a backend that shares in-process state — or
    # an OS service that serializes — sets False and the pipeline stays
    # sequential (ADR-0005's 2026-09-14 Update).
    parallelizable: bool = False
    # Which substrate this backend drives (one of the ``RUNTIME_*`` values).
    runtime: str = RUNTIME_WHISPER_CLI
    # True when the pipeline may split a source into overlapping chunks. A
    # whole-file/streaming backend (e.g. an OS transcription service) sets False
    # and is handed each source once; the per-source chunk cache still provides
    # coarse progress and resume (ADR-0019).
    chunked: bool = True
    # Which decoder knobs this backend can honour, by their option field name
    # (the decoder rows of ``core.options.RUN_KNOBS``). A requested knob outside
    # this set makes the transcribe stage fail loudly rather than silently drop
    # it; the default is "none", so a backend must opt in explicitly.
    decoder_knobs: tuple[str, ...] = ()

    @property
    def uses_ggml_plugin(self) -> bool:
        """True iff the ``whisper-cli`` ggml-plugin probe applies to this backend."""
        return self.runtime == RUNTIME_WHISPER_CLI


class Backend(Protocol):
    """A live, callable ASR backend.

    ``available()`` performs a cheap runtime probe (platform + import). It must
    stay cheap enough to call on every CLI invocation and must **never** import a
    heavy GPU framework on module import.
    """

    info: BackendInfo

    def available(self) -> bool: ...

    def availability(self) -> Availability:
        """The :meth:`available` verdict plus the concrete reason it is False.

        The boolean is derived from this by :class:`BackendBase`, so a backend
        implements **one** of the pair and both call sites stay honest.
        """
        ...

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
        beam_size: int | None = None,
        best_of: int | None = None,
        temperature: float | None = None,
        entropy_thold: float | None = None,
        no_speech_thold: float | None = None,
        max_context: int | None = None,
        threads: int | None = None,
    ) -> TranscriptionResult:
        """Transcribe ``audio_path`` and return timestamped segments.

        ``language`` is the BCP-47-ish whisper hint, or ``None``/"auto" to
        auto-detect. ``model`` selects the checkpoint name/size (or a path).
        ``model_dir`` is where to download/read model weights. ``initial_prompt``
        biases decoding toward a glossary of names/terms. ``process_runner``
        optionally overrides how the backend launches its CLI, so a caller can
        scope cancellation to its own children (see ``clear_record.core.process``).

        The trailing decoder knobs are optional; a backend advertises which it
        supports via :attr:`BackendInfo.decoder_knobs` and must implement the
        rest. ``None`` means "unset" — the backend's own default applies. The
        knobs a caller may pass are the declaration, ``core.options.RUN_KNOBS``;
        an adapter may accept them as one ``**decoder_knobs`` mapping instead of
        one keyword each, so it need not restate it — the whisper-cli adapter
        does.
        """
        ...


class BackendBase:
    """Concrete defaults for the optional parts of :class:`Backend`.

    ``Backend`` is a structural ``Protocol``, so a backend that only satisfies
    ``info`` / ``available()`` / ``transcribe()`` would still be missing
    ``prepare`` and ``availability`` at the call site. Inherit this for the
    no-op default, and override ``prepare`` when the backend owns a
    downloadable model or a provisioning step.
    """

    def available(self) -> bool:
        """Cheap boolean probe, derived from :meth:`availability` by default."""
        return self.availability().available

    def availability(self) -> Availability:
        """The probe with its reason; adapts a legacy boolean ``available()``.

        A backend implements **one** of this pair. The shipped ``whisper-cli``
        adapters implement ``available()`` and inherit this default (which wraps
        their boolean with no reason); a system backend overrides this method to
        name the OS version/capability/asset it is missing and inherits
        ``available()``.
        """
        if type(self).available is BackendBase.available:
            # Neither method is overridden: fail loudly here rather than recurse.
            raise NotImplementedError(
                f"{type(self).__name__} must implement available() or availability()"
            )
        return Availability(self.available())

    def prepare(self, model: str | None, model_dir: str | None) -> str | None:
        return None


__all__ = [
    "APPLE_SPEECH_BACKEND_ID",
    "Availability",
    "Backend",
    "BackendBase",
    "BackendId",
    "BackendInfo",
    "DEFAULT_MODEL",
    "RUNTIME_SYSTEM",
    "RUNTIME_WHISPER_CLI",
    "WINDOWS_AI_BACKEND_ID",
]
