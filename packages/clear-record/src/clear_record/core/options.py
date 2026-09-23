"""The run-options value and the built-in transcription profiles.

``core`` is dependency-free and every layer may import it (``engine → core``,
``providers → core``, ``cli → core``, ``service → core``, ``web → core``,
``tray → core``, ``mcp → core``), so this is the one home for
the run configuration. The CLI, the service, the web console and the MCP server
all read the same :class:`PipelineOptions` and the same :data:`PROFILES` table,
so a profile cannot mean different things on different surfaces.

The profile presets trade decoder effort at a **fixed model**: a profile never
chooses a backend (that stays an explicit/capability choice) and ``custom`` — the
default — sets nothing, so with no ``--profile`` the pipeline behaves exactly as
before profiles existed.

**The declaration.** The run knobs — the flags a user may tune — are declared
once, in :data:`RUN_KNOBS`: one row per knob carrying its field name, its
command-line spelling, its ``CR_*`` binding, its default and (for a decoder knob)
the flag the whisper-cli adapter appends. The resolver, the CLI and the
whisper-cli adapter derive what they need from those rows instead of restating
them.

A new knob is that row plus the places a static type or signature still names it
by hand: its field annotation in this file, the keyword the ``Backend`` protocol
takes (``providers/base.py``) and the keyword the stage's ``transcribe`` takes
(``cli/stages.py``). Each of those three is pinned to the declaration by a test,
so a forgotten one fails in the suite, named, rather than at a user's run.

Resolution precedence (see :func:`resolve_options`)::

    explicit flag/argument  >  CR_* environment  >  profile  >  built-in default
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping

from clear_record.core.scope import ChunkScope

# Canonical chunking defaults (seconds). ``core`` is their single owner;
# ``clear_record.engine`` re-exports them for its timeline math (``engine → core``
# is an allowed edge), so there is only one literal to keep right.
DEFAULT_CHUNK_S = 600.0  # 10 minutes
DEFAULT_OVERLAP_S = 5.0


@dataclasses.dataclass(frozen=True)
class RunKnob:
    """One tunable run knob, the declaration's row.

    Every site that used to state a knob itself reads this row instead: the
    ``CR_*`` binding and the converter drive :func:`resolve_options`; the CLI
    derives the flag, its type, its ``envvar=`` and its help; the whisper-cli
    adapter derives the long flag it appends. This row is the knob's meaning —
    no site below restates it.

    ``cli`` holds the option's spellings, longest first. ``default`` is the
    built-in value the resolver falls back to (the CLI's own default is always
    unset). ``cli_slot`` names the block of the backend option group the flag
    renders in: the rows of one block keep their table order in ``--help``, and
    ``cli.cli`` places the blocks — a row in a block no splice site renders would
    never reach the CLI, which the CLI-surface test catches.
    """

    name: str
    cli: tuple[str, ...]
    env: str
    convert: Callable[[str], object]
    default: object
    help: str
    cli_slot: str
    #: True for a decoder knob: one a backend may advertise via
    #: ``BackendInfo.decoder_knobs`` and honour in its own ``transcribe``.
    decoder: bool = False
    #: The long flag the whisper-cli adapter appends when the knob is set.
    provider_flag: str | None = None


#: The run knobs, one row each. The order is significant: the CLI renders the
#: flags in it and :data:`RESOLVABLE_FIELDS` resolves in it.
RUN_KNOBS: tuple[RunKnob, ...] = (
    RunKnob(
        name="chunk_seconds",
        cli=("--chunk-seconds",),
        env="CR_CHUNK_SECONDS",
        convert=float,
        default=DEFAULT_CHUNK_S,
        help=f"chunk length for long tape transcription (default {DEFAULT_CHUNK_S:.0f}s)",
        cli_slot="chunking",
    ),
    RunKnob(
        name="overlap_seconds",
        cli=("--overlap-seconds",),
        env="CR_OVERLAP_SECONDS",
        convert=float,
        default=DEFAULT_OVERLAP_S,
        help=f"overlap between chunks (default {DEFAULT_OVERLAP_S:.0f}s)",
        cli_slot="chunking",
    ),
    RunKnob(
        name="jobs",
        cli=("--jobs", "-j"),
        env="CR_JOBS",
        convert=int,
        default=0,
        help="parallel transcription workers (0 = auto; process-isolated "
        "backends only, e.g. the AMD/NVIDIA whisper-cli)",
        cli_slot="sizing",
    ),
    RunKnob(
        name="beam_size",
        cli=("--beam-size",),
        env="CR_BEAM_SIZE",
        convert=int,
        default=None,
        help="beam search width; larger = slower and (usually) more accurate",
        cli_slot="decoder",
        decoder=True,
        provider_flag="--beam-size",
    ),
    RunKnob(
        name="best_of",
        cli=("--best-of",),
        env="CR_BEST_OF",
        convert=int,
        default=None,
        help="candidates tried in greedy decoding; larger = slower",
        cli_slot="decoder",
        decoder=True,
        provider_flag="--best-of",
    ),
    RunKnob(
        name="temperature",
        cli=("--temperature",),
        env="CR_TEMPERATURE",
        convert=float,
        default=None,
        help="decoding temperature (0.0 = deterministic)",
        cli_slot="decoder",
        decoder=True,
        provider_flag="--temperature",
    ),
    RunKnob(
        name="entropy_thold",
        cli=("--entropy-thold",),
        env="CR_ENTROPY_THOLD",
        convert=float,
        default=None,
        help="entropy threshold; decoding stops when it falls below it",
        cli_slot="decoder",
        decoder=True,
        provider_flag="--entropy-thold",
    ),
    RunKnob(
        name="no_speech_thold",
        cli=("--no-speech-thold",),
        env="CR_NO_SPEECH_THOLD",
        convert=float,
        default=None,
        help="probability below which a window counts as silence/skip",
        cli_slot="decoder",
        decoder=True,
        provider_flag="--no-speech-thold",
    ),
    RunKnob(
        name="max_context",
        cli=("--max-context",),
        env="CR_MAX_CONTEXT",
        convert=int,
        default=None,
        help="max tokens of previous text used as decoder context (-1 = default)",
        cli_slot="decoder",
        decoder=True,
        provider_flag="--max-context",
    ),
    RunKnob(
        name="threads",
        cli=("--threads",),
        env="CR_THREADS",
        convert=int,
        default=None,
        help="CPU threads for the decoder (matters on CPU-only paths)",
        cli_slot="decoder",
        decoder=True,
        provider_flag="--threads",
    ),
)

#: The decoder rows of :data:`RUN_KNOBS`.
DECODER_KNOBS: tuple[RunKnob, ...] = tuple(knob for knob in RUN_KNOBS if knob.decoder)


@dataclasses.dataclass(frozen=True)
class SupersededKey:
    """One stored run-option key a **released build of this application** wrote.

    A stored run's options are JSON text, and the release that enqueued the run
    wrote them from *its* :class:`PipelineOptions`. When that value loses a field,
    the rows that release left behind carry a key this build has no field for, and
    the reader has to know what the key meant rather than guess:

    ``successor`` names the current option field the stored value belongs to — a
    *rename*, where dropping the value would throw away something that still
    means exactly what it did. ``None`` means the capability was removed with no
    successor: the key is dropped, and the read says so (see
    :mod:`clear_record.service.run_options`).

    This is the declaration the reader derives its tolerance from, the same way
    :data:`RUN_KNOBS` is the declaration the resolver and the CLI derive from: a
    released key is one row here and no second list of names anywhere.
    """

    stored: str
    successor: str | None


#: The stored option keys an earlier release wrote and this build does not have.
#:
#: ``formats`` is the one row so far. The released line
#: (``public/releases/v0.2.x``, ``public/main``) carried
#: ``formats: tuple[str, ...] | None`` and threaded it to ``stages.export``,
#: which wrote the set it named or — with no ``formats`` — the same four
#: artifacts this build always writes (``md``, ``srt``, ``vtt``, ``json``); the
#: field's removal left the export set declared rather than configurable, so there
#: is no successor to hand the value to. It is dropped, and a run read from such a
#: row still runs — with the export set this build has.
SUPERSEDED_KEYS: tuple[SupersededKey, ...] = (
    SupersededKey(stored="formats", successor=None),
)

#: Their field names, for the places that carry names only (a backend's
#: ``decoder_knobs`` and the stage's support check). A backend advertises the
#: subset it supports; a requested but unsupported knob fails loudly instead of
#: being silently dropped (see ``cli.transcription``).
DECODER_KNOB_FIELDS: tuple[str, ...] = tuple(knob.name for knob in DECODER_KNOBS)

#: The fields :func:`resolve_options` manages: a caller leaves one ``None`` to
#: mean "unset", and the resolver fills it from ``CR_*`` env → profile → built-in
#: default. The CLI defaults these flags to ``None`` (never to the concrete
#: default), so an explicit value that *equals* the default — ``--jobs 0``, the
#: documented "auto" — is still recognized as explicit and beats a profile or the
#: environment. Every profile key and every ``CR_*`` knob is a row of the
#: declaration, so this is derived rather than restated.
RESOLVABLE_FIELDS: tuple[str, ...] = tuple(knob.name for knob in RUN_KNOBS)

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
class DecoderKnobs:
    """The decoder knobs a run may ask for, as fields (``None`` = unset).

    Python needs the annotations, so this is the one place they exist: the field
    names are the decoder rows of :data:`RUN_KNOBS`, and the two types that carry
    the knobs — :class:`PipelineOptions` (the whole run) and
    ``cli.transcription.TranscriptionOptions`` (the stage) — inherit them instead
    of restating them.
    """

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


@dataclasses.dataclass(frozen=True)
class PipelineOptions(DecoderKnobs):
    """The full-pipeline run configuration, threaded to every stage as one value.

    Replaces the 17-keyword interface ``run`` used to take; the CLI fills it once
    from the parsed arguments. Field defaults match the old keyword defaults, so
    ``PipelineOptions()`` is the old bare ``run(directory)``.

    The decoder fields (see :class:`DecoderKnobs`) default to ``None`` —
    **unset**, meaning no flag is added to the backend command and the backend's
    own default applies. That is what keeps the built command unchanged for an
    existing user.

    A ``None`` on a resolver-managed field (:data:`RESOLVABLE_FIELDS`) means
    "unset": :func:`resolve_options` fills it from ``CR_*`` environment → profile
    → built-in default. After resolution those fields are always concrete.
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
    attribute_energy: bool = False
    mixed_source: str | None = None
    window_s: float | None = None
    jobs: int | None = 0
    check_plugin: bool = False
    #: An explicit re-run scope (ADR-0018's 2026-09-15 update, which stands):
    #: re-decode only these sources and/or this time range, reuse every other
    #: chunk from the cache. Raw inputs, so :meth:`chunk_scope` is the one place
    #: that parses/validates them.
    rerun_sources: tuple[str, ...] | None = None
    rerun_range: str | None = None
    #: The profile this configuration was resolved from (informational).
    profile: str = PROFILE_CUSTOM

    def chunk_scope(self) -> ChunkScope | None:
        """The parsed re-run scope, or ``None`` when the run is unscoped.

        Raises :class:`clear_record.core.ScopeError` on a malformed range; the
        scope is never quietly dropped, because "unscoped" re-decodes everything
        and a typo must not silently cost a full pass.
        """
        return ChunkScope.parse(self.rerun_sources, self.rerun_range)


