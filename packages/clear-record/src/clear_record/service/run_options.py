"""A stored run's options, as a declared model (ADR-0030).

``pipeline_run.run_options`` holds the :class:`~clear_record.core.PipelineOptions`
a queued run will execute with — ``dataclasses.asdict`` of the resolved options,
written when the run is enqueued so a restart can pick it up. The column is JSON
text, and nothing about the schema keeps it honest: the read seam is where a row
that no longer fits the options used to be *reduced* — the keys the options no
longer had were dropped, and the ones missing quietly became built-in defaults —
so a run could execute with a configuration nobody chose.

This module is that seam's model. It lives in ``service`` because validation is
a third-party concern (``core`` imports no third-party package, by rule and by
guard) and the service layer is where a stored row crosses into a run: the
registry validates the row as it reads it, so a caller gets a row this build can
run, or a :class:`MalformedRunOptions` naming the run and its failing fields —
and a read that walks rows stops at the first row it refuses.

**The field set is derived, not restated.** The names are the run-knob
declaration's (``core.options.RESOLVABLE_FIELDS``, one entry per row of
``RUN_KNOBS``) plus the option fields no knob row names, and every type is the
value type's own annotation — the declaration
:func:`~clear_record.core.resolve_options` fills and the CLI spells. A knob added
to the declaration is a field of this model without a second edit here; the field
list is computed, so there is no copy of it to keep in step.

**A row an earlier release wrote still reads.** A stored row outlives the build
that wrote it, and the released line (``public/releases/v0.2.x``,
``public/main``) carried a ``formats`` field its rows still hold: it chose which
artifacts the export stage wrote, and this build's export writes the declared
four (``md``, ``srt``, ``vtt``, ``json``) unconditionally — so the field was
**removed with no successor**, and the read drops it and warns once, naming the
run and the key, while the run reads on. The keys that were an earlier release's
are declared in ``core.options.SUPERSEDED_KEYS`` beside the run knobs, and the
tolerance is the reader's, not the model's: the settle step runs before the model
sees the row, so the row shape stays exactly the options a run executes with, and
``create_run`` writes current keys only.

**Everything else is refused**, which is the distinction that matters: a missing
field, a value the field's type cannot read (``jobs: "30"``, ``resume: "yes"``),
and a key *no* build of this application ever wrote. A row is read **strictly**,
so a string is not quietly accepted where the options declare a number or a
boolean — only JSON's own ``30``-for-a-``float`` is, which is the same number
written another way.
"""

from __future__ import annotations

import dataclasses
import json
import warnings
from typing import Any, get_type_hints

from pydantic import (
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
    create_model,
)

from clear_record.core.options import (
    RESOLVABLE_FIELDS,
    SUPERSEDED_KEYS,
    PipelineOptions,
)


class MalformedRunOptions(ValueError):
    """A stored run-options row that no longer fits the options of a run.

    Raised where the row is read, naming the run and every field that failed —
    the registry that has drifted from this build says so at the seam instead of
    the run executing with a reduced set of options. A :class:`ValueError`, like
    the other refusals a bad value earns in this layer.

    ``run_id`` and ``meeting_id`` identify the row the refusal is about, so a
    reader that holds only the exception can act on that one run without reading
    the row again — which is the one thing it cannot do (the queue does exactly
    that; see ``RunManager._drain``).
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: int | None = None,
        meeting_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.meeting_id = meeting_id


class SupersededRunOptions(UserWarning):
    """A stored row carried a key a released build wrote and this build does not.

    Warned once per read that dropped one, naming the run and the key: the run
    still runs, but it runs without what that key asked for.
    """


def row_fields() -> dict[str, tuple[Any, Any]]:
    """The stored row's fields, as ``create_model`` wants them.

    The declaration names the knobs; the value type supplies every field's type
    (a declaration row carries the flag, the ``CR_*`` name, the converter and the
    default — not a Python type — so the annotation is the one place a knob's
    type exists). The rest are the option fields no knob row names: several of
    them are flags of their own, so what they lack is a row in the declaration,
    and the value type's annotation is likewise the only place their type is
    written.

    A key an earlier release wrote is *not* one of these fields: the read settles
    those before the model sees the row (see :func:`read_run_options`), so the
    shape here is exactly the options a run executes with and a key nobody
    declares stays refused.

    Both sources are read here, by name, rather than captured as a default
    argument: the field list is computed at every call from the declarations, so
    growing a knob is growing them and nothing here.
    """
    hints = get_type_hints(PipelineOptions)
    knobs = set(RESOLVABLE_FIELDS)
    names = (
        *RESOLVABLE_FIELDS,
        *(f.name for f in dataclasses.fields(PipelineOptions) if f.name not in knobs),
    )
    return {name: (hints[name], ...) for name in names}


def row_model() -> type[BaseModel]:
    """The stored row as a declared model, its fields derived (see :func:`row_fields`)."""
    return create_model(
        "RunOptionsRow",
        __config__=ConfigDict(extra="forbid"),
        **row_fields(),
    )


#: The model a stored row is validated by.
RunOptionsRow = row_model()

#: The one adapter the read seam uses (ADR-0030's ``TypeAdapter``).
RUN_OPTIONS_ROW: TypeAdapter[BaseModel] = TypeAdapter(RunOptionsRow)


def _detail(exc: ValidationError) -> str:
    """Every field the row failed on, as ``field: why``, joined into one line."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<row>'}: {error['msg']}"
        for error in exc.errors()
    )


