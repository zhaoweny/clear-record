"""The clear-record pipeline stage implementations (ingest / align / transcribe /
reconcile / export / run / calibrate).

Everything here is the thin wiring layer: it reads/writes the workspace files
and delegates the real work to ``clear_record.core`` (domain),
``clear_record.engine`` (audio/align/merge) and ``clear_record.providers``
(ASR). No vendor logic lives here.
"""

from __future__ import annotations

import dataclasses
import random
from collections.abc import Callable
from pathlib import Path

import soundfile as sf

from clear_record.core import (
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    EventSink,
    PipelineOptions,
    Progress,
    RecordDocument,
    Segment,
    Source,
    Step,
    pipeline_spec,
    write_json,
)
from clear_record.engine import (
    SYNTH_SR,
    align_sources,
    attribute_by_source,
    channel_count,
    diarize as diarize_segments,
    make_scene,
    prepare_16k_wav,
    record as synth_record,
    reconcile as reconcile_segments,
)
from clear_record.engine.audio import read_audio
from clear_record.providers import get_backend, probe_ggml_plugin_load

from clear_record.cli import eval as _eval
from clear_record.cli import transcription
from clear_record.cli.workspace import Workspace, discover_audio


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
    *,
    on_event: EventSink | None = None,
) -> list[Source]:
    """Discover/declare sources and normalize them to 16 kHz mono WAV.

    ``split`` controls multi-channel handling:

    - ``"auto"`` (default): split channels when a file has **more than two**
      (clearly a multichannel meeting/DJI-quadraphonic capture); downmix 1–2 ch.
    - ``"split"``: split every channel of any multichannel file into its own
      source — preserves per-speaker isolation ("closest mic wins").
    - ``"mix"``: always downmix to mono.
    """
    w = Workspace.at(directory)
    d = w.root
    files = [Path(a) for a in audio_files] if audio_files else discover_audio(d)
    if not files:
        raise SystemExit(f"[ingest] no audio files found in {d}")
    audio_dir = w.audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)

    progress = Progress(Step.INGEST.value, len(files), on_event)
    progress.start(f"decoding {len(files)} file(s)")
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
        progress.advance(source=base)
    w.write_manifest(sources)
    _print_sources(sources, w)
    return sources


def _print_sources(sources: list[Source], w: Workspace) -> None:
    print(f"[ingest] {len(sources)} source(s) -> {w.manifest_path}")
    for s in sources:
        print(f"  {s.id:24s} {s.path}")


