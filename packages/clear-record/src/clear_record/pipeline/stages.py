"""The clear-record pipeline stage implementations.

The module's surface: the five declared stages (ingest / align / transcribe /
reconcile / export); the conveniences that are not stage-derived (diarize /
attribute / glossary); ``run``, which composes them; ``run_cancel_signal``, the
cancel signal the transcribe pool takes off the caller's sink; the model
provisioning (``prepare_model`` / ``download_ggml_model``) that ``service``
reaches the providers through; ``calibration_report``, whose numbers the
`calibrate` command writes into ``export/calibration.json``; and
``format_timestamp``, the one ``HH:MM:SS.mmm`` the Markdown serializer renders
with the command surface's transcript preview — on the record's own reference
clock, so a cue from before zero keeps its sign — while the SRT/VTT renderers go
through ``_srt_tc``, which writes the comma form of a time on the *export*
timeline: unsigned as the format is, it is translated by the pre-roll
(``_cue_origin``) rather than clamped onto zero.

A stage **returns** what it produced — the record, the artifact set, the report
of what it did — and reports everything it has to say through the sink it is
handed; nothing here writes to stdout and nothing here exits the process. Two
shapes carry that:

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

**What a stage says, it says on the sink.** A mid-stage line is reported where
it happens (:func:`~clear_record.pipeline.workspace.report_line`), and what a
pass knows only when it is over — its summary and the table its report holds —
is reported on the same channel through :func:`_report_pass`, in the order the
command surface prints it. The event's ``message`` *is* the line, so one payload
serves the console, a client reading a run's stream, and the command line's own
stdout; the returns above are for a caller that wants the typed result, not for
rendering it.

Left out on purpose: ``PipelineOptions``, a re-export of ``core``'s, and
everything private — the ``_run_*`` body one per stage and ``_STAGE_RUNNERS``,
``_report_pass`` / ``_report_written`` (the pass's own words on the channel), the
export serializers, ``_load_glossary``, ``_source_id`` / ``_staged_id`` and the
``_eval`` alias. They are machinery the module runs on, not what a caller comes here for.

Everything here is the thin wiring layer: it reads/writes the workspace files
and delegates the real work to ``clear_record.core`` (domain),
``clear_record.engine`` (audio/align/merge) and ``clear_record.providers``
(ASR). No vendor logic lives here.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

from clear_record.core import (
    DECODER_KNOB_FIELDS,
    DEFAULT_CHUNK_S,
    DEFAULT_OVERLAP_S,
    SOURCE_ROLES,
    Alignment,
    ChunkScope,
    EventSink,
    JobEvent,
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
    han_scripts,
    prepare_16k_wav,
    reconcile as reconcile_segments,
    source_speaker_names,
    unplaced_sources as unplaced_segments,
)
from clear_record.engine.audio import read_audio
from clear_record.providers import (
    download_ggml_model as _providers_download_ggml_model,
    get_backend,
    probe_ggml_plugin_load,
)

from clear_record.pipeline import eval as _eval
from clear_record.pipeline import transcription
from clear_record.pipeline.workspace import (
    IGNORE_FILE,
    DeclarationUnreadable,
    Workspace,
    discover_inputs,
    is_audio,
    report_line,
)


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
    and chunk count — returned so a caller that wants the typed result holds
    what the stage did without re-reading the file it just wrote. ``attribution``
    is the ``--attribute-energy`` pass ``run`` drives *inside* this stage (an
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
    loaded segments — a label the pass **removed** counts, not only one it
    assigned, so it covers a segment a non-candidate source's own name is stripped
    from as well as one the pass re-attributed — and ``speakers`` the distinct
    labels of the segments the pass returned — so a reader never has to recompute
    any of the three from a state that no longer exists. ``speakers`` counts the
    labels the pass emitted, so a segment left unnamed (a reference's or an
    excluded feed's own transcript, declined) is not one of them.

    ``mixed_references`` is every reference the pass was **asked** to gate with,
    by id — the ids the caller named for the pass itself, in the order named, then
    the manifest's declared ``mixed`` roles in manifest order, with a source the
    caller named not repeated. It is the declared set, not the set that gated: a
    reference whose audio cannot be read gates nothing (see the engine's own
    contract). An empty tuple means nothing was asked to.
    """

    per_source: dict[str, list[Segment]]
    segments: int
    speakers: int
    changed: int
    mixed_references: tuple[str, ...] = ()
    window_s: float | None = None


@dataclasses.dataclass(frozen=True)
class GlossaryReport:
    """The glossary a workspace holds: where it lives and its terms, in order."""

    path: Path
    terms: tuple[str, ...]


# --------------------------------------------------------------------------- #
# the pass's own words, on the one channel
# --------------------------------------------------------------------------- #
def _report_pass(
    w: Workspace,
    sink: EventSink | None,
    stage: str,
    summary: str,
    rows: Sequence[tuple[str, str | None]] = (),
    *,
    report: JobEvent | None = None,
    index: int = 0,
    total: int = 0,
    reused: int = 0,
) -> None:
    """Report one pass's words: its summary line, then a line per row.

    A stage's mid-stage lines are reported where they happen
    (:func:`~clear_record.pipeline.workspace.report_line`); what a pass knows
    only when it is over — the summary and the table the returned report holds —
    is reported here, on the **same** channel, in the same order the command
    surface prints it. That is what makes one payload serve every consumer: the
    command line prints these messages, and a client reading a run's stream
    reads the same lines, with the same text and in the same order.

    Each line carries the pass's counters, so the console's bar keeps reading a
    real count off the newest event:

    - ``report`` — the pass's own closing progress event, for a pass that draws
      a bar: the lines copy its counters, its elapsed clock and its ``done``,
      exactly as a line that belongs to a chunk does, so the run's cost record
      reads the same stage wall-clock back;
    - ``index``/``total``/``reused`` — the counters themselves, for a pass whose
      closing report is not in reach (``transcribe`` reports its tally on the
      chunk pool's own events).

    ``rows`` are ``(line text, source id)`` pairs: the source a line is about
    travels in the event's ``source``, the way a mid-stage line's does.
    """
    # ``report_line``'s two branches are exclusive: with a ``report`` the line is
    # a copy of that event and its counters come from there, so the counters this
    # call holds go out only when there is no report to copy them from — never
    # beside one, where they would be dropped without a word.
    counters: dict[str, int] = (
        {} if report is not None else {"index": index, "total": total, "reused": reused}
    )
    report_line(w, sink, stage, summary, report=report, **counters)
    for text, source in rows:
        report_line(w, sink, stage, text, source=source, report=report, **counters)


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
def _source_id(path: Path, directory: Path) -> str:
    """Slug one input into a source id: its path under *directory*, suffix
    dropped, every separator folded to ``__`` — or, for an input outside
    *directory*, its bare stem, unfolded.

    Neither shape is injective, so the slug is a *proposal* for an id rather than
    the pass's final word: ``sub/meeting.wav`` and ``sub__meeting.wav`` both read
    ``sub__meeting``, ``/a/x.wav`` and ``/b/x.wav`` both read ``x``, and the
    multichannel split's ``<base>__chN`` meets an ordinary top-level
    ``<base>__chN.wav``. :func:`_staged_id` is what settles a proposal against
    the ids the pass has already staged.
    """
    try:
        rel = path.relative_to(directory)
    except ValueError:
        return path.stem
    return str(rel.with_suffix("")).replace("/", "__").replace("\\", "__")


