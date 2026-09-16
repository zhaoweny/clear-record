"""Resumable, chunked transcription: one module for the stage's hard parts.

This owns the whole chunked transcription stage — chunk planning, the chunk
cache key and invalidation (through the ``Workspace``/``ChunkCache`` seam), the
bounded worker pool, prompt cancellation via an injected
:class:`CancellableProcessRunner`, and the overlap-aware chunk merge.

It also owns the **scoped re-run** decision: which cached chunks a run reuses and
which it re-decodes, and the tally that reports it. A
:class:`clear_record.core.ChunkScope` (or no scope) plus a conservative
near-match guard decide per chunk; see :func:`transcribe` and
:class:`ChunkReport`.

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

from clear_record.core import (
    DECODER_KNOB_FIELDS,
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    ChunkScope,
    EventSink,
    Progress,
    ScopeError,
    Segment,
    Source,
)
from clear_record.core.i18n import tr
from clear_record.engine import (
    changed_terms,
    clean_segments,
    plan_chunks,
    term_could_affect,
    write_chunk,
)
from clear_record.engine.audio import read_audio
from clear_record.providers import CancellableProcessRunner

from clear_record.cli.workspace import (
    ChunkCache,
    Workspace,
    chunk_cache_key,
    chunk_glossary,
    glossary_digest,
    plan_matches,
)


class UnsupportedDecoderKnob(ValueError):
    """A requested decoder knob the resolved backend does not advertise.

    A :class:`ValueError` so the in-process seam keeps its documented contract,
    but distinct so the CLI can turn exactly this usage problem into an
    actionable error without relabelling an unrelated ``ValueError`` from the
    backend or the workspace.
    """


@dataclasses.dataclass(frozen=True)
class TranscriptionOptions:
    """Everything that shapes a run and its chunk-cache key.

    ``model`` is passed through to the backend as written (the backend resolves
    its own default when it is ``None``); the effective model used for the cache
    key and the record meta is ``model or backend.info.default_model``.

    ``scope`` is a re-run scope, not part of the cache key: it decides which
    chunks this run is allowed to re-decode, while the cache key decides which
    chunks *may* be reused at all.
    """

    model: str | None = None
    language: str | None = None
    model_dir: str | None = None
    initial_prompt: str = ""
    chunk_seconds: float = DEFAULT_CHUNK_S
    overlap_seconds: float = DEFAULT_OVERLAP_S
    resume: bool = True
    jobs: int = 0
    scope: ChunkScope | None = None
    # Decoder knobs; ``None`` = unset (the backend's own default applies).
    beam_size: int | None = None
    best_of: int | None = None
    temperature: float | None = None
    entropy_thold: float | None = None
    no_speech_thold: float | None = None
    max_context: int | None = None
    threads: int | None = None

    def decoder_knobs(self) -> dict[str, object]:
        """The decoder knobs that are set, keyed by field name."""
        out: dict[str, object] = {}
        for name in DECODER_KNOB_FIELDS:
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


@dataclasses.dataclass(frozen=True)
class ChunkReport:
    """What a transcription run cost, in re-decoded vs reused chunks.

    Reported rather than inferred: the whole point of a scoped re-run is that it
    is cheaper, and a run that does not say how much is cheaper has to be
    believed. ``carried_over`` is the honest part — chunks that were reused while
    they still carry an earlier glossary, which only an explicit scope permits.
    """

    scoped: bool = False
    scope: str = ""
    reused: int = 0
    redecoded: int = 0
    carried_over: int = 0
    guard_redecoded: int = 0


@dataclasses.dataclass(frozen=True)
class Transcription:
    """The result of a resumable transcription run."""

    per_source: dict[str, list[Segment]]
    source_meta: dict[str, dict]
    jobs: int
    model: str
    chunk_report: ChunkReport = dataclasses.field(default_factory=ChunkReport)


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
# A unified-memory GPU (an NVIDIA GB10 / DGX Spark, or any iGPU) has no dedicated
# framebuffer, so ``nvidia-smi`` memory fields report ``N/A`` and the DRM path
# (amdgpu-only) finds nothing. The probe then claims this *fraction* of total
# system memory (``/proc/meminfo`` ``MemTotal``). Half is deliberately
# conservative: that same RAM backs the CPU, the compositor and every other
# process, and ``_VRAM_SAFETY`` (0.85) is applied on top, so the pool's budget is
# ~42% of RAM. On a 128 GB DGX Spark that is ~62 GB — far above the 4-way cap —
# while a small UMA box stays under-provisioned rather than handed memory it does
# not have. ``MemTotal`` (capacity) is used rather than ``MemAvailable`` so the
# advisory probe is stable, not a snapshot of the moment.
_UMA_MEMORY_SHARE = 0.5
# ``/proc/meminfo`` is Linux-only: off Linux it is simply absent and the 8 GB
# floor applies, with no new dependency and no platform-specific import.
_PROC_MEMINFO = Path("/proc/meminfo")
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
    """Best-effort VRAM (GB) the worker pool may claim, or ``None``.

    Advisory only: it bounds the ``jobs=0`` default so a small-VRAM GPU cannot
    OOM. It never gates backend availability — that stays in
    ``clear_record.providers``. Probes, highest precedence first:

    1. ``CR_VRAM_GB`` (the operator override);
    2. the DRM card's ``mem_info_vram_total`` (amdgpu);
    3. ``nvidia-smi --query-gpu=memory.total``;
    4. when ``nvidia-smi`` runs but reports no numeric total — the unified-memory
       signature on a GPU with no dedicated framebuffer — a conservative share
       of ``/proc/meminfo`` ``MemTotal`` (see :data:`_UMA_MEMORY_SHARE`);
    5. otherwise ``None``, and :func:`auto_jobs` uses the 8 GB floor.
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
    # A failed query (no device attached, driver hiccup) tells us nothing about
    # memory, so it must not be mistaken for UMA: only a successful query with a
    # device row may be read as unified memory. A row that yields no number
    # (``N/A`` / ``Not Supported``) is exactly that signal.
    totals: list[float] = []
    rows = 0
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        rows += 1
        try:
            totals.append(float(line.strip()))
        except ValueError:
            continue
    if proc.returncode != 0 or rows == 0:
        return None
    if totals:
        return max(totals) / 1024
    return _uma_vram_gb()