def _settle_superseded(raw: dict[str, Any]) -> list[str]:
    """Take the keys an earlier release wrote out of a just-parsed row.

    A renamed key hands its value to its successor — the release that wrote the
    old name wrote no new name, so the value is the only one the row has — and a
    key with no successor is dropped. Returns the keys dropped, for the one
    warning the read raises.
    """
    dropped: list[str] = []
    for row in SUPERSEDED_KEYS:
        if row.stored not in raw:
            continue
        value = raw.pop(row.stored)
        if row.successor is None:
            dropped.append(row.stored)
        elif value is not None:
            raw.setdefault(row.successor, value)
    return dropped


def read_run_options(
    run_id: int,
    stored: str | None,
    *,
    meeting_id: int | None = None,
) -> dict[str, Any] | None:
    """One stored options row as a plain dict, or ``None`` when the run has none.

    ``stored`` is the column's text, read strictly. Anything that is not the
    whole :class:`~clear_record.core.PipelineOptions` shape — text that is not
    JSON, a field missing, a value the field's type cannot read, a key no build
    of this application ever wrote — raises :class:`MalformedRunOptions`, naming
    the run and each failing field. A key an *earlier* release wrote is settled
    instead (see :data:`~clear_record.core.SUPERSEDED_KEYS`): mapped onto its
    successor, or dropped with one warning.

    The result is the validated model's Python-mode dump, so the value type's own
    shapes survive the round trip (``audio_files`` comes back a tuple, as
    :class:`PipelineOptions` declares it) and the caller can rebuild the options
    without a second pass of coercion.
    """
    if not stored:
        return None
    try:
        raw = json.loads(stored)
    except json.JSONDecodeError as exc:
        raise MalformedRunOptions(
            f"run {run_id} carries options that are not JSON: {exc}",
            run_id=run_id,
            meeting_id=meeting_id,
        ) from exc
    dropped = _settle_superseded(raw)
    try:
        # The settled row is validated as *JSON text* rather than as the parsed
        # dict: strict mode reads a JSON array as the tuple the options declare,
        # while a dict input would have to be a tuple already — the one place
        # where "the row came from a JSON column" has to be said out loud.
        row = RUN_OPTIONS_ROW.validate_json(json.dumps(raw), strict=True)
    except ValidationError as exc:
        raise MalformedRunOptions(
            f"run {run_id} carries malformed options: {_detail(exc)}",
            run_id=run_id,
            meeting_id=meeting_id,
        ) from exc
    if dropped:
        # One warning per read, however many keys it dropped — the run is named
        # once and every key it lost is named with it.
        warnings.warn(
            f"run {run_id} was queued by an earlier release and carries "
            f"{', '.join(sorted(dropped))}, which this build does not have; the "
            f"run reads on without it",
            SupersededRunOptions,
            stacklevel=3,
        )
    return dict(row.model_dump())


__all__ = [
    "MalformedRunOptions",
    "RUN_OPTIONS_ROW",
    "RunOptionsRow",
    "SupersededRunOptions",
    "read_run_options",
    "row_fields",
    "row_model",
]
