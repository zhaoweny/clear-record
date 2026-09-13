"""Resumable, chunked transcription: one module for the stage's hard parts.

This owns the whole chunked transcription stage — chunk planning, the chunk
cache key and invalidation (through the ``Workspace``/``ChunkCache`` seam), the
bounded worker pool, prompt cancellation via an injected
:class:`CancellableProcessRunner`, and the overlap-aware chunk merge.

``clear_record.cli.stages.transcribe`` is only wiring: it loads the manifest,
resolves the backend and model, then calls :func:`transcribe` here. Everything
the tests need
— job sizing and the merge rule — is public in this module, so no test reaches
into a private helper.
"""

from __future__ import annotations

import dataclasses
import glob
import os
import shutil
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from pathlib import Path

import soundfile as sf

from clear_record.core import Segment, Source
from clear_record.engine import (
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    clean_segments,
    plan_chunks,
    write_chunk,
)
from clear_record.engine.audio import read_audio
from clear_record.providers import CancellableProcessRunner

from clear_record.cli.workspace import ChunkCache, Workspace, chunk_cache_key


@dataclasses.dataclass(frozen=True)
class TranscriptionOptions:
    """Everything that shapes a run and its chunk-cache key.

    ``model`` is passed through to the backend as written (the backend resolves
    its own default when it is ``None``); the effective model used for the cache
    key and the record meta is ``model or backend.info.default_model``.
    """

    model: str | None = None
    language: str | None = None
    model_dir: str | None = None
    initial_prompt: str = ""
    chunk_seconds: float = DEFAULT_CHUNK_S
    overlap_seconds: float = DEFAULT_OVERLAP_S
    resume: bool = True
    jobs: int = 0


@dataclasses.dataclass(frozen=True)
class Transcription:
    """The result of a resumable transcription run."""

    per_source: dict[str, list[Segment]]
    source_meta: dict[str, dict]
    jobs: int
    model: str


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


def merge_chunk_segments(chunks: list[list[Segment]]) -> list[Segment]:
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


# --- default job sizing vs. model size and VRAM ---------------------------- #
#
# Each concurrent transcription is its own ``whisper-cli`` process with its own
# copy of the model, so N *large* models can OOM a small-VRAM GPU. The default
# ``--jobs`` is therefore bounded by the model's resident size against the
# detected VRAM. Explicit ``--jobs N`` / ``CR_JOBS=N`` always win (the operator
# may know better); only the ``jobs=0`` auto path is affected.
#
# Approximate resident cost per process, in GB: the ggml checkpoint plus the
# driver context and a 30 s-window activation budget, rounded up.
_MODEL_VRAM_GB: dict[str, float] = {
    "tiny": 0.6,
    "base": 0.7,
    "small": 1.1,
    "medium": 2.1,
    "large": 3.7,
    "large-v1": 3.7,
    "large-v2": 3.7,
    "large-v3": 3.7,
    "large-v3-turbo": 1.7,
    "turbo": 1.7,
    "distil-large-v3": 2.2,
}
# An unrecognised checkpoint is assumed to be a large model: that can only
# lower the default, never raise it.
_UNKNOWN_MODEL_VRAM_GB = _MODEL_VRAM_GB["large"]
# VRAM floor assumed when the GPU cannot be probed. 8 GB matches the sizing
# floor documented in the README; below it the pipeline still allows one worker.
_DEFAULT_VRAM_GB = 8.0
# Leave headroom for the display/compositor and driver overhead.
_VRAM_SAFETY = 0.85
_DEFAULT_MAX_JOBS = 4


def model_vram_gb(model: str | None) -> float:
    """Approximate resident VRAM (GB) for one ``whisper-cli`` process.

    Accepts a size name, a ``ggml-*.bin`` filename, or a path. A quantisation
    suffix (``-q5_0``) is ignored, which over-estimates the model and so errs on
    the safe side; an unknown name is treated as ``large``.
    """
    name = os.path.basename(model or "").lower()
    if name.startswith("ggml-"):
        name = name[len("ggml-") :]
    if name.endswith(".bin"):
        name = name[: -len(".bin")]
    name = name.split("-q", 1)[0]
    if name in _MODEL_VRAM_GB:
        return _MODEL_VRAM_GB[name]
    # Longest-prefix match so e.g. ``large-v3-turbo`` beats ``large``.
    for known in sorted(_MODEL_VRAM_GB, key=len, reverse=True):
        if name.startswith(known):
            return _MODEL_VRAM_GB[known]
    return _UNKNOWN_MODEL_VRAM_GB


