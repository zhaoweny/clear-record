"""Concrete backend adapters for the three supported compute families.

Each backend is a *capability*: a cheap probe in ``available()`` decides whether
the machine can run it, and any heavy stack is touched only inside
``transcribe()``. If a dependency or the runtime probe is missing, the backend
reports itself unavailable and the CLI proceeds without crashing.

Mapping to compute families (ADR-0005):

- ``apple``  — Apple Silicon via ``whisper.cpp`` (Metal / Core ML / ANE),
  preferring the *system* ``whisper-cli`` (Homebrew ``whisper-cpp`` + a ggml
  ``libggml-metal`` plugin) and falling back to the in-process
  ``pywhispercpp`` wheel.
- ``nvidia`` — NVIDIA via the *system* ``whisper-cli`` (CUDA / Vulkan),
  Linux-only probe.
- ``amd``    — AMD Radeon via the *system* ``whisper-cli`` (Vulkan / ROCm),
  Linux-only probe.

All three now share one process-isolated code path: no PyPI wheel ships a
GPU-accelerated ggml backend, so each drives a system ``whisper-cli`` linked
against the system ``ggml``, which loads a GPU backend as a plugin (``ggml-cuda``
/ ``ggml-vulkan`` / ``ggml-hip`` on Linux; ``ggml-metal`` on macOS). The probe
checks for an accepted plugin and, on Linux, the vendor's GPU device — not just
an import. Apple keeps ``pywhispercpp`` as a fallback so a pip-only Mac still
works.
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

    Uses the ``pywhispercpp`` wheel. It is now the *fallback* on Apple: the
    preferred path is the system ``whisper-cli`` + ggml Metal plugin (see
    :class:`_WhisperCliBackend`), which is process-isolated. The wheel is kept
    so a Mac that only has the pip extra still works.
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
# system ``whisper-cli`` adapter (Apple Metal / AMD / NVIDIA via ggml plugins)
# --------------------------------------------------------------------------- #
# PyPI's ``pywhispercpp`` wheels are CPU-only, so Apple, AMD and NVIDIA shell out
# to the system's ``whisper-cli`` (Homebrew ``whisper-cpp`` on macOS; e.g. Arch
# ``whisper-cpp`` on Linux), which links the system ``ggml`` and loads a GPU
# backend plugin — ``ggml-metal`` for Apple, ``ggml-vulkan``/``ggml-hip`` for
# AMD, ``ggml-cuda``/``ggml-vulkan`` for NVIDIA. A backend is only offered when
# an accepted plugin (and, on Linux, the vendor's GPU device) is present.
_WHISPER_CLI_CANDIDATES = ("whisper-cli", "whisper-cpp")
# Homebrew bin dirs as a fallback when Homebrew's prefix is absent from PATH
# (Apple Silicon first, then Intel). `CR_WHISPER_CLI` and PATH still win.
_WHISPER_CLI_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
# Arch/Fedora install plugin `.so`s directly under a `ggml` dir; Debian/Ubuntu
# put them under the multiarch tuple. macOS/Homebrew installs the ggml backend
# plugins under `libexec` (not `lib`), reachable via the stable `opt/` symlink
# or the versioned Cellar. Missing a layout here is a false negative (the probe
# rejects a machine that can actually run the backend), so list them all.
_GGML_BACKEND_DIRS = (
    "/usr/lib/ggml",
    "/usr/lib64/ggml",
    "/usr/local/lib/ggml",
    "/usr/lib/x86_64-linux-gnu/ggml",
    "/usr/lib/aarch64-linux-gnu/ggml",
    # macOS / Homebrew (Apple Silicon prefix, then Intel prefix).
    "/opt/homebrew/lib",
    "/opt/homebrew/libexec",
    "/opt/homebrew/lib/ggml",
    "/opt/homebrew/opt/ggml/lib",
    "/opt/homebrew/opt/ggml/libexec",
    "/opt/homebrew/Cellar/ggml/*/lib",
    "/opt/homebrew/Cellar/ggml/*/libexec",
    "/usr/local/lib",
    "/usr/local/libexec",
    "/usr/local/lib/ggml",
    "/usr/local/opt/ggml/lib",
    "/usr/local/opt/ggml/libexec",
    "/usr/local/Cellar/ggml/*/lib",
    "/usr/local/Cellar/ggml/*/libexec",
)
# One or more glob patterns per family. Metal is a dynamic backend plugin on
# macOS; Homebrew ships it as `libggml-metal.so` under `libexec`, while other
# builds may produce a `.dylib` or embed the `.metal` library.
_GGML_BACKEND_PATTERNS = {
    "vulkan": ("libggml-vulkan*.so*",),
    "hip": ("libggml-hip*.so*",),  # whisper.cpp's ROCm/HIP plugin
    "cuda": ("libggml-cuda*.so*",),
    "metal": (
        "libggml-metal*.so*",
        "libggml-metal*.dylib",
        "ggml-metal.metal",
    ),
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
    for directory in _WHISPER_CLI_BIN_DIRS:
        for name in _WHISPER_CLI_CANDIDATES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _ggml_backend_dirs() -> tuple[str, ...]:
    """Where to look for ggml backend plugins.

    ``CR_GGML_BACKEND_DIRS`` (``os.pathsep``-separated) is prepended, so a
    from-source ``whisper.cpp`` build — or a non-standard distro/brew layout —
    can point the probe at its own ``libggml-*.{so,dylib}`` directory.
    """
    extra = os.environ.get("CR_GGML_BACKEND_DIRS", "")
    return tuple(p for p in extra.split(os.pathsep) if p) + _GGML_BACKEND_DIRS


def _find_ggml_gpu_backend(families: tuple[str, ...]) -> str | None:
    """Path to a ggml GPU backend plugin for any accepted backend family."""
    for family in families:
        patterns = _GGML_BACKEND_PATTERNS.get(family)
        if not patterns:
            continue
        for pattern in patterns:
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

    The document structure is validated here so a malformed ``-ojf`` payload
    raises a clear ``RuntimeError`` naming the backend instead of leaking an
    ``AttributeError`` from an unexpected ``entry``/``token``/``text`` shape
    (``text`` must be a ``str`` or ``None``). A single segment with non-numeric
    timestamps is skipped (a per-segment glitch), but structurally wrong
    containers and field types are a hard error.
    """
    if not isinstance(entries, list):
        raise RuntimeError(
            f"whisper-cli ({source}) returned unexpected JSON: 'transcription' "
            f"must be a list, got {type(entries).__name__}."
        )
    out: list[Segment] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"whisper-cli ({source}) returned a non-object transcription "
                f"entry: {entry!r}."
            )
        offsets = entry.get("offsets") or {}
        if not isinstance(offsets, dict):
            raise RuntimeError(
                f"whisper-cli ({source}) returned non-object 'offsets': {offsets!r}."
            )
        try:
            start = float(offsets.get("from", 0.0)) / 1000.0
            end = float(offsets.get("to", 0.0)) / 1000.0
        except (TypeError, ValueError):
            continue
        tokens = entry.get("tokens") or []
        if not isinstance(tokens, list):
            raise RuntimeError(
                f"whisper-cli ({source}) returned non-list 'tokens': {tokens!r}."
            )
        probs: list[float] = []
        for token in tokens:
            if not isinstance(token, dict):
                raise RuntimeError(
                    f"whisper-cli ({source}) returned a non-object token: {token!r}."
                )
            if isinstance(token.get("p"), (int, float)) and not _is_special_token(
                str(token.get("text", ""))
            ):
                probs.append(float(token["p"]))
        confidence = sum(probs) / len(probs) if probs else None
        text = entry.get("text")
        if text is not None and not isinstance(text, str):
            raise RuntimeError(
                f"whisper-cli ({source}) returned non-string 'text': {text!r}."
            )
        segment = _make_segment(
            start,
            end,
            text,
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


def _load_whispercli_json(path: str, backend_id: str, stdout: str) -> dict:
    """Read the ``-ojf`` output, turning every failure into a clear error.

    whisper.cpp's argument parser calls ``exit(0)`` on an unknown flag, so a
    ``whisper-cli`` without ``-ojf``/``--prompt`` "succeeds" while writing no
    file. Read defensively so a bare ``FileNotFoundError`` / ``JSONDecodeError``
    / ``UnicodeDecodeError`` never escapes; every failure names the backend and
    the cause.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        tail = (stdout or "").strip()[-800:]
        raise RuntimeError(
            f"whisper-cli ({backend_id}) produced no readable -ojf JSON at "
            f"{path!r}: {exc}. The installed whisper-cli may not support "
            f"-ojf/--prompt (usage: {tail!r})."
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"whisper-cli ({backend_id}) produced unparseable JSON at {path!r}: {exc}."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"whisper-cli ({backend_id}) returned unexpected JSON (expected an "
            f"object, got {type(data).__name__})."
        )
    return data


class _WhisperCliBackend:
    """System ``whisper-cli`` backend, accelerated by a ggml backend plugin.

    Subclasses declare which ggml backend families they accept, how to probe the
    vendor's GPU device, and which OS they run on, so Apple, AMD and NVIDIA share
    one process-isolated code path (and the same parallel transcription).

    Residual risk in ``available()``: the probe proves that a ``whisper-cli``
    binary and a matching ``libggml-*`` *file* are present plus (on Linux) a
    vendor device, but not that this build can *load* the plugin (a ggml
    ABI/build mismatch still passes). Such a build warns on stderr and may
    silently fall back to CPU; the probe cannot tell without a model-dependent
    run, which would make ``available()`` expensive. The failure surfaces at
    ``transcribe()`` time, where a missing/invalid ``-ojf`` result now raises a
    clear ``RuntimeError`` instead of an unhelpful ``FileNotFoundError``.
    """

    def __init__(
        self,
        info: BackendInfo,
        *,
        gpu_backends: tuple[str, ...],
        device_check: Callable[[], bool],
        system: str = "Linux",
    ) -> None:
        self.info = info
        self._gpu_backends = gpu_backends
        self._device_check = device_check
        self._system = system

    def available(self) -> bool:
        return (
            platform.system() == self._system
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
                    f"whisper-cli ({self.info.id}) failed (exit "
                    f"{proc.returncode}): {tail}"
                )
            data = _load_whispercli_json(
                out_prefix + ".json", self.info.id, proc.stdout or ""
            )

        result = data.get("result")
        if result is not None and not isinstance(result, dict):
            raise RuntimeError(
                f"whisper-cli ({self.info.id}) returned non-object 'result': "
                f"{result!r}."
            )
        detected = (result or {}).get("language") or ("" if lang == "auto" else lang)
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


def _has_metal_device() -> bool:
    """Metal is present on every macOS host (Apple Silicon and recent Intel).

    There is no DRM/``nvidia-smi`` equivalent to probe: the Metal plugin file
    plus the ``whisper-cli`` binary is the capability check, so this is a
    constant-true device predicate (see :class:`_WhisperCliBackend`).
    """
    return True


class AppleBackend:
    """Apple Silicon: Metal / Core ML / ANE via whisper.cpp.

    Prefers the same process-isolated system ``whisper-cli`` path as AMD and
    NVIDIA — a Homebrew ``whisper-cpp`` links the system ``ggml`` and loads the
    ``libggml-metal`` plugin. When the CLI or the Metal plugin is missing, it
    falls back to the in-process ``pywhispercpp`` wheel, so a pip-only Mac still
    works. The backend id stays ``apple`` either way.
    """

    def __init__(self) -> None:
        self.info = BackendInfo(
            id="apple",
            vendor="Apple",
            frameworks=("Metal", "Core ML", "ANE"),
            description="macOS Apple Silicon ASR via whisper.cpp "
            "(system whisper-cli + ggml/Metal, or in-process pywhispercpp).",
        )
        self._inprocess = _WhisperCppBackend(self.info)
        self._cli = _WhisperCliBackend(
            self.info,
            gpu_backends=("metal",),
            device_check=_has_metal_device,
            system="Darwin",
        )

    def available(self) -> bool:
        return platform.system() == "Darwin" and (
            self._cli.available() or self._inprocess.available()
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
        backend = self._cli if self._cli.available() else self._inprocess
        return backend.transcribe(
            audio_path,
            language=language,
            model=model,
            model_dir=model_dir,
            initial_prompt=initial_prompt,
        )


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
