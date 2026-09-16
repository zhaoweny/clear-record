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
from clear_record.providers import (
    APPLE_SPEECH_BACKEND_ID,
    DEFAULT_MODEL,
    WINDOWS_AI_BACKEND_ID,
    available_backend_ids,
    resolve_models_dir,
)

from clear_record.cli.transcription import auto_jobs, detect_vram_gb, model_vram_gb
from clear_record.cli.workspace import discover_audio
from clear_record.core.i18n import deferred

#: The sentinel ``--backend`` accepts for capability-driven selection.
BACKEND_AUTO = "auto"

#: Preference order for ``--backend auto``: **native first, ``whisper-cli``
#: fallback** (ADR-0005's 2026-09-14 Update). This is **data, not branching**, so
#: the order evolves by editing one tuple.
#:
#: ``apple-speech`` (macOS 26+ ``SpeechTranscriber``) is a registered backend:
#: the catalog in :data:`clear_record.providers.backends.BACKENDS` is the one
#: list of which ids exist, and it sits first in this order. ``windows-ai``
#: (``Microsoft.Windows.AI.Speech``) is **not registered** — ADR-0019 leaves it
#: deferred (gated on the MSIX/``systemAIModels`` packaging decision), so its id
#: is only a placeholder here until that work lands. The remaining ids are the
#: shipped ``whisper-cli`` family and are the portable fallback.
BACKEND_PREFERENCE: tuple[str, ...] = (
    APPLE_SPEECH_BACKEND_ID,  # macOS 26+ SpeechTranscriber (native path)
    WINDOWS_AI_BACKEND_ID,  # Windows AI Speech (unregistered; deferred, ADR-0019)
    "apple",  # whisper-cli + ggml Metal
    "nvidia",  # whisper-cli + ggml CUDA/Vulkan
    "amd",  # whisper-cli + ggml Vulkan/ROCm
)

#: Models ``--auto`` may choose, smallest to largest. These are the size names
#: the ggml resolver accepts (``providers.backends._resolve_ggml_model``);
#: quantisation is a backend detail, so the ladder is by size only.
MODEL_LADDER: tuple[str, ...] = ("tiny", "base", "small", "medium", "large-v3")

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


# --------------------------------------------------------------------------- #
# translatable explanations: a message ID plus parameters, rendered at a boundary
# --------------------------------------------------------------------------- #
#: The English renderer: the identity message ID, placeholders filled. It is what
#: ``str(Message)`` uses so the terminal and machine surfaces stay English.
def _english(msgid: str, **params: object) -> str:
    return msgid.format(**params) if params else msgid


@dataclasses.dataclass(frozen=True)
class Message:
    """A stable message ID plus its parameters — an explanation a boundary renders.

    The ID **is the English source string** (the i18n rule), and every ID is
    marked with :func:`clear_record.core.i18n.deferred` so ``pybabel`` extracts
    it. ``render(translate)`` fills it in the caller's locale; ``str(message)``
    is the English form, so the terminal's own print and the run meta a machine
    reads are unchanged. A parameter may itself be a :class:`Message` (or a
    :class:`Joined` of them), so a composed explanation translates as a tree
    rather than a half-English sentence.

    A boundary — the console — renders it with ``tr``; the resolver never calls
    ``tr`` itself, which keeps it pure and keeps the English default byte-for-byte
    identical.
    """

    msgid: str
    params: tuple[tuple[str, object], ...] = ()

    def render(self, translate) -> str:
        """Render in a locale, using ``translate`` (the boundary's ``tr``)."""
        return render_message(self.as_json(), translate)

    def as_json(self) -> dict:
        """The JSON-safe form recorded in run meta (machine-read, untranslated)."""
        return {
            "id": self.msgid,
            "params": {name: _json_value(value) for name, value in self.params},
        }

    def __str__(self) -> str:
        return render_message(self.as_json(), _english)


@dataclasses.dataclass(frozen=True)
class Joined:
    """A locale-aware join of message parts (the "chose a, b and c" clause)."""

    separator: str
    parts: tuple[Message, ...]


def _json_value(value: object) -> object:
    if isinstance(value, Message):
        return value.as_json()
    if isinstance(value, Joined):
        return {
            "join": value.separator,
            "parts": [part.as_json() for part in value.parts],
        }
    return value


