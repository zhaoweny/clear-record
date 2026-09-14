"""Domain values of the clear-record registry.

Plain frozen dataclasses (no third-party imports) so the service layer stays as
light as the core. ``clear_record.core`` owns the *audio* domain (sources,
segments, records); this module owns the *project* domain (projects and glossary
terms), which only exists for the web/service surface.
"""

from __future__ import annotations

import dataclasses

#: A glossary term's lifecycle. ``candidate`` is what an agent's draft produces;
#: ``confirmed`` is owner-accepted truth; ``retired`` is kept for history.
TERM_STATUSES = ("candidate", "confirmed", "retired")

#: Who a term came from. A human edit and an agent draft are never conflated.
TERM_AUTHORS = ("human", "agent")


@dataclasses.dataclass(frozen=True)
class Project:
    """A durable container for meetings, a glossary and archives."""

    id: int
    slug: str
    name: str
    notes: str
    default_archive_root: str | None
    created_at: str


@dataclasses.dataclass(frozen=True)
class GlossaryTerm:
    """One term in a project's glossary.

    ``project_slug`` is joined in on reads so a cross-project table can show
    where a term belongs without a second query.
    """

    id: int
    project_id: int
    project_slug: str
    term: str
    reading: str | None
    aliases: str | None
    definition: str | None
    status: str
    added_by: str
    notes: str | None
    created_at: str


__all__ = ["GlossaryTerm", "Project", "TERM_AUTHORS", "TERM_STATUSES"]
