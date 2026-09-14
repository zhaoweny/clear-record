"""The explainable recommended default: ``--auto`` and ``--backend auto``.

Both are *capability* resolvers, kept **pure over injected probe results** so
they are unit-testable with no hardware. The CLI performs the real probes —
available backends, VRAM, CPU count, the ggml checkpoints already on disk, and
the tape's duration and channel count — and passes the facts in; the resolver
returns a choice plus a one-line explanation naming the inputs behind it.

``--auto`` (this module's :func:`resolve_auto`) picks a **profile + model**: it
reuses the existing sizing heuristics (``model_vram_gb`` / ``auto_jobs`` from
:mod:`clear_record.cli.transcription`) rather than inventing a second one, never
triggers a download, and turns on per-speaker attribution for multi-channel
tapes. It never chooses a *backend* — profiles tune decoder knobs only.

``--backend auto`` (:func:`resolve_backend`) is the separate capability knob:
first available backend in :data:`BACKEND_PREFERENCE`, which is **native first,
``whisper-cli`` fallback** (ADR-0005's 2026-09-14 Update).

Neither resolver runs on the default path: with no ``--auto`` and an explicit
backend, the CLI calls neither probe and behaviour is exactly as before.
"""

from __future__ import annotations

import dataclasses
import glob
import os
from collections.abc import Iterable
from pathlib import Path

import soundfile as sf

from clear_record.engine import channel_count
from clear_record.engine.audio import read_audio
from clear_record.providers import available_backend_ids, resolve_models_dir

from clear_record.cli.transcription import auto_jobs, detect_vram_gb, model_vram_gb
from clear_record.cli.workspace import discover_audio

#: The sentinel ``--backend`` accepts for capability-driven selection.
BACKEND_AUTO = "auto"

#: Preference order for ``--backend auto``: **native first, ``whisper-cli``
#: fallback** (ADR-0005's 2026-09-14 Update). This is **data, not branching**, so
#: the order evolves by editing one tuple.
#:
#: ``apple-speech`` (macOS 26+ ``SpeechTranscriber``) and ``windows-ai``
#: (``Microsoft.Windows.AI.Speech``) are the intended native top of the order,
#: scoped in ``.scratch/system-speech-backends/``; they are **not registered
#: backends yet** and become selectable with no change here once that work
#: lands. Until then the tuple is honest today — no native id can be available —
#: and already correct for later. The remaining ids are the shipped
#: ``whisper-cli`` family and are the portable fallback.
BACKEND_PREFERENCE: tuple[str, ...] = (
    "apple-speech",  # macOS 26+ SpeechTranscriber (unbuilt; future native path)
    "windows-ai",  # Windows AI Speech (unbuilt; future native path)
    "apple",  # whisper-cli + ggml Metal
    "nvidia",  # whisper-cli + ggml CUDA/Vulkan
    "amd",  # whisper-cli + ggml Vulkan/ROCm
)

#: Models ``--auto`` may choose, smallest to largest. These are the size names
#: the ggml resolver accepts (``providers.backends._resolve_ggml_model``);
#: quantisation is a backend detail, so the ladder is by size only.
_MODEL_LADDER: tuple[str, ...] = ("tiny", "base", "small", "medium", "large-v3")

#: A candidate model "fits" when the existing ``auto_jobs`` heuristic can afford
#: at least this many concurrent workers. One worker is the floor ``auto_jobs``
#: clamps to; requiring two means the model is not merely loadable but leaves the
#: VRAM/CPU headroom the parallel path is built around.
_MIN_JOBS_FOR_MODEL = 2

#: For *model selection* only the VRAM/CPU budget matters, not the tape, so probe
#: ``auto_jobs`` with a pending count comfortably above its 4-way fan-out cap.
_JOBS_PROBE_PENDING = 64

#: Tape-length thresholds (seconds). At/above ``_LONG_TAPE_S`` a thorough decode
#: would run too long to read while it runs, so a quick first pass is chosen; at/
#: below ``_SHORT_TAPE_S`` thoroughness is cheap.
_LONG_TAPE_S = 3600.0
_SHORT_TAPE_S = 900.0

#: A machine is "roomy" — worth spending decoder effort — with a large GPU or
#: many CPUs. A modest machine gets the quicker preset.
_ROOMY_VRAM_GB = 16.0
_ROOMY_CPUS = 8

#: More than two channels is a multichannel meeting/quad capture (the same rule
#: ``ingest(split="auto")`` uses), so per-speaker attribution is turned on.
_MULTICHANNEL_MIN = 3


class NoBackendAvailable(RuntimeError):
    """No known ASR backend is available on this machine for ``--backend auto``."""


@dataclasses.dataclass(frozen=True)
class BackendChoice:
    """The backend ``--backend auto`` resolved, and why."""

    backend: str
    explanation: str


