"""The vendor-neutral ASR backend interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

# The three supported compute families (owner voice, 2026-09-09).
BackendId = str


@dataclass(frozen=True)
class BackendInfo:
    """Static metadata describing a backend capability (not a live backend)."""

    id: BackendId
    vendor: str
    frameworks: tuple[str, ...]
    description: str


class Backend(Protocol):
    """A live, callable ASR backend.

    ``available()`` may perform a runtime probe (e.g. import the optional
    framework, query GPUs, or touch the Metal device). It must be cheap enough
    to call on every CLI invocation.
    """

    info: BackendInfo

    def available(self) -> bool: ...

    def transcribe(self, audio_path: str, *, language: str | None = None) -> str:
        """Return the best-effort transcript for ``audio_path``.

        ``language`` is optional; a backend may auto-detect when omitted.
        """
        ...


class BackendRegistry:
    """A small name -> backend factory map."""

    def __init__(self) -> None:
        self._factories: dict[str, type] = {}

    def register(self, id: BackendId, factory: type) -> None:
        self._factories[id] = factory

    def ids(self) -> tuple[BackendId, ...]:
        return tuple(sorted(self._factories))

    def get(self, id: BackendId) -> Backend:
        if id not in self._factories:
            raise KeyError(f"unknown ASR backend {id!r}")
        return self._factories[id]()

    def __contains__(self, id: BackendId) -> bool:
        return id in self._factories

    def __len__(self) -> int:
        return len(self._factories)


_global: BackendRegistry = BackendRegistry()


def register(id: BackendId, factory: type) -> None:
    """Register a backend factory in the process-wide registry."""
    _global.register(id, factory)


# Re-exported typing helper for callers that read backend metadata from a
# registry: mapping BackendId -> static metadata.
BackendCatalog = Mapping[BackendId, BackendInfo]
