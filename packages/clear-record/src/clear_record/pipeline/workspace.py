"""Workspace I/O for clear-record.

A *workspace* is an ordinary directory (typically gitignored, e.g. ``recordings/``
or ``data/<name>/``) that contains the source recordings and the derived record
artifacts. It is intentionally not a database::

    <dir>/
        manifest.json       Sources discovered/declared by `ingest` (+ alignment).
        segments.json       Per-source ASR segments (one file, all sources) + meta.
        record.json         The reconciled RecordDocument.
        glossary.txt        Decoder initial prompt (one term/line).
        transcribe.log      Durable append-only transcription log.
        audio/              Normalized 16 kHz mono copies of the sources.
        export/             Markdown/SRT/VTT/JSON artifacts from `export`.

The resumable per-source chunk cache is **not** part of the workspace: it is
app-owned *cache* (ADR-0007/ADR-0025) under the platform cache directory, keyed
once per workspace (see :meth:`Workspace.chunk_cache`).

:class:`Workspace` is the single owner of every one of those paths and
read/writes; stages and the transcription module talk to it rather than composing
paths themselves. Audio never has to live *in* the workspace (paths in the
manifest are resolved as written, absolute or relative-to-workspace). Nothing
here is ever committed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path

from clear_record.core import (
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
from clear_record.core.paths import CHUNKS_DIRNAME, resolve_cache_dir

MANIFEST = "manifest.json"
SEGMENTS = "segments.json"
RECORD = "record.json"
EXPORT_DIR = "export"
AUDIO_DIR = "audio"
CHUNKS_DIR = "chunks"
GLOSSARY = "glossary.txt"
TRANSCRIBE_LOG = "transcribe.log"
GROUND_TRUTH = "ground_truth.json"

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


# Workspace-managed subdirectories that must never be re-discovered as sources
# (they hold our own normalized/derived output).
SKIP_DIRS = {AUDIO_DIR, EXPORT_DIR, CHUNKS_DIR}

# `transcribe.log` is append-only and shared by the stage and its worker pool.
_log_lock = threading.Lock()


def _cache_key(root: Path) -> str:
    """A stable short key for one workspace, so two workspaces never share a cache.

    The chunk cache is content-keyed by source but app-owned and global; without
    a workspace key two workspaces with the same source id would clobber each
    other's chunks. The resolved absolute root is the workspace's identity.
    """
    return hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def discover_audio(directory: Path) -> list[Path]:
    """Recursively list *input* audio files under ``directory`` (sorted, stable).

    Excludes the workspace's own output dirs (``audio/``, ``export/``) so that
    re-running `ingest` is idempotent.
    """
    found: list[Path] = []
    for p in sorted(directory.rglob("*")):
        if not p.is_file() or not is_audio(p):
            continue
        rel = p.relative_to(directory).parts
        if rel and rel[0] in SKIP_DIRS:
            continue
        found.append(p)
    return found


def _publish_json(path: Path, payload) -> None:
    """Write ``payload`` to ``path`` atomically via a sibling ``.tmp`` file.

    A cancellation can strike while a writer is mid-write; publishing by rename
    means a reader (resume) only ever sees a complete JSON body, or no file at
    all — never a truncated one.
    """
    tmp = path.with_name(path.name + ".tmp")
    write_json(tmp, payload)
    os.replace(tmp, path)


def chunk_cache_key(
    *,
    backend: str,
    model: str | None,
    language: str | None,
    glossary: str,
    chunk_seconds: float,
    overlap_seconds: float,
    n_chunks: int,
    decoders: Mapping[str, object] | None = None,
) -> dict:
    """The run-meta that keys one source's chunk cache.

    The key is split into two parts with **different** invalidation semantics,
    and the split is the whole point of this function's contract:

    - **The plan** — everything except ``glossary``: ``backend``, ``model``,
      ``language``, the chunk plan (``chunk_seconds``/``overlap_seconds``/
      ``n_chunks``) and any set decoder knob. A change to any of these makes the
      cached chunk *bodies* meaningless in place (different decoder, different
      window boundaries), so it still invalidates **every chunk of the source**.
    - **The glossary** — the decoder's initial prompt. One edit here still
      invalidates every chunk of an **unscoped** run, exactly as before: the
      prompt participates in decoding, so any chunk's output *can* change and
      nothing narrower is sound on its own. It is no longer the unit of
      invalidation, though: a **scoped** re-run
      (:class:`clear_record.core.ChunkScope`) re-decodes only the chunks its
      scope selects, and each cached body records the glossary digest it was
      actually decoded under (see :func:`chunk_glossary`). A later unscoped run
      therefore still re-decodes the chunks a scope carried over.

    An unset decoder knob adds nothing, so a run that does not set any keeps the
    cache key it had before tunable decoding existed.
    """
    key = {
        "backend": backend,
        "model": model,
        "language": language or "auto",
        "glossary": glossary,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
        "n_chunks": n_chunks,
    }
    if decoders:
        key["decoders"] = dict(decoders)
    return key


#: Meta key holding ``{chunk index: glossary digest}`` — the glossary each cached
#: body was actually decoded under. Absent on a cache written before scoped
#: re-runs existed; such a cache is treated as having no known provenance and is
#: re-decoded once (the conservative migration).
CHUNK_GLOSSARY = "chunk_glossary"


def glossary_digest(glossary: str) -> str:
    """A short stable digest of a glossary prompt, for per-chunk provenance."""
    return hashlib.sha1((glossary or "").encode("utf-8")).hexdigest()[:16]


def cache_plan(run_meta: Mapping) -> dict:
    """The part of :func:`chunk_cache_key`'s output that is *not* per-chunk.

    ``glossary`` is excluded because it is tracked per chunk (see
    :data:`CHUNK_GLOSSARY`); the same field is excluded from a *stored* meta
    alongside the overlay itself, so the two compare equal regardless of which
    glossary each body was decoded under.
    """
    return {
        key: value
        for key, value in run_meta.items()
        if key not in {"glossary", CHUNK_GLOSSARY}
    }


def plan_matches(stored: Mapping | None, run_meta: Mapping) -> bool:
    """True when a stored run-meta describes the same chunk plan and decoders."""
    if not stored:
        return False
    return cache_plan(stored) == cache_plan(run_meta)


def chunk_glossary(stored: Mapping | None) -> dict[int, str]:
    """The per-chunk glossary digests a stored meta records (``{}`` if none).

    A body with no recorded digest has unknown provenance and is never reused by
    digest; the caller treats it as absent.
    """
    raw = (stored or {}).get(CHUNK_GLOSSARY) or {}
    out: dict[int, str] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        try:
            out[int(key)] = str(value)
        except (TypeError, ValueError):
            continue
    return out


@dataclasses.dataclass(frozen=True)
class ChunkCache:
    """One source's resumable chunk cache; owns its files and atomic publish.

    Layout under the source's cache directory::

        <source>/_meta.json    run-meta (the cache key + per-chunk glossary digests)
        <source>/NNNN.json     cached chunk segments (published atomically)
        <source>/NNNN.wav      transient decode; regenerable, never trusted
        <source>/*.tmp         atomic-publish scratch; never read

    ``_meta.json`` is :func:`chunk_cache_key`'s flat plan key, the run's full
    ``glossary`` text, and a ``chunk_glossary`` map of the glossary digest each
    body was decoded under. The digest map is what lets a scoped re-run keep an
    out-of-scope chunk *and stay honest about it*: the chunk's provenance is the
    old glossary, not the new one, and an unscoped run re-decodes it (see
    :func:`chunk_glossary` / :func:`plan_matches`).

    ADR-0007/ADR-0025 place the chunks root in the app-owned cache directory;
    because every chunk path is derived here, that location has one owner
    (:meth:`Workspace.chunk_cache`) and callers never compose a path themselves.
    """

    directory: Path

    @property
    def meta_path(self) -> Path:
        return self.directory / "_meta.json"

    def segments_path(self, index: int) -> Path:
        return self.directory / f"{index:04d}.json"

    def audio_path(self, index: int) -> Path:
        """Transient chunk WAV path; regenerable and deleted after decoding."""
        return self.directory / f"{index:04d}.wav"

    def ensure(self) -> ChunkCache:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self

    def read_meta(self) -> dict | None:
        try:
            return load_json(self.meta_path)
        except (OSError, ValueError):
            return None

    def write_meta(
        self,
        run_meta: Mapping,
        chunk_glossary: Mapping[int, str] | None = None,
    ) -> None:
        """Publish the run-meta, plus the glossary digest each chunk now carries.

        Written **before** decoding so a cancelled pass stays resumable: a body
        that does not exist yet is re-decoded on the next pass, while the bodies
        already published carry the digest recorded here.
        """
        payload = dict(run_meta)
        if chunk_glossary is not None:
            payload[CHUNK_GLOSSARY] = {
                str(index): digest for index, digest in sorted(chunk_glossary.items())
            }
        _publish_json(self.meta_path, payload)

    def invalidate(self) -> None:
        """Drop chunk results, transient WAVs and any half-written scratch file.

        A stale body must never be read, so the sweep covers ``*.json`` (chunk
        bodies and the meta), ``*.wav`` and ``*.tmp``.
        """
        for stale in (
            list(self.directory.glob("*.json"))
            + list(self.directory.glob("*.wav"))
            + list(self.directory.glob("*.tmp"))
        ):
            stale.unlink(missing_ok=True)

    def has_segments(self, index: int) -> bool:
        return self.segments_path(index).exists()

    def read_segments(self, index: int) -> list[Segment]:
        return [segment_from_dict(x) for x in load_json(self.segments_path(index))]

    def write_segments(self, index: int, segments: list[Segment]) -> None:
        """Publish a chunk's segments atomically (``.tmp`` + ``os.replace``)."""
        _publish_json(self.segments_path(index), [to_dict(s) for s in segments])


@dataclasses.dataclass(frozen=True)
class Workspace:
    """The single owner of a workspace's on-disk layout and read/writes.

    Open one with :meth:`at`. Every path and every read/write on workspace state
    goes through this object, so the layout lives in one place and a temporary
    directory can substitute for it in tests.
    """

    root: Path

    @classmethod
    def at(cls, directory: str | Path) -> Workspace:
        return cls(Path(directory))

    # --- paths ------------------------------------------------------------- #
    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST

    @property
    def segments_path(self) -> Path:
        return self.root / SEGMENTS

    @property
    def record_path(self) -> Path:
        return self.root / RECORD

    @property
    def glossary_path(self) -> Path:
        return self.root / GLOSSARY

    @property
    def export_dir(self) -> Path:
        return self.root / EXPORT_DIR

    @property
    def audio_dir(self) -> Path:
        return self.root / AUDIO_DIR

    @property
    def chunks_dir(self) -> Path:
        """This workspace's app-owned chunk-cache root (ADR-0007/ADR-0025)."""
        return resolve_cache_dir() / CHUNKS_DIRNAME / _cache_key(self.root)

    @property
    def ground_truth_path(self) -> Path:
        return self.root / GROUND_TRUTH

    def export_file(self, name: str) -> Path:
        return self.export_dir / name

    def chunk_cache(self, source_id: str) -> ChunkCache:
        """The resumable chunk cache for one source.

        The cache is app-owned (the platform cache directory, keyed per
        workspace) rather than a workspace document: it is evictable, and it
        outlives a cancelled run so the next pass resumes (ADR-0007/ADR-0025).
        """
        return ChunkCache(self.chunks_dir / source_id)

    # --- manifest ---------------------------------------------------------- #
    def write_manifest(
        self, sources: list[Source], alignment: Alignment | None = None
    ) -> None:
        data: dict = {"sources": [to_dict(s) for s in sources]}
        if alignment is not None:
            data["alignment"] = to_dict(alignment)
        write_json(self.manifest_path, data)

    def load_manifest(self) -> tuple[list[Source], Alignment | None]:
        data = load_json(self.manifest_path)
        sources = [source_from_dict(s) for s in data["sources"]]
        alignment = (
            alignment_from_dict(data["alignment"]) if data.get("alignment") else None
        )
        return sources, alignment

    # --- segments ---------------------------------------------------------- #
    def write_segments(
        self,
        per_source: dict[str, list[Segment]],
        meta: dict | None = None,
    ) -> None:
        payload: dict = {
            "sources": {
                src: [to_dict(s) for s in segs] for src, segs in per_source.items()
            }
        }
        if meta:
            payload["meta"] = meta
        write_json(self.segments_path, payload)

    def load_segments(self) -> tuple[dict[str, list[Segment]], dict]:
        data = load_json(self.segments_path)
        per_source = {
            src: [segment_from_dict(s) for s in segs]
            for src, segs in data.get("sources", {}).items()
        }
        return per_source, dict(data.get("meta", {}))

    # --- record ------------------------------------------------------------ #
    def write_record(self, record: RecordDocument) -> None:
        write_json(self.record_path, record)

    def load_record(self) -> RecordDocument:
        return record_from_dict(load_json(self.record_path))

    # --- ground truth (synth) ---------------------------------------------- #
    def write_ground_truth(self, ground_truth: dict) -> None:
        write_json(self.ground_truth_path, ground_truth)

    # --- glossary ---------------------------------------------------------- #
    def append_glossary(self, terms: Iterable[str]) -> Path:
        """Append non-blank terms to the workspace glossary; return its path."""
        path = self.glossary_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for term in terms:
                term = term.strip()
                if term:
                    fh.write(term + "\n")
        return path

    def glossary_terms(self) -> list[str]:
        """The glossary's terms, ignoring blanks and ``#`` comments."""
        if not self.glossary_path.exists():
            return []
        return [
            line.strip()
            for line in self.glossary_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    # --- log --------------------------------------------------------------- #
    def log(self, message: str) -> None:
        """Print to stdout and append to the workspace's durable transcription log."""
        with _log_lock:
            print(message)
            try:
                with (self.root / TRANSCRIBE_LOG).open("a", encoding="utf-8") as fh:
                    fh.write(message + "\n")
            except OSError:
                pass


__all__ = [
    "AUDIO_SUFFIXES",
    "CHUNK_GLOSSARY",
    "ChunkCache",
    "Workspace",
    "cache_plan",
    "chunk_cache_key",
    "chunk_glossary",
    "discover_audio",
    "glossary_digest",
    "is_audio",
    "plan_matches",
]
