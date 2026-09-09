"""Concrete backend adapters for the three supported compute families.

Each backend is a *capability*: its heavy stack is imported lazily inside
``transcribe()`` (and only probed cheaply in ``available()``), so merely
importing this module never forces a GPU framework. If the optional dependency
is absent or the runtime probe fails, the backend reports itself as unavailable
and the CLI can proceed without crashing the whole world.

Mapping to compute families (ADR-0005):

- ``apple``  — Apple Silicon via ``whisper.cpp`` (Metal / Core ML / ANE).
- ``nvidia`` — NVIDIA via ``faster-whisper`` / CTranslate2 (CUDA / cuBLAS / cuDNN).
- ``amd``    — AMD Radeon via ``whisper.cpp`` (ROCm / Vulkan), Linux-only probe.
"""

from __future__ import annotations

import math
import os
import platform

from cr_core import Segment, TranscriptionResult

from cr_providers.base import Backend, BackendInfo, _importable


def _time_scale(segments, duration: float | None) -> float:
    """Choose the divisor that turns pywhispercpp ``t0/t1`` values into seconds.

    whisper.cpp reports segment timestamps in fixed sub-second units that differ
    across bindings/versions (milliseconds vs 10 ms). Because the caller already
    knows the audio duration, we pick the scale whose total segment span best
    matches it:
      - span_t = max(t1) / 1000  -> seconds if units are milliseconds
      - span_t = max(t1) / 100   -> seconds if units are 10 ms
    When duration is unknown we default to the 10 ms convention (pywhispercpp).
    """
    if not segments:
        return 100.0
    last = max(float(getattr(s, "t1", 0)) for s in segments)
    if duration and last > 0:
        # If ms -> seconds is already >= half the audio, ms is correct.
        if (last / 1000.0) >= duration * 0.5:
            return 1000.0
        return 100.0
    return 100.0


def _whispercpp_segments(
    segs, source: str, language: str, duration: float | None = None
) -> tuple[Segment, ...]:
    """Normalize ``pywhispercpp`` Segment objects into core ``Segment``."""
    scale = _time_scale(list(segs), duration)
    out: list[Segment] = []
    for s in segs:
        start = float(getattr(s, "t0", getattr(s, "start", 0.0))) / scale
        end = float(getattr(s, "t1", getattr(s, "end", start))) / scale
        if end < start:
            start, end = end, start
        conf = getattr(s, "probability", None)
        if conf is None:
            conf = getattr(s, "prob", None)
        if conf is None:
            conf = getattr(s, "p", None)
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        out.append(
            Segment(
                start=round(start, 3),
                end=round(end, 3),
                text=text,
                source=source,
                confidence=float(conf) if conf is not None else None,
                language=language,
            )
        )
    return tuple(out)


def _whispercpp_available() -> bool:
    return _importable("pywhispercpp")


class _WhisperCppBackend:
    """Shared whisper.cpp backend (used by both apple and amd)."""

    def __init__(self, info: BackendInfo) -> None:
        self.info = info
        self._model = None

    def available(self) -> bool:
        return _whispercpp_available()

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
    ) -> TranscriptionResult:
        from pywhispercpp.model import Model  # lazy

        name = model or self.info.default_model
        if self._model is None:
            threads = max(2, os.cpu_count() or 2)
            self._model = Model(
                name,
                models_dir=model_dir,
                n_threads=threads,
                print_realtime=False,
                print_progress=False,
            )
        # Detect (or honour) the language, then pin it for a stable transcript.
        if language in (None, "", "auto"):
            detected = _detect_whispercpp_language(self._model, audio_path)
        else:
            detected = language
        segs = self._model.transcribe(
            audio_path,
            language=detected,
            extract_probability=True,
        )
        duration = _whispercpp_duration(audio_path)
        segments = _whispercpp_segments(
            segs, source=self.info.id, language=detected, duration=duration
        )
        return TranscriptionResult(
            source=self.info.id,
            segments=segments,
            language=detected,
            backend=self.info.id,
            model=name,
            audio_duration=duration,
        )


