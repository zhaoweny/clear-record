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


#: The roles a source can play in attribution, by the name the manifest carries.
#:
#: - ``candidate`` (the default): a recording of one person, so it may be named
#:   as a speaker. Every source is one until something says otherwise.
#: - ``mixed``: a **mixed reference** — a source that hears the whole room
#:   (a room microphone, a mixer's sum) rather than one person. It is a *witness*:
#:   it gates a candidate's claim and is never itself a speaker.
#: - ``excluded``: not a speaker and not a witness either. A microphone nobody
#:   wore picks up whoever is nearest instead of the room, and a duplicate feed
#:   (a phone memo fed by the receiver's downmix of the same mics) carries no
#:   evidence the sources it copies do not already carry — so neither may claim a
#:   segment or refuse one.
#:
#: ``candidate`` and ``mixed`` are the two jobs a source can do — be a speaker, or
#: witness the room — and ``excluded`` is the absence of both: a source that is
#: neither. ``clear_record.core`` does not police the value: a manifest is a
#: document an operator edits, and ``attribute`` — the pass that decides who may be
#: a speaker — refuses a name it does not know rather than quietly treating it as
#: one, while the passes that only carry or honour the value take it as written
#: (``ingest`` carries it across the manifest it rebuilds, ``reconcile`` drops the
#: label fallback for a source that is not a candidate). A run that does not
#: attribute — ``run`` without ``--attribute-energy`` — refuses nothing.
SOURCE_ROLES = ("candidate", "mixed", "excluded")


@dataclass(frozen=True)
class Source:
    """A single recording that contributes to a record."""

    id: str
    path: str
    label: str = ""
    clock_domain: str = "wall"
    sample_rate: int | None = None
    channels: int | None = None
    # The recording's own **declared** start, in seconds, on the common timebase
    # its name states (a recorder that splits one capture into numbered files
    # writes the start time of each part into its name, and ``ingest`` reads it
    # there). ``None`` means nothing declared a start and the source's offset has
    # to be *estimated* from the audio. Of two sources that both declare one,
    # ``align`` places the pair from the difference of the declarations when they
    # cannot overlap or are further apart than its search band, lets the audio
    # decide when they can overlap, and falls back to the declaration when the
    # audio has no verdict.
    start_s: float | None = None
    #: What this recording is to the record, when it is not simply a person's
    #: microphone (see :data:`SOURCE_ROLES`). The default keeps a manifest that
    #: names no role — every workspace written before this field existed — reading
    #: exactly as before: every source a speaker candidate. A **role**, not a
    #: per-pass flag, because what a source *is* does not change between passes:
    #: the room microphone was a witness in the field and is a witness in the
    #: record, whatever a caller names on one command line.
    role: str = "candidate"


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
class UnplacedSource:
    """A source ``reconcile`` left out of the record, and how much of it.

    ``align`` records what it could not place in ``Alignment.unresolved`` and
    writes no offset for it. No offset means no honest position on the reference
    clock, so that source's segments are left out of the timeline instead of
    being stacked on the reference's zero point. The alignment already named
    which sources those are; what this adds is **how much** went with each of
    them, in the artifact a reader opens rather than only in the pass's output at
    stage time, and only for a source that actually had segments to drop — one
    with nothing to place is named by the alignment alone, and by nothing when the
    manifest carries no alignment at all.
    ``segments`` is how many of its segments were dropped and ``speech_s`` the
    speaking time they covered.
    """

    id: str
    segments: int
    speech_s: float


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
    """The reconciled record: many recordings -> one attributable timeline.

    ``metadata`` carries the reconcile pass's own facts about the record. In
    particular ``metadata["unplaced"]`` is the drop summary: one
    :class:`UnplacedSource` (as a plain dict) per source left out for having no
    alignment offset **and holding segments to drop** — so the artifact says what
    the alignment's own ``unresolved`` list does not, how much transcript went
    with each. A source with nothing to place is named by the alignment itself —
    in its ``unresolved`` list, or as a null offset — and by nothing at all when
    the manifest carries no alignment.

    ``metadata["scripts"]`` is the Han scripts each source's text shows, as the
    transcribe stage read them and ``reconcile`` carried them through:
    ``{"<source id>": ["simplified"]}``, both scripts for a source holding one of
    each, and no entry for a source whose text settles neither. It is the list the
    stage recorded in ``segments.json`` (``meta.sources.<id>.scripts``), carried
    into the record so a reader who opens only the artifact sees it too.
    """

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
    "SOURCE_ROLES",
    "Source",
    "Alignment",
    "UnplacedSource",
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
