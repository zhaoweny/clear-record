"""Workspace I/O for clear-record.

A *workspace* is an ordinary directory (typically gitignored, e.g. ``recordings/``
or ``data/<name>/``) that contains the source recordings and the derived record
artifacts. It is intentionally not a database:

    <dir>/
        manifest.json       Sources discovered/declared by `ingest` (+ alignment).
        segments.json       Per-source ASR segments (one file, all sources) + meta.
        record.json         The reconciled RecordDocument.
        export/             Markdown/SRT/VTT/JSON artifacts from `export`.

Audio never has to live *in* the workspace (paths in the manifest are resolved as
written, absolute or relative-to-workspace). Nothing here is ever committed.
"""

from __future__ import annotations

from pathlib import Path

from cr_core import (
    Alignment,
    RecordDocument,
    Segment,
    Source,
    alignment_from_dict,
    load_json,
    record_from_dict,
    segment_from_dict,
    source_from_dict,
    to_dict,
    write_json,
)

MANIFEST = "manifest.json"
SEGMENTS = "segments.json"
RECORD = "record.json"
EXPORT_DIR = "export"

AUDIO_SUFFIXES = {
    ".wav",
    ".flac",
    ".ogg",
    ".opus",
    ".aiff",
    ".aif",
    ".m4a",
    ".mp3",
    ".aac",
}


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_SUFFIXES


def discover_audio(directory: Path) -> list[Path]:
    """Recursively list audio files under ``directory`` (sorted, stable)."""
    return sorted(p for p in directory.rglob("*") if p.is_file() and is_audio(p))


def manifest_path(directory: Path) -> Path:
    return directory / MANIFEST


def segments_path(directory: Path) -> Path:
    return directory / SEGMENTS


def record_path(directory: Path) -> Path:
    return directory / RECORD


def export_dir(directory: Path) -> Path:
    return directory / EXPORT_DIR


def write_manifest(
    directory: Path, sources: list[Source], alignment: Alignment | None = None
) -> None:
    data: dict = {"sources": [to_dict(s) for s in sources]}
    if alignment is not None:
        data["alignment"] = to_dict(alignment)
    write_json(manifest_path(directory), data)


def load_manifest(directory: Path) -> tuple[list[Source], Alignment | None]:
    data = load_json(manifest_path(directory))
    sources = [source_from_dict(s) for s in data["sources"]]
    alignment = (
        alignment_from_dict(data["alignment"]) if data.get("alignment") else None
    )
    return sources, alignment


def write_segments(
    directory: Path,
    per_source: dict[str, list[Segment]],
    meta: dict | None = None,
) -> None:
    payload: dict = {
        "sources": {src: [to_dict(s) for s in segs] for src, segs in per_source.items()}
    }
    if meta:
        payload["meta"] = meta
    write_json(segments_path(directory), payload)


def load_segments(directory: Path) -> tuple[dict[str, list[Segment]], dict]:
    data = load_json(segments_path(directory))
    per_source = {
        src: [segment_from_dict(s) for s in segs]
        for src, segs in data.get("sources", {}).items()
    }
    return per_source, dict(data.get("meta", {}))


def write_record(directory: Path, record: RecordDocument) -> None:
    write_json(record_path(directory), record)


def load_record(directory: Path) -> RecordDocument:
    return record_from_dict(load_json(record_path(directory)))


__all__ = [
    "AUDIO_SUFFIXES",
    "discover_audio",
    "export_dir",
    "is_audio",
    "load_manifest",
    "load_record",
    "load_segments",
    "manifest_path",
    "record_path",
    "segments_path",
    "write_manifest",
    "write_record",
    "write_segments",
]