def detect_vram_gb() -> float | None:
    """Best-effort total VRAM (GB) of the machine's GPU, or ``None``.

    Advisory only: it bounds the ``jobs=0`` default so a small-VRAM GPU cannot
    OOM. It never gates backend availability — that stays in
    ``clear_record.providers``. ``CR_VRAM_GB`` overrides the probe; NVIDIA is
    read from ``nvidia-smi`` and
    AMD/Intel from the DRM card's ``mem_info_vram_total``.
    """
    override = os.environ.get("CR_VRAM_GB", "").strip()
    if override:
        try:
            value = float(override)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    best = 0
    for node in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        try:
            best = max(best, int(Path(node).read_text(encoding="ascii").strip()))
        except (OSError, ValueError):
            continue
    if best > 0:
        return best / (1024**3)
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        proc = subprocess.run(
            [smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    totals: list[float] = []
    for line in (proc.stdout or "").splitlines():
        try:
            totals.append(float(line.strip()))
        except ValueError:
            continue
    return max(totals) / 1024 if totals else None


def auto_jobs(
    n_pending: int,
    model: str | None,
    vram_gb: float | None,
    cpu: int | None = None,
) -> int:
    """Auto ``--jobs``: CPU, a small fan-out cap, and the VRAM/model budget."""
    cpus = cpu if cpu and cpu > 0 else (os.cpu_count() or 1)
    vram = vram_gb if vram_gb and vram_gb > 0 else _DEFAULT_VRAM_GB
    budget = int(vram * _VRAM_SAFETY / model_vram_gb(model))
    return max(1, min(cpus, _DEFAULT_MAX_JOBS, max(1, budget), n_pending))


def resolve_jobs(
    parallelizable: bool,
    n_pending: int,
    requested: int,
    *,
    model: str | None = None,
    vram_gb: float | None = None,
) -> int:
    """Choose the chunk-transcription worker count adaptively.

    Precedence: explicit ``requested`` (>0), then ``CR_JOBS``, else a small
    default for process-isolated backends. The GPU is the shared bottleneck, so
    the default is modest (measured ~93% ``gpu_busy`` at 4 concurrent
    ``whisper-cli`` processes) and bounded by the model's resident size against
    the detected VRAM (or the 8 GB floor when the GPU cannot be probed). A
    backend that is not process-isolated (shares in-process model state) is
    always serialized, even when asked otherwise.
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
    if vram_gb is None:
        vram_gb = detect_vram_gb()
    return auto_jobs(n_pending, model, vram_gb)


# --- cancellation ---------------------------------------------------------- #
_CANCEL_GRACE_S = 1.0


class _PoolCancelled(Exception):
    """Raised inside a pool worker once cancellation has been requested."""


def _run_pending(
    pending,
    plan_by_id,
    workers: int,
    run_chunk,
    runner: CancellableProcessRunner,
) -> None:
    """Run pending chunks, honouring prompt cancellation.

    The backend launches every ``whisper-cli`` child through ``runner`` -- an
    explicit, pool-scoped :class:`CancellableProcessRunner` -- so on operator
    interruption we cancel the queued futures and terminate only this pool's
    in-flight children, then re-raise, rather than letting
    ``ThreadPoolExecutor.__exit__`` block until every queued chunk has decoded.
    Two pools in one process have separate runners and cannot interfere.
    """
    if workers <= 1:
        try:
            for task in pending:
                if runner.cancelled:
                    raise _PoolCancelled()
                src_id, index, shifted = run_chunk(task)
                plan_by_id[src_id].segments[index] = shifted
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                runner.cancel()
                runner.terminate_all()
            raise
        return

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cr-chunk")
    futures = []
    try:
        futures = [
            pool.submit(_run_chunk_checked, run_chunk, task, runner) for task in pending
        ]
        for future in as_completed(futures):
            src_id, index, shifted = future.result()
            plan_by_id[src_id].segments[index] = shifted
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            # Operator interruption: stop queued work and kill what is already
            # decoding, then return promptly.
            runner.cancel()
            for future in futures:
                future.cancel()
            runner.terminate_all()
            wait(futures, timeout=_CANCEL_GRACE_S)
            runner.terminate_all(grace=0.0)
            pool.shutdown(wait=False, cancel_futures=True)
        else:
            # A chunk failed (e.g. a backend error): finish the already
            # submitted work so its cache is complete, then propagate.
            pool.shutdown(wait=True)
        raise
    else:
        pool.shutdown(wait=True)


def _run_chunk_checked(run_chunk, task, runner: CancellableProcessRunner):
    """Run one chunk on a worker thread, unless the pool already cancelled."""
    if runner.cancelled:
        raise _PoolCancelled()
    return run_chunk(task)


@dataclasses.dataclass
class _ChunkTask:
    """One uncached chunk to transcribe — a unit of parallel work."""

    source_id: str
    index: int
    audio_path: str
    start_s: float
    end_s: float
    cache: ChunkCache
    n_chunks: int


@dataclasses.dataclass
class _SourcePlan:
    """A source's chunk plan plus the per-chunk segments as they fill in."""

    source: Source
    duration: float
    chunks: list[tuple[float, float]]
    segments: list[list[Segment] | None]


def transcribe(
    sources: Sequence[Source],
    backend,
    options: TranscriptionOptions,
    *,
    workspace: Workspace,
) -> Transcription:
    """Transcribe ``sources`` in resumable overlapping chunks.

    The chunk cache is keyed on backend/model/language/glossary/chunk plan;
    matching cached chunks are loaded instead of recomputed. Pending chunks
    (across all sources) run concurrently through one bounded pool when the
    backend is process-isolated (``options.jobs`` overrides the adaptive default;
    backends that share in-process model state are always serialized).
    Cancellation terminates exactly this pool's children through its own
    injected :class:`CancellableProcessRunner`.
    """
    backend_id = backend.info.id
    chosen_model = options.model or backend.info.default_model
    language = options.language
    model_dir = options.model_dir
    prompt = options.initial_prompt
    chunk_seconds = options.chunk_seconds
    overlap_seconds = options.overlap_seconds
    log = workspace.log

    # Plan every source first (cheap I/O), then run the uncached chunks through
    # one bounded pool so the GPU stays fed across source boundaries too.
    plans: list[_SourcePlan] = []
    pending: list[_ChunkTask] = []
    for src in sources:
        duration = _duration(src.path)
        chunks = plan_chunks(duration, chunk_seconds, overlap_seconds)
        cache = workspace.chunk_cache(src.id).ensure()
        run_meta = chunk_cache_key(
            backend=backend_id,
            model=chosen_model,
            language=language,
            glossary=prompt,
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
            n_chunks=len(chunks),
        )

        reuse = options.resume and cache.matches(run_meta)
        if not reuse:
            # Drop chunk results, transient WAVs and any half-written cache file
            # from a previous interrupted pass: a stale body must never be read.
            cache.invalidate()
            cache.write_meta(run_meta)

        log(
            f"[transcribe] {src.id}: {len(chunks)} chunk(s), {duration:.1f}s, "
            f"{backend.info.description} ({chosen_model})"
        )
        segs: list[list[Segment] | None] = [None] * len(chunks)
        for i, (start_s, end_s) in enumerate(chunks):
            if reuse and cache.has_segments(i):
                segs[i] = cache.read_segments(i)
                log(f"[transcribe]   {src.id} chunk {i + 1}/{len(chunks)} cached")
            else:
                pending.append(
                    _ChunkTask(src.id, i, src.path, start_s, end_s, cache, len(chunks))
                )
        plans.append(_SourcePlan(src, duration, chunks, segs))

    plan_by_id = {plan.source.id: plan for plan in plans}
    workers = resolve_jobs(
        backend.info.parallelizable, len(pending), options.jobs, model=chosen_model
    )
    if pending:
        log(
            f"[transcribe] {len(pending)} pending chunk(s), jobs={workers} "
            f"({chosen_model})"
        )

    # One explicit, pool-scoped runner: the backend launches every child
    # through it, so cancellation can terminate exactly this pool's processes.
    runner = CancellableProcessRunner()

    def _run_chunk(task: _ChunkTask) -> tuple[str, int, list[Segment]]:
        chunk_wav = task.cache.audio_path(task.index)
        write_chunk(task.audio_path, chunk_wav, task.start_s, task.end_s)
        try:
            res = backend.transcribe(
                str(chunk_wav),
                language=language,
                model=options.model,
                model_dir=model_dir,
                initial_prompt=prompt or None,
                process_runner=runner,
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
        task.cache.write_segments(task.index, shifted)
        log(
            f"[transcribe]   {task.source_id} chunk {task.index + 1}/{task.n_chunks} "
            f"[{task.start_s:.0f}-{task.end_s:.0f}s] -> {len(shifted)} segment(s)"
        )
        return task.source_id, task.index, shifted

    _run_pending(pending, plan_by_id, workers, _run_chunk, runner)

    per_source: dict[str, list[Segment]] = {}
    source_meta: dict[str, dict] = {}
    for plan in plans:
        merged = merge_chunk_segments([s or [] for s in plan.segments])
        per_source[plan.source.id] = merged
        source_meta[plan.source.id] = {
            "duration": plan.duration,
            "segments": len(merged),
            "language": None,
            "chunks": len(plan.chunks),
        }
    return Transcription(
        per_source=per_source,
        source_meta=source_meta,
        jobs=workers,
        model=chosen_model,
    )


__all__ = [
    "Transcription",
    "TranscriptionOptions",
    "auto_jobs",
    "detect_vram_gb",
    "merge_chunk_segments",
    "model_vram_gb",
    "resolve_jobs",
    "transcribe",
]