def _uma_vram_gb() -> float | None:
    """Claimable VRAM (GB) on a unified-memory GPU, or ``None`` off Linux.

    Called only when ``nvidia-smi`` ran but reported no numeric total, which on
    an iGPU means memory is shared with the CPU (NVIDIA's own guidance for the
    DGX Spark/GB10). Read total system memory from ``/proc/meminfo`` and claim
    only :data:`_UMA_MEMORY_SHARE` of it. That file is Linux-only, so elsewhere
    this returns ``None`` — the 8 GB floor then applies — without a new
    dependency.
    """
    try:
        text = _PROC_MEMINFO.read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.startswith("MemTotal:"):
            continue
        try:
            kib = float(line.split()[1])
        except (IndexError, ValueError):
            return None
        return kib / (1024**2) * _UMA_MEMORY_SHARE
    return None


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


# What to do with one cached chunk. ``_REUSE`` and ``_CARRY`` both reuse a body
# and differ only in the provenance they record; ``_REDECODE`` and ``_GUARD``
# both re-decode one and differ only in *why* (the guard is the conservative
# widening of an explicit scope).
_REUSE = "reuse"
_CARRY = "carry"
_REDECODE = "redecode"
_GUARD = "guard"


def _validate_scope(
    scope: ChunkScope, sources: Sequence[Source], *, resume: bool
) -> None:
    """Refuse a scope that cannot be honoured before any work is planned.

    A scope is an assertion about *reusing* chunks, so it is meaningless with
    resume off; and a source name that is not in the manifest is a typo, not an
    instruction to re-decode everything.
    """
    if not resume:
        raise ScopeError(
            tr(
                "[transcribe] a re-run scope only means something when cached "
                "chunks may be reused, but resume is off; drop one of the two."
            )
        )
    known = {src.id for src in sources}
    unknown = [name for name in scope.sources if name not in known]
    if unknown:
        available = ", ".join(sorted(known)) or "none"
        raise ScopeError(
            tr(
                "[transcribe] re-run scope names source(s) not in the manifest: "
                "{unknown} (available: {available}).",
                unknown=", ".join(unknown),
                available=available,
            )
        )


def _empty_scope_message(scope: ChunkScope, durations: dict[str, float]) -> str:
    """The actionable error for a scope that selects no chunk at all.

    Silence here would mean either a full re-decode (scope ignored) or a no-op
    (scope treated as "nothing to do"); both are worse than saying so.
    """
    looked_at = (
        ", ".join(
            f"{sid}={seconds:.1f}s"
            for sid, seconds in durations.items()
            if scope.selects_source(sid)
        )
        or "no source"
    )
    return tr(
        "[transcribe] re-run scope ({scope}) selects no chunk of {looked_at}; "
        "it would re-decode nothing. Widen the range or drop the scope.",
        scope=scope.describe(),
        looked_at=looked_at,
    )


