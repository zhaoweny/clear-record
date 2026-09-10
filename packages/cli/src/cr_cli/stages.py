"""The clear-record pipeline stage implementations (ingest / align / transcribe /
reconcile / export / run / calibrate).

Everything here is the thin wiring layer: it reads/writes the workspace files
and delegates the real work to ``cr_core`` (domain), ``cr_engine`` (audio/align/
merge) and ``cr_providers`` (ASR). No vendor logic lives here.
"""

from __future__ import annotations

import dataclasses
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import soundfile as sf

from cr_core import (
    RecordDocument,
    Segment,
    Source,
    load_json,
    segment_from_dict,
    to_dict,
    write_json,
)
from cr_engine import (
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    SYNTH_SR,
    align_sources,
    attribute_segments,
    channel_count,
    clean_segments,
    diarize as diarize_segments,
    make_scene,
    plan_chunks,
    prepare_16k_wav,
    record as synth_record,
    reconcile as reconcile_segments,
    write_chunk,
)
from cr_engine.audio import read_audio
from cr_providers import get_backend

from cr_cli import eval as _eval
from cr_cli import workspace as ws


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
def _source_id(path: Path, directory: Path) -> str:
    try:
        rel = path.relative_to(directory)
    except ValueError:
        return path.stem
    return str(rel.with_suffix("")).replace("/", "__").replace("\\", "__")


def ingest(
    directory: str,
    audio_files: list[str] | None = None,
    split: str = "auto",
) -> list[Source]:
    """Discover/declare sources and normalize them to 16 kHz mono WAV.

    ``split`` controls multi-channel handling:

    - ``"auto"`` (default): split channels when a file has **more than two**
      (clearly a multichannel meeting/DJI-quadraphonic capture); downmix 1–2 ch.
    - ``"split"``: split every channel of any multichannel file into its own
      source — preserves per-speaker isolation ("closest mic wins").
    - ``"mix"``: always downmix to mono.
    """
    d = Path(directory)
    files = [Path(a) for a in audio_files] if audio_files else ws.discover_audio(d)
    if not files:
        raise SystemExit(f"[ingest] no audio files found in {d}")
    audio_dir = d / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    sources: list[Source] = []
    for p in files:
        base = _source_id(p, d)
        nch = channel_count(p)
        do_split = (split == "split") or (split == "auto" and nch > 2)
        if do_split and nch > 1:
            # One source per channel: this is the multi-mic meeting case, where
            # each speaker is nearest one channel and diarization is nearly free.
            for ch in range(nch):
                sid = f"{base}__ch{ch + 1}"
                norm = audio_dir / f"{sid}.wav"
                print(f"[ingest] decode {p.name} ch{ch + 1}/{nch} -> {norm.name}")
                prepare_16k_wav(p, norm, channel=ch)
                sources.append(
                    Source(
                        id=sid,
                        path=str(norm),
                        label=f"{p.stem} ch{ch + 1}",
                        clock_domain="wall",
                    )
                )
        else:
            norm = audio_dir / f"{base}.wav"
            print(f"[ingest] decode {p.name} -> {norm.name}")
            prepare_16k_wav(p, norm)
            sources.append(
                Source(id=base, path=str(norm), label=p.stem, clock_domain="wall")
            )
    ws.write_manifest(d, sources)
    _print_sources(sources, d)
    return sources


def _print_sources(sources: list[Source], d: Path) -> None:
    print(f"[ingest] {len(sources)} source(s) -> {ws.manifest_path(d)}")
    for s in sources:
        print(f"  {s.id:24s} {s.path}")


# --------------------------------------------------------------------------- #
# align
# --------------------------------------------------------------------------- #
def align(directory: str, reference: str | None = None):
    d = Path(directory)
    sources, _ = ws.load_manifest(d)
    alignment = align_sources(sources, reference_id=reference)
    ws.write_manifest(d, sources, alignment)
    print(
        f"[align] reference={alignment.reference} method={alignment.method} "
        f"conf={alignment.confidence} unresolved={len(alignment.unresolved)}"
    )
    for sid, off in alignment.offsets.items():
        marker = " (ref)" if sid == alignment.reference else ""
        print(f"  {sid:24s} offset={off:+.4f}s{marker}")
    for sid in alignment.unresolved:
        print(f"  {sid:24s} UNRESOLVED (could not place this source)")
    return alignment