def profile_values(profile: str) -> dict[str, object]:
    """The knob values a preset wants to set (``custom`` returns ``{}``)."""
    try:
        return dict(PROFILES[profile])
    except KeyError as exc:
        raise ValueError(
            f"unknown profile {profile!r}; choose from {sorted(PROFILES)}"
        ) from exc


#: Environment overrides for the run knobs, from the ``env`` name each row
#: declares. An explicit argument always wins; these win over a profile. Only the
#: existing ``CR_*`` convention is used.
_ENV_KNOBS: dict[str, tuple[str, Callable[[str], object]]] = {
    knob.env: (knob.name, knob.convert) for knob in RUN_KNOBS
}

#: The built-in default per resolver-managed field, from the declaration.
_KNOB_DEFAULTS: dict[str, object] = {knob.name: knob.default for knob in RUN_KNOBS}


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

    resolved: dict[str, object] = {}
    for name in RESOLVABLE_FIELDS:
        value = getattr(base, name)
        if value is not None:  # explicit argument: it beats everything
            resolved[name] = value
        elif name in env_vals:  # CR_* environment beats the profile
            resolved[name] = env_vals[name]
        elif name in profile_vals:  # profile beats the built-in default
            resolved[name] = profile_vals[name]
        else:  # the built-in default the declaration states
            resolved[name] = _KNOB_DEFAULTS[name]
    resolved["profile"] = chosen
    return dataclasses.replace(base, **resolved)


__all__ = [
    "DECODER_KNOB_FIELDS",
    "DECODER_KNOBS",
    "DEFAULT_CHUNK_S",
    "DEFAULT_OVERLAP_S",
    "PROFILE_CUSTOM",
    "PROFILES",
    "RESOLVABLE_FIELDS",
    "RUN_KNOBS",
    "SUPERSEDED_KEYS",
    "DecoderKnobs",
    "PipelineOptions",
    "RunKnob",
    "SupersededKey",
    "profile_values",
    "resolve_options",
]