def _staged_id(
    desired: str, taken: dict[str, str], subject: str
) -> tuple[str, str | None]:
    """Settle one proposed id (*desired*) against the ids already staged.

    ``taken`` maps every id this pass has staged to the unit holding it — an
    input, or one channel of one — and is updated here. The first unit staged
    under an id **keeps** it; a unit that meets a taken id, whether the id was
    :func:`_source_id`'s proposal for it or a ``-N`` id this function handed out
    earlier, takes the next free ``-N`` suffix. Holding the handed-out ids too is
    what keeps the *next* proposal off them (holding the proposals alone would let
    a later input land on an id already staged). So a unit nobody meets keeps
    exactly the id its slug read, and a disambiguated one can itself be
    disambiguated again: of ``[x.wav (4-channel), x__ch1.wav, x__ch1-2.wav]``, the
    last stages as ``x__ch1-2-2``. Staged audio is named after the id
    (``audio/<id>.wav``), so **without** this a second unit settling on one id
    would decode onto the first's audio while the manifest kept two entries naming
    that one path.

    Returns ``(id, holder)``: ``holder`` is the unit whose id the later one
    collided with, or ``None`` when there was no collision, so the caller can
    report the collision rather than take the file silently.
    """
    holder = taken.get(desired)
    if holder is None:
        taken[desired] = subject
        return desired, None
    number = 2
    while f"{desired}-{number}" in taken:
        number += 1
    sid = f"{desired}-{number}"
    taken[sid] = subject
    return sid, holder


def _settled_id_for(base: str, declared: set[str], staged: set[str]) -> str | None:
    """The id a declaration for the unit whose slug reads *base* settled under.

    :func:`_staged_id` hands a unit one of exactly two ids: the slug it proposed,
    or the ``-N`` it was disambiguated onto when that slug was already taken. A
    declaration keyed by either of those is that unit's — which is what lets a
    folded copy's declaration be found under the id the settling produced rather
    than under the slug, where a copy the settling had renamed left it behind.

    An id in *staged* is not it: this pass settled an input there, and the
    declaration is that input's own (a kept ``take.wav`` beside a folded
    ``take.wv`` stages under ``take``). The unnumbered id wins when both forms
    are declared, and the lowest ``-N`` after it, which is the order
    :func:`_staged_id` hands them out. ``None`` when neither is declared.
    """
    candidates = {mid for mid in declared if mid not in staged}
    if base in candidates:
        return base
    numbered = sorted(
        (
            mid
            for mid in candidates
            if mid.startswith(f"{base}-") and mid[len(base) + 1 :].isdigit()
        ),
        key=lambda mid: int(mid[len(base) + 1 :]),
    )
    return numbered[0] if numbered else None


def _shown(path: Path, directory: Path) -> str:
    """*path* as the pass names it: relative to the workspace when it is under
    it, the path as given when it is not (a declared input, which may live
    anywhere the caller names)."""
    try:
        return str(path.relative_to(directory))
    except ValueError:
        return str(path)


#: The kinds of name this pass reads a start off, spelled out because they are
#: *this* pass's set and not a vendor's: a date (``20260101`` or ``2026-01-01`` or
#: ``2026.01.01``), then a clock time whose seconds are present
#: (``120320``, ``12:03:20``, ``12-03-20``, ``12.03.20``), joined by at most one
#: ``T``/``_``/``-``/``.``/space — ``20260101_120320``, ``20260101T120320``,
#: ``2026-01-01_12-03-20``, ``2026.01.01 12.03.20``. The recorder names in the
#: owner's own playbook (``mac-05``, ``tx01``…) carry no clock time at all, so a
#: name outside these shapes — a date alone, minutes without seconds, a shape no
#: recorder here writes — declares **nothing**, and its source is then estimated
#: from the audio as before (or placed by a start the operator declares in the
#: manifest). A date alone is refused on purpose: reading it as a start would
#: declare every device that shares a day simultaneous, which is the confusion a
#: declared start exists to prevent.
_STAMP_RE = re.compile(
    r"(?<!\d)"
    r"(?P<y>\d{4})[-.]?(?P<mo>\d{2})[-.]?(?P<d>\d{2})"
    r"[T_\- .]?(?P<h>\d{2})[:.\-]?(?P<mi>\d{2})[:.\-]?(?P<s>\d{2})"
    r"(?!\d)"
)


def _declared_start(path: Path) -> float | None:
    """*path*'s start, as the recorder that wrote its name declared it.

    Seconds since the epoch, read as UTC (only differences between two of these
    are ever taken, so the zone they are read in cancels), or ``None`` when the
    name states no start time. The **first** stamp in the name is the start: a
    name that carries both ends of a part
    (``REC_20260101_120320_to_20260101_120500``) states its start first, and the
    start is the end a part is placed by.
    """
    match = _STAMP_RE.search(path.stem)
    if match is None:
        return None
    try:
        return datetime(
            int(match["y"]),
            int(match["mo"]),
            int(match["d"]),
            int(match["h"]),
            int(match["mi"]),
            int(match["s"]),
            tzinfo=timezone.utc,
        ).timestamp()
    except ValueError:
        # A run of digits shaped like a full stamp but not a date or a clock
        # time (2026-13-45_12-03-20, 20260101_126099): the name declares
        # nothing, and inventing a start from it would place the source on a
        # timeline it never saw.
        return None


def _stamp_text(seconds: float) -> str:
    """A declared start as the operator reads it (whole seconds, UTC — the zone
    :func:`_declared_start` read it in, so the text is the name's own)."""
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _usable_start(value: object) -> float | None:
    """*value* as a declared start this pass can use, or ``None``.

    ``Source.start_s`` is a number when :func:`_declared_start` reads it off a file
    name; it is whatever a hand-edit put there when an operator declares one in
    the manifest, which is the flow this feature exists for. So both readings take
    only a value a clock can **render** — the pass reports every declaration it
    keeps as a date and a time, and a value it cannot say is a value it cannot
    back. A name (``"noon"``), a bool, ``nan``/``inf``, or a number no
    ``datetime`` holds (``1767000000000.0``, year 57964) is therefore **no
    declaration at all**: dropped where it is read, never raised over.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
        datetime.fromtimestamp(number, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return number


def _manifest_starts(w: Workspace) -> dict[str, float]:
    """The declared starts the workspace's manifest already holds, by source id.

    This pass **rebuilds** the manifest from what it discovered, so a start an
    operator declared by hand — or one an earlier pass read off a name — would be
    dropped by the next pass, and ``run`` ingests every time: the documented flow
    (declare, then run) has to survive it. What is carried over is keyed by id,
    which is what the manifest names a source by, and only a start
    :func:`_usable_start` accepts is carried: a value this pass cannot render is
    dropped, not carried into a source or a sink line.

    A manifest this pass cannot take a declaration out of declares nothing, and
    that is not this pass's refusal: the pass has not been asked to *use* the
    manifest, only not to lose what it says, so a broken one costs the record its
    declarations and nothing else. The manifest read is the workspace's **root**
    one (:meth:`Workspace.load_published_manifest`): a run writes its own copy
    fresh in its scope, while a hand-edit lands at the root, which is where the
    run it precedes publishes and reads (ADR-0033). **The shapes a hand-edit
    produces are exactly what that has to cover** — an operator editing
    ``manifest.json`` to declare a start is who this function is for — so every
    way the read can fail is named here: bytes that cannot be read (``OSError``),
    bytes that are not JSON (``ValueError``), a body that is not a mapping
    (``TypeError``), a mapping with no ``sources`` key (``KeyError``), a
    ``sources`` that is not a list or an entry without ``path`` or ``id``
    (``TypeError``), an entry that is not a mapping at all (``AttributeError``,
    which is also what iterating a plain string yields), and a ``start_s`` no
    clock can render (:func:`_usable_start`).
    """
    try:
        sources, _ = w.load_published_manifest()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    starts: dict[str, float] = {}
    for source in sources:
        start = _usable_start(source.start_s)
        # The id is an unhashable hand-edit too (a list, an object): it cannot key
        # a mapping, so no id means no way to carry the declaration anywhere.
        if start is not None and isinstance(source.id, str):
            starts[source.id] = start
    return starts


#: One read block of the copy check below: a copy is compared as a stream, so a
#: folder of long takes is never held in memory to compare them.
_DIGEST_BLOCK = 1 << 20


def _digest(path: Path) -> str:
    """The sha256 of one input's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_DIGEST_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _collapse_copies(
    files: Sequence[Path],
) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """Fold byte-identical inputs into one: ``(kept, [(copy, kept_file), ...])``.

    A recording folder is not one event. It accumulates earlier sessions, a case
    copied in brings its own copies along, and an operator's
    ``cp take.wav take-copy.wav`` is the same bytes under a second name — so two
    sources naming them means the same audio is decoded, transcribed and merged
    twice. One input per content is kept here, the first in discovery order: the
    operator already names files to force discovery order, so which of a pair
    survives is a choice that stays theirs. What the copy declared is not lost
    with it — a start its name states, or one the manifest declares for its id,
    is a start about the survivor's audio too, and ``ingest`` hands it to the
    survivor's units.

    What is folded is a **byte** copy, and only that. A *processed* twin — a
    DJI-style export's ``_edit`` beside the ``_orig`` it was made from carries a
    tone preset and gain the original does not — is not byte-identical, so this
    pass never folds it; a folder that means to leave such a file out says so in
    its own declaration (``.clear-record-ignore``).

    The inputs are grouped by size first — two byte-identical files always share
    one — so only the files that *could* be copies of each other are read: taking
    a digest of every input would read a whole folder twice for nothing. An
    input that cannot be read is left where it is, for the decode to refuse; it
    is not this pass's business to drop it silently.
    """
    sizes: dict[Path, int] = {}
    shared: dict[int, int] = {}
    for p in files:
        try:
            size = p.stat().st_size
        except OSError:
            continue
        sizes[p] = size
        shared[size] = shared.get(size, 0) + 1
    kept: list[Path] = []
    copies: list[tuple[Path, Path]] = []
    first_by_digest: dict[str, Path] = {}
    for p in files:
        size = sizes.get(p)
        if size is None or shared[size] < 2:
            # Unreadable, or a size no other input has: cannot be a copy.
            kept.append(p)
            continue
        try:
            digest = _digest(p)
        except OSError:
            kept.append(p)
            continue
        first = first_by_digest.setdefault(digest, p)
        if first is p:
            kept.append(p)
        else:
            copies.append((p, first))
    return kept, copies