# --------------------------------------------------------------------------- #
# transcribe
# --------------------------------------------------------------------------- #
def _duration(path: str | Path) -> float:
    """Duration in seconds of a WAV (0.0 if unknown)."""
    try:
        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate or 1)
    except Exception:
        try:
            data, sr = read_audio(path, target_sr=None)
            return float(len(data) / sr)
        except Exception:
            return 0.0


def _load_glossary(directory: Path, explicit: str | None) -> tuple[str, str]:
    """Return ``(prompt, source)``. The glossary is one term/phrase per line;
    ``#`` comments and blanks are ignored. Capped to stay a sane prompt."""
    path = Path(explicit) if explicit else ws.glossary_path(directory)
    if not path.exists():
        return "", ""
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    prompt = ", ".join(terms)[:2000]
    return prompt, str(path)


_log_lock = threading.Lock()


def _log_line(directory: Path, message: str) -> None:
    """Print and append to the workspace's durable transcription log."""
    with _log_lock:
        print(message)
        try:
            with (directory / "transcribe.log").open("a", encoding="utf-8") as fh:
                fh.write(message + "\n")
        except OSError:
            pass


def _merge_chunk_segments(chunks: list[list[Segment]]) -> list[Segment]:
    """Merge overlapping chunk results with deterministic ownership.

    Adjacent chunks overlap, so the same words can be decoded in both. Ownership
    is decided by *coverage*, not by comparing starts to an overlap midpoint: we
    track the furthest end emitted so far and

    - drop a later segment whose span is fully covered by that frontier
      (``seg.end <= emitted_end``) — it is a duplicate;
    - clip a straddler's ``start`` up to the frontier so its unique tail survives;
    - keep outright a segment that starts at or after the frontier.

    The frontier only moves forward, so the result cannot contain overlapping
    duplicates, while a unique later-chunk tail near a boundary is retained.
    """
    out: list[Segment] = []
    emitted_end = float("-inf")
    for segs in chunks:
        for seg in segs:
            if seg.end <= emitted_end + 1e-9:
                continue  # already covered by an earlier chunk
            if seg.start < emitted_end:
                seg = dataclasses.replace(seg, start=emitted_end)
            if seg.end <= seg.start:
                continue
            out.append(seg)
            emitted_end = max(emitted_end, seg.end)
    out.sort(key=lambda s: (s.start, s.end))
    return clean_segments(out)


_DEFAULT_MAX_JOBS = 4


def _resolve_jobs(parallelizable: bool, n_pending: int, requested: int) -> int:
    """Choose the chunk-transcription worker count adaptively.

    Precedence: explicit ``requested`` (>0), then ``CR_JOBS``, else a small
    default for process-isolated backends. The GPU is the shared bottleneck, so
    the default is modest (measured ~93% ``gpu_busy`` at 4 concurrent
    ``whisper-cli`` processes); a backend that shares in-process model state
    (Apple ``pywhispercpp``) is always serialized, even when asked otherwise.
    """
    if n_pending <= 1:
        return 1
    if not parallelizable:
        return 1
    if requested and requested > 0:
        return max(1, min(int(requested), n_pending))
    env = os.environ.get("CR_JOBS", "").strip()
    if env.isdigit() and int(env) > 0:
        return max(1, min(int(env), n_pending))
    return max(1, min(os.cpu_count() or 1, _DEFAULT_MAX_JOBS, n_pending))


@dataclasses.dataclass
class _ChunkTask:
    """One uncached chunk to transcribe — a unit of parallel work."""

    source_id: str
    index: int
    audio_path: str
    start_s: float
    end_s: float
    cache: Path
    n_chunks: int


@dataclasses.dataclass
class _SourcePlan:
    """A source's chunk plan plus the per-chunk segments as they fill in."""

    source: Source
    duration: float
    chunks: list[tuple[float, float]]
    segments: list[list[Segment] | None]


