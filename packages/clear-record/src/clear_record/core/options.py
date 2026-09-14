"""The run-options value and the built-in transcription profiles.

``core`` is dependency-free and every layer may import it (``cli → core``,
``service → core``, ``web → core``, ``mcp → core``), so this is the one home for
the run configuration. The CLI, the service, the web console and the MCP server
all read the same :class:`PipelineOptions` and the same :data:`PROFILES` table,
so a profile cannot mean different things on different surfaces.

The profile presets trade decoder effort at a **fixed model**: a profile never
chooses a backend (that stays an explicit/capability choice) and ``custom`` — the
default — sets nothing, so with no ``--profile`` the pipeline behaves exactly as
before profiles existed.

Resolution precedence (see :func:`resolve_options`)::

    explicit flag/argument  >  CR_* environment  >  profile  >  built-in default
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping

# Canonical chunking defaults (seconds). ``core`` is their single owner;
# ``clear_record.engine`` re-exports them for its timeline math (``engine → core``
# is an allowed edge), so there is only one literal to keep right.
DEFAULT_CHUNK_S = 600.0  # 10 minutes
DEFAULT_OVERLAP_S = 5.0

#: The decoder knobs a backend may honour, by their ``PipelineOptions`` field
#: name. A backend advertises the subset it supports via
#: ``BackendInfo.decoder_knobs``; a requested but unsupported knob fails loudly
#: instead of being silently dropped (see ``cli.transcription``).
DECODER_KNOB_FIELDS: tuple[str, ...] = (
    "beam_size",
    "best_of",
    "temperature",
    "entropy_thold",
    "no_speech_thold",
    "max_context",
    "threads",
)

#: The fields :func:`resolve_options` manages: a caller leaves one ``None`` to
#: mean "unset", and the resolver fills it from profile → ``CR_*`` env → built-in
#: default. The CLI defaults these flags to ``None`` (never to the concrete
#: default), so an explicit value that *equals* the default — ``--jobs 0``, the
#: documented "auto" — is still recognised as explicit and beats a profile or the
#: environment. Every profile key and every ``CR_*`` knob must be listed here.
RESOLVABLE_FIELDS: tuple[str, ...] = (
    "chunk_seconds",
    "overlap_seconds",
    "jobs",
    *DECODER_KNOB_FIELDS,
)

#: The explicit default: it selects no preset and leaves every knob to the user.
PROFILE_CUSTOM = "custom"

#: The built-in presets. Values are real :class:`PipelineOptions` fields and
#: trade decoder effort at a fixed model — the accuracy dial that was previously
#: unreachable. ``custom`` is empty by definition.
PROFILES: dict[str, dict[str, object]] = {
    PROFILE_CUSTOM: {},
    # Greedy, one candidate: the fastest decode.
    "fast": {"best_of": 1},
    # Beam search, moderate width.
    "balanced": {"beam_size": 5},
    # Beam search, wide: the slowest, most thorough decode.
    "accurate": {"beam_size": 8},
}


@dataclasses.dataclass(frozen=True)
class PipelineOptions:
    """The full-pipeline run configuration, threaded to every stage as one value.

    Replaces the 17-keyword interface ``run`` used to take; the CLI fills it once
    from the parsed arguments. Field defaults match the old keyword defaults, so
    ``PipelineOptions()`` is the old bare ``run(directory)``.

    The decoder fields default to ``None`` — **unset**, meaning no flag is added
    to the backend command and the backend's own default applies. That is what
    keeps the built command unchanged for an existing user.

    A ``None`` on a resolver-managed field (:data:`RESOLVABLE_FIELDS`) means
    "unset": :func:`resolve_options` fills it from profile → environment →
    built-in default. After resolution those fields are always concrete.
    """

    backend: str = "apple"
    model: str | None = None
    language: str | None = None
    model_dir: str | None = None
    audio_files: tuple[str, ...] | None = None
    split: str = "auto"
    glossary: str | None = None
    chunk_seconds: float | None = DEFAULT_CHUNK_S
    overlap_seconds: float | None = DEFAULT_OVERLAP_S
    resume: bool = True
    do_diarize: bool | None = None
    speakers: int | None = None
    reference: str | None = None
    formats: tuple[str, ...] | None = None
    attribute_energy: bool = False
    mixed_source: str | None = None
    window_s: float | None = None
    jobs: int | None = 0
    check_plugin: bool = False
    #: The profile this configuration was resolved from (informational).
    profile: str = PROFILE_CUSTOM
    # --- decoder knobs (``None`` = unset) ---------------------------------- #
    beam_size: int | None = None
    best_of: int | None = None
    temperature: float | None = None
    entropy_thold: float | None = None
    no_speech_thold: float | None = None
    max_context: int | None = None
    threads: int | None = None

    def decoder_knobs(self) -> dict[str, object]:
        """The decoder knobs that are set, keyed by field name.

        Unset (``None``) knobs are omitted, so a caller that passes these to a
        backend adds no flag and a cache key gains no entry.
        """
        out: dict[str, object] = {}
        for name in DECODER_KNOB_FIELDS:
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


def profile_values(profile: str) -> dict[str, object]:
    """The knob values a preset wants to set (``custom`` returns ``{}``)."""
    try:
        return dict(PROFILES[profile])
    except KeyError as exc:
        raise ValueError(
            f"unknown profile {profile!r}; choose from {sorted(PROFILES)}"
        ) from exc


#: Environment overrides for the run knobs. An explicit argument always wins;
#: these win over a profile. Only the existing ``CR_*`` convention is used.
_ENV_KNOBS: dict[str, tuple[str, Callable[[str], object]]] = {
    "CR_CHUNK_SECONDS": ("chunk_seconds", float),
    "CR_OVERLAP_SECONDS": ("overlap_seconds", float),
    "CR_JOBS": ("jobs", int),
    "CR_BEAM_SIZE": ("beam_size", int),
    "CR_BEST_OF": ("best_of", int),
    "CR_TEMPERATURE": ("temperature", float),
    "CR_ENTROPY_THOLD": ("entropy_thold", float),
    "CR_NO_SPEECH_THOLD": ("no_speech_thold", float),
    "CR_MAX_CONTEXT": ("max_context", int),
    "CR_THREADS": ("threads", int),
}


def _env_overrides(environ: Mapping[str, str] | None) -> dict[str, object]:
    """Read the knobs the environment sets; a blank or unparseable value is ignored."""
    env = os.environ if environ is None else environ
    out: dict[str, object] = {}
    for variable, (name, convert) in _ENV_KNOBS.items():
        raw = env.get(variable)
        if raw is None or not raw.strip():
            continue
        try:
            out[name] = convert(raw.strip())
        except ValueError:
            continue
    return out


def resolve_options(
    options: PipelineOptions | None = None,
    *,
    profile: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> PipelineOptions:
    """Resolve a run configuration layer by layer.

    Precedence, strongest first: **explicit argument** > ``CR_*`` environment >
    **profile** > **built-in default**.

    Unset is a real sentinel: a resolver-managed field
    (:data:`RESOLVABLE_FIELDS`) is explicit exactly when it is **not** ``None``.
    Because the CLI defaults those flags to ``None`` rather than to the concrete
    default, an explicit ``--jobs 0`` (the documented "auto") stays ``0`` and is
    never mistaken for "unset" and overridden by a profile or the environment.
    Every other field passes through from ``options`` untouched.

    With no profile and no ``CR_*`` in the environment, the result equals the
    input — behaviour is unchanged for an existing user. ``environ`` is
    injectable so the precedence is testable without touching the real process.
    """
    base = options or PipelineOptions()
    chosen = profile or base.profile
    profile_vals = profile_values(chosen)
    env_vals = _env_overrides(environ)
    defaults = PipelineOptions()

    resolved: dict[str, object] = {}
    for name in RESOLVABLE_FIELDS:
        value = getattr(base, name)
        if value is not None:  # explicit argument: it beats everything
            resolved[name] = value
        elif name in env_vals:  # CR_* environment beats the profile
            resolved[name] = env_vals[name]
        elif name in profile_vals:  # profile beats the built-in default
            resolved[name] = profile_vals[name]
        else:  # built-in default
            resolved[name] = getattr(defaults, name)
    resolved["profile"] = chosen
    return dataclasses.replace(base, **resolved)


__all__ = [
    "DECODER_KNOB_FIELDS",
    "DEFAULT_CHUNK_S",
    "DEFAULT_OVERLAP_S",
    "PROFILE_CUSTOM",
    "PROFILES",
    "RESOLVABLE_FIELDS",
    "PipelineOptions",
    "profile_values",
    "resolve_options",
]
