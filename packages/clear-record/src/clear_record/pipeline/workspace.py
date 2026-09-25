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
        .clear-record-ignore  Inputs discovery must not take (one glob/line).

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
from fnmatch import fnmatchcase
from pathlib import Path

from clear_record.core import (
    Alignment,
    EventSink,
    JobEvent,
    RecordDocument,
    Segment,
    Source,
    alignment_from_dict,
    emit,
    load_json,
    record_from_dict,
    segment_from_dict,
    source_from_dict,
    to_dict,
    write_json,
)
from clear_record.core.paths import CHUNKS_DIRNAME, resolve_cache_dir
from clear_record.core.i18n import deferred

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
    # WavPack — what an Audacity export ships. libsndfile refuses it, so it
    # decodes through the ffmpeg fallback (``engine.audio.read_audio``); absent
    # from this set, a field tape's five tracks were invisible to `ingest`/`run`
    # while the same file named as an explicit input decoded fine.
    ".wv",
}


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_SUFFIXES


# Workspace-managed subdirectories that must never be re-discovered as sources
# (they hold our own normalized/derived output).
SKIP_DIRS = {AUDIO_DIR, EXPORT_DIR, CHUNKS_DIR}

#: The workspace's own bookkeeping files, by name, wherever the walk meets them.
#: They are not inputs: naming them as files discovery "cannot use" would
#: narrate the workspace's own state back at the operator on every ingest.
WORKSPACE_FILES = {MANIFEST, SEGMENTS, RECORD, GLOSSARY, TRANSCRIBE_LOG, GROUND_TRUTH}

#: The workspace's own ignore declaration, read by discovery: one glob per line,
#: matched against a path relative to the workspace root (``old/take.wav``,
#: ``*_edit.wav``), with a blank line or a ``#`` comment declaring nothing. A
#: recording folder is not one event — it accumulates earlier sessions, a case
#: copied in brings its own along, and a DJI-style export ships an ``_edit``
#: beside the ``_orig`` it was made from — so the folder itself carries the one
#: declaration of which files discovery must not take. The name is hidden (a
#: leading dot), which is what keeps the declaration out of its own walk: it is
#: neither an input nor a file :func:`discover_inputs` narrates back.
IGNORE_FILE = ".clear-record-ignore"

#: The app's own agent-artifact directory under a meeting workspace
#: (``service.agent_review.AGENT_DIRNAME``, ADR-0031): the drafts, locks and
#: promoted minutes the console's agent flow keeps there. It is not a tape
#: either, and its name is spelled out here because ``pipeline`` may not import
#: ``service`` (``tests/test_layering.py``). Only the *naming* walk is kept off
#: it — joining :data:`SKIP_DIRS` instead would also drop any audio beneath it
#: from :func:`discover_audio`, which is not this rule.
AGENT_DIRNAME = "agent"

# `transcribe.log` is append-only and shared by the stage and its worker pool.
_log_lock = threading.Lock()


def _cache_key(root: Path) -> str:
    """A stable short key for one workspace, so two workspaces never share a cache.

    The chunk cache is content-keyed by source but app-owned and global; without
    a workspace key two workspaces with the same source id would clobber each
    other's chunks. The resolved absolute root is the workspace's identity.
    """
    return hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def _ignored(relative: str, patterns: Iterable[str]) -> bool:
    """Whether any of *patterns* names the workspace-relative *relative*.

    ``fnmatch`` runs against the whole relative path, so ``*`` matches a
    separator too: ``old/*`` names everything under a directory, ``*_edit.wav``
    the copy an editor left beside the file it was made from.
    """
    return any(fnmatchcase(relative, pattern) for pattern in patterns)


#: The refusal for a workspace whose own declaration cannot be **read**: the walk
#: cannot know which of the folder's files it is meant to leave out, and taking
#: every one of them would be exactly the silent wrong answer the declaration
#: exists to prevent. One edit resolves it — the file itself.
#:
#: It is a **message ID** for the surfaces that show refusals; it lives here,
#: beside the declaration it is about, because every reader of the declaration
#: needs it (:func:`clear_record.pipeline.stages.ingest` in its own words,
#: ``service.runs.workspace_run_meeting``, ``service.auto.resolve_run`` and
#: ``cli.cli._apply_auto`` at their own edges).
CANNOT_READ_DECLARATION = deferred(
    "the workspace's .clear-record-ignore cannot be read, so which of its files "
    "are inputs is unknown; make it readable or remove it"
)


