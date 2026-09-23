"""The boundary shapes: the domain values as declared models (ADR-0030).

The web JSON API and the MCP tool surface both hand the registry's values out as
data, and both used to do it by convention: ``dataclasses.asdict`` on the way
out, with the response annotation a bare ``dict``. Nothing published the shape,
and nothing checked it at the edge.

This module is that vocabulary's **shared** half: a shape that *is* a domain value
is **derived** from it here, by :func:`out_model` — the fields and their types come
from the frozen dataclass, and every one of them is required, since a published
value carries them all whatever their defaults are on the way in — so the published
shape cannot drift from the value it describes, and one value has one shape on both
edges rather than a copy per edge. The rest have a home beside what they describe,
one each: a shape a *function* computes (the run-state summary, a draft view) is
declared where that function builds it; a shape **both edges** publish while
neither builds it (the draft surface, ``AgentDraftsOut``) is declared beside
the service module that owns that surface; and a shape an *edge* declares for its
own envelope (a health answer, a page of a run's event stream) is declared beside
that edge, under its own name.

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
from clear_record.service.agent_drafts import Provenance
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


def out_model(cls: type, /, *, name: str, **extra: Any) -> type[BaseModel]:
    """A response model for the value ``cls``, derived from it and named ``name``.

    ``extra`` declares the fields a boundary carries *beside* the value — a term
    count, the meeting's tape paths — and they are required like the value's own:
    the model is the shape the edge publishes, and a caller fills all of it.

    ``name`` is taken rather than derived from ``cls``, because the model's own
    ``__name__`` is what the edge publishes it *as*: FastAPI names the OpenAPI
    component after it, and two shapes derived from one value — a project, a
    project with its term count — would arrive there under two models of one name,
    which FastAPI disambiguates by mangling the module path into the published
    name (``clear_record__service__schemas__ProjectOut__1``) with a suffix that
    depends on route order. Every derivation therefore says what it publishes,
    which is also the name the module binds it to.
    """
    hints = get_type_hints(cls)
    fields: dict[str, tuple[Any, Any]] = {
        field.name: (hints[field.name], ...) for field in dataclasses.fields(cls)
    }
    fields.update({field: (type_, ...) for field, type_ in extra.items()})
    return create_model(name, __base__=Shape, **fields)


# --- the registry's values, as the boundaries publish them ------------------ #
#
# One model per domain value the web API or the MCP tools return, plus the two
# shapes a boundary adds to: a project with its term count (both edges count it),
# and a meeting with its latest tape set (MCP's read; the tape set itself is a
# value the web API returns in full). Each one is named as it is bound here, so
# the name the edge publishes is the name a reader of this module looks for — and
# so the two shapes derived from one value stay two shapes on the wire
# (:func:`out_model` says why that has to be declared).
ProjectOut = out_model(Project, name="ProjectOut")
ProjectCountOut = out_model(Project, name="ProjectCountOut", term_count=int)
TermOut = out_model(GlossaryTerm, name="TermOut")
MeetingOut = out_model(Meeting, name="MeetingOut")
MeetingTapesOut = out_model(Meeting, name="MeetingTapesOut", tapes=list[str])
TapeOut = out_model(Tape, name="TapeOut")
TapeSetOut = out_model(RecordingSet, name="TapeSetOut")
ArchiveOut = out_model(Archive, name="ArchiveOut")
ArtifactOut = out_model(Artifact, name="ArtifactOut")
RunOut = out_model(PipelineRun, name="RunOut")
EventOut = out_model(JobEvent, name="EventOut")
ProvenanceOut = out_model(Provenance, name="ProvenanceOut")
TranscriptOut = out_model(TranscriptSlice, name="TranscriptOut")

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