def _detect_whispercpp_language(model, audio_path: str, default: str = "") -> str:
    """Best-effort language auto-detection (returns ``default`` on any failure)."""
    try:
        if not hasattr(model, "auto_detect_language"):
            return default
        result = model.auto_detect_language(audio_path)
        return result[0][0] if result and result[0] else default
    except Exception:
        return default


def _whispercpp_duration(audio_path: str) -> float | None:
    try:
        import soundfile as sf

        data, sr = sf.read(audio_path, dtype="float32")
        return float(len(data) / sr)
    except Exception:  # non-fatal (e.g. ffmpeg-only format)
        return None


class AppleBackend(_WhisperCppBackend):
    """Apple Silicon: Metal / Core ML / ANE via whisper.cpp."""

    def __init__(self) -> None:
        super().__init__(
            BackendInfo(
                id="apple",
                vendor="Apple",
                frameworks=("Metal", "Core ML", "ANE"),
                description="macOS Apple Silicon ASR via whisper.cpp (Metal / Core ML / ANE).",
            )
        )

    def available(self) -> bool:
        return platform.system() == "Darwin" and _whispercpp_available()


class AmdBackend(_WhisperCppBackend):
    """AMD Radeon on Linux: ROCm / Vulkan via whisper.cpp."""

    def __init__(self) -> None:
        super().__init__(
            BackendInfo(
                id="amd",
                vendor="AMD",
                frameworks=("ROCm", "Vulkan"),
                description="AMD Radeon ASR via whisper.cpp (ROCm / Vulkan, e.g. gfx1100).",
            )
        )

    def available(self) -> bool:
        return platform.system() == "Linux" and _whispercpp_available()


class NvidiaBackend:
    """NVIDIA: CUDA / cuBLAS / cuDNN via faster-whisper / CTranslate2."""

    info = BackendInfo(
        id="nvidia",
        vendor="NVIDIA",
        frameworks=("CUDA", "cuBLAS", "cuDNN"),
        description="NVIDIA CUDA ASR via faster-whisper / CTranslate2.",
        default_model="small",
    )

    def __init__(self) -> None:
        self._model = None

    def available(self) -> bool:
        if not _importable("faster_whisper"):
            return False
        import shutil

        return shutil.which("nvidia-smi") is not None

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
    ) -> TranscriptionResult:
        from faster_whisper import WhisperModel  # lazy

        name = model or self.info.default_model
        if self._model is None:
            self._model = WhisperModel(
                name, device="cuda", compute_type="float16", download_root=model_dir
            )
        lang = language if language and language != "auto" else None
        seg_iter, info = self._model.transcribe(
            audio_path, language=lang, vad_filter=True, beam_size=5
        )
        segments: list[Segment] = []
        for s in seg_iter:
            conf = None
            logprob = getattr(s, "avg_logprob", None)
            if logprob is not None:
                conf = float(math.exp(max(-6.0, min(0.0, logprob))))
            text = (s.text or "").strip()
            if not text:
                continue
            segments.append(
                Segment(
                    start=round(float(s.start), 3),
                    end=round(float(s.end), 3),
                    text=text,
                    source="nvidia",
                    confidence=conf,
                    language=getattr(info, "language", "") or (lang or ""),
                )
            )
        return TranscriptionResult(
            source="nvidia",
            segments=tuple(segments),
            language=getattr(info, "language", "") or (lang or ""),
            backend="nvidia",
            model=name,
            audio_duration=getattr(info, "duration", None),
        )


# The advertised catalog in stable order (backends may be unavailable at runtime).
BACKENDS: dict[str, Backend] = {
    "apple": AppleBackend(),
    "nvidia": NvidiaBackend(),
    "amd": AmdBackend(),
}


def available_backend_ids() -> tuple[str, ...]:
    """Backend ids whose runtime probe currently succeeds, in catalog order."""
    return tuple(bid for bid, backend in BACKENDS.items() if backend.available())


def get_backend(backend_id: str) -> Backend:
    try:
        return BACKENDS[backend_id]
    except KeyError as exc:  # pragma: no cover - trivial guard
        raise KeyError(f"unknown ASR backend {backend_id!r}") from exc


__all__ = [
    "BACKENDS",
    "available_backend_ids",
    "get_backend",
]