def transcribe(
    directory: str,
    backend_id: str,
    model: str | None = None,
    language: str | None = None,
    model_dir: str | None = None,
    glossary: str | None = None,
    chunk_seconds: float = DEFAULT_CHUNK_S,
    overlap_seconds: float = DEFAULT_OVERLAP_S,
    resume: bool = True,
    jobs: int = 0,
):
    """Transcribe every source, in **resumable overlapping chunks** for long
    tapes, with an optional **glossary** as the decoder's initial prompt.

    Chunk results are cached under ``<dir>/chunks/<source>/``; the cache is
    invalidated when the backend/model/language/glossary/prompt or chunk plan
    changes, which is what lets you do a first pass in the background and then
    re-run with a finished glossary.

    Pending chunks (across all sources) are transcribed **concurrently** by a
    bounded worker pool when the backend is process-isolated (``jobs=0`` picks
    the count adaptively; ``--jobs`` / ``CR_JOBS`` override; in-process backends
    such as Apple's are always serialized). Cached chunks are loaded instead of
    recomputed, so resume is unchanged.
    """
    d = Path(directory)
    sources, _ = ws.load_manifest(d)
    backend = get_backend(backend_id)
    if not backend.available():
        hint = (
            "`uv sync --extra apple`"
            if backend_id == "apple"
            else "a system `whisper-cli` + a ggml GPU plugin (see README)"
        )
        raise SystemExit(
            f"[transcribe] backend '{backend_id}' is not available on this machine.\n"
            f"  Install {hint}; runtime requirements are in docs/adr/0005."
        )

    chosen_model = model or backend.info.default_model
    prompt, prompt_src = _load_glossary(d, glossary)
    if prompt:
        _log_line(d, f"[transcribe] glossary: {len(prompt)} chars from {prompt_src}")

    # Plan every source first (cheap I/O), then run the uncached chunks through
    # one bounded pool so the GPU stays fed across source boundaries too.
    plans: list[_SourcePlan] = []
    pending: list[_ChunkTask] = []
    for src in sources:
        duration = _duration(src.path)
        chunks = plan_chunks(duration, chunk_seconds, overlap_seconds)
        cache = ws.chunks_dir(d) / src.id
        cache.mkdir(parents=True, exist_ok=True)
        run_meta = {
            "backend": backend_id,
            "model": chosen_model,
            "language": language or "auto",
            "glossary": prompt,
            "chunk_seconds": chunk_seconds,
            "overlap_seconds": overlap_seconds,
            "n_chunks": len(chunks),
        }
        meta_file = cache / "_meta.json"

        reuse = resume and meta_file.exists() and _read_json_safe(meta_file) == run_meta
        if not reuse:
            for stale in cache.glob("*.json"):
                stale.unlink()
            write_json(meta_file, run_meta)

        _log_line(
            d,
            f"[transcribe] {src.id}: {len(chunks)} chunk(s), {duration:.1f}s, "
            f"{backend.info.description} ({chosen_model})",
        )
        segs: list[list[Segment] | None] = [None] * len(chunks)
        for i, (start_s, end_s) in enumerate(chunks):
            seg_file = cache / f"{i:04d}.json"
            if reuse and seg_file.exists():
                segs[i] = [segment_from_dict(x) for x in load_json(seg_file)]
                _log_line(
                    d, f"[transcribe]   {src.id} chunk {i + 1}/{len(chunks)} cached"
                )
            else:
                pending.append(
                    _ChunkTask(src.id, i, src.path, start_s, end_s, cache, len(chunks))
                )
        plans.append(_SourcePlan(src, duration, chunks, segs))

    plan_by_id = {plan.source.id: plan for plan in plans}
    workers = _resolve_jobs(backend.info.parallelizable, len(pending), jobs)
    if pending:
        _log_line(d, f"[transcribe] {len(pending)} pending chunk(s), jobs={workers}")

    def _run_chunk(task: _ChunkTask) -> tuple[str, int, list[Segment]]:
        chunk_wav = task.cache / f"{task.index:04d}.wav"
        write_chunk(task.audio_path, chunk_wav, task.start_s, task.end_s)
        try:
            res = backend.transcribe(
                str(chunk_wav),
                language=language,
                model=model,
                model_dir=model_dir,
                initial_prompt=prompt or None,
            )
        finally:
            chunk_wav.unlink(missing_ok=True)  # segments cached; wav regenerable
        shifted = [
            dataclasses.replace(
                s,
                start=round(s.start + task.start_s, 4),
                end=round(s.end + task.start_s, 4),
                source=task.source_id,
            )
            for s in res.segments
        ]
        write_json(task.cache / f"{task.index:04d}.json", [to_dict(s) for s in shifted])
        _log_line(
            d,
            f"[transcribe]   {task.source_id} chunk {task.index + 1}/{task.n_chunks} "
            f"[{task.start_s:.0f}-{task.end_s:.0f}s] -> {len(shifted)} segment(s)",
        )
        return task.source_id, task.index, shifted

    if workers <= 1:
        for task in pending:
            src_id, i, shifted = _run_chunk(task)
            plan_by_id[src_id].segments[i] = shifted
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_chunk, task) for task in pending]
            for future in as_completed(futures):
                src_id, i, shifted = future.result()
                plan_by_id[src_id].segments[i] = shifted

    per_source: dict[str, list[Segment]] = {}
    meta: dict = {
        "backend": backend_id,
        "model": chosen_model,
        "language": language or "auto",
        "model_dir": model_dir,
        "glossary": prompt_src or None,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
        "jobs": workers,
        "sources": {},
    }
    for plan in plans:
        merged = _merge_chunk_segments([s or [] for s in plan.segments])
        per_source[plan.source.id] = merged
        meta["sources"][plan.source.id] = {
            "duration": plan.duration,
            "segments": len(merged),
            "language": None,
            "chunks": len(plan.chunks),
        }
    ws.write_segments(d, per_source, meta)
    _print_transcription(per_source, meta)
    return per_source