def render_message(node: object, translate) -> str:
    """Render a :meth:`Message.as_json` node (or a scalar) in a locale.

    Recursive so a nested explanation translates whole: a plain value passes
    through, a ``{"join": …}`` node joins its rendered parts, and a message node
    is the message ID translated with its rendered parameters. The console calls
    this with ``tr`` — the service never translates.
    """
    if not isinstance(node, dict):
        return node  # type: ignore[return-value]
    if "join" in node:
        return node["join"].join(
            render_message(part, translate) for part in node["parts"]
        )
    params = {
        name: render_message(value, translate) if isinstance(value, dict) else value
        for name, value in node["params"].items()
    }
    return translate(node["id"], **params)


class NoBackendAvailable(RuntimeError):
    """No known ASR backend is available on this machine for ``--backend auto``."""

    def __init__(self, message: Message) -> None:
        self.message = message
        super().__init__(str(message))


@dataclasses.dataclass(frozen=True)
class BackendChoice:
    """The backend ``--backend auto`` resolved, and why."""

    backend: str
    message: Message

    @property
    def explanation(self) -> str:
        """The English one-liner the terminal prints (unchanged default)."""
        return str(self.message)


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
                message=Message(
                    deferred(
                        "--backend auto: chose {backend!r} — first available in "
                        "the native-first preference order ({candidates}); "
                        "available here: {here}."
                    ),
                    (
                        ("backend", backend_id),
                        ("candidates", candidates),
                        ("here", here),
                    ),
                ),
            )
    raise NoBackendAvailable(
        Message(
            deferred(
                "no ASR backend is available on this machine; `--backend auto` found "
                "none of: {backends}.\n"
                "  Install a system `whisper-cli` + a ggml GPU plugin (on macOS, "
                "`brew install whisper-cpp`; on Linux, e.g. a distro `whisper-cpp` plus "
                "`ggml-cuda`/`ggml-vulkan`; see docs/adr/0005), or pass an explicit "
                "`--backend <id>`."
            ),
            (("backends", ", ".join(BACKEND_PREFERENCE)),),
        )
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
    message: Message

    @property
    def explanation(self) -> str:
        """The English one-liner the terminal prints (unchanged default)."""
        return str(self.message)


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
) -> tuple[str, Message]:
    """Profile by tape length and machine headroom, with a reason."""
    if duration_s <= 0:
        return "balanced", Message(deferred("the tape length is unknown"))
    roomy = _roomy(vram_gb, cpu_count)
    if duration_s >= _LONG_TAPE_S:
        if roomy:
            return "balanced", Message(deferred("a long tape on a roomy machine"))
        return "fast", Message(
            deferred("a long tape on a modest machine (quick first pass)")
        )
    if duration_s <= _SHORT_TAPE_S and roomy:
        return "accurate", Message(
            deferred("a short tape on a machine that can afford it")
        )
    return "balanced", Message(deferred("a middle-of-the-road tape"))


def _choose_model(probe: AutoProbe) -> tuple[str, bool, str, Message]:
    """The model to use, whether it is on disk, the VRAM-preferred model, why.

    ``preferred`` is the largest ladder model the VRAM/CPU budget affords. A
    model already on disk always wins over downloading: the largest *fitting*
    present model is used, else the smallest present one, and only when nothing
    is on disk is the preferred model returned with ``on_disk=False``.
    """
    fitting = [
        model for model in MODEL_LADDER if _fits(model, probe.vram_gb, probe.cpu_count)
    ]
    preferred = fitting[-1] if fitting else MODEL_LADDER[0]
    present = [model for model in MODEL_LADDER if model in probe.models_on_disk]
    present_fitting = [model for model in fitting if model in probe.models_on_disk]

    if present_fitting:
        model = present_fitting[-1]
        if model == preferred:
            why = Message(deferred("largest checkpoint that fits the VRAM/CPU budget"))
        else:
            why = Message(
                deferred(
                    "already on disk; the preferred {preferred!r} is absent and "
                    "--auto does not download"
                ),
                (("preferred", preferred),),
            )
        return model, True, preferred, why
    if present:
        model = present[0]
        return (
            model,
            True,
            preferred,
            Message(
                deferred(
                    "the only checkpoint(s) on disk; the preferred {preferred!r} is "
                    "absent and --auto does not download"
                ),
                (("preferred", preferred),),
            ),
        )
    return (
        preferred,
        False,
        preferred,
        Message(
            deferred(
                "the largest checkpoint that fits this machine, but none is on disk"
            )
        ),
    )


