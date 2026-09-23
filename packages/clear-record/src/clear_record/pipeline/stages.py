"""The clear-record pipeline stage implementations.

The module's surface: the five declared stages (ingest / align / transcribe /
reconcile / export); the conveniences that are not stage-derived (diarize /
attribute / glossary); ``run``, which composes them; ``run_cancel_signal``, the
cancel signal the transcribe pool takes off the caller's sink; the model
provisioning (``prepare_model`` / ``download_ggml_model``) that ``service``
reaches the providers through; ``calibration_report``, whose numbers the
`calibrate` command writes into ``export/calibration.json``; and
``format_timestamp``, the one ``HH:MM:SS.mmm`` the Markdown serializer renders
with the command surface's transcript preview — the SRT/VTT renderers go through
``_srt_tc``, which writes the comma form.

A stage **returns** what it produced — the record, the artifact set, the report
of what it did — and reports its progress through the sink it is handed; nothing
here writes to stdout and nothing here exits the process. Two shapes carry that:

- ``PipelineError``, the module's failure channel: a stage that cannot do what
  it was asked raises it with the operator's message, and the caller decides
  what a failure means (the command surface turns it into an exit, the run
  queue records the run as failed, the hello check reports a finding);
- the reports a stage hands back beside its result — ``IngestReport``,
  ``TranscribeReport`` (the meta the stage writes into ``segments.json``),
  ``DiarizeReport`` (one ``DiarizedSource`` per source the pass looked at: the
  counts each source's line states, and the decode failure that skipped a
  source), ``AttributeReport``, ``GlossaryReport`` — because the
  boundary dataclasses are frozen and are never widened to hold them (ADR-0030):
  a gap closes as a report object in this layer instead.

Left out on purpose: ``PipelineOptions``, a re-export of ``core``'s, and
everything private — the ``_run_*`` body one per stage and ``_STAGE_RUNNERS``,
the export serializers, ``_load_glossary``, ``_source_id`` and the ``_eval``
alias. They are machinery the module runs on, not what a caller comes here for.

Everything here is the thin wiring layer: it reads/writes the workspace files
and delegates the real work to ``clear_record.core`` (domain),
``clear_record.engine`` (audio/align/merge) and ``clear_record.providers``
(ASR). No vendor logic lives here.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from pathlib import Path

from clear_record.core import (
    DECODER_KNOB_FIELDS,
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    Alignment,
    ChunkScope,
    EventSink,
    PipelineOptions,
    Progress,
    RecordDocument,
    RunCancelled,
    ScopeError,
    Segment,
    Source,
    Step,
    log_event,
    pipeline_spec,
    write_json,
)
from clear_record.engine import (
    align_sources,
    attribute_by_source,
    channel_count,
    diarize as diarize_segments,
    prepare_16k_wav,
    reconcile as reconcile_segments,
    source_speaker_names,
)
from clear_record.engine.audio import read_audio
from clear_record.providers import (
    download_ggml_model as _providers_download_ggml_model,
    get_backend,
    probe_ggml_plugin_load,
)

from clear_record.pipeline import eval as _eval
from clear_record.pipeline import transcription
from clear_record.pipeline.workspace import Workspace, discover_audio, report_line


# --------------------------------------------------------------------------- #
# the failure channel, and what a stage hands back beside its result
# --------------------------------------------------------------------------- #
class PipelineError(Exception):
    """A stage cannot do what it was asked, carrying the operator's message.

    The pipeline's own failure channel, and the only one: a stage used to end the
    process itself for these, which took its fate out of its caller's hands.
    ``str(exc)`` is the whole message, exactly as it reached the terminal before:
    the command surface prints it and exits, a run records it as the failure, and
    the hello check reports it as a finding.
    """


@dataclasses.dataclass(frozen=True)
class IngestReport:
    """What one ``ingest`` pass produced: the sources, in file order."""

    sources: tuple[Source, ...]


@dataclasses.dataclass(frozen=True)
class TranscribeReport:
    """What one ``transcribe`` pass produced.

    ``meta`` is the mapping the stage writes into ``segments.json`` — the
    backend and model that decoded, and per source its duration, segment count
    and chunk count — returned so a caller renders what the stage did without
    re-reading the file it just wrote. ``attribution`` is the
    ``--attribute-energy`` pass ``run`` drives *inside* this stage (an
    alternative to diarization, not a declared stage); it is ``None`` when that
    pass did not run.
    """

    per_source: dict[str, list[Segment]]
    meta: dict
    attribution: AttributeReport | None = None


@dataclasses.dataclass(frozen=True)
class DiarizedSource:
    """One source ``diarize`` looked at, and what its pass found there.

    ``speakers`` is the distinct label count the clustering decided, ``segments``
    how many segments it was handed, and ``skipped`` the decode failure that took
    the source out of the pass (``None`` when it ran) — a skipped source decided
    nothing, so its ``speakers`` stays 0. Only that decision is missing from the
    returned segments: a skipped source keeps its segments and its old labels, so
    nothing there tells it apart from a source the pass found one speaker in.
    """

    id: str
    segments: int
    speakers: int = 0
    skipped: str | None = None


@dataclasses.dataclass(frozen=True)
class DiarizeReport:
    """What one ``diarize`` pass produced: the segments it holds, by source.

    ``per_source`` is the workspace's segment map with the speaker labels the
    pass applied — a source it found one speaker in keeps the labels it had.
    ``sources`` is one :class:`DiarizedSource` per source the pass looked at, in
    the order the segment map holds them: what the pass decided about each one. A
    source with no segments, or one the manifest does not hold, is not looked at
    and has no entry.
    """

    per_source: dict[str, list[Segment]]
    sources: tuple[DiarizedSource, ...]


@dataclasses.dataclass(frozen=True)
class AttributeReport:
    """What one ``attribute`` pass found, beside the segments it rewrote.

    Each count is measured where the pass measured it: ``segments`` is the flat
    pre-pass list it was handed, ``changed`` the before/after label diff over the
    loaded segments, and ``speakers`` the distinct labels of the segments the
    pass returned — so a reader never has to recompute any of the three from a
    state that no longer exists.
    """

    per_source: dict[str, list[Segment]]
    segments: int
    speakers: int
    changed: int
    mixed_source: str | None = None
    window_s: float | None = None


@dataclasses.dataclass(frozen=True)
class GlossaryReport:
    """The glossary a workspace holds: where it lives and its terms, in order."""

    path: Path
    terms: tuple[str, ...]


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
) -> IngestReport:
    """Discover/declare sources and normalize them to 16 kHz mono WAV.

    ``split`` controls multi-channel handling:

    - ``"auto"`` (default): split channels when a file has **more than two**
      (clearly a multichannel meeting/DJI-quadraphonic capture); downmix 1–2 ch.
    - ``"split"``: split every channel of any multichannel file into its own
      source — preserves per-speaker isolation ("closest mic wins").
    - ``"mix"``: always downmix to mono.

    No audio to ingest is an actionable :class:`PipelineError`, not an empty
    report: a run that discovered nothing has nothing to say.

    Each input's decode line is reported on the sink (:func:`report_line`) as
    that decode begins — one line per decode, before normalizing that input, not
    batched after the pass — so a caller watching a long ingest sees every file
    announced where its work starts.
    """
    w = Workspace.at(directory)
    d = w.root
    files = [Path(a) for a in audio_files] if audio_files else discover_audio(d)
    if not files:
        raise PipelineError(f"[ingest] no audio files found in {d}")
    audio_dir = w.audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)

    progress = Progress(Step.INGEST.value, len(files), on_event)
    progress.start(f"decoding {len(files)} file(s)")
    # A source's ``label`` is the speaker name ``reconcile``/``attribute`` fall
    # back to. Never derive it from the file name: a tape's name is not a person,
    # and the minutes must not list one as an attendee. Channels are speaker-like,
    # so every source gets a positional ``Speaker N`` (an explicit caller label
    # still wins in ``engine.merge.source_speaker_names``).
    sources: list[Source] = []
    for number, p in enumerate(files, start=1):
        base = _source_id(p, d)
        nch = channel_count(p)
        do_split = (split == "split") or (split == "auto" and nch > 2)
        if do_split and nch > 1:
            # One source per channel: this is the multi-mic meeting case, where
            # each speaker is nearest one channel and diarization is nearly free.
            for ch in range(nch):
                sid = f"{base}__ch{ch + 1}"
                norm = audio_dir / f"{sid}.wav"
                # The line announces this decode, where the pre-move CLI printed
                # it: before normalizing that input, not batched after the pass.
                report_line(
                    w,
                    on_event,
                    Step.INGEST.value,
                    f"[ingest] decode {p.name} ch{ch + 1}/{nch} -> {norm.name}",
                    source=sid,
                    index=number - 1,
                    total=len(files),
                )
                prepare_16k_wav(p, norm, channel=ch)
                sources.append(
                    Source(
                        id=sid,
                        path=str(norm),
                        label=f"Speaker {len(sources) + 1}",
                        clock_domain="wall",
                    )
                )
        else:
            norm = audio_dir / f"{base}.wav"
            report_line(
                w,
                on_event,
                Step.INGEST.value,
                f"[ingest] decode {p.name} -> {norm.name}",
                source=base,
                index=number - 1,
                total=len(files),
            )
            prepare_16k_wav(p, norm)
            sources.append(
                Source(
                    id=base,
                    path=str(norm),
                    label=f"Speaker {len(sources) + 1}",
                    clock_domain="wall",
                )
            )
        progress.advance(source=base)
    w.write_manifest(sources)
    return IngestReport(sources=tuple(sources))


# --------------------------------------------------------------------------- #
# align
# --------------------------------------------------------------------------- #
def align(
    directory: str,
    reference: str | None = None,
    *,
    on_event: EventSink | None = None,
) -> Alignment:
    w = Workspace.at(directory)
    sources, _ = w.load_manifest()
    progress = Progress(Step.ALIGN.value, 1, on_event)
    progress.start(f"aligning {len(sources)} source(s)")
    alignment = align_sources(sources, reference_id=reference)
    progress.advance(message=f"reference={alignment.reference}")
    w.write_manifest(sources, alignment)
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


def prepare_model(
    backend_id: str, model: str | None = None, model_dir: str | None = None
) -> str:
    """Resolve a backend's model, downloading and verifying it on first use.

    The same pinned, checksum-verified fetch a normal run performs, exposed on
    its own so the setup wizard can offer an explicit, user-triggered download.
    Nothing calls it implicitly: the hello-world acceptance check never
    downloads.

    It belongs to the pipeline layer: provisioning a stage's model is pipeline
    work, and ``service`` reaches ``providers`` through that layer.
    """
    return get_backend(backend_id).prepare(model, model_dir)


def download_ggml_model(model: str, model_dir: str | None = None) -> str:
    """Download a named ggml checkpoint, independent of the active backend.

    The bridge ``service`` uses, from the pipeline layer that may import
    ``providers``. Unlike :func:`prepare_model`, the argument is always a ggml
    name/size, so a named download fetches exactly ``ggml-<model>.bin`` whatever
    backend this machine prefers -- on macOS 26 that is ``apple-speech``, whose
    ``prepare`` provisions a language asset rather than a ggml checkpoint.
    """
    return _providers_download_ggml_model(model, model_dir)


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
    rerun_sources: tuple[str, ...] | None = None,
    rerun_range: str | None = None,
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

    ``rerun_sources``/``rerun_range`` are an explicit **re-run scope**: only the
    chunks they select are re-decoded, and every other chunk is reused from the
    cache. A scope that names an unknown source, has an unreadable range, or
    selects no chunk is an actionable :class:`PipelineError` — never a silent
    full pass.

    This stage is only wiring: the resumable chunking, cache key/invalidation,
    worker pool, cancellation and chunk merge live in
    :mod:`clear_record.pipeline.transcription`. Here we load the manifest, gate on
    the backend and the opt-in plugin probe, resolve the model once
    (single-threaded, before the pool), then persist the result and return the
    report the caller renders.
    """
    # Captured before any other local exists in this frame: exactly the
    # decoder-knob values this call received, keyed by the shared declaration
    # (``DECODER_KNOB_FIELDS``) rather than restated a second time below when
    # ``TranscriptionOptions`` is built. A knob this function's signature does
    # not yet accept would already have failed at the call above; a knob this
    # dict does not yet know about (the declaration grew and this frame's
    # keyword-only parameters did not follow) raises ``KeyError`` here, loudly,
    # instead of the value silently never reaching the backend.
    _received = locals()
    decoder_values = {name: _received[name] for name in DECODER_KNOB_FIELDS}
    w = Workspace.at(directory)
    try:
        scope = ChunkScope.parse(rerun_sources, rerun_range)
    except ScopeError as exc:
        raise PipelineError(f"[transcribe] {exc}") from exc
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
        reason = f" — {str(status.reason)}" if status.reason else ""
        log_event(
            "error",
            "transcribe",
            "backend.unavailable",
            backend=backend_id,
            reason=str(status.reason),
        )
        raise PipelineError(
            f"[transcribe] backend '{backend_id}' is not available on this machine"
            f"{reason}.\n"
            f"  Install {hint}; runtime requirements are in docs/adr/0005."
        )

    if check_plugin and not backend.info.uses_ggml_plugin:
        # `--check-plugin` is a whisper-cli/ggml probe; a system-native backend
        # has no plugin to load, so the flag is a documented no-op rather than an
        # inconclusive result.
        report_line(
            w,
            on_event,
            "transcribe",
            f"[transcribe] --check-plugin ignored: backend '{backend_id}' is a "
            f"{backend.info.runtime} backend (it has no ggml plugin)",
        )
    elif check_plugin:
        # Opt-in: `available()` proves the plugin *file* is present, not that the
        # CLI can load it. A one-shot probe (cached per CLI invocation) catches an
        # ABI/build mismatch that would otherwise fall back to CPU silently.
        probe = probe_ggml_plugin_load(backend)
        if probe.loaded is True:
            report_line(
                w,
                on_event,
                "transcribe",
                f"[transcribe] plugin load probe OK: {probe.detail}",
            )
        elif probe.loaded is False:
            log_event(
                "error",
                "transcribe",
                "plugin.load_failed",
                backend=backend_id,
                detail=probe.detail,
            )
            raise PipelineError(
                f"[transcribe] backend '{backend_id}' ggml plugin did not load: "
                f"{probe.detail}.\n  The plugin file is present but whisper-cli "
                f"could not load it (often a ggml ABI/build mismatch); it would "
                f"fall back to CPU."
            )
        else:
            report_line(
                w,
                on_event,
                "transcribe",
                f"[transcribe] plugin load probe inconclusive: {probe.detail}",
            )

    # Resolve/download the model once, single-threaded, before the chunk pool:
    # ``apple`` is parallelizable, so workers must never race the first-use
    # download the provider would otherwise trigger per chunk.
    backend.prepare(model, model_dir)
    log_event(
        "info",
        "transcribe",
        "backend.selected",
        backend=backend_id,
        model=model or backend.info.default_model,
        language=language or "auto",
        jobs=jobs,
    )
    prompt, prompt_src = _load_glossary(w, glossary)
    if prompt:
        report_line(
            w,
            on_event,
            "transcribe",
            f"[transcribe] glossary: {len(prompt)} chars from {prompt_src}",
        )

    try:
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
                scope=scope,
                **decoder_values,
            ),
            workspace=w,
            on_event=on_event,
            cancel=run_cancel_signal(on_event),
        )
    except ScopeError as exc:
        # A scope that cannot be honoured is a usage problem, not a crash: name
        # it and stop, rather than falling back to an unscoped full pass.
        raise PipelineError(str(exc)) from exc
    except transcription.UnsupportedDecoderKnob as exc:
        # A decoder knob this backend cannot honour (see
        # `transcription.transcribe`): a usage problem, so carry it as an
        # actionable error rather than letting it become a traceback.
        raise PipelineError(str(exc)) from exc

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
        # What the run cost: re-decoded vs reused chunks (and, for a scoped
        # re-run, how many reused chunks still carry an earlier glossary). This
        # is the loop's economics, recorded so a later pass can show it.
        "chunk_report": dataclasses.asdict(result.chunk_report),
        # Peak RSS of this stage's decoder workers, or None with the reason it
        # could not be measured (never zero) -- the record's memory axis.
        "peak_rss_bytes": result.peak_rss_bytes,
        "peak_rss_reason": result.peak_rss_reason,
    }
    # Stamp each raw segment with its source's speaker name before it is
    # written: the agent flow can read ``segments.json`` without a reconcile
    # pass, and the transcript must never present a tape's file name as a person.
    names = source_speaker_names(sources)
    per_source = {
        sid: [
            dataclasses.replace(seg, speaker=seg.speaker or names[sid]) for seg in segs
        ]
        for sid, segs in result.per_source.items()
    }
    w.write_segments(per_source, meta)
    log_event(
        "info",
        "transcribe",
        "transcribe.finished",
        backend=backend_id,
        model=result.model,
        sources=len(per_source),
    )
    return TranscribeReport(per_source=per_source, meta=meta)


