"""The clear-record domain model (dependency-free).

These are the pure data types and their (de)serialization. This module must stay
free of numpy/GPU/framework imports. Audio loading, alignment and merging are
the job of ``clear_record.engine``; ASR is the job of
``clear_record.providers``; wiring is the job of ``clear_record.pipeline``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Source:
    """A single recording that contributes to a record."""

    id: str
    path: str
    label: str = ""
    clock_domain: str = "wall"
    sample_rate: int | None = None
    channels: int | None = None


@dataclass(frozen=True)
class Alignment:
    """Estimated timing relationship of each source to a reference on a common
    timebase. ``offsets[sid]`` is **reference_time - source_time** (seconds), so
    ``ref_time = source_time + offsets[sid]``."""

    reference: str
    offsets: dict[str, float]
    method: str = "cross-correlation"
    confidence: float | None = None
    # Sources alignment could not place. They are deliberately absent from
    # ``offsets`` so downstream stages do not read a fake ``0.0`` as "aligned".
    unresolved: tuple[str, ...] = ()


@dataclass(frozen=True)
class Segment:
    """One attributed unit of transcript in reference-timeline time (seconds)."""

    start: float
    end: float
    text: str
    source: str
    speaker: str = ""
    confidence: float | None = None
    language: str = ""


@dataclass(frozen=True)
class TranscriptionResult:
    """The raw ASR output for a single source (source-local timestamps)."""

    source: str
    segments: tuple[Segment, ...]
    language: str = ""
    backend: str = ""
    model: str = ""
    audio_duration: float | None = None


@dataclass(frozen=True)
class RecordDocument:
    """The reconciled record: many recordings -> one attributable timeline."""

    sources: tuple[Source, ...]
    alignment: Alignment | None
    segments: tuple[Segment, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# --------------------------------------------------------------------------- #
# (De)serialization helpers
# --------------------------------------------------------------------------- #


def _from_dataclass(cls: type, data: Mapping[str, Any]):
    """Build a frozen dataclass from a dict, skipping unknown keys."""
    names = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in names})


def source_from_dict(data: Mapping[str, Any]) -> Source:
    return _from_dataclass(Source, data)


def alignment_from_dict(data: Mapping[str, Any]) -> Alignment:
    return _from_dataclass(Alignment, data)


def segment_from_dict(data: Mapping[str, Any]) -> Segment:
    return _from_dataclass(Segment, data)


def record_from_dict(data: Mapping[str, Any]) -> RecordDocument:
    return RecordDocument(
        sources=tuple(source_from_dict(s) for s in data.get("sources", [])),
        alignment=(
            alignment_from_dict(data["alignment"]) if data.get("alignment") else None
        ),
        segments=tuple(segment_from_dict(s) for s in data.get("segments", [])),
        metadata=dict(data.get("metadata", {})),
        created=data.get("created", ""),
    )


def to_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)


def write_json(path: Path, obj: Any) -> None:
    payload = obj if isinstance(obj, (dict, list)) else to_dict(obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "Source",
    "Alignment",
    "Segment",
    "TranscriptionResult",
    "RecordDocument",
    "source_from_dict",
    "alignment_from_dict",
    "segment_from_dict",
    "record_from_dict",
    "to_dict",
    "write_json",
    "load_json",
]
