"""The boundary shapes: the domain values as declared models (ADR-0030).

The web JSON API and the MCP tool surface both hand the registry's values out as
data, and both used to do it by convention: ``dataclasses.asdict`` on the way
out, with the response annotation a bare ``dict``. Nothing published the shape,
and nothing checked it at the edge.

This module is that vocabulary, declared once for both edges. A shape that *is* a
domain value is **derived** from it by :func:`out_model` — the fields and their
types come from the frozen dataclass, and every one of them is required, since a
published value carries them all whatever their defaults are on the way in — so
the published shape cannot drift from the value it describes and adding a field
to the domain type adds it to every boundary that carries it. A shape a *function*
computes (the run-state summary, a draft view, the agent-task surface) is declared
where that function builds it, so it too has one home.

What is deliberately still a plain ``dict``: the JSON-shaped payloads whose
schema is owned elsewhere — a run's ``options``/``progress`` meta, a draft's
``value`` and ``promotion`` record — and the template contexts the HTML routes
render. Neither is a request or a response of the machine boundaries.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Self, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

from clear_record.core.events import JobEvent
from clear_record.service.agent import Provenance
from clear_record.service.models import (
    Archive,
    Artifact,
    GlossaryTerm,
    Meeting,
    PipelineRun,
    Project,
    RecordingSet,
    Tape,
)
from clear_record.service.transcript import TranscriptSlice


class Shape(BaseModel):
    """A boundary shape declared by hand rather than derived from one value.

    ``extra="forbid"`` is the point of the base: a model that quietly ignored
    what a handler added would be the same convention this vocabulary removes.
    ``from_attributes`` is what lets a derived model be filled from the frozen
    dataclass it describes, in one move and with no intermediate dict.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    @classmethod
    def of(cls, value: object, /, **extra: Any) -> Self:
        """This shape for ``value``, plus the fields the boundary adds to it.

        ``value`` is the domain value the shape describes; its declared fields are
        read off it and ``extra`` supplies the ones the boundary adds (a term
        count, a live progress summary). The result is validated like any other —
        a boundary that forgot one of its own fields fails here, not in a client.
        """
        fields = {
            name: getattr(value, name) for name in cls.model_fields if name not in extra
        }
        return cls.model_validate(fields | extra)


def out_model(cls: type, /, **extra: Any) -> type[BaseModel]:
    """A response model for the value ``cls``, derived from it.

    ``extra`` declares the fields a boundary carries *beside* the value — a term
    count, the meeting's tape paths — and they are required like the value's own:
    the model is the shape the edge publishes, and a caller fills all of it. The
    model is named ``<cls>Out``, which is also what the edge publishes.
    """
    hints = get_type_hints(cls)
    fields: dict[str, tuple[Any, Any]] = {
        field.name: (hints[field.name], ...) for field in dataclasses.fields(cls)
    }
    fields.update({name: (type_, ...) for name, type_ in extra.items()})
    return create_model(f"{cls.__name__}Out", __base__=Shape, **fields)


# --- the registry's values, as the boundaries publish them ------------------ #
#
# One model per domain value the web API or the MCP tools return, plus the two
# shapes a boundary adds to: a project with its term count (both edges count it),
# and a meeting with its latest tape set (MCP's read; the tape set itself is a
# value the web API returns in full).
ProjectOut = out_model(Project)
ProjectCountOut = out_model(Project, term_count=int)
TermOut = out_model(GlossaryTerm)
MeetingOut = out_model(Meeting)
MeetingTapesOut = out_model(Meeting, tapes=list[str])
TapeOut = out_model(Tape)
TapeSetOut = out_model(RecordingSet)
ArchiveOut = out_model(Archive)
ArtifactOut = out_model(Artifact)
RunOut = out_model(PipelineRun)
EventOut = out_model(JobEvent)
ProvenanceOut = out_model(Provenance)
TranscriptOut = out_model(TranscriptSlice)

__all__ = [
    "ArchiveOut",
    "ArtifactOut",
    "EventOut",
    "MeetingOut",
    "MeetingTapesOut",
    "ProjectCountOut",
    "ProjectOut",
    "ProvenanceOut",
    "RunOut",
    "Shape",
    "TapeOut",
    "TapeSetOut",
    "TermOut",
    "TranscriptOut",
    "out_model",
]
