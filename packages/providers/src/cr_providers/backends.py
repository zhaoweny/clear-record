"""Concrete backend adapters for the three supported compute families.

Each backend is a *capability*: its heavy stack is imported lazily inside
``available()`` so that merely importing this module never forces a GPU
framework. If the optional dependency is absent or the runtime probe fails, the
backend reports itself as unavailable and the CLI can proceed (or fail) without
crashing the whole world.
"""

from __future__ import annotations

import importlib.util

from cr_providers.base import Backend, BackendInfo


def _module_importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


class AppleBackend:
    """Apple Silicon: Metal / Core ML / ANE via whisper.cpp."""

    info = BackendInfo(
        id="apple",
        vendor="Apple",
        frameworks=("Metal", "Core ML", "ANE"),
        description="macOS Apple Silicon ASR via whisper.cpp (Metal / Core ML / ANE).",
    )

    def __init__(self) -> None:
        # Filled in lazily; keeps the heavy import out of module import time.
        self._whispercpp: object | None = None

    def available(self) -> bool:
        # whisper.cpp exposes Metal/CoreML through its own runner; the Python
        # bindings may be absent. Probe only the generic pulse: this backend is
        # "advertised" when we are on Darwin; runtime selection happens later.
        import platform

        if platform.system() != "Darwin":
            return False
        return _module_importable("whispercpp") or _module_importable("ctranslate2")

    def transcribe(self, audio_path: str, *, language: str | None = None) -> str:
        raise NotImplementedError(
            "apple backend transcription is not implemented yet; see docs/architecture.md §5"
        )


class NvidiaBackend:
    """NVIDIA: CUDA / cuBLAS / cuDNN via faster-whisper / CTranslate2."""

    info = BackendInfo(
        id="nvidia",
        vendor="NVIDIA",
        frameworks=("CUDA", "cuBLAS", "cuDNN"),
        description="NVIDIA CUDA ASR via faster-whisper / CTranslate2.",
    )

    def available(self) -> bool:
        return _module_importable("ctranslate2")

    def transcribe(self, audio_path: str, *, language: str | None = None) -> str:
        raise NotImplementedError(
            "nvidia backend transcription is not implemented yet; see docs/architecture.md §5"
        )


class AmdBackend:
    """AMD Radeon: ROCm / Vulkan via whisper.cpp (e.g. gfx1100)."""

    info = BackendInfo(
        id="amd",
        vendor="AMD",
        frameworks=("ROCm", "Vulkan"),
        description="AMD Radeon ASR via whisper.cpp (ROCm / Vulkan, e.g. gfx1100).",
    )

    def available(self) -> bool:
        # ROCm and Vulkan paths are runtime-selected; probe the vendor lib.
        return _module_importable("whispercpp")

    def transcribe(self, audio_path: str, *, language: str | None = None) -> str:
        raise NotImplementedError(
            "amd backend transcription is not implemented yet; see docs/architecture.md §5"
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