# --------------------------------------------------------------------------- #
# diarize (multi-speaker attribution for a single mixed stream)
# --------------------------------------------------------------------------- #
def diarize(
    directory: str,
    speakers: int | None = None,
    *,
    on_event: EventSink | None = None,
) -> DiarizeReport:
    """Assign speaker labels to segments per source (baseline spectral clustering).

    For per-channel sources this is harmless (each channel is one speaker, so the
    labels collapse to one and are left as the source label). For a single mixed
    stream it is how you get `Speaker 1/2/…` in the record.

    What the pass produced comes back as a :class:`DiarizeReport`: the segments
    with the labels it applied and, per source, the counts that source's line
    states, plus the decode failure that took a source out of the pass — the one
    fact the returned segments cannot carry (see :class:`DiarizedSource`). Each
    source's line is reported as it is decided, on the sink, one source at a time:
    a long tape's diarization is minutes of work, and its count is worth watching
    arrive.
    """
    w = Workspace.at(directory)
    sources, _ = w.load_manifest()
    per_source, meta = w.load_segments()
    src_by_id = {s.id: s for s in sources}
    total = len(per_source)
    applied = False
    facts: list[DiarizedSource] = []
    for index, (sid, segs) in enumerate(per_source.items(), start=1):
        src = src_by_id.get(sid)
        if not segs or src is None:
            continue
        try:
            audio, sr = read_audio(src.path, 16000)
        except Exception as exc:  # decode failure is non-fatal
            facts.append(DiarizedSource(id=sid, segments=len(segs), skipped=str(exc)))
            report_line(
                w,
                on_event,
                "diarize",
                f"[diarize] {sid}: skipped ({exc})",
                source=sid,
                index=index,
                total=total,
            )
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
        facts.append(DiarizedSource(id=sid, segments=len(segs), speakers=n_found))
        report_line(
            w,
            on_event,
            "diarize",
            f"[diarize] {sid}: {n_found} speaker(s) over {len(segs)} segment(s)",
            source=sid,
            index=index,
            total=total,
        )
    if applied:
        w.write_segments(per_source, meta)
    return DiarizeReport(per_source=per_source, sources=tuple(facts))