def resolve_backend(available: Iterable[str]) -> BackendChoice:
    """Pick the best **available** backend, first in :data:`BACKEND_PREFERENCE`.

    Pure over the injected availability set (``available()`` is the only probe,
    and the caller runs it); no native runtime is imported. Raises
    :class:`NoBackendAvailable` with an actionable install hint when nothing is
    available.
    """
    present = set(available)
    for backend_id in BACKEND_PREFERENCE:
        if backend_id in present:
            candidates = ", ".join(BACKEND_PREFERENCE)
            here = ", ".join(sorted(present)) or "none"
            return BackendChoice(
                backend=backend_id,
                explanation=(
                    f"--backend auto: chose {backend_id!r} — first available in "
                    f"the native-first preference order ({candidates}); "
                    f"available here: {here}."
                ),
            )
    raise NoBackendAvailable(
        "no ASR backend is available on this machine; `--backend auto` found "
        f"none of: {', '.join(BACKEND_PREFERENCE)}.\n"
        "  Install a system `whisper-cli` + a ggml GPU plugin (on macOS, "
        "`brew install whisper-cpp`; on Linux, e.g. a distro `whisper-cpp` plus "
        "`ggml-cuda`/`ggml-vulkan`; see docs/adr/0005), or pass an explicit "
        "`--backend <id>`."
    )


@dataclasses.dataclass(frozen=True)
class AutoProbe:
    """The machine/tape facts :func:`resolve_auto` is pure over.

    Every field is injected, so the resolver is testable without hardware. The
    real values come from :func:`probe_auto`, which wraps ``available()``,
    ``detect_vram_gb`` and a scan of the models directory.
    """

    available_backends: tuple[str, ...]
    vram_gb: float | None
    cpu_count: int
    models_on_disk: frozenset[str]
    duration_s: float
    channels: int
    language: str | None = None


@dataclasses.dataclass(frozen=True)
class AutoChoice:
    """The profile + model ``--auto`` resolved, plus its explanation.

    ``model_on_disk`` is the no-download guarantee: when it is ``False`` the
    caller must report the absent model rather than let the backend fetch it.
    ``preferred_model`` is what the machine's VRAM would choose before the
    on-disk preference is applied, so an explanation can say when it was not
    available.
    """

    profile: str
    model: str
    model_on_disk: bool
    preferred_model: str
    jobs: int
    diarize: bool
    explanation: str


def _roomy(vram_gb: float | None, cpu_count: int) -> bool:
    """Whether the machine has headroom to spend on decoder effort."""
    return (vram_gb is not None and vram_gb >= _ROOMY_VRAM_GB) or (
        cpu_count >= _ROOMY_CPUS
    )


def _fits(model: str, vram_gb: float | None, cpu_count: int) -> bool:
    """Whether the existing ``auto_jobs`` heuristic affords >= 2 workers."""
    return (
        auto_jobs(_JOBS_PROBE_PENDING, model, vram_gb, cpu_count) >= _MIN_JOBS_FOR_MODEL
    )


def _choose_profile(
    duration_s: float, vram_gb: float | None, cpu_count: int
) -> tuple[str, str]:
    """Profile by tape length and machine headroom, with a reason."""
    if duration_s <= 0:
        return "balanced", "the tape length is unknown"
    roomy = _roomy(vram_gb, cpu_count)
    if duration_s >= _LONG_TAPE_S:
        if roomy:
            return "balanced", "a long tape on a roomy machine"
        return "fast", "a long tape on a modest machine (quick first pass)"
    if duration_s <= _SHORT_TAPE_S and roomy:
        return "accurate", "a short tape on a machine that can afford it"
    return "balanced", "a middle-of-the-road tape"


def _choose_model(probe: AutoProbe) -> tuple[str, bool, str, str]:
    """The model to use, whether it is on disk, the VRAM-preferred model, why.

    ``preferred`` is the largest ladder model the VRAM/CPU budget affords. A
    model already on disk always wins over downloading: the largest *fitting*
    present model is used, else the smallest present one, and only when nothing
    is on disk is the preferred model returned with ``on_disk=False``.
    """
    fitting = [
        model for model in _MODEL_LADDER if _fits(model, probe.vram_gb, probe.cpu_count)
    ]
    preferred = fitting[-1] if fitting else _MODEL_LADDER[0]
    present = [model for model in _MODEL_LADDER if model in probe.models_on_disk]
    present_fitting = [model for model in fitting if model in probe.models_on_disk]

    if present_fitting:
        model = present_fitting[-1]
        if model == preferred:
            why = "largest checkpoint that fits the VRAM/CPU budget"
        else:
            why = (
                f"already on disk; the preferred {preferred!r} is absent and "
                "--auto does not download"
            )
        return model, True, preferred, why
    if present:
        model = present[0]
        return (
            model,
            True,
            preferred,
            f"the only checkpoint(s) on disk; the preferred {preferred!r} is "
            "absent and --auto does not download",
        )
    return (
        preferred,
        False,
        preferred,
        "the largest checkpoint that fits this machine, but none is on disk",
    )


def _fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return "unknown"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 60:.0f} min"


