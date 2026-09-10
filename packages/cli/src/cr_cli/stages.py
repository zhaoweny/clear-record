"""The clear-record pipeline stage implementations (ingest / align / transcribe /
reconcile / export / run / calibrate).

Everything here is the thin wiring layer: it reads/writes the workspace files
and delegates the real work to ``cr_core`` (domain), ``cr_engine`` (audio/align/
merge) and ``cr_providers`` (ASR). No vendor logic lives here.
"""

from __future__ import annotations

import dataclasses
import random
from pathlib import Path

import soundfile as sf

from cr_core import RecordDocument, Segment, Source, write_json
from cr_engine import (
    SYNTH_SR,
    align_sources,
    channel_count,
    make_scene,
    prepare_16k_wav,
    record as synth_record,
    reconcile as reconcile_segments,
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
        f"[align] reference={alignment.reference} method={alignment.method} conf={alignment.confidence}"
    )
    for sid, off in alignment.offsets.items():
        marker = " (ref)" if sid == alignment.reference else ""
        print(f"  {sid:24s} offset={off:+.4f}s{marker}")
    return alignment


# --------------------------------------------------------------------------- #
# transcribe
# --------------------------------------------------------------------------- #
def transcribe(
    directory: str,
    backend_id: str,
    model: str | None = None,
    language: str | None = None,
    model_dir: str | None = None,
):
    d = Path(directory)
    sources, _ = ws.load_manifest(d)
    backend = get_backend(backend_id)
    if not backend.available():
        raise SystemExit(
            f"[transcribe] backend '{backend_id}' is not available on this machine.\n"
            f"  Install its extra (e.g. `uv sync --extra {backend_id}`) and check the runtime "
            f"requirements in docs/adr/0005."
        )

    chosen_model = model or backend.info.default_model
    per_source: dict[str, list[Segment]] = {}
    meta: dict = {
        "backend": backend_id,
        "model": chosen_model,
        "language": language or "auto",
        "model_dir": model_dir,
        "sources": {},
    }
    for src in sources:
        print(f"[transcribe] {src.id}: {backend.info.description} ({chosen_model})")
        res = backend.transcribe(
            src.path, language=language, model=model, model_dir=model_dir
        )
        segs = [dataclasses.replace(s, source=src.id) for s in res.segments]
        per_source[src.id] = segs
        meta["sources"][src.id] = {
            "duration": res.audio_duration,
            "segments": len(segs),
            "language": res.language,
        }
    ws.write_segments(d, per_source, meta)
    _print_transcription(per_source, meta)
    return per_source


def _print_transcription(per_source: dict[str, list[Segment]], meta: dict) -> None:
    print(
        f"[transcribe] {meta.get('model')!r} via {meta.get('backend')} -> segments.json"
    )
    for sid, segs in per_source.items():
        info = meta.get("sources", {}).get(sid, {})
        dur = info.get("duration")
        print(
            f"  {sid:24s} segments={len(segs):4d}  duration={dur if dur is not None else '?'}  "
            f"lang={info.get('language') or '?'}"
        )


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
    reference: str | None = None,
    formats: list[str] | None = None,
):
    ingest(directory, audio_files, split=split)
    align(directory, reference=None)
    transcribe(directory, backend, model=model, language=language, model_dir=model_dir)
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
    "calibrate_report",
    "export",
    "ingest",
    "reconcile",
    "run",
    "synth",
    "transcribe",
]