# --------------------------------------------------------------------------- #
# attribute (cross-talk-aware attribution by relative source energy)
# --------------------------------------------------------------------------- #
def attribute(
    directory: str, mixed_source: str | None = None, window_s: float | None = None
) -> AttributeReport:
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

    Each count the summary reports is measured where the pass measures it — the
    pre-pass list, the before/after label diff, the labels it returned — and comes
    back with the segments, so a reader recomputes none of them (see
    :class:`AttributeReport`).
    """
    w = Workspace.at(directory)
    sources, alignment = w.load_manifest()
    per_source, meta = w.load_segments()
    mixed = None
    if mixed_source:
        mixed = next((s for s in sources if s.id == mixed_source), None)
        if mixed is None:
            raise PipelineError(f"[attribute] no source '{mixed_source}' in manifest")
    offsets = dict(alignment.offsets) if alignment else {}
    # The room is a witness, never a speaker: keep it out of the candidate set.
    candidates = [s for s in sources if mixed is None or s.id != mixed.id]

    flat = [seg for segs in per_source.values() for seg in segs]
    if not flat:
        return AttributeReport(
            per_source=per_source,
            segments=0,
            speakers=0,
            changed=0,
            mixed_source=mixed_source,
            window_s=window_s,
        )

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
    return AttributeReport(
        per_source=per_source,
        segments=len(flat),
        speakers=len(speakers),
        changed=changed,
        mixed_source=mixed_source,
        window_s=window_s,
    )


# --------------------------------------------------------------------------- #
# glossary
# --------------------------------------------------------------------------- #
def glossary(directory: str, add: list[str] | None = None) -> GlossaryReport:
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
    return GlossaryReport(path=path, terms=tuple(w.glossary_terms()))


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #
def reconcile(
    directory: str,
    prefer: str | None = None,
    *,
    on_event: EventSink | None = None,
) -> RecordDocument:
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
    return record


def format_timestamp(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60.0)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export(
    directory: str,
    *,
    on_event: EventSink | None = None,
) -> dict[str, Path]:
    w = Workspace.at(directory)
    record = w.load_record()
    w.export_dir.mkdir(parents=True, exist_ok=True)
    # The declared artifact set (the spec's "write Markdown/SRT/VTT/JSON
    # artifacts"); every one of them is written.
    wanted = {"md", "srt", "vtt", "json"}
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
            f"**[{format_timestamp(seg.start)}–{format_timestamp(seg.end)}] {seg.speaker or seg.source}**"
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
) -> IngestReport:
    return ingest(
        directory,
        list(options.audio_files) if options.audio_files else None,
        split=options.split,
        on_event=on_event,
    )


def _run_align(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> Alignment:
    return align(directory, reference=options.reference, on_event=on_event)


def _run_transcribe(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> TranscribeReport:
    report = transcribe(
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
        rerun_sources=options.rerun_sources,
        rerun_range=options.rerun_range,
        on_event=on_event,
        # Every set decoder knob, derived from ``options`` (a ``PipelineOptions``,
        # which carries the shared ``DecoderKnobs`` fields) rather than restated
        # by name here: a knob the declaration grows still reaches the stage.
        **options.decoder_knobs(),
    )
    sources, _ = Workspace.at(directory).load_manifest()
    # Energy attribution (close-mic cross-talk) is an opt-in alternative to
    # spectral diarization; when off, the default `diarize` path is unchanged.
    if options.attribute_energy and sources:
        # The attribution pass is part of what this stage produced, so its report
        # rides with the transcribe report: `run` renders one stage, and this is
        # the stage that ran it.
        attribution = attribute(
            directory, mixed_source=options.mixed_source, window_s=options.window_s
        )
        report = dataclasses.replace(report, attribution=attribution)
    else:
        # Per-channel capture already attributes per source, and a single voice
        # must not be split on weak evidence, so diarization is opt-in:
        # `--diarize` forces it; a known `--speakers N` turns it on.
        do_diarize = options.do_diarize
        if do_diarize is None:
            do_diarize = options.speakers is not None
        if do_diarize and sources:
            diarize(directory, speakers=options.speakers, on_event=on_event)
    return report


def _run_reconcile(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> RecordDocument:
    return reconcile(directory, prefer=options.reference, on_event=on_event)


def _run_export(
    directory: str, options: PipelineOptions, on_event: EventSink | None
) -> dict[str, Path]:
    return export(directory, on_event=on_event)


def run_cancel_signal(on_event: EventSink | None) -> threading.Event | None:
    """The run's cancel signal, when the caller's sink is a run queue channel.

    A run's cancel travels with the sink the run queue hands a pipeline (RUN-04):
    the queue's channel both raises on the next report and exposes the signal
    itself, which is what the transcribe pool needs — a chunk already decoding
    must be able to stop promptly rather than at the next report. A plain sink
    (the CLI, a test) carries none, so this answers ``None`` and nothing changes
    for a caller that is not a run.
    """
    return getattr(on_event, "signal", None)


# One runner per declared stage; the drift test checks the keys against the spec.
# A runner returns what its stage produced, and ``run`` hands that value on.
_STAGE_RUNNERS: dict[
    Step, Callable[[str, PipelineOptions, EventSink | None], object]
] = {
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
    on_result: Callable[[Step, object], None] | None = None,
) -> None:
    """Run every stage the spec declares, in the spec's order.

    The order is not restated here: it is read from
    :func:`clear_record.core.pipeline_spec`, the same spec the CLI builds its
    subcommands from, and each stage's wiring lives in :data:`_STAGE_RUNNERS`.
    ``on_event`` is threaded to every stage so a caller can follow progress.

    ``on_result`` receives each stage's step and the value that stage returned,
    **as that stage lands**. A caller that renders the stages needs them in the
    order they ran — the command surface does, and a mapping collected and
    returned at the end would put every block after the last stage's live lines.
    A caller that only runs the pipeline (``service``) passes nothing.
    """
    options = options or PipelineOptions()
    log_event(
        "info",
        "cli",
        "cli.run.started",
        backend=options.backend,
        model=options.model,
        language=options.language,
        jobs=options.jobs,
    )
    try:
        for stage in pipeline_spec().stages:
            name = stage.step.value
            log_event("info", "stage", "stage.started", stage=name)
            result = _STAGE_RUNNERS[stage.step](directory, options, on_event)
            if on_result is not None:
                on_result(stage.step, result)
            log_event("info", "stage", "stage.finished", stage=name)
    except RunCancelled:
        # A cancellation is an outcome, not a failure: the run queue records the
        # run as ``stopped``, so the log must not call it a failed pipeline.
        log_event("info", "cli", "cli.run.stopped")
        raise
    except Exception as exc:  # the failure path already raises
        log_event(
            "error",
            "cli",
            "cli.run.failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    log_event("info", "cli", "cli.run.finished")


def calibration_report(directory: str, reference: str | None = None) -> dict:
    """The raw calibration numbers a workspace yields, with no side effects.

    This is the one implementation of the accuracy arithmetic: the CLI's
    ``calibrate`` report writes it out, and the console's accuracy axis reads
    it (through :func:`clear_record.service.benchmark.run_axes`), so the two
    surfaces cannot disagree. ``coverage`` is the transcript span over the
    longest source; ``mean_confidence`` is over the segments that carry one;
    ``wer``/``similarity`` appear only when ``reference`` names a reference
    transcript file.
    """
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

    return report


__all__ = [
    "AttributeReport",
    "DiarizeReport",
    "DiarizedSource",
    "GlossaryReport",
    "IngestReport",
    "PipelineError",
    "PipelineOptions",
    "TranscribeReport",
    "align",
    "attribute",
    "calibration_report",
    "diarize",
    "download_ggml_model",
    "export",
    "format_timestamp",
    "glossary",
    "ingest",
    "prepare_model",
    "reconcile",
    "run",
    "run_cancel_signal",
    "transcribe",
]