def _fmt_vram(vram_gb: float | None) -> str:
    if vram_gb is None or vram_gb <= 0:
        return "unprobed (8 GB floor)"
    return f"{vram_gb:g} GB"


def resolve_auto(probe: AutoProbe) -> AutoChoice:
    """Choose a profile + model for ``probe`` and explain the choice.

    Pure: it reads only ``probe`` (and the existing pure sizing helpers), so an
    injected machine is fully deterministic. It never resolves a backend and
    never fetches a model.
    """
    profile, profile_why = _choose_profile(
        probe.duration_s, probe.vram_gb, probe.cpu_count
    )
    model, on_disk, preferred, model_why = _choose_model(probe)
    jobs = auto_jobs(_JOBS_PROBE_PENDING, model, probe.vram_gb, probe.cpu_count)
    diarize = probe.channels >= _MULTICHANNEL_MIN

    decisions = [
        f"profile {profile!r} ({profile_why})",
        f"model {model!r} ({model_why}; ~{model_vram_gb(model):g} GB per worker)",
        f"up to {jobs} worker(s)",
    ]
    if diarize:
        decisions.append(f"per-speaker attribution on ({probe.channels}-channel tape)")
    disk = ", ".join(sorted(probe.models_on_disk)) or "none"
    backends = ", ".join(probe.available_backends) or "none"
    inputs = (
        f"tape {_fmt_duration(probe.duration_s)}, {probe.channels} channel(s), "
        f"language {probe.language or 'auto'}, VRAM {_fmt_vram(probe.vram_gb)}, "
        f"{probe.cpu_count} CPU(s), models on disk: {disk}, "
        f"backends available: {backends}"
    )
    explanation = f"--auto: chose {', '.join(decisions)}; inputs: {inputs}."

    return AutoChoice(
        profile=profile,
        model=model,
        model_on_disk=on_disk,
        preferred_model=preferred,
        jobs=jobs,
        diarize=diarize,
        explanation=explanation,
    )


# --- real probes (CLI only; never run on the default path) ----------------- #
def _file_duration_s(path: str | Path) -> float:
    """Duration in seconds of one audio file (0.0 if unknown)."""
    try:
        info = sf.info(str(path))
        return float(info.frames) / float(info.samplerate or 1)
    except Exception:
        try:
            data, sr = read_audio(path, target_sr=None)
            return float(len(data) / sr)
        except Exception:
            return 0.0


def _normalize_model_name(filename: str) -> str | None:
    """``/x/ggml-large-v3-q5_0.bin`` -> ``large-v3`` (mirrors the sizing parser)."""
    name = os.path.basename(filename).lower()
    if name.startswith("ggml-"):
        name = name[len("ggml-") :]
    if name.endswith(".bin"):
        name = name[: -len(".bin")]
    name = name.split("-q", 1)[0]
    return name or None


def models_on_disk(model_dir: str | None = None) -> frozenset[str]:
    """The ggml size names already present in the models directory.

    Uses the single models-directory resolver (``model_dir`` -> ``CR_MODELS_DIR``
    -> ``<cwd>/models``) so it agrees with the backend's own lookup. Missing
    directory == nothing on disk; no download is ever attempted.
    """
    base = resolve_models_dir(model_dir)
    names: set[str] = set()
    for path in glob.glob(os.path.join(base, "ggml-*.bin")):
        name = _normalize_model_name(path)
        if name:
            names.add(name)
    return frozenset(names)


def max_channels(directory: str | Path) -> int:
    """The widest channel count among the workspace's input audio (1 if none).

    Probing the *original* recordings (not the normalized manifest sources) is
    what detects a multichannel tape; ``ingest`` has already split those into
    mono per-speaker files by the time transcription runs.
    """
    files = discover_audio(Path(directory))
    return max((channel_count(p) for p in files), default=1)


def tape_duration_s(directory: str | Path) -> float:
    """The longest input recording's duration in seconds (0.0 if none).

    One tape is one timeline: concurrent sources of the same meeting do not add
    up, so the maximum — not the sum — is the length that gates the profile.
    """
    files = discover_audio(Path(directory))
    return max((_file_duration_s(p) for p in files), default=0.0)


def probe_auto(
    directory: str | Path,
    *,
    model_dir: str | None = None,
    language: str | None = None,
) -> AutoProbe:
    """Run the real probes for :func:`resolve_auto` on this machine and tape."""
    return AutoProbe(
        available_backends=available_backend_ids(),
        vram_gb=detect_vram_gb(),
        cpu_count=os.cpu_count() or 1,
        models_on_disk=models_on_disk(model_dir),
        duration_s=tape_duration_s(directory),
        channels=max_channels(directory),
        language=language,
    )


__all__ = [
    "BACKEND_AUTO",
    "BACKEND_PREFERENCE",
    "AutoChoice",
    "AutoProbe",
    "BackendChoice",
    "NoBackendAvailable",
    "max_channels",
    "models_on_disk",
    "probe_auto",
    "resolve_auto",
    "resolve_backend",
    "tape_duration_s",
]