def _read_json_safe(path: Path):
    try:
        return load_json(path)
    except Exception:
        return None


def _print_transcription(per_source: dict[str, list[Segment]], meta: dict) -> None:
    print(
        f"[transcribe] {meta.get('model')!r} via {meta.get('backend')} -> segments.json"
    )
    for sid, segs in per_source.items():
        info = meta.get("sources", {}).get(sid, {})
        dur = info.get("duration")
        print(
            f"  {sid:24s} segments={len(segs):4d}  duration={dur if dur is not None else '?'}  "
            f"chunks={info.get('chunks', '?')}"
        )


# --------------------------------------------------------------------------- #
# diarize (multi-speaker attribution for a single mixed stream)
# --------------------------------------------------------------------------- #
def diarize(directory: str, speakers: int | None = None):
    """Assign speaker labels to segments per source (baseline spectral clustering).

    For per-channel sources this is harmless (each channel is one speaker, so the
    labels collapse to one and are left as the source label). For a single mixed
    stream it is how you get `Speaker 1/2/…` in the record.
    """
    d = Path(directory)
    sources, _ = ws.load_manifest(d)
    per_source, meta = ws.load_segments(d)
    src_by_id = {s.id: s for s in sources}
    applied = False
    for sid, segs in per_source.items():
        src = src_by_id.get(sid)
        if not segs or src is None:
            continue
        try:
            audio, sr = read_audio(src.path, 16000)
        except Exception as exc:  # decode failure is non-fatal
            _log_line(d, f"[diarize] {sid}: skipped ({exc})")
            continue
        labels = diarize_segments(
            audio, sr, [(s.start, s.end) for s in segs], n_speakers=speakers
        )
        n_found = len(set(labels))
        if n_found > 1:
            per_source[sid] = [
                dataclasses.replace(s, speaker=f"Speaker {labels[i] + 1}")
                for i, s in enumerate(segs)
            ]
            applied = True
        _log_line(
            d, f"[diarize] {sid}: {n_found} speaker(s) over {len(segs)} segment(s)"
        )
    if applied:
        ws.write_segments(d, per_source, meta)
    return per_source