def _manifest_roles(w: Workspace) -> dict[str, str]:
    """The roles an existing manifest declares, keyed by source id.

    The twin of :func:`_manifest_starts` for the other declaration an operator
    makes by hand: ``ingest`` **rebuilds** the manifest from the discovered files,
    and ``run`` ingests every time, so a role written into ``manifest.json`` — the
    flow that lets a capture say which of its mics are rooms and which feed
    duplicates — would be dropped by the next pass, and the documented
    declare-then-run flow has to survive it. What is carried over is keyed by id,
    which is what the manifest names a source by, and only a non-empty string is
    carried: a hand-edit can put anything in the field, and a value this build
    cannot honour is better read where a role means something (``attribute``
    refuses a name outside the vocabulary, naming the three) than dropped here in
    silence.

    A manifest this pass cannot take a declaration out of declares nothing, and
    that is not this pass's refusal — it was not asked to *use* the manifest, only
    not to lose what it says — so the read covers exactly the shapes
    :func:`_manifest_starts` names, and reads the same **published** manifest
    (the workspace root's) for the same reason.
    """
    try:
        sources, _ = w.load_published_manifest()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    roles: dict[str, str] = {}
    for src in sources:
        # Only a string id can key the mapping: a hand-edit can put a list or an
        # object where the manifest names a source, and this function's key is the
        # id — an unhashable one raised ``TypeError`` out of the pass for exactly
        # the manifests that carried a role. :func:`_manifest_starts` drops the
        # same shape, and only a non-empty string declares a role here at all.
        if not isinstance(src.id, str):
            continue
        if isinstance(src.role, str) and src.role not in ("", "candidate"):
            roles[src.id] = src.role
    return roles


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

    A file discovery could not use is *named* on the sink as one line per file
    (:func:`report_line`, level ``warn``) — a tape the operator meant to hand
    over must not go unnamed — and the line arrives before the pass's bar opens,
    so it carries no counters. What is named *this way* is what the walk
    (:func:`~clear_record.pipeline.workspace.discover_inputs`) could not use as
    an input: a regular file whose suffix is outside ``AUDIO_SUFFIXES``, less
    the workspace's own state — its output dirs, its bookkeeping files and the
    app's own ``agent/`` drafts — and hidden entries, none of which is a tape.
    (The walk's other name — an audio file the workspace's own declaration takes
    out — is the paragraph below.) Only a discovered walk names anything: an
    explicit ``audio_files`` list declares its inputs, and nothing beside them
    was looked at.

    An input the workspace's own ``.clear-record-ignore`` declaration names is
    named the same way, at level ``info`` — "excluded …, named in
    ``.clear-record-ignore``" — because a file the declaration takes out is not
    a file the pass silently forgot. A declaration that cannot be **read** is the
    pass's refusal (:class:`PipelineError`, naming the file and the reason — the
    OS's words, or a codec's): the folder's exclusions are unknown, and taking
    every file would be the silent wrong answer the declaration exists to
    prevent.

    Byte-identical inputs are **one** source, not two: the same bytes decoded,
    transcribed and merged twice is one session counted twice. The first input
    in discovery order keeps the source and every later copy is named on the
    sink as folded into it (level ``warn``), before the bar opens. Which of a
    pair survives is discovery order, which the operator names files to control.

    A recorder that splits one long capture into numbered files hands the pass
    files that are *sequential*, not simultaneous. A name that states when the
    part started (:func:`_declared_start`) declares it — reported as its own
    line, level ``info``, saying what *this* pass did (it read the name) rather
    than what ``align`` will do with it, which depends on the reference's own
    declaration and on whether the two can overlap — and the start is carried
    into the manifest as ``Source.start_s``. That is where it belongs: the
    rotation seam between two parts is not sample-continuous, and consecutive
    parts share no passage, so ``align`` places a declared pair from the
    difference of the declarations rather than correlating two recordings that
    never overlap — when the two cannot overlap (their distance reaches the
    length of the part that began first, less one correlation window: the pre-roll
    a rotating recorder may keep, its parts meeting inside it) or when that
    distance is wider than its search band. A pair that *can* overlap is the
    audio's to place, and the declaration is the fallback when it has no verdict.
    A name that states no start time (or only a date, or only minutes) declares
    nothing, and that input is staged exactly as it was before — unless a
    byte-identical copy of it was folded away, in which case the survivor carries
    the start that copy's name states (the line names the file whose name declares
    it), as it carries one the manifest declares for the copy's id.

    A start the manifest **already** declares is not lost to this pass: the
    manifest it rebuilds is read first, and a source whose name declares nothing
    keeps the start that manifest held for its id (reported as ``keeps declared
    start …``). An operator's own declaration, and the ``run`` that ingests
    before it aligns, therefore survive the re-ingest. A manifest that cannot be
    read declares nothing and is not this pass's refusal.

    Each input's decode line is reported on the sink (:func:`report_line`) as
    that decode begins — one line per decode, before normalizing that input, not
    batched after the pass — so a caller watching a long ingest sees every file
    announced where its work starts.
    """
    w = Workspace.at(directory)
    d = w.root
    if audio_files:
        files, skipped = [Path(a) for a in audio_files], []
    else:
        try:
            files, skipped = discover_inputs(d)
        except DeclarationUnreadable as exc:
            # The walk reads the workspace's own declaration, and nothing else in
            # it can fail this way (``rglob`` skips a subdirectory it cannot
            # list): the pass cannot know which files the folder excludes, and
            # taking every one of them would be the silent wrong answer the
            # declaration exists to prevent. Refused in the pass's own voice —
            # naming the file and the reason — rather than as a traceback.
            raise PipelineError(f"[ingest] {exc}") from exc
    # Where discovery happens, so the operator reads what was left out beside
    # the files that were taken — and before the refusal below, so a directory
    # of nothing but unusable files still says which ones they were. A file with
    # an audio suffix reaches this list only when the workspace's own
    # ``.clear-record-ignore`` named it: nothing else takes an input out of the
    # walk, so the two are told apart by the suffix, and each gets its own words.
    for p in skipped:
        if is_audio(p):
            report_line(
                w,
                on_event,
                Step.INGEST.value,
                f"[ingest] excluded {_shown(p, d)}: named in {IGNORE_FILE}",
                level="info",
            )
        else:
            report_line(
                w,
                on_event,
                Step.INGEST.value,
                f"[ingest] cannot use {_shown(p, d)}: not a recognized audio "
                f"file ({p.suffix or 'no suffix'})",
                level="warn",
            )
    # The same bytes twice is the same audio: one input per content is staged,
    # and the copies discovery took are named here — before the bar opens, like
    # the lines above — rather than dropped in silence.
    files, copies = _collapse_copies(files)
    for p, kept_file in copies:
        report_line(
            w,
            on_event,
            Step.INGEST.value,
            f"[ingest] duplicate of {_shown(kept_file, d)}: {_shown(p, d)} is "
            f"byte-identical, folded into one source",
            level="warn",
        )
    if not files:
        raise PipelineError(f"[ingest] no audio files found in {d}")
    audio_dir = w.audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)

    progress = Progress(Step.INGEST.value, len(files), on_event)
    progress.start()
    # Every id this pass has staged, and the unit holding it: the slug alone is
    # not injective (:func:`_source_id`), and the staged audio is named after the
    # id — so without this a second unit that slugs alike would decode onto the
    # first's ``audio/<id>.wav`` while the manifest kept two sources naming one
    # path. The first unit staged under an id keeps it; one that meets a taken id
    # — a slug another unit proposed, or a ``-N`` id this pass handed out — is
    # disambiguated, and the collision is reported where its id is settled.
    taken: dict[str, str] = {}

    def stage_id(desired: str, subject: str, index: int) -> str:
        sid, holder = _staged_id(desired, taken, subject)
        if holder is not None:
            report_line(
                w,
                on_event,
                Step.INGEST.value,
                f"[ingest] id collision: {desired!r} already taken by {holder}; "
                f"{subject} staged as {sid!r}",
                level="warn",
                source=sid,
                index=index,
                total=len(files),
            )
        return sid

    # A source's ``label`` is the speaker name ``reconcile``/``attribute`` fall
    # back to. Never derive it from the file name: a tape's name is not a person,
    # and the minutes must not list one as an attendee. Channels are speaker-like,
    # so every source gets a positional ``Speaker N`` (an explicit caller label
    # still wins in ``engine.merge.source_speaker_names``).
    sources: list[Source] = []
    # The manifest this pass is about to rebuild, read first: both declarations it
    # can hold about a source are part of the record — a **start** (by an operator
    # by hand, or by an earlier pass off a name) and the source's **role** (the
    # room microphone, the duplicate feed a capture carries beside its speakers) —
    # so a source whose own name and id declare nothing keeps what the manifest
    # held for it, and neither is lost to the ``run`` that follows.
    carried = _manifest_starts(w)
    roles = _manifest_roles(w)
    # Every unit's id is settled **before** anything is decoded: a declaration is
    # filed under the id the unit settled on, not under the slug it proposed, so
    # the fold below can only read one off the ids the settling produced. The
    # decode loop reads this map rather than settling again, and the collision
    # line arrives here, where the id is decided.
    units: dict[Path, list[str]] = {}
    channels: dict[Path, int] = {}
    for number, p in enumerate(files, start=1):
        base = _source_id(p, d)
        nch = channel_count(p)
        channels[p] = nch
        do_split = (split == "split") or (split == "auto" and nch > 2)
        if do_split and nch > 1:
            units[p] = [
                stage_id(
                    f"{base}__ch{ch + 1}", f"{p.name} ch{ch + 1}/{nch}", number - 1
                )
                for ch in range(nch)
            ]
        else:
            units[p] = [stage_id(base, p.name, number - 1)]

    # A folded copy is the same audio under another name, so a declaration about
    # the copy — a start its id holds, the role it was given ("this one is a
    # room, that one feeds a duplicate") — is a declaration about the survivor's
    # audio too, and dropping it would re-promote the very microphone the
    # operator declared not to be a speaker. Which id it lives under is what the
    # **settling** says: the copy keeps the slug it proposed, or the ``-N`` it was
    # disambiguated onto, and its channels read ``<slug>__chN`` — reading that off
    # the slug alone left a renamed copy's declaration behind, and handed a kept
    # input that merely shares the copy's slug its own declaration as the
    # survivor's. The copy can be the survivor — the fold keeps the first in
    # discovery order — and then that id is exactly what leaves the manifest:
    # dropped silently, with only the audio left to place a source its own name
    # had already placed. The copy is the survivor's own bytes, so it parts into
    # the survivor's units, and each declaration follows its channel across.
    declared_ids = set(carried) | set(roles)
    staged_ids = set(taken)
    folded_names: dict[Path, tuple[str, float]] = {}
    inherited: dict[str, float] = {}
    inherited_roles: dict[str, str] = {}
    for copy, kept_file in copies:
        copy_name_start = _declared_start(copy)
        if copy_name_start is not None:
            folded_names.setdefault(kept_file, (copy.name, copy_name_start))
        copy_base = _source_id(copy, d)
        survived = units.get(kept_file, [])
        nch = channels.get(kept_file, 1)
        do_split = (split == "split") or (split == "auto" and nch > 2)
        bases = (
            [f"{copy_base}__ch{ch + 1}" for ch in range(nch)]
            if do_split and nch > 1
            else [copy_base]
        )
        for index, base in enumerate(bases):
            if index >= len(survived):
                break
            settled = _settled_id_for(base, declared_ids, staged_ids)
            if settled is None:
                continue
            target = survived[index]
            if settled in carried:
                inherited.setdefault(target, carried[settled])
            if settled in roles:
                inherited_roles.setdefault(target, roles[settled])

    def declared_role(sid: str) -> str:
        """The role this source carries: its own id's declaration, a folded copy's,
        or the default. ``_manifest_roles`` carries no empty value, so either
        declaration either stands or the default does."""
        return roles.get(sid) or inherited_roles.get(sid) or "candidate"

    def kept_line(sid: str, subject: str, start: float, number: int) -> None:
        """Name a start this input **keeps** from the manifest (its name declared
        none), where the source is the one it settled on."""
        report_line(
            w,
            on_event,
            Step.INGEST.value,
            f"[ingest] {subject} keeps declared start {_stamp_text(start)}: "
            f"declared in the manifest",
            level="info",
            source=sid,
            index=number - 1,
            total=len(files),
        )

    for number, p in enumerate(files, start=1):
        base = _source_id(p, d)
        # A recorder that splits one capture into numbered files writes the start
        # time of each part into its name; that is the pass's only evidence that
        # this input is a *slice of one session* rather than a device of its own,
        # and the name is the honest place for the fact. What the line says is
        # what this pass did — it read the name — and not what `align` will do
        # with it: whether the reference declares a start, and whether the two
        # can overlap, is not known here.
        from_name = _declared_start(p)
        named = p.name
        folded_name = folded_names.get(p)
        if from_name is None and folded_name is not None:
            # The copy's name is where the start comes from — and the fold's own
            # line above has already said the two files are one source.
            named, from_name = folded_name
        if from_name is not None:
            report_line(
                w,
                on_event,
                Step.INGEST.value,
                f"[ingest] {named} declares start {_stamp_text(from_name)} "
                f"(from its filename): carried into the manifest",
                level="info",
                source=base,
                index=number - 1,
                total=len(files),
            )
        nch = channels[p]
        # One source per channel is the multi-mic meeting case, where each speaker
        # is nearest one channel and diarization is nearly free; the ids above are
        # what the settling produced, so the split decision is that list's length.
        splitting = len(units[p]) > 1
        for ch, sid in enumerate(units[p]):
            subject = f"{p.name} ch{ch + 1}/{nch}" if splitting else p.name
            declared = from_name if from_name is not None else carried.get(sid)
            if declared is None:
                declared = inherited.get(sid)
            if from_name is None and declared is not None:
                kept_line(sid, subject, declared, number)
            norm = audio_dir / f"{sid}.wav"
            # The line announces this decode, where the pre-move CLI printed it:
            # before normalizing that input, not batched after the pass.
            report_line(
                w,
                on_event,
                Step.INGEST.value,
                f"[ingest] decode {subject} -> {norm.name}",
                source=sid,
                index=number - 1,
                total=len(files),
            )
            if splitting:
                prepare_16k_wav(p, norm, channel=ch)
            else:
                prepare_16k_wav(p, norm)
            sources.append(
                Source(
                    id=sid,
                    path=str(norm),
                    label=f"Speaker {len(sources) + 1}",
                    clock_domain="wall",
                    start_s=declared,
                    role=declared_role(sid),
                )
            )
        advance_source = base if splitting else units[p][0]
        closing = progress.advance(source=advance_source)
    w.write_manifest(sources)
    # The pass as a whole: the summary and the source table, reported on the same
    # channel as the decode lines above and carrying the pass's closing
    # counters — so the command surface prints them and a client reading the
    # run's stream reads the same text.
    _report_pass(
        w,
        on_event,
        Step.INGEST.value,
        f"[ingest] {len(sources)} source(s) -> {w.manifest_path}",
        [
            (
                f"  {source.id:24s} {source.path}"
                + (f"  [{source.role}]" if source.role != "candidate" else ""),
                source.id,
            )
            for source in sources
        ],
        report=closing,
    )
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
    progress.start()
    alignment = align_sources(sources, reference_id=reference)
    closing = progress.advance()
    w.write_manifest(sources, alignment)
    # The alignment as a whole: where every source landed, and the ones that
    # could not be placed — the same words the command surface prints, on the
    # run's one channel.
    rows = [
        (
            f"  {sid:24s} offset={offset:+.4f}s"
            + (" (ref)" if sid == alignment.reference else ""),
            sid,
        )
        for sid, offset in alignment.offsets.items()
    ] + [
        (f"  {sid:24s} UNRESOLVED (could not place this source)", sid)
        for sid in alignment.unresolved
    ]
    _report_pass(
        w,
        on_event,
        Step.ALIGN.value,
        f"[align] reference={alignment.reference} method={alignment.method} "
        f"conf={alignment.confidence} unresolved={len(alignment.unresolved)}",
        rows,
        report=closing,
    )
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


def _scripts_label(shown: tuple[str, ...]) -> str:
    """The scripts one source's text shows, as the pass names them.

    ``"simplified+traditional"`` for a source whose own chunks hold both, and
    ``"unknown"`` for one the classifier could not settle -- the two cases a
    reader needs to tell apart from a source that is plainly one script.
    """
    return "+".join(shown) if shown else "unknown"


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

    Chinese comes back in whichever **script** the backend writes — whisper.cpp's
    ``-l zh`` in Traditional characters, the native transcriber in Simplified —
    and nothing here rewrites it. The pass therefore records, per source, the
    scripts the source's text actually shows in ``segments.json``'s meta
    (``engine.text.han_scripts``: both of them wherever the text itself holds one
    of each, whatever wrote it — a single entry says what the text shows, not how
    many passes wrote it — and nothing where the text settles neither), and names
    the sources
    whenever that is not uniform — a difference between two sources or inside one
    is never silent.

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
    typed report.
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
    # Han script, per source. Nothing in the pipeline rewrites a script, and the
    # paths do not agree on one: whisper.cpp's ``-l zh`` writes Mandarin in
    # Traditional characters where Apple's on-device transcriber writes
    # Simplified, so a workspace that mixes backends across sources
    # (``--rerun-source``) would otherwise interleave the two with no marker at
    # all. What each source's text *shows* is recorded -- both scripts where it
    # shows both, whether one decode wrote them or several did -- and the pass
    # names the sources whenever that is not uniform,
    # whether the difference is between two sources or inside one.
    scripts = {
        sid: han_scripts(" ".join(seg.text for seg in segs))
        for sid, segs in per_source.items()
    }
    for sid, shown in scripts.items():
        if shown:
            meta["sources"].setdefault(sid, {})["scripts"] = list(shown)
    # Uniform means: every source shows the same, single script. A source showing
    # both is already not uniform on its own, and an undetermined source (empty)
    # is never counted as agreeing or disagreeing.
    shown_sets = {shown for shown in scripts.values() if shown}
    named_scripts = any(len(shown) > 1 for shown in shown_sets) or len(shown_sets) > 1
    w.write_segments(per_source, meta)
    # The pass as a whole, on the run's one channel: what decoded and where the
    # transcript went, then one line per source. Transcribe's own closing report
    # belongs to the chunk pool (its tally line above), so the counters the pass
    # stands at are the ones it recorded — the chunks that source set held.
    chunked = sum(int(info.get("chunks") or 0) for info in meta["sources"].values())
    reused = int(meta["chunk_report"].get("reused") or 0)
    rows: list[tuple[str, str | None]] = []
    for sid, segs in per_source.items():
        info = meta["sources"].get(sid, {})
        duration = info.get("duration")
        # Where the scripts are not uniform, each row names what that source
        # shows -- both scripts where it shows both: the mix is exactly the case
        # a reader must be able to see, and a uniform pass needs no column (the
        # record's meta still carries it).
        script = (
            f"  script={_scripts_label(scripts.get(sid, ()))}" if named_scripts else ""
        )
        rows.append(
            (
                f"  {sid:24s} segments={len(segs):4d}  "
                f"duration={duration if duration is not None else '?'}  "
                f"chunks={info.get('chunks', '?')}{script}",
                sid,
            )
        )
    if named_scripts:
        # This line is the newest event on the run's stream, so it carries the
        # pass's own tally: a line that states no counters of its stage's base
        # would move the console's bar backwards (``report_line``'s contract).
        report_line(
            w,
            on_event,
            "transcribe",
            "[transcribe] Han script is not uniform: "
            + ", ".join(
                f"{sid}={_scripts_label(scripts.get(sid, ()))}" for sid in per_source
            ),
            index=chunked,
            total=chunked,
            reused=reused,
            level="warn",
        )
    _report_pass(
        w,
        on_event,
        Step.TRANSCRIBE.value,
        f"[transcribe] {meta['model']!r} via {meta['backend']} -> segments.json",
        rows,
        index=chunked,
        total=chunked,
        reused=reused,
    )
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

    A source the manifest does not declare a candidate — the room microphone, the
    duplicate feed (see :func:`_attribution_roles`) — is left exactly as it came
    in: this pass clusters **one source's own audio**, so the ``Speaker N`` it finds
    is that source's index rather than a person's name, and the record reads it as
    a speaker the microphone merely heard (its first cluster collides with the
    first candidate's label). `attribute`, which reads every candidate's mic in the
    segment's window, is the pass that may name one.

    A one-off `attribute --mixed-source` declaration (:func:`_declined_references`)
    is left unnamed here for the same reason, even though its source is a manifest
    candidate: the declaration is what keeps a microphone out of the record, and a
    cluster label is a name like any other — relabelled here, the declaration the
    pass after this one honours would be explaining labels that no longer exist.

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
    # The one-off a caller named for the attribute pass alone, read from this file's
    # own declaration: a source that may not be named is not a person, so this pass
    # must not hand it a cluster label either — the label would be a name the pass
    # after this one can no longer strip, and the declaration would be left
    # explaining labels that no longer exist.
    declared = set(_declined_references(meta))
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
        if n_found > 1 and src.role == "candidate" and sid not in declared:
            per_source[sid] = [
                dataclasses.replace(s, speaker=f"Speaker {labels[i] + 1}")
                for i, s in enumerate(segs)
            ]
            applied = True
        facts.append(DiarizedSource(id=sid, segments=len(segs), speakers=n_found))
        # The source's own declared role is the reason that describes it when it
        # has one: a caller's one-off is for a source the manifest leaves a
        # candidate (that is how a name the caller gives for one pass is told from
        # a durable role), and the recorded gate set holds both.
        if src.role != "candidate":
            note = f" (declared {src.role}: left unnamed)"
        elif sid in declared:
            note = " (named a reference for this pass: left unnamed)"
        else:
            note = ""
        report_line(
            w,
            on_event,
            "diarize",
            f"[diarize] {sid}: {n_found} speaker(s) over {len(segs)} segment(s){note}",
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
def _named_references(named: str | Sequence[str] | None) -> tuple[str, ...]:
    """The reference ids a caller named for one pass, as the tuple the pass reads.

    ``mixed_source`` predates source roles: it names, for one pass, the source the
    caller knows is not a speaker (the field playbook's room reference). A manifest
    role says the same thing for every pass that follows, and can say it of several
    sources, so this call form stays as the one-off and either form composes with
    the other. Its one exception is a source the manifest already declares
    ``excluded``: naming one of those is a contradiction this pass refuses (see
    :func:`_attribution_roles`), because a duplicate feed or a microphone nobody
    wore is not a witness a caller can promote by naming it.
    """
    if named is None:
        return ()
    if isinstance(named, str):
        return (named,)
    return tuple(named)


def _attribution_roles(
    sources: Sequence[Source], named: Sequence[str]
) -> tuple[list[Source], list[Source]]:
    """The candidate sources and the references that gate them, by declared role.

    A source's ``role`` says what it is (``clear_record.core.SOURCE_ROLES``): a
    ``candidate`` may be named a speaker, a ``mixed`` source is a witness that
    gates weak claims and is never a speaker, and an ``excluded`` source — a
    microphone nobody wore, a duplicate feed — is neither a candidate nor a
    witness. ``named`` is the pass's own one-off: a caller naming a source that way
    declares it a reference for this pass, whatever its role — save a source
    declared ``excluded``, which is refused below, because a duplicate feed or a
    microphone nobody wore is not a witness a caller can promote by naming it. That
    one-off is what the option has always meant.

    A role outside the vocabulary is **refused** rather than read as a speaker: a
    typo'd role is a caller asking for something this pass cannot honour, and
    leaving that source a candidate would emit the very source they tried to
    silence. A name that is no source at all is refused the same way, and a source
    declared ``excluded`` cannot be named as a reference — the two roles say
    different things, and one pass cannot hold both.
    """
    for src in sources:
        if src.role not in SOURCE_ROLES:
            raise PipelineError(
                f"[attribute] source '{src.id}' declares role '{src.role}', which is "
                f"not one of: {', '.join(SOURCE_ROLES)}"
            )
    by_id = {src.id: src for src in sources}
    references: list[Source] = []
    seen: set[str] = set()
    for sid in named:
        src = by_id.get(sid)
        if src is None:
            raise PipelineError(f"[attribute] no source '{sid}' in manifest")
        if src.role == "excluded":
            raise PipelineError(
                f"[attribute] source '{sid}' is declared role 'excluded', so it "
                "cannot be this pass's mixed reference"
            )
        if sid not in seen:
            seen.add(sid)
            references.append(src)
    for src in sources:
        if src.role == "mixed" and src.id not in seen:
            seen.add(src.id)
            references.append(src)
    candidates = [s for s in sources if s.role == "candidate" and s.id not in seen]
    return candidates, references


def attribute(
    directory: str,
    mixed_source: str | Sequence[str] | None = None,
    window_s: float | None = None,
    *,
    on_event: EventSink | None = None,
) -> AttributeReport:
    """Re-attribute each segment's speaker from the relative source energy.

    Cross-talk correction for close microphones: instead of trusting the source a
    segment came from ("one source == one speaker", the closest-mic-wins rule),
    pick the source with the highest gain-normalized energy in the segment's
    aligned window. Which sources must **not** be speakers is what the manifest's
    ``role`` says: ``"mixed"`` for a mixed/room reference, ``"excluded"`` for a
    microphone nobody wore or a duplicate feed — how a capture with two room
    lavaliers and a phone memo tells this pass about all of them, where one
    ``--mixed-source`` was all a caller could name before. A caller may still
    name one for a single pass with ``mixed_source`` (one id, or several), and
    either form composes with the other.

    Every reference that reads back gates weak claims and is never itself a
    speaker candidate: a claim has to be one each witness that heard the window
    can account for. (A reference whose audio cannot be read gates nothing; the
    report still names it, because it names what the pass was asked to gate with.)
    A role outside the vocabulary is refused, naming the three that exist, rather
    than quietly read as a candidate. Composable with `reconcile`, which preserves
    the assigned speaker.

    With ``window_s`` set, normalize against each source's **recent** level over a
    causal rolling window of that many seconds (tracking drifting gain) and write
    a calibrated per-segment confidence. Without it, the static whole-recording
    correction is unchanged.

    A segment whose own source is one this pass must not name — a reference's
    transcription, an excluded feed's copy — is attributed like any other, but
    enters the pass **unnamed**, and that un-naming is a change the pass counts
    and writes: where no candidate carries its window it stays unnamed in
    ``segments.json`` too, instead of carrying a microphone's label into the
    record. The ids it was asked to gate with are recorded there as well, so
    `reconcile` — which is what names an unnamed segment from its source's label —
    honours a one-off declaration that is in no manifest role.

    Each count the summary reports is measured where the pass measures it — the
    pre-pass list, the before/after label diff, the labels it returned — and comes
    back with the segments, so a reader recomputes none of them (see
    :class:`AttributeReport`).
    """
    w = Workspace.at(directory)
    sources, alignment = w.load_manifest()
    per_source, meta = w.load_segments()
    candidates, references = _attribution_roles(
        sources, _named_references(mixed_source)
    )
    # What this pass was asked to gate with — the manifest's declared roles and the
    # ids a caller named for this pass alone — goes into ``segments.json``'s meta,
    # where `reconcile` reads it: a name the caller gives on one command line is in
    # no manifest role, so the record would otherwise name every segment this pass
    # left unnamed from the microphone's own label. The manifest's `role` is the
    # durable form and needs nothing here; a pass asked to gate with nothing clears
    # the key, and `transcribe` rewrites this file, so a declaration cannot outlive
    # the labels it explains.
    declared = [ref.id for ref in references]
    recorded = _declined_references(meta)
    if declared:
        meta["mixed_references"] = declared
    else:
        meta.pop("mixed_references", None)
    # What the pass was handed, before anything it does to a label: `changed` is
    # the loaded-vs-returned diff, so a label this pass *removes* counts as a
    # change — and a change is what makes it write (below).
    loaded = {sid: [seg.speaker for seg in segs] for sid, segs in per_source.items()}
    # A source that may not be named a speaker is not a person, so its segments
    # enter the pass with no name to fall back on. The decision itself is unchanged
    # — they are attributed to the best candidate like any other segment — but a
    # declined one is left unnamed rather than printing the microphone's own label.
    # "May not be named" is read off the pass's own candidate set, so it covers a
    # source this pass's caller named as a reference as well as the manifest roles.
    speaker_ids = {src.id for src in candidates}
    for src in sources:
        if src.id in speaker_ids or src.id not in per_source:
            continue
        per_source[src.id] = [
            dataclasses.replace(seg, speaker="") if seg.speaker else seg
            for seg in per_source[src.id]
        ]
    offsets = dict(alignment.offsets) if alignment else {}

    flat = [seg for segs in per_source.values() for seg in segs]
    if not flat:
        report = AttributeReport(
            per_source=per_source,
            segments=0,
            speakers=0,
            changed=0,
            mixed_references=tuple(ref.id for ref in references),
            window_s=window_s,
        )
    else:
        # Attribution owns the grouping: the result is keyed by each segment's own
        # `source`, so no positional reassembly over dict order is needed.
        attributed = attribute_by_source(
            flat,
            candidates,
            offsets=offsets,
            mixed=tuple(references),
            window_s=window_s,
        )
        changed = 0
        for sid in per_source:
            updated = attributed.get(sid, [])
            for before, after in zip(loaded.get(sid, []), updated):
                if before != after.speaker:
                    changed += 1
            per_source[sid] = updated
        # A label the pass changed has to reach ``segments.json`` — including one
        # it only *removed*, or the file would keep a label the pass's own return
        # does not have and the artifact would name a microphone this pass refuses
        # to name. The windowed path also writes a confidence, so persist even if
        # no label changed (otherwise the new confidence would be lost to reconcile)
        # — and so does a declaration that differs from the recorded one, which is
        # what carries a one-off reference to `reconcile` when nothing else changed.
        if changed or window_s is not None or recorded != tuple(declared):
            w.write_segments(per_source, meta)
        speakers = {
            seg.speaker for segs in attributed.values() for seg in segs if seg.speaker
        }
        report = AttributeReport(
            per_source=per_source,
            segments=len(flat),
            speakers=len(speakers),
            changed=changed,
            mixed_references=tuple(ref.id for ref in references),
            window_s=window_s,
        )
    # What the pass did, as one line on the run's own channel: the same words
    # the command surface prints, and the counts it measured rather than guessed.
    if not report.segments:
        line = "[attribute] no segments to attribute"
    else:
        suffix = ""
        if report.mixed_references:
            noun = "reference" if len(report.mixed_references) == 1 else "references"
            suffix = f" (mixed {noun}: {', '.join(report.mixed_references)})"
        if report.window_s is not None:
            suffix += f" (rolling window: {report.window_s:g}s)"
        line = (
            f"[attribute] {report.segments} segment(s), {report.speakers} speaker(s), "
            f"{report.changed} re-attributed or unnamed{suffix}"
        )
    report_line(w, on_event, "attribute", line)
    return report


# --------------------------------------------------------------------------- #
# glossary
# --------------------------------------------------------------------------- #
def glossary(
    directory: str,
    add: list[str] | None = None,
    *,
    on_event: EventSink | None = None,
) -> GlossaryReport:
    """Show (and optionally append to) the workspace glossary.

    The glossary is one term/phrase per line; it becomes the ASR decoder's
    initial prompt. Edit it while a first transcription pass runs in the
    background, then re-run `transcribe`: the chunk cache is keyed on the
    glossary, so the finished terms are applied.

    What the workspace holds — where the file is, and its terms in order — is
    reported as the pass's own lines, on the same channel as every other line,
    so the command surface prints them and a client reads them off the stream.
    """
    w = Workspace.at(directory)
    path = w.glossary_path
    if add:
        w.append_glossary(add)
    report = GlossaryReport(path=path, terms=tuple(w.glossary_terms()))
    _report_pass(
        w,
        on_event,
        "glossary",
        f"[glossary] {report.path} ({len(report.terms)} term(s))",
        [(f"  {term}", None) for term in report.terms],
    )
    return report


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #
#: How many of the record's opening segments the preview reports. A record can
#: hold thousands; the command surface's preview is a look, and a client reading
#: the channel gets the same window.
_PREVIEW_LINES = 12


def _declined_references(meta: dict) -> tuple[str, ...]:
    """The reference ids an `attribute` pass recorded, as the ids they are.

    Every reference that pass was **asked** to gate with — the ids the caller
    named for the pass itself, and the manifest's declared ``mixed`` roles — as
    the pass recorded them (see ``AttributeReport.mixed_references``).

    ``segments.json``'s meta is a hand-editable file like the manifest, so only a
    list of strings is a declaration here: any other shape reads as none, the way
    :func:`_manifest_starts` drops a start it cannot read. (An id that is no source
    names no segment either way; the shape is what is guarded.)
    """
    value = meta.get("mixed_references")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _unname_non_candidates(
    segments: Sequence[Segment],
    sources: Sequence[Source],
    declined: Sequence[str] = (),
) -> list[Segment]:
    """Leave a non-candidate source's own segments unnamed in the record.

    ``engine.reconcile`` names a segment's speaker from the diarizer/attributor,
    falling back to its **source's label** — right for a source that is a person's
    microphone, and wrong for the rest of them: a room microphone or a duplicate
    feed declared by role has no person to fall back to, so its surviving segments
    (the ones no candidate claimed) would otherwise be listed as an attendee. The
    role is what the manifest says the source *is*, so it is read there, and the
    segment is left unnamed exactly as the attribution pass leaves it — the
    fallback is the one thing that could put a microphone's name back in.

    ``declined`` is the pass's own record of what it was asked to gate with: every
    reference id that pass was given — the caller's named ids (``--mixed-source``)
    and the manifest's declared ``mixed`` roles — read from the declaration that
    pass records in ``segments.json``'s meta. A caller naming one declares it for
    that pass rather than in the manifest, so without the declaration here the
    fallback would name every segment the pass left unnamed — the one-off's
    guarantee has to hold in the record too.

    Only the source's **own** label is dropped, and only for its own segments: a
    name the attribution pass judged from the candidates' own mics is that
    speaker's — the diarizing pass leaves a non-candidate unnamed for exactly this
    reason — and a candidate's segments are untouched.
    """
    unnameable = {src.id for src in sources if src.role != "candidate"} | set(declined)
    if not unnameable:
        return list(segments)
    labels = source_speaker_names(sources)
    return [
        dataclasses.replace(seg, speaker="")
        if seg.source in unnameable and seg.speaker == labels.get(seg.source)
        else seg
        for seg in segments
    ]


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
    # The one-off a pass was asked to gate with: the ids `attribute` declined are in
    # this file's meta, because a name the caller gives for a single pass is in no
    # manifest role, and the fallback `engine.reconcile` applies would otherwise
    # name that source anyway. Read here so the pass's own declaration reaches the
    # record it wrote the labels for.
    declined = _declined_references(meta)
    segments = _unname_non_candidates(
        reconcile_segments(per_source, alignment, sources), sources, declined
    )
    # What the pass could not place: a source with no alignment offset is left
    # out of the timeline. The alignment already named it in `unresolved`, but
    # nothing in the artifact said *how much* transcript went with it — an order
    # of magnitude of tape can hide behind an id — and the pass said it only at
    # stage time. The summary goes in the record and on the channel both.
    unplaced = unplaced_segments(per_source, alignment, sources)
    closing = progress.advance()
    # The Han scripts each source's text shows, as the transcribe stage read them
    # (``segments.json``'s ``meta.sources.<id>.scripts``). The record carries the
    # same lists, so a reader who opens only the artifact -- not the stage's own
    # meta beside it -- sees which scripts sit side by side in it. A source whose
    # text settles neither is absent, exactly as the stage recorded it.
    scripts = {
        sid: list(info["scripts"])
        for sid, info in meta.get("sources", {}).items()
        if isinstance(info, dict) and info.get("scripts")
    }

    record = RecordDocument(
        sources=tuple(sources),
        alignment=alignment,
        segments=tuple(segments),
        metadata={
            "title": w.root.name,
            "backend": meta.get("backend"),
            "model": meta.get("model"),
            "language": meta.get("language"),
            "scripts": scripts,
            "prefer": prefer,
            "unplaced": [dataclasses.asdict(u) for u in unplaced],
        },
    )
    w.write_record(record)
    # The record and its opening lines, on the run's one channel. The preview is
    # the record's own first segments — what the command surface shows, and all
    # of it: a client reads the same window, not a summary of it.
    speakers = {seg.speaker for seg in record.segments if seg.speaker}
    _report_pass(
        w,
        on_event,
        Step.RECONCILE.value,
        f"[reconcile] {len(record.segments)} segment(s), {len(speakers)} attributed "
        f"speaker(s) -> {w.record_path}",
        report=closing,
    )
    # Then what the pass left out: the sources the alignment gave no offset, and
    # how much of each went with them. The record carries the same thing in its
    # metadata, so a reader who only opens the artifact sees it too.
    if unplaced:
        dropped = sum(u.segments for u in unplaced)
        dropped_s = round(sum(u.speech_s for u in unplaced), 4)
        report_line(
            w,
            on_event,
            Step.RECONCILE.value,
            f"[reconcile] {len(unplaced)} unplaced source(s) left out: "
            f"{dropped} segment(s), {dropped_s}s of transcript",
            report=closing,
        )
        for u in unplaced:
            report_line(
                w,
                on_event,
                Step.RECONCILE.value,
                f"  {u.id:24s} UNPLACED (no alignment offset)",
                source=u.id,
                report=closing,
            )
    for seg in record.segments[:_PREVIEW_LINES]:
        report_line(
            w,
            on_event,
            Step.RECONCILE.value,
            f"  {format_timestamp(seg.start)} [{seg.speaker or seg.source}] {seg.text}",
            source=seg.source,
            report=closing,
        )
    if len(record.segments) > _PREVIEW_LINES:
        report_line(
            w,
            on_event,
            Step.RECONCILE.value,
            f"  … {len(record.segments) - _PREVIEW_LINES} more",
            report=closing,
        )
    return record


def format_timestamp(seconds: float) -> str:
    """``HH:MM:SS.mmm`` on the record's own (reference) clock.

    The clock's zero is the reference source's start, and a source that began
    *before* it — the phone started first, the ordinary case — has negative
    reference times. The sign is part of the time: ``-00:01:54.365`` is a
    pre-roll, and clamping it to ``00:00:00.000`` both states the wrong instant
    and collapses the cue's span to nothing. Only the subtitle timecode, which
    has no room for a minus, is translated instead (see ``_cue_origin``).

    The time is rounded to the millisecond it prints before the sign is read, so
    the last half-millisecond before zero is zero rather than a signed
    ``-00:00:00.000`` — a sign that is not real, and one the subtitles would not
    share — while a real sub-millisecond negative keeps its sign, rounded
    (``-0.0006`` reads ``-00:00:00.001``).
    """
    total = round(seconds, 3)
    sign = "-" if total < 0 else ""
    m, s = divmod(abs(total), 60.0)
    h, m = divmod(int(m), 60)
    return f"{sign}{h:02d}:{m:02d}:{s:06.3f}"


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
        _report_written(w, on_event, "md", p, progress.advance(source="md"))
    if "srt" in wanted:
        p = w.export_file("record.srt")
        p.write_text(_render_srt(record), encoding="utf-8")
        written["srt"] = p
        _report_written(w, on_event, "srt", p, progress.advance(source="srt"))
    if "vtt" in wanted:
        p = w.export_file("record.vtt")
        p.write_text(_render_vtt(record), encoding="utf-8")
        written["vtt"] = p
        _report_written(w, on_event, "vtt", p, progress.advance(source="vtt"))
    if "json" in wanted:
        p = w.export_file("record.json")
        write_json(p, record)
        written["json"] = p
        _report_written(w, on_event, "json", p, progress.advance(source="json"))

    return written


def _report_written(
    w: Workspace,
    sink: EventSink | None,
    fmt: str,
    path: Path,
    report: JobEvent,
) -> None:
    """Report one written artifact, as the export pass's own line.

    Each format is its own line, reported where that file lands: the export pass
    is a sequence of writes, and a reader watching a long one sees each format
    as it is written rather than the set at the end. The format travels as the
    line's ``source`` — the value the advance that closed it already carries, so
    the row and the report beside it name the same thing.
    """
    report_line(
        w,
        sink,
        Step.EXPORT.value,
        f"[export] {fmt:4s} -> {path}",
        source=fmt,
        report=report,
    )


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


def _cue_origin(record: RecordDocument) -> float:
    """The time the subtitle timeline starts at: zero, or the pre-roll.

    A subtitle timecode is unsigned — ``HH:MM:SS,mmm`` has no place for a minus
    — so a cue before zero is neither rendered where it is nor clamped where it
    does not fit: the renderers translate the whole timeline by this origin,
    which keeps every cue's length and the distance between cues. Zero unless
    the first cue *is* a pre-roll, so a record that already starts at or after
    zero is exported at the times it holds. ``format_timestamp`` and
    ``record.json`` keep the reference clock, where the pre-roll is visible as
    what it is.
    """
    earliest = min((seg.start for seg in record.segments), default=0.0)
    return min(earliest, 0.0)


def _srt_tc(seconds: float) -> str:
    """``HH:MM:SS,mmm`` — an unsigned timecode, so a time on the *export* timeline.

    A negative time has no representation here and is refused rather than folded
    onto ``00:00:00,000``: folding a pre-roll that way is what made its cues
    zero-length. The renderers translate first (see ``_cue_origin``).

    The time is rounded to the millisecond it prints, the rule
    `format_timestamp` follows, so the two surfaces never disagree in the last
    half-millisecond before zero — and a cue a hair short of a minute reads
    ``00:01:00,000`` rather than ``00:00:60,000``, which is no timecode at all.
    """
    total = round(seconds, 3)
    if total < 0.0:
        raise ValueError(f"a subtitle timecode is unsigned, got {seconds}")
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def _render_srt(record: RecordDocument) -> str:
    origin = _cue_origin(record)
    blocks = []
    for i, seg in enumerate(record.segments, start=1):
        start, end = _srt_tc(seg.start - origin), _srt_tc(seg.end - origin)
        blocks.append(f"{i}\n{start} --> {end}\n{seg.text}\n")
    return "\n".join(blocks)


def _render_vtt(record: RecordDocument) -> str:
    origin = _cue_origin(record)
    lines = ["WEBVTT", ""]
    for seg in record.segments:
        start = _srt_tc(seg.start - origin).replace(",", ".")
        end = _srt_tc(seg.end - origin).replace(",", ".")
        lines.append(f"{start} --> {end}")
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
        # rides with the transcribe report, and its line goes on the run's own
        # channel like every other line.
        attribution = attribute(
            directory,
            mixed_source=options.mixed_source,
            window_s=options.window_s,
            on_event=on_event,
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
# A runner returns what its stage produced, so a caller that wants the typed
# result calls the stage itself; ``run`` drives them for their side effects —
# the workspace's files and the words they report on the sink.
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
) -> None:
    """Run every stage the spec declares, in the spec's order.

    The order is not restated here: it is read from
    :func:`clear_record.core.pipeline_spec`, the same spec the CLI builds its
    subcommands from, and each stage's wiring lives in :data:`_STAGE_RUNNERS`.
    ``on_event`` is threaded to every stage, so a caller that wants the run's
    words and progress attaches one sink and reads them off it; a stage's typed
    result is for a caller that calls that stage itself.
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
            _STAGE_RUNNERS[stage.step](directory, options, on_event)
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