# --------------------------------------------------------------------------- #
# align
# --------------------------------------------------------------------------- #
def align(
    directory: str,
    reference: str | None = None,
    *,
    on_event: EventSink | None = None,
):
    w = Workspace.at(directory)
    sources, _ = w.load_manifest()
    progress = Progress(Step.ALIGN.value, 1, on_event)
    progress.start(f"aligning {len(sources)} source(s)")
    alignment = align_sources(sources, reference_id=reference)
    progress.advance(message=f"reference={alignment.reference}")
    w.write_manifest(sources, alignment)
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
def _load_glossary(w: Workspace, explicit: str | None) -> tuple[str, str]:
    """Return ``(prompt, source)``. The glossary is one term/phrase per line;
    ``#`` comments and blanks are ignored. Capped to stay a sane prompt."""
    path = Path(explicit) if explicit else w.glossary_path
    if not path.exists():
        return "", ""
    if explicit:
        terms = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    else:
        terms = w.glossary_terms()
    return ", ".join(terms)[:2000], str(path)


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
    check_plugin: bool = False,
    *,
    beam_size: int | None = None,
    best_of: int | None = None,
    temperature: float | None = None,
    entropy_thold: float | None = None,
    no_speech_thold: float | None = None,
    max_context: int | None = None,
    threads: int | None = None,
    on_event: EventSink | None = None,
):
    """Transcribe every source, in **resumable overlapping chunks** for long
    tapes, with an optional **glossary** as the decoder's initial prompt.

    The decoder knobs (``beam_size`` … ``threads``) are passed through to the
    backend only when set; unset means "add no flag", so the built command is
    unchanged for a caller that does not ask for tuning.

    This stage is only wiring: the resumable chunking, cache key/invalidation,
    worker pool, cancellation and chunk merge live in
    :mod:`clear_record.cli.transcription`. Here we load the manifest, gate on
    the backend and the opt-in plugin probe, resolve the model once
    (single-threaded, before the pool), then persist and print the result.
    """
    w = Workspace.at(directory)
    sources, _ = w.load_manifest()
    backend = get_backend(backend_id)
    status = backend.availability()
    if not status.available:
        if not backend.info.uses_ggml_plugin:
            hint = "the runtime this backend needs (see docs/adr/0019)"
        elif backend_id == "apple":
            hint = "`brew install whisper-cpp`"
        else:
            hint = "a system `whisper-cli` + a ggml GPU plugin (see README)"
        reason = f" — {status.reason}" if status.reason else ""
        raise SystemExit(
            f"[transcribe] backend '{backend_id}' is not available on this machine"
            f"{reason}.\n"
            f"  Install {hint}; runtime requirements are in docs/adr/0005."
        )

    if check_plugin and not backend.info.uses_ggml_plugin:
        # `--check-plugin` is a whisper-cli/ggml probe; a system-native backend
        # has no plugin to load, so the flag is a documented no-op rather than an
        # inconclusive result.
        w.log(
            f"[transcribe] --check-plugin ignored: backend '{backend_id}' is a "
            f"{backend.info.runtime} backend (it has no ggml plugin)"
        )
    elif check_plugin:
        # Opt-in: `available()` proves the plugin *file* is present, not that the
        # CLI can load it. A one-shot probe (cached per CLI invocation) catches an
        # ABI/build mismatch that would otherwise fall back to CPU silently.
        probe = probe_ggml_plugin_load(backend)
        if probe.loaded is True:
            w.log(f"[transcribe] plugin load probe OK: {probe.detail}")
        elif probe.loaded is False:
            raise SystemExit(
                f"[transcribe] backend '{backend_id}' ggml plugin did not load: "
                f"{probe.detail}.\n  The plugin file is present but whisper-cli "
                f"could not load it (often a ggml ABI/build mismatch); it would "
                f"fall back to CPU."
            )
        else:
            w.log(f"[transcribe] plugin load probe inconclusive: {probe.detail}")

    # Resolve/download the model once, single-threaded, before the chunk pool:
    # ``apple`` is parallelizable, so workers must never race the first-use
    # download the provider would otherwise trigger per chunk.
    backend.prepare(model, model_dir)
    prompt, prompt_src = _load_glossary(w, glossary)
    if prompt:
        w.log(f"[transcribe] glossary: {len(prompt)} chars from {prompt_src}")

    result = transcription.transcribe(
        sources,
        backend,
        transcription.TranscriptionOptions(
            model=model,
            language=language,
            model_dir=model_dir,
            initial_prompt=prompt,
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
            resume=resume,
            jobs=jobs,
            beam_size=beam_size,
            best_of=best_of,
            temperature=temperature,
            entropy_thold=entropy_thold,
            no_speech_thold=no_speech_thold,
            max_context=max_context,
            threads=threads,
        ),
        workspace=w,
        on_event=on_event,
    )

    meta: dict = {
        "backend": backend_id,
        "model": result.model,
        "language": language or "auto",
        "model_dir": model_dir,
        "glossary": prompt_src or None,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": overlap_seconds,
        "jobs": result.jobs,
        "sources": result.source_meta,
    }
    w.write_segments(result.per_source, meta)
    _print_transcription(result.per_source, meta)
    return result.per_source


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
    w = Workspace.at(directory)
    sources, _ = w.load_manifest()
    per_source, meta = w.load_segments()
    src_by_id = {s.id: s for s in sources}
    applied = False
    for sid, segs in per_source.items():
        src = src_by_id.get(sid)
        if not segs or src is None:
            continue
        try:
            audio, sr = read_audio(src.path, 16000)
        except Exception as exc:  # decode failure is non-fatal
            w.log(f"[diarize] {sid}: skipped ({exc})")
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
        w.log(f"[diarize] {sid}: {n_found} speaker(s) over {len(segs)} segment(s)")
    if applied:
        w.write_segments(per_source, meta)
    return per_source