def transcribe(
    sources: Sequence[Source],
    backend,
    options: TranscriptionOptions,
    *,
    workspace: Workspace,
    on_event: EventSink | None = None,
) -> Transcription:
    """Transcribe ``sources`` in resumable overlapping chunks.

    The chunk cache is keyed on backend/model/language/glossary/chunk plan;
    matching cached chunks are loaded instead of recomputed. Pending chunks
    (across all sources) run concurrently through one bounded pool when the
    backend is process-isolated (``options.jobs`` overrides the adaptive default;
    backends that share in-process model state are always serialized).
    Cancellation terminates exactly this pool's children through its own
    injected :class:`CancellableProcessRunner`.

    A backend that declares ``chunked=False`` — a whole-file OS service — is
    handed each source as a single window; the cache and the worker pool are
    otherwise unchanged (ADR-0019).

    With an ``options.scope``, only the chunks the scope selects are re-decoded;
    every other chunk is reused from the cache. A reused chunk whose body was
    decoded under an earlier glossary is *reported* as carried over and its old
    digest is kept, so an unscoped run still re-decodes it — the scope is an
    explicit, one-way assertion, never a silent claim that the glossary applied.
    The near-match guard can only add chunks to the re-decode set.
    """
    backend_id = backend.info.id
    chosen_model = options.model or backend.info.default_model
    language = options.language
    model_dir = options.model_dir
    prompt = options.initial_prompt
    chunk_seconds = options.chunk_seconds
    overlap_seconds = options.overlap_seconds
    scope = options.scope
    log = workspace.log

    # Decoder knobs are passed to the backend only when set. A backend that does
    # not advertise a requested knob must fail loudly here, not silently drop it.
    decoders = options.decoder_knobs()
    supported = tuple(getattr(backend.info, "decoder_knobs", ()) or ())
    unsupported = sorted(set(decoders) - set(supported))
    if unsupported:
        raise UnsupportedDecoderKnob(
            tr(
                "[transcribe] backend '{backend}' cannot honour decoder "
                "option(s): {unsupported}. It supports: {supported}.",
                backend=backend_id,
                unsupported=", ".join(unsupported),
                supported=", ".join(supported) or "none",
            )
        )

    if scope is not None:
        _validate_scope(scope, sources, resume=options.resume)

    # Plan every source first (cheap I/O, and no cache writes yet). A scope that
    # selects nothing must fail *before* the cache is touched: a typo must not
    # invalidate a good cache on its way to an error.
    planned: list[tuple[Source, float, list[tuple[float, float]]]] = []
    for src in sources:
        duration = _duration(src.path)
        if backend.info.chunked:
            chunks = plan_chunks(duration, chunk_seconds, overlap_seconds)
        else:
            # A whole-file/streaming backend (``chunked=False``) is handed the
            # source once; the per-source chunk cache still provides coarse
            # progress and resume (ADR-0019).
            chunks = [(0.0, duration)] if duration > 0 else []
        planned.append((src, duration, chunks))

    if scope is not None:
        durations = {src.id: duration for src, duration, _ in planned}
        selected_n = sum(
            1
            for src, _duration, chunks in planned
            for start_s, end_s in chunks
            if scope.selects(src.id, start_s, end_s)
        )
        if selected_n == 0:
            raise ScopeError(_empty_scope_message(scope, durations))

    # Then run the uncached chunks through one bounded pool so the GPU stays fed
    # across source boundaries too.
    plans: list[_SourcePlan] = []
    pending: list[_ChunkTask] = []
    cached_hits: list[str] = []
    reused_n = redecoded_n = carried_n = guard_n = 0
    for src, duration, chunks in planned:
        cache = workspace.chunk_cache(src.id).ensure()
        run_meta = chunk_cache_key(
            backend=backend_id,
            model=chosen_model,
            language=language,
            glossary=prompt,
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
            n_chunks=len(chunks),
            decoders=decoders,
        )
        current_digest = glossary_digest(prompt)

        stored = cache.read_meta() if options.resume else None
        if options.resume and plan_matches(stored, run_meta):
            digests = chunk_glossary(stored)
            # Terms added *or removed* since the cached run: either direction can
            # change how a chunk that resembles the term was decoded.
            affected = changed_terms(
                str(stored.get("glossary", "")), str(run_meta["glossary"])
            )
        else:
            # Drop chunk results, transient WAVs and any half-written cache file
            # from a previous interrupted pass: a stale body must never be read.
            # A meta with no per-chunk digests (written before scoped re-runs)
            # also lands here, so its bodies are re-decoded once rather than
            # trusted at unknown provenance.
            cache.invalidate()
            stored, digests, affected = None, {}, ()

        log(
            f"[transcribe] {src.id}: {len(chunks)} chunk(s), {duration:.1f}s, "
            f"{backend.info.description} ({chosen_model})"
        )
        segs: list[list[Segment] | None] = [None] * len(chunks)
        # The glossary digest every chunk will carry once this run finishes;
        # written up front so a cancelled pass stays resumable.
        final: dict[int, str] = {}
        for i, (start_s, end_s) in enumerate(chunks):
            selected = scope is None or scope.selects(src.id, start_s, end_s)
            cached = (
                cache.read_segments(i) if digests and cache.has_segments(i) else None
            )
            if cached is None:
                decision = _REDECODE
            elif digests.get(i) == current_digest:
                decision = _REUSE
            elif selected:
                decision = _REDECODE
            elif affected and any(
                term_could_affect(term, " ".join(seg.text for seg in cached))
                for term in affected
            ):
                # Out of scope, but a changed term could be in this chunk: the
                # guard only ever *adds* work, so being wrong here is slow, not
                # stale.
                decision = _GUARD
            else:
                decision = _CARRY

            if decision in (_REUSE, _CARRY):
                segs[i] = cached
                cached_hits.append(src.id)
                reused_n += 1
                if decision == _CARRY:
                    carried_n += 1
                    final[i] = digests[i]
                    log(
                        f"[transcribe]   {src.id} chunk {i + 1}/{len(chunks)} "
                        "kept (decoded under an earlier glossary)"
                    )
                else:
                    final[i] = current_digest
                    log(f"[transcribe]   {src.id} chunk {i + 1}/{len(chunks)} cached")
            else:
                pending.append(
                    _ChunkTask(src.id, i, src.path, start_s, end_s, cache, len(chunks))
                )
                redecoded_n += 1
                if decision == _GUARD:
                    guard_n += 1
                final[i] = current_digest
        cache.write_meta(run_meta, final)
        plans.append(_SourcePlan(src, duration, chunks, segs))

    report = ChunkReport(
        scoped=scope is not None,
        scope=scope.describe() if scope is not None else "",
        reused=reused_n,
        redecoded=redecoded_n,
        carried_over=carried_n,
        guard_redecoded=guard_n,
    )

    # One stage-level progress bar over **chunks across all sources**: cached
    # chunks count as already done, pending ones advance as the pool finishes
    # them (advance is thread-safe).
    total_chunks = sum(len(plan.chunks) for plan in plans)
    progress = Progress("transcribe", total_chunks, on_event)
    progress.start(f"{total_chunks} chunk(s) over {len(plans)} source(s)")
    for src_id in cached_hits:
        progress.advance(source=src_id, message="cached")

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
                **decoders,
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
        progress.advance(
            source=task.source_id,
            message=f"chunk {task.index + 1}/{task.n_chunks}",
        )
        return task.source_id, task.index, shifted

    _run_pending(pending, plan_by_id, workers, _run_chunk, runner)
    if total_chunks == 0:
        progress.finish("no chunks to transcribe")

    # Report the cost, so the loop's economics are visible rather than inferred.
    log(
        f"[transcribe] chunks: {report.redecoded} re-decoded, {report.reused} reused"
        + (
            f" ({report.carried_over} of the reused under an earlier glossary)"
            if report.carried_over
            else ""
        )
        + (
            f", {report.guard_redecoded} pulled back by the near-match guard"
            if report.guard_redecoded
            else ""
        )
    )
    if report.carried_over:
        log(
            f"[transcribe] note: {report.carried_over} reused chunk(s) still carry "
            "an earlier glossary; re-run without a scope to apply the current one."
        )

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
        chunk_report=report,
    )


__all__ = [
    "ChunkReport",
    "Transcription",
    "TranscriptionOptions",
    "auto_jobs",
    "detect_vram_gb",
    "merge_chunk_segments",
    "model_vram_gb",
    "resolve_jobs",
    "transcribe",
]