# --------------------------------------------------------------------------- #
# attribute (cross-talk-aware attribution by relative source energy)
# --------------------------------------------------------------------------- #
def attribute(directory: str, mixed_source: str | None = None):
    """Re-attribute each segment's speaker from the relative source energy.

    Cross-talk correction for close microphones: instead of trusting the source a
    segment came from ("one source == one speaker", the closest-mic-wins rule),
    pick the source with the highest gain-normalized energy in the segment's
    aligned window. A mixed/room reference named by ``mixed_source`` gates weak
    claims and is never itself a speaker candidate. Composable with `reconcile`,
    which preserves the assigned speaker.
    """
    d = Path(directory)
    sources, alignment = ws.load_manifest(d)
    per_source, meta = ws.load_segments(d)
    mixed = None
    if mixed_source:
        mixed = next((s for s in sources if s.id == mixed_source), None)
        if mixed is None:
            raise SystemExit(f"[attribute] no source '{mixed_source}' in manifest")
    offsets = dict(alignment.offsets) if alignment else {}
    # The room is a witness, never a speaker: keep it out of the candidate set.
    candidates = [s for s in sources if mixed is None or s.id != mixed.id]

    order = [sid for sid, segs in per_source.items() if segs]
    flat = [seg for sid in order for seg in per_source[sid]]
    if not flat:
        print("[attribute] no segments to attribute")
        return per_source

    attributed = attribute_segments(flat, candidates, offsets=offsets, mixed=mixed)
    changed = 0
    pos = 0
    for sid in order:
        updated = []
        for seg in per_source[sid]:
            seg = attributed[pos]
            pos += 1
            updated.append(seg)
        per_source[sid] = updated
    for before, after in zip(flat, attributed):
        if before.speaker != after.speaker:
            changed += 1
    if changed:
        ws.write_segments(d, per_source, meta)
    speakers = {s.speaker for s in attributed}
    suffix = f" (room reference: {mixed_source})" if mixed_source else ""
    print(
        f"[attribute] {len(flat)} segment(s), {len(speakers)} speaker(s), "
        f"{changed} re-attributed{suffix}"
    )
    return per_source


# --------------------------------------------------------------------------- #
# glossary
# --------------------------------------------------------------------------- #
def glossary(directory: str, add: list[str] | None = None) -> Path:
    """Show (and optionally append to) the workspace glossary.

    The glossary is one term/phrase per line; it becomes the ASR decoder's
    initial prompt. Edit it while a first transcription pass runs in the
    background, then re-run `transcribe`: the chunk cache is keyed on the
    glossary, so the finished terms are applied.
    """
    d = Path(directory)
    path = ws.glossary_path(d)
    if add:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for term in add:
                term = term.strip()
                if term:
                    fh.write(term + "\n")
    terms: list[str] = []
    if path.exists():
        terms = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    print(f"[glossary] {path} ({len(terms)} term(s))")
    for term in terms:
        print(f"  {term}")
    return path


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #
def reconcile(directory: str, prefer: str | None = None):
    d = Path(directory)
    sources, alignment = ws.load_manifest(d)
    per_source, meta = ws.load_segments(d)
    segments = reconcile_segments(per_source, alignment, sources)

    record = RecordDocument(
        sources=tuple(sources),
        alignment=alignment,
        segments=tuple(segments),
        metadata={
            "backend": meta.get("backend"),
            "model": meta.get("model"),
            "language": meta.get("language"),
            "prefer": prefer,
        },
    )
    ws.write_record(d, record)
    speakers = {s.speaker for s in segments}
    print(
        f"[reconcile] {len(segments)} segment(s), {len(speakers)} attributed speaker(s)"
        f" -> {ws.record_path(d)}"
    )
    _print_transcript_preview(segments)
    return record


def _print_transcript_preview(segments: list[Segment], limit: int = 12) -> None:
    for seg in segments[:limit]:
        print(f"  {_fmt_ts(seg.start)} [{seg.speaker or seg.source}] {seg.text}")
    if len(segments) > limit:
        print(f"  … {len(segments) - limit} more")