def _fmt_duration(seconds: float) -> Message:
    if seconds <= 0:
        return Message(deferred("unknown"))
    if seconds >= 3600:
        return Message(deferred("{hours:.1f} h"), (("hours", seconds / 3600),))
    return Message(deferred("{minutes:.0f} min"), (("minutes", seconds / 60),))


def _fmt_vram(vram_gb: float | None) -> Message:
    if vram_gb is None or vram_gb <= 0:
        return Message(deferred("unprobed (8 GB floor)"))
    return Message(deferred("{vram:g} GB"), (("vram", vram_gb),))


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
        Message(
            deferred("profile {profile!r} ({why})"),
            (("profile", profile), ("why", profile_why)),
        ),
        Message(
            deferred("model {model!r} ({why}; ~{vram:g} GB per worker)"),
            (("model", model), ("why", model_why), ("vram", model_vram_gb(model))),
        ),
        Message(deferred("up to {jobs} worker(s)"), (("jobs", jobs),)),
    ]
    if diarize:
        decisions.append(
            Message(
                deferred("per-speaker attribution on ({channels}-channel tape)"),
                (("channels", probe.channels),),
            )
        )
    disk = ", ".join(sorted(probe.models_on_disk)) or "none"
    backends = ", ".join(probe.available_backends) or "none"
    inputs = Message(
        deferred(
            "tape {duration}, {channels} channel(s), language {language}, VRAM "
            "{vram}, {cpus} CPU(s), models on disk: {disk}, backends available: "
            "{backends}"
        ),
        (
            ("duration", _fmt_duration(probe.duration_s)),
            ("channels", probe.channels),
            ("language", probe.language or "auto"),
            ("vram", _fmt_vram(probe.vram_gb)),
            ("cpus", probe.cpu_count),
            ("disk", disk),
            ("backends", backends),
        ),
    )
    message = Message(
        deferred("--auto: chose {decisions}; inputs: {inputs}."),
        (("decisions", Joined(", ", tuple(decisions))), ("inputs", inputs)),
    )

    return AutoChoice(
        profile=profile,
        model=model,
        model_on_disk=on_disk,
        preferred_model=preferred,
        jobs=jobs,
        diarize=diarize,
        message=message,
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


def model_paths_on_disk(model_dir: str | None = None) -> tuple[Path, ...]:
    """The ggml checkpoint paths already present in the models directory.

    The one place the ``ggml-*.bin`` scan lives: :func:`models_on_disk` derives
    the size names from it, and a caller that needs the concrete file (a
    quantised name cannot be reconstructed from its normalized size) reads it
    here. Uses the single models-directory resolver (``model_dir`` ->
    ``CR_MODELS_DIR`` -> ``<data>/models``); a missing directory is empty.
    """
    base = resolve_models_dir(model_dir)
    return tuple(sorted(Path(p) for p in glob.glob(os.path.join(base, "ggml-*.bin"))))


def models_on_disk(model_dir: str | None = None) -> frozenset[str]:
    """The ggml size names already present in the models directory.

    Uses the single models-directory resolver (``model_dir`` -> ``CR_MODELS_DIR``
    -> ``<data>/models``) so it agrees with the backend's own lookup. Missing
    directory == nothing on disk; no download is ever attempted.
    """
    names: set[str] = set()
    for path in model_paths_on_disk(model_dir):
        name = _normalize_model_name(str(path))
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
    "DEFAULT_MODEL",
    "MODEL_LADDER",
    "AutoChoice",
    "AutoProbe",
    "BackendChoice",
    "Message",
    "NoBackendAvailable",
    "max_channels",
    "model_paths_on_disk",
    "models_on_disk",
    "probe_auto",
    "render_message",
    "resolve_auto",
    "resolve_backend",
    "tape_duration_s",
]