class DeclarationUnreadable(ValueError):
    """The workspace's own declaration could not be read.

    Raised by :meth:`Workspace.ignore_patterns` — the one place the file is read —
    so every walker that consults the declaration fails the same way, and no
    caller has to know how a file can refuse to be read: an ``OSError`` for
    permissions, a ``UnicodeDecodeError`` for a declaration saved in another
    encoding (a plausible field file: GBK, cp1252).

    It is a :class:`ValueError`, because the surfaces that answer a bad value with
    a sentence already catch that: the run edges answer
    :data:`CANNOT_READ_DECLARATION` for it, and ``str(exc)`` carries the file and
    the reason for a caller that wants to say it in its own words.
    """

    def __init__(self, path: Path, reason: BaseException) -> None:
        detail = getattr(reason, "strerror", None) or str(reason)
        self.path = path
        #: The OS's (or the codec's) own words for the reason, without the path —
        #: the *reason* half of :attr:`path`, for a sentence that already names it.
        self.reason = str(detail)
        super().__init__(f"cannot read {path}: {detail}")


def discover_inputs(directory: Path) -> tuple[list[Path], list[Path]]:
    """Recursively classify *directory*: ``(audio inputs, files walked past)``.

    One walk, two lists. The first is the audio inputs of :func:`discover_audio`
    (same selection, same order). The second is what the walk did **not** take as
    an input, and it names both kinds, because both are a file the operator may
    have meant to hand over:

    - a regular file whose suffix is outside :data:`AUDIO_SUFFIXES`. A format the
      decoder handles but the set did not list — WavPack ``.wv`` was one — used
      to vanish here without a word;
    - an **audio** file the workspace's own :data:`IGNORE_FILE` declaration
      names. It would have been an input; the folder's declaration is what says
      it is not today's. That a file reaches this list with an audio suffix is
      therefore the mark of an exclusion, and nothing else.

    Not every other file is narrated, because not every other file is a tape:
    the workspace's own output dirs (:data:`SKIP_DIRS`), its bookkeeping files
    (:data:`WORKSPACE_FILES`) and the app's own agent-artifact directory
    (:data:`AGENT_DIRNAME`) are its own state, and a hidden entry (a checkout's
    ``.git``, a stray ``.DS_Store``, the upload scratch file
    ``.cr-upload-*.part``) is machinery, not an input. Only *this* walk is kept
    off the agent directory: audio beneath it still discovers, so it is not in
    :data:`SKIP_DIRS`.
    """
    audio: list[Path] = []
    skipped: list[Path] = []
    patterns = Workspace.at(directory).ignore_patterns()
    for p in sorted(directory.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(directory).parts
        if rel and rel[0] in SKIP_DIRS:
            continue
        if is_audio(p):
            if _ignored("/".join(rel), patterns):
                skipped.append(p)
            else:
                audio.append(p)
        elif rel and rel[0] == AGENT_DIRNAME:
            continue
        elif p.name not in WORKSPACE_FILES and not any(
            part.startswith(".") for part in rel
        ):
            skipped.append(p)
    return audio, skipped


def discover_audio(directory: Path) -> list[Path]:
    """Recursively list *input* audio files under ``directory`` (sorted, stable).

    Excludes the workspace's own output dirs (``audio/``, ``export/``) so that
    re-running `ingest` is idempotent, and whichever inputs the workspace's own
    :data:`IGNORE_FILE` declaration names — so `run`, which resolves a
    directory's sources through here, honours the same declaration `ingest` does.
    The walk itself is :func:`discover_inputs`', which returns the files it
    walked past beside these.
    """
    return discover_inputs(directory)[0]


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

    @property
    def ignore_path(self) -> Path:
        """The workspace's own ignore declaration (:data:`IGNORE_FILE`)."""
        return self.root / IGNORE_FILE

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

    # --- ignore declaration ------------------------------------------------ #
    def ignore_patterns(self) -> tuple[str, ...]:
        """The globs :data:`IGNORE_FILE` declares, in file order (``()`` if none).

        The declaration's own reader, beside the glossary's: an **absent** file
        declares nothing, so a workspace without one is discovered exactly as it
        was before the declaration existed — while a file that is there and
        cannot be read raises :class:`DeclarationUnreadable`, never read as
        empty, because a declaration that silently stops applying means files the
        operator excluded ingested into the record against their word. That
        exception is the *only* failure this read has, and the readers that make a
        **decision** from what they read all name it: a stage
        (:func:`~clear_record.pipeline.stages.ingest`, its own sentence), a
        directory run's tape resolution and ``auto``'s probe
        (:data:`CANNOT_READ_DECLARATION`, mapped by the run edges), and the
        command line's own probe (the same id, rendered). The one reader that
        stands down is ``service.runs.report_declaration_exclusions``, whose line
        is a *report* of a run it already knows: it says nothing when it cannot
        read the file, while the walk that picks the run's inputs is where the
        refusal belongs. It is read as ``utf-8-sig``: an editor that saves "UTF-8
        with BOM" (the default of some field tooling) would otherwise put ``\ufeff``
        at the head of the **first line** — and on the one-line declaration the
        README documents, whose first line *is* the pattern, that pattern then
        matches nothing and the declaration stops applying *silently*, the one
        answer this read must never give. (On a declaration that opens with a
        comment the mark only spoils that comment: the patterns below it still
        apply.) Blank lines and ``#`` comments declare nothing.
        """
        try:
            lines = self.ignore_path.read_text(encoding="utf-8-sig").splitlines()
        except FileNotFoundError:
            return ()
        except (OSError, UnicodeDecodeError) as exc:
            raise DeclarationUnreadable(self.ignore_path, exc) from exc
        patterns: list[str] = []
        for line in lines:
            pattern = line.strip()
            if pattern and not pattern.startswith("#"):
                patterns.append(pattern)
        return tuple(patterns)

    # --- log --------------------------------------------------------------- #
    def log(self, message: str) -> None:
        """Append one line to the workspace's durable transcription log.

        The log is an **artifact**: ``transcribe.log`` outlives the process, the
        support bundle gathers it (``service.diagnostics``), and a person
        watching a long run reads it. Resuming reads the chunk cache and
        ``segments.json``, not this. *Showing* a line is not this method's job —
        :func:`report_line` is the one that reports a line, and it calls this to
        keep the durable copy.
        """
        with _log_lock:
            try:
                with (self.root / TRANSCRIBE_LOG).open("a", encoding="utf-8") as fh:
                    fh.write(message + "\n")
            except OSError:
                pass


def report_line(
    workspace: Workspace,
    sink: EventSink | None,
    stage: str,
    message: str,
    *,
    level: str = "info",
    source: str | None = None,
    report: JobEvent | None = None,
    index: int = 0,
    total: int = 0,
    reused: int = 0,
) -> None:
    """Report one mid-stage line: durable, structured, and shown where it happens.

    A stage's own prose — a chunk finishing, a backend probed, a source skipped —
    is produced *inside* the stage's loops, for the transcriber on its pool's
    worker threads, so it cannot come back with the stage's return value without
    being held until the stage ends. It travels on the stage's **sink** instead,
    as one structured line on the event the console already listens to: a
    :class:`~clear_record.core.JobEvent` carrying the line's stage and severity,
    the source it is about, the line's text in ``message`` (byte for byte what the
    command surface prints), and the counters described below.

    **One unit base per stage's bar.** The console draws its bar from the
    *newest* event of a run's stream (``RunState.summary``, the "N / M" beside
    it, and the rate ``_run_speed_so_far`` divides by), so a line that reported a
    unit finer than its stage's — a chunk of one source, under a pass that counts
    chunks across all of them — would move that bar backwards the moment it
    landed. A line therefore reports on the base its stage already reports on,
    and where the stage's base is that coarse the line's own subject *is* that
    base: ``diarize`` counts sources, one line per source, and states
    ``index``/``total`` over them.

    - ``report`` — the progress report the line belongs to (the chunk it just
      finished): the line goes out as a copy of that event, with the line's own
      message and source, so every counter — and the elapsed clock the console's
      rate divides by — agrees with the bar. A line the stage reports before its
      bar opens carries none (0/0, the same 0% the opening report draws), and a
      line that speaks for the pass rather than one chunk of it — the plan lines,
      the final tally — states the counters it has (``index``/``total``/``reused``).
    - the per-source facts are ``source`` and the line's own text ("a chunk 3/4
      [4-7s] -> 1 segment(s)"), which is where a reader of one source's line
      wants them: ``index``/``total`` carry the console's bar, so a per-source
      ordinal travels in the line's text rather than in a field of its own.

    The line's *kind*, and the window it covers where it has one, are part of
    that text: ``JobEvent`` is one of the frozen boundary dataclasses of ADR-0030,
    so no field is added to it for them.

    Beyond the event there is one durable copy, and no second channel:

    - the workspace's durable ``transcribe.log`` (``Workspace.log``), always;
    - **the event is the only channel**: ``message`` is the line, so a consumer
      that prints every non-empty ``message`` prints this line exactly once, and
      a client reading the run's stream over the API reads the same text the
      command surface prints — there is no capability to attach beside it, and a
      sink that wants the words reads them off the event.
    """
    workspace.log(message)
    if report is None:
        line = JobEvent(
            stage=stage,
            level=level,
            source=source,
            index=index,
            total=total,
            reused=reused,
            message=message,
        )
    else:
        line = dataclasses.replace(
            report, stage=stage, level=level, source=source, message=message
        )
    emit(sink, line)


__all__ = [
    "AUDIO_SUFFIXES",
    "CANNOT_READ_DECLARATION",
    "CHUNK_GLOSSARY",
    "ChunkCache",
    "DeclarationUnreadable",
    "IGNORE_FILE",
    "Workspace",
    "cache_plan",
    "chunk_cache_key",
    "chunk_glossary",
    "discover_audio",
    "discover_inputs",
    "glossary_digest",
    "is_audio",
    "plan_matches",
    "report_line",
]