def _fmt_ts(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60.0)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export(directory: str, formats: list[str] | None = None) -> dict[str, Path]:
    d = Path(directory)
    record = ws.load_record(d)
    out = ws.export_dir(d)
    out.mkdir(parents=True, exist_ok=True)
    wanted = set(formats or ["md", "srt", "vtt", "json"])
    written: dict[str, Path] = {}

    if "md" in wanted:
        p = out / "record.md"
        p.write_text(_render_markdown(record), encoding="utf-8")
        written["md"] = p
    if "srt" in wanted:
        p = out / "record.srt"
        p.write_text(_render_srt(record), encoding="utf-8")
        written["srt"] = p
    if "vtt" in wanted:
        p = out / "record.vtt"
        p.write_text(_render_vtt(record), encoding="utf-8")
        written["vtt"] = p
    if "json" in wanted:
        p = out / "record.json"
        write_json(p, record)
        written["json"] = p

    for fmt, p in written.items():
        print(f"[export] {fmt:4s} -> {p}")
    return written


def _render_markdown(record: RecordDocument) -> str:
    lines = ["# Record", ""]
    lines.append(f"- Sources: {len(record.sources)} · Segments: {len(record.segments)}")
    lines.append(
        f"- Backend: {record.metadata.get('backend')} / {record.metadata.get('model')}"
    )
    lines.append("")
    for seg in record.segments:
        lines.append(
            f"**[{_fmt_ts(seg.start)}–{_fmt_ts(seg.end)}] {seg.speaker or seg.source}**"
        )
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