# --------------------------------------------------------------------------- #
# attribute (cross-talk-aware attribution by relative source energy)
# --------------------------------------------------------------------------- #
def attribute(
    directory: str, mixed_source: str | None = None, window_s: float | None = None
):
    """Re-attribute each segment's speaker from the relative source energy.

    Cross-talk correction for close microphones: instead of trusting the source a
    segment came from ("one source == one speaker", the closest-mic-wins rule),
    pick the source with the highest gain-normalized energy in the segment's
    aligned window. A mixed/room reference named by ``mixed_source`` gates weak
    claims and is never itself a speaker candidate. Composable with `reconcile`,
    which preserves the assigned speaker.

    With ``window_s`` set, normalize against each source's **recent** level over a
    causal rolling window of that many seconds (tracking drifting gain) and write
    a calibrated per-segment confidence. Without it, the static whole-recording
    correction is unchanged.
    """
    w = Workspace.at(directory)
    sources, alignment = w.load_manifest()
    per_source, meta = w.load_segments()
    mixed = None
    if mixed_source:
        mixed = next((s for s in sources if s.id == mixed_source), None)
        if mixed is None:
            raise SystemExit(f"[attribute] no source '{mixed_source}' in manifest")
    offsets = dict(alignment.offsets) if alignment else {}
    # The room is a witness, never a speaker: keep it out of the candidate set.
    candidates = [s for s in sources if mixed is None or s.id != mixed.id]

    flat = [seg for segs in per_source.values() for seg in segs]
    if not flat:
        print("[attribute] no segments to attribute")
        return per_source

    # Attribution owns the grouping: the result is keyed by each segment's own
    # `source`, so no positional reassembly over dict order is needed.
    attributed = attribute_by_source(
        flat, candidates, offsets=offsets, mixed=mixed, window_s=window_s
    )
    changed = 0
    for sid, segs in per_source.items():
        updated = attributed.get(sid, [])
        for before, after in zip(segs, updated):
            if before.speaker != after.speaker:
                changed += 1
        per_source[sid] = updated
    # The windowed path also writes a confidence, so persist even if no label
    # changed (otherwise the new confidence would be lost to reconcile).
    if changed or window_s is not None:
        w.write_segments(per_source, meta)
    speakers = {seg.speaker for segs in attributed.values() for seg in segs}
    suffix = f" (room reference: {mixed_source})" if mixed_source else ""
    if window_s is not None:
        suffix += f" (rolling window: {window_s:g}s)"
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
    w = Workspace.at(directory)
    path = w.glossary_path
    if add:
        w.append_glossary(add)
    terms = w.glossary_terms()
    print(f"[glossary] {path} ({len(terms)} term(s))")
    for term in terms:
        print(f"  {term}")
    return path


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #
def reconcile(
    directory: str,
    prefer: str | None = None,
    *,
    on_event: EventSink | None = None,
):
    w = Workspace.at(directory)
    progress = Progress(Step.RECONCILE.value, 1, on_event)
    progress.start()
    sources, alignment = w.load_manifest()
    per_source, meta = w.load_segments()
    segments = reconcile_segments(per_source, alignment, sources)
    progress.advance(message=f"{len(segments)} segment(s)")

    record = RecordDocument(
        sources=tuple(sources),
        alignment=alignment,
        segments=tuple(segments),
        metadata={
            "title": w.root.name,
            "backend": meta.get("backend"),
            "model": meta.get("model"),
            "language": meta.get("language"),
            "prefer": prefer,
        },
    )
    w.write_record(record)
    speakers = {s.speaker for s in segments}
    print(
        f"[reconcile] {len(segments)} segment(s), {len(speakers)} attributed speaker(s)"
        f" -> {w.record_path}"
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
def export(
    directory: str,
    formats: list[str] | None = None,
    *,
    on_event: EventSink | None = None,
) -> dict[str, Path]:
    w = Workspace.at(directory)
    record = w.load_record()
    w.export_dir.mkdir(parents=True, exist_ok=True)
    wanted = set(formats or ["md", "srt", "vtt", "json"])
    written: dict[str, Path] = {}
    progress = Progress(Step.EXPORT.value, len(wanted), on_event)
    progress.start()

    if "md" in wanted:
        p = w.export_file("record.md")
        p.write_text(_render_markdown(record), encoding="utf-8")
        written["md"] = p
        progress.advance(source="md")
    if "srt" in wanted:
        p = w.export_file("record.srt")
        p.write_text(_render_srt(record), encoding="utf-8")
        written["srt"] = p
        progress.advance(source="srt")
    if "vtt" in wanted:
        p = w.export_file("record.vtt")
        p.write_text(_render_vtt(record), encoding="utf-8")
        written["vtt"] = p
        progress.advance(source="vtt")
    if "json" in wanted:
        p = w.export_file("record.json")
        write_json(p, record)
        written["json"] = p
        progress.advance(source="json")

    for fmt, p in written.items():
        print(f"[export] {fmt:4s} -> {p}")
    return written


def _render_markdown(record: RecordDocument) -> str:
    title = record.metadata.get("title")
    lines = [f"# Record — {title}" if title else "# Record", ""]
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
def _run_ingest(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> None:
    ingest(
        directory,
        list(options.audio_files) if options.audio_files else None,
        split=options.split,
        on_event=on_event,
    )


def _run_align(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> None:
    align(directory, reference=options.reference, on_event=on_event)


def _run_transcribe(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> None:
    transcribe(
        directory,
        options.backend,
        model=options.model,
        language=options.language,
        model_dir=options.model_dir,
        glossary=options.glossary,
        chunk_seconds=options.chunk_seconds,
        overlap_seconds=options.overlap_seconds,
        resume=options.resume,
        jobs=options.jobs,
        check_plugin=options.check_plugin,
        beam_size=options.beam_size,
        best_of=options.best_of,
        temperature=options.temperature,
        entropy_thold=options.entropy_thold,
        no_speech_thold=options.no_speech_thold,
        max_context=options.max_context,
        threads=options.threads,
        on_event=on_event,
    )
    sources, _ = Workspace.at(directory).load_manifest()
    # Energy attribution (close-mic cross-talk) is an opt-in alternative to
    # spectral diarization; when off, the default `diarize` path is unchanged.
    if options.attribute_energy and sources:
        attribute(
            directory, mixed_source=options.mixed_source, window_s=options.window_s
        )
    else:
        # Per-channel capture already attributes per source, and a single voice
        # must not be split on weak evidence, so diarization is opt-in:
        # `--diarize` forces it; a known `--speakers N` turns it on.
        do_diarize = options.do_diarize
        if do_diarize is None:
            do_diarize = options.speakers is not None
        if do_diarize and sources:
            diarize(directory, speakers=options.speakers)


def _run_reconcile(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> None:
    reconcile(directory, prefer=options.reference, on_event=on_event)


def _run_export(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> None:
    export(
        directory,
        list(options.formats) if options.formats else None,
        on_event=on_event,
    )


# One runner per declared stage; the drift test checks the keys against the spec.
_STAGE_RUNNERS: dict[Step, Callable[[str, PipelineOptions, EventSink | None], None]] = {
    Step.INGEST: _run_ingest,
    Step.ALIGN: _run_align,
    Step.TRANSCRIBE: _run_transcribe,
    Step.RECONCILE: _run_reconcile,
    Step.EXPORT: _run_export,
}


def run(
    directory: str,
    options: PipelineOptions | None = None,
    *,
    on_event: EventSink | None = None,
) -> None:
    """Run every stage the spec declares, in the spec's order.

    The order is not restated here: it is read from
    :func:`clear_record.core.pipeline_spec`, the same spec the CLI builds its
    subcommands from, and each stage's wiring lives in :data:`_STAGE_RUNNERS`.
    ``on_event`` is threaded to every stage so a caller can follow progress.
    """
    options = options or PipelineOptions()
    for stage in pipeline_spec().stages:
        _STAGE_RUNNERS[stage.step](directory, options, on_event)


def calibrate_report(directory: str, reference: str | None = None) -> dict:
    w = Workspace.at(directory)
    record = w.load_record()
    per_source, meta = w.load_segments()
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

    out = w.export_file("calibration.json")
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

    This realizes the owner's strategy: genuine bad published multi-track is
    scarce and usually has no correct answer, so we **synthesize the badness**
    and keep the clean aligned ground truth to score recovery against — the
    reliable way to calibrate `align`/`reconcile`.
    """
    w = Workspace.at(directory)
    d = w.root
    rng = random.Random(seed)
    scene, events = make_scene(duration_s, speakers, seed=seed)
    audio_dir = w.audio_dir
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

    w.write_manifest(sources)
    gt = {
        "scene_duration_s": round(duration_s, 4),
        "devices": devices_meta,
        "events": events,
    }
    w.write_ground_truth(gt)

    print(f"[synth] {len(sources)} device(s), {len(events)} speaker event(s) -> {d}")
    for m in devices_meta:
        print(f"  {m['id']:10s} true_offset={m['true_offset_s']:+.4f}s")
    print(f"  ground truth -> {w.ground_truth_path}")
    return gt


__all__ = [
    "PipelineOptions",
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
