"""Concrete backend adapters for the three supported compute families.

Each backend is a *capability*: a cheap probe in ``available()`` decides whether
the machine can run it, and any heavy stack is touched only inside
``transcribe()``. If a dependency or the runtime probe is missing, the backend
reports itself unavailable and the CLI proceeds without crashing.

Mapping to compute families (ADR-0005):

- ``apple``  — Apple Silicon via ``whisper.cpp`` (Metal / Core ML / ANE),
  in process through the ``pywhispercpp`` wheel.
- ``nvidia`` — NVIDIA via the *system* ``whisper-cli`` (CUDA / Vulkan),
  Linux-only probe.
- ``amd``    — AMD Radeon via the *system* ``whisper-cli`` (Vulkan / ROCm),
  Linux-only probe.

NVIDIA and AMD share one process-isolated code path: no PyPI wheel ships a
GPU-accelerated ggml backend, so both drive a distro ``whisper-cli`` linked
against the system ``ggml``, which loads a GPU backend as a plugin (Arch
``ggml-cuda`` / ``ggml-vulkan`` / ``ggml-hip``). The probe checks for an accepted
plugin and the vendor's GPU device, not just an import.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable

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


def _make_segment(
    start: float,
    end: float,
    text: str | None,
    *,
    source: str,
    confidence: float | None,
    language: str,
) -> Segment | None:
    """Build a normalized core ``Segment``, or ``None`` when the text is empty."""
    if end < start:
        start, end = end, start
    text = (text or "").strip()
    if not text:
        return None
    return Segment(
        start=round(start, 3),
        end=round(end, 3),
        text=text,
        source=source,
        confidence=confidence,
        language=language,
    )


def _whispercpp_segments(
    segs, source: str, language: str, duration: float | None = None
) -> tuple[Segment, ...]:
    """Normalize ``pywhispercpp`` Segment objects into core ``Segment``."""
    scale = _time_scale(list(segs), duration)
    out: list[Segment] = []
    for s in segs:
        start = float(getattr(s, "t0", getattr(s, "start", 0.0))) / scale
        end = float(getattr(s, "t1", getattr(s, "end", start))) / scale
        conf = getattr(s, "probability", None)
        if conf is None:
            conf = getattr(s, "prob", None)
        if conf is None:
            conf = getattr(s, "p", None)
        segment = _make_segment(
            start,
            end,
            getattr(s, "text", ""),
            source=source,
            confidence=float(conf) if conf is not None else None,
            language=language,
        )
        if segment is not None:
            out.append(segment)
    return tuple(out)


def _whispercpp_available() -> bool:
    return _importable("pywhispercpp")


class _WhisperCppBackend:
    """In-process whisper.cpp backend (Apple Metal / Core ML / ANE).

    Uses the ``pywhispercpp`` wheel, whose bundled ggml is CPU-only; this is the
    proven Apple path. AMD does **not** use it — see :class:`_WhisperCliBackend`.
    """

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
        initial_prompt: str | None = None,
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
        params: dict = {"language": detected, "extract_probability": True}
        if initial_prompt:
            params["initial_prompt"] = initial_prompt
        segs = self._model.transcribe(audio_path, **params)
        duration = _audio_duration(audio_path)
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


def _audio_duration(audio_path: str) -> float | None:
    try:
        import soundfile as sf

        data, sr = sf.read(audio_path, dtype="float32")
        return float(len(data) / sr)
    except Exception:  # non-fatal (e.g. ffmpeg-only format)
        return None


# --------------------------------------------------------------------------- #
# system ``whisper-cli`` adapter (AMD / NVIDIA: GPU via ggml backend plugins)
# --------------------------------------------------------------------------- #
# PyPI's ``pywhispercpp`` wheels are CPU-only, so AMD and NVIDIA shell out to
# the distro's ``whisper-cli`` (e.g. Arch ``whisper-cpp``), which links the
# system ``ggml`` and loads a GPU backend plugin — ``ggml-vulkan``/``ggml-hip``
# for AMD, ``ggml-cuda``/``ggml-vulkan`` for NVIDIA. A backend is only offered
# when an accepted plugin and the vendor's GPU device are present.
_WHISPER_CLI_CANDIDATES = ("whisper-cli", "whisper-cpp")
_GGML_BACKEND_DIRS = ("/usr/lib/ggml", "/usr/lib64/ggml", "/usr/local/lib/ggml")
_GGML_BACKEND_PATTERNS = {
    "vulkan": "libggml-vulkan*.so*",
    "hip": "libggml-hip*.so*",  # whisper.cpp's ROCm/HIP plugin
    "cuda": "libggml-cuda*.so*",
}


def _find_whisper_cli() -> str | None:
    """Path to a system ``whisper-cli`` (``CR_WHISPER_CLI`` overrides)."""
    override = os.environ.get("CR_WHISPER_CLI")
    if override and os.path.isfile(override):
        return override
    for name in _WHISPER_CLI_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _ggml_backend_dirs() -> tuple[str, ...]:
    """Where to look for ggml backend plugins.

    ``CR_GGML_BACKEND_DIRS`` (``os.pathsep``-separated) is prepended, so a
    from-source ``whisper.cpp`` build — or a non-Arch distro's layout — can
    point the probe at its own ``libggml-*.so`` directory.
    """
    extra = os.environ.get("CR_GGML_BACKEND_DIRS", "")
    return tuple(p for p in extra.split(os.pathsep) if p) + _GGML_BACKEND_DIRS


def _find_ggml_gpu_backend(families: tuple[str, ...]) -> str | None:
    """Path to a ggml GPU backend plugin for any accepted backend family."""
    for family in families:
        pattern = _GGML_BACKEND_PATTERNS.get(family)
        if not pattern:
            continue
        for directory in _ggml_backend_dirs():
            matches = sorted(glob.glob(os.path.join(directory, pattern)))
            if matches:
                return matches[0]
    return None


_AMD_VENDOR_ID = "0x1002"


def _has_amd_gpu() -> bool:
    """An AMD GPU is present (vendor id 0x1002 on a DRM card/render node)."""
    nodes = glob.glob("/sys/class/drm/renderD*/device/vendor")
    nodes += glob.glob("/sys/class/drm/card[0-9]*/device/vendor")
    for node in nodes:
        try:
            with open(node, encoding="ascii") as fh:
                if fh.read().strip().lower().startswith(_AMD_VENDOR_ID):
                    return True
        except OSError:
            continue
    return False


def _has_nvidia_device() -> bool:
    return (
        bool(glob.glob("/dev/nvidia[0-9]*"))  # native Linux proprietary driver
        or os.path.exists("/dev/dxg")  # WSL2 CUDA passthrough
        or shutil.which("nvidia-smi") is not None
    )


def _is_special_token(text: str) -> bool:
    return text.startswith("[") and text.endswith("]")


def _whispercli_segments(entries, source: str, language: str) -> tuple[Segment, ...]:
    """Normalize ``whisper-cli -ojf`` entries into core ``Segment`` objects.

    ``offsets`` are milliseconds; per-segment confidence is the mean token
    probability (control tokens such as ``[_BEG_]`` are excluded).
    """
    out: list[Segment] = []
    for entry in entries:
        offsets = entry.get("offsets") or {}
        try:
            start = float(offsets.get("from", 0.0)) / 1000.0
            end = float(offsets.get("to", 0.0)) / 1000.0
        except (TypeError, ValueError):
            continue
        probs = [
            float(token["p"])
            for token in (entry.get("tokens") or [])
            if isinstance(token.get("p"), (int, float))
            and not _is_special_token(str(token.get("text", "")))
        ]
        confidence = sum(probs) / len(probs) if probs else None
        segment = _make_segment(
            start,
            end,
            entry.get("text"),
            source=source,
            confidence=confidence,
            language=language,
        )
        if segment is not None:
            out.append(segment)
    return tuple(out)


def _resolve_ggml_model(model: str, model_dir: str | None) -> str:
    """Resolve a model name/size (e.g. ``small``) to a ``ggml-*.bin`` path.

    Accepts an explicit existing path, or a name resolved against ``model_dir``
    (then ``CR_MODELS_DIR``, then ``<cwd>/models``).
    """
    expanded = os.path.expanduser(model)
    if os.path.isfile(expanded):
        return expanded
    base = (
        model_dir
        or os.environ.get("CR_MODELS_DIR")
        or os.path.join(os.getcwd(), "models")
    )
    name = os.path.basename(model)
    if not name.startswith("ggml-"):
        name = f"ggml-{name}"
    if not name.endswith(".bin"):
        name = f"{name}.bin"
    candidate = os.path.join(base, name)
    if os.path.isfile(candidate):
        return candidate
    raise FileNotFoundError(
        f"ggml model not found for {model!r}; looked for {candidate}. "
        f"Download one, e.g. `hf download ggerganov/whisper.cpp {name} "
        f"--local-dir {base}`."
    )


class _WhisperCliBackend:
    """System ``whisper-cli`` backend, GPU-accelerated by a ggml plugin.

    Subclasses declare which ggml backend families they accept and how to probe
    the vendor's GPU device, so AMD and NVIDIA share one process-isolated code
    path (and therefore the same parallel transcription).
    """

    def __init__(
        self,
        info: BackendInfo,
        *,
        gpu_backends: tuple[str, ...],
        device_check: Callable[[], bool],
    ) -> None:
        self.info = info
        self._gpu_backends = gpu_backends
        self._device_check = device_check

    def available(self) -> bool:
        return (
            platform.system() == "Linux"
            and _find_whisper_cli() is not None
            and _find_ggml_gpu_backend(self._gpu_backends) is not None
            and self._device_check()
        )

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
        initial_prompt: str | None = None,
    ) -> TranscriptionResult:
        cli = _find_whisper_cli()
        if cli is None:  # defensive: available() already checked this
            raise RuntimeError("whisper-cli not found on PATH (set CR_WHISPER_CLI).")
        name = model or self.info.default_model
        model_path = _resolve_ggml_model(name, model_dir)
        lang = language if language and language not in ("", "auto") else "auto"

        with tempfile.TemporaryDirectory(prefix="cr-whisper-") as tmp:
            out_prefix = os.path.join(tmp, "out")
            cmd = [
                cli,
                "-m",
                model_path,
                "-f",
                audio_path,
                "-l",
                lang,
                "-ojf",
                "-of",
                out_prefix,
            ]
            if initial_prompt:
                cmd += ["--prompt", initial_prompt]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip()[-800:]
                raise RuntimeError(
                    f"whisper-cli failed (exit {proc.returncode}): {tail}"
                )
            with open(out_prefix + ".json", encoding="utf-8") as fh:
                data = json.load(fh)

        detected = (data.get("result") or {}).get("language") or (
            "" if lang == "auto" else lang
        )
        duration = _audio_duration(audio_path)
        segments = _whispercli_segments(
            data.get("transcription", []), source=self.info.id, language=detected
        )
        return TranscriptionResult(
            source=self.info.id,
            segments=segments,
            language=detected,
            backend=self.info.id,
            model=name,
            audio_duration=duration,
        )


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


class AmdBackend(_WhisperCliBackend):
    """AMD Radeon on Linux: Vulkan / ROCm via the system ``whisper-cli``."""

    def __init__(self) -> None:
        super().__init__(
            BackendInfo(
                id="amd",
                vendor="AMD",
                frameworks=("ROCm", "Vulkan"),
                description="AMD Radeon ASR via the system whisper-cli "
                "(ggml Vulkan/HIP backend, e.g. gfx1100).",
                parallelizable=True,
            ),
            gpu_backends=("vulkan", "hip"),
            device_check=_has_amd_gpu,
        )


class NvidiaBackend(_WhisperCliBackend):
    """NVIDIA on Linux: CUDA / Vulkan via the system ``whisper-cli``."""

    def __init__(self) -> None:
        super().__init__(
            BackendInfo(
                id="nvidia",
                vendor="NVIDIA",
                frameworks=("CUDA", "Vulkan"),
                description="NVIDIA ASR via the system whisper-cli "
                "(ggml CUDA/Vulkan backend).",
                parallelizable=True,
            ),
            gpu_backends=("cuda", "vulkan"),
            device_check=_has_nvidia_device,
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