def _srt_tc(seconds: float) -> str:
    ms = max(0.0, seconds)
    h = int(ms // 3600)
    m = int((ms % 3600) // 60)
    s = ms % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def _render_srt(record: RecordDocument) -> str:
    blocks = []
    for i, seg in enumerate(record.segments, start=1):
        blocks.append(f"{i}\n{_srt_tc(seg.start)} --> {_srt_tc(seg.end)}\n{seg.text}\n")
    return "\n".join(blocks)


def _render_vtt(record: RecordDocument) -> str:
    lines = ["WEBVTT", ""]
    for seg in record.segments:
        lines.append(
            f"{_srt_tc(seg.start).replace(',', '.')} --> {_srt_tc(seg.end).replace(',', '.')}"
        )
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# run / calibrate
# --------------------------------------------------------------------------- #
def run(
    directory: str,
    backend: str = "apple",
    model: str | None = None,
    language: str | None = None,
    model_dir: str | None = None,
    audio_files: list[str] | None = None,
    split: str = "auto",
    glossary: str | None = None,
    chunk_seconds: float = DEFAULT_CHUNK_S,
    overlap_seconds: float = DEFAULT_OVERLAP_S,
    resume: bool = True,
    do_diarize: bool | None = None,
    speakers: int | None = None,
    reference: str | None = None,
    formats: list[str] | None = None,
    attribute_energy: bool = False,
    mixed_source: str | None = None,
    jobs: int = 0,
):
    ingest(directory, audio_files, split=split)
    align(directory, reference=reference)
    transcribe(
        directory,
        backend,
        model=model,
        language=language,
        model_dir=model_dir,
        glossary=glossary,
        chunk_seconds=chunk_seconds,
        overlap_seconds=overlap_seconds,
        resume=resume,
        jobs=jobs,
    )
    sources, _ = ws.load_manifest(Path(directory))
    # Energy attribution (close-mic cross-talk) is an opt-in alternative to
    # spectral diarization; when off, the default `diarize` path is unchanged.
    if attribute_energy and sources:
        attribute(directory, mixed_source=mixed_source)
    else:
        # Per-channel capture already attributes per source, and a single voice
        # must not be split on weak evidence, so diarization is opt-in:
        # `--diarize` forces it; a known `--speakers N` turns it on.
        if do_diarize is None:
            do_diarize = speakers is not None
        if do_diarize and sources:
            diarize(directory, speakers=speakers)
    reconcile(directory, prefer=reference)
    export(directory, formats)


def calibrate_report(directory: str, reference: str | None = None) -> dict:
    d = Path(directory)
    record = ws.load_record(d)
    per_source, meta = ws.load_segments(d)
    meta_sources = meta.get("sources", {})

    def source_duration(src: Source) -> float:
        dur = meta_sources.get(src.id, {}).get("duration")
        if dur is not None:
            return float(dur)
        try:
            data, sr = read_audio(src.path, target_sr=None)
            return float(len(data) / sr)
        except Exception:
            return 0.0

    long_dur = max((source_duration(s) for s in record.sources), default=0.0)
    span = (
        (record.segments[-1].end - record.segments[0].start) if record.segments else 0.0
    )
    confs = [s.confidence for s in record.segments if s.confidence is not None]
    mean_conf = sum(confs) / len(confs) if confs else None

    report: dict = {
        "source_duration": round(long_dur, 3),
        "transcript_span": round(span, 3),
        "coverage": round(span / long_dur, 4) if long_dur else None,
        "segments": len(record.segments),
        "mean_confidence": round(mean_conf, 4) if mean_conf is not None else None,
        "words": sum(len(s.text.split()) for s in record.segments),
    }

    if reference:
        text = Path(reference).read_text(encoding="utf-8")
        hyp = "\n".join(s.text for s in record.segments)
        err = _eval.error_rates(text, hyp)
        report["wer"] = err["wer"]
        report["similarity"] = err["similarity"]

    out = ws.export_dir(d) / "calibration.json"
    write_json(out, report)
    print("\n[calibrate] report:")
    for k, v in report.items():
        print(f"  {k:18s} {v}")
    print(f"  -> {out}")
    return report


# --------------------------------------------------------------------------- #
# synthesize (owner strategy: build the badness, keep the ground truth)
# --------------------------------------------------------------------------- #
def synth(
    directory: str,
    devices: int = 4,
    duration_s: float = 20.0,
    speakers: int = 4,
    seed: int = 0,
) -> dict:
    """Generate a clean multi-speaker scene + degraded per-device recordings,
    plus an exact ground-truth alignment/event timeline.

    This realizes the owner's strategy (logbook
    `10-19-projects/15-clear-record`): genuine bad published multi-track is
    scarce and usually has no correct answer, so we **synthesize the badness**
    and keep the clean aligned ground truth to score recovery against — the
    reliable way to calibrate `align`/`reconcile`.
    """
    d = Path(directory)
    rng = random.Random(seed)
    scene, events = make_scene(duration_s, speakers, seed=seed)
    audio_dir = d / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    sources: list[Source] = []
    devices_meta: list[dict] = []
    for i in range(devices):
        dev_id = f"device_{i}"
        start_s = 0.0 if i == 0 else round(rng.uniform(0.2, 1.4), 3)
        # mild but realistic degradation; kept recoverable so align is scoreable
        deg = dict(
            start_s=start_s,
            drift_ppm=rng.uniform(-60, 60),
            gain=rng.uniform(0.75, 1.25),
            noise=rng.uniform(0.0005, 0.006),
            lowpass_ms=rng.uniform(0.0, 1.2),
            rir_s=rng.uniform(0.04, 0.16),
            dropout_frac=rng.uniform(0.0, 0.02),
            seed=seed * 1000 + i,
        )
        audio, true_offset = synth_record(scene, **deg)
        wav = audio_dir / f"{dev_id}.wav"
        sf.write(str(wav), audio, SYNTH_SR)
        sources.append(
            Source(id=dev_id, path=str(wav), label=f"device{i}", clock_domain="wall")
        )
        devices_meta.append(
            {"id": dev_id, "true_offset_s": round(true_offset, 4), **deg}
        )

    ws.write_manifest(d, sources)
    gt = {
        "scene_duration_s": round(duration_s, 4),
        "devices": devices_meta,
        "events": events,
    }
    write_json(d / "ground_truth.json", gt)

    print(f"[synth] {len(sources)} device(s), {len(events)} speaker event(s) -> {d}")
    for m in devices_meta:
        print(f"  {m['id']:10s} true_offset={m['true_offset_s']:+.4f}s")
    print(f"  ground truth -> {d / 'ground_truth.json'}")
    return gt


__all__ = [
    "align",
    "attribute",
    "calibrate_report",
    "diarize",
    "export",
    "glossary",
    "ingest",
    "reconcile",
    "run",
    "synth",
    "transcribe",
]
