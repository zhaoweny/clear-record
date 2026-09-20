"""Find the thing a request named — or answer that it is not here.

The console is asked for a project, a meeting, a tape, a run, a term, an archive,
an agent draft, an agent-task kind or one of its own settings sections — by a
page, by a fragment or by the JSON API — and the answer to *"and if it is not
there?"* is one rule. That rule lives here, once: a finder returns the value, or
raises :class:`NotFound`.

The **two behaviours** the surfaces need are preserved exactly, and they are a
difference in *answering*, never in *finding*:

* a **page** re-renders with a message of its own — the section's nav, a sentence
  in the reader's locale — so a page route catches ``NotFound`` where it renders
  (``web/app.py``). The one *fragment* that answers its own miss is the draft
  review (``web/app.py``): a draft that is not there re-renders the meeting it
  belongs to, which is the fragment's own answer and not a 404;
* a **machine surface** raises the status it must: the JSON API — and every
  fragment route that does not answer its own miss — answer ``{"detail": …}``,
  which is what the app's one ``NotFound`` handler composes out of
  :attr:`NotFound.detail`: the sentence this module composed, English by rule
  (``docs/i18n.md``).

``detail`` is therefore **never** a message ID: it is the machine's sentence, the
same text those routes raised by hand before this module existed. A page's
message stays at the page route that shows it, where the section's chrome is
known; nothing here decides how a miss is presented. A miss **no machine surface
can ask for** carries no ``detail`` at all (:func:`settings_section`).
"""

from __future__ import annotations

from collections.abc import Sequence

from clear_record.service import (
    Archive,
    Draft,
    GlossaryTerm,
    Meeting,
    MeetingAgent,
    PipelineRun,
    Project,
    Registry,
    RunManager,
    RunState,
    Tape,
    TASK_KINDS,
)


class NotFound(LookupError):
    """A named thing this console was asked for and does not have.

    One exception for every kind, so a caller that answers a miss itself (a page
    route, and the draft review's fragment) catches one class rather than a
    per-kind zoo, and a caller that does not (the JSON API, and a fragment that
    does not answer its own miss) is answered once by the app's handler.
    ``detail`` is the machine-facing sentence — English, never translated — and
    is also ``str(exc)``.

    A miss **no machine surface can ask for** carries none: the console's own
    Settings sections are named only by a page URL, so :func:`settings_section`
    raises a sentence-less ``NotFound`` and the page composes the translated
    message it shows. A machine sentence for that kind would be prose no client
    could ever read.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.detail = detail


def project(registry: Registry, slug: str) -> Project:
    """The project ``slug`` names."""
    found = registry.get_project(slug)
    if found is None:
        raise NotFound(f"no project {slug!r}")
    return found


def meeting(registry: Registry, meeting_id: int) -> Meeting:
    """The meeting with this id."""
    found = registry.meeting_by_id(meeting_id)
    if found is None:
        raise NotFound(f"no meeting {meeting_id}")
    return found


def meeting_in(registry: Registry, slug: str, meeting_slug: str) -> Meeting:
    """The meeting ``meeting_slug`` names inside project ``slug`` — the page pair."""
    found = registry.get_meeting(slug, meeting_slug)
    if found is None:
        raise NotFound(f"no meeting {meeting_slug!r} in {slug!r}")
    return found


def term(registry: Registry, term_id: int) -> GlossaryTerm:
    """The glossary term with this id."""
    found = registry.get_term(term_id)
    if found is None:
        raise NotFound(f"no term {term_id}")
    return found


def run(registry: Registry, run_id: int) -> PipelineRun:
    """The run row with this id."""
    found = registry.get_run(run_id)
    if found is None:
        raise NotFound(f"no run {run_id}")
    return found


def run_state(runs: RunManager, run_id: int) -> RunState:
    """The run's live state, read from the registry the manager drains."""
    found = runs.state(run_id)
    if found is None:
        raise NotFound(f"no run {run_id}")
    return found


def archive(registry: Registry, archive_id: int) -> Archive:
    """The archive row with this id."""
    found = registry.get_archive(archive_id)
    if found is None:
        raise NotFound(f"no archive {archive_id}")
    return found


def tape(registry: Registry, meeting: Meeting, tape_id: int) -> Tape:
    """The meeting's tape with this id.

    A tape of *another* meeting is not this meeting's tape: the row's own owner
    is what makes it found, exactly as the delete's guard reads it.
    """
    found = registry.get_tape(tape_id)
    if found is None or found.meeting_id != meeting.id:
        raise NotFound(f"no tape {tape_id} for meeting {meeting.id}")
    return found


def draft(agent: MeetingAgent, run_id: str) -> Draft:
    """The agent draft ``run_id`` names for the agent's own meeting."""
    found = agent.draft(run_id)
    if found is None:
        raise NotFound(f"no draft {run_id!r} for meeting {agent.meeting.id}")
    return found


def task_kind(kind: str) -> str:
    """``kind`` if the service declares it as an agent task."""
    if kind not in TASK_KINDS:
        raise NotFound(f"no agent task {kind!r}")
    return kind


def settings_section(
    entries: Sequence[tuple[str, str, str]], slug: str
) -> tuple[str, str, str]:
    """The ``(slug, label, template)`` entry of ``entries`` that names ``slug``.

    The console's own section table is its chrome — the slugs, the ``deferred``
    labels and the partials each section includes — so the table lives in
    ``web/views.py`` and the caller supplies it; the **rule** is this module's.
    Its miss is the one no machine surface can ask for (a settings section has a
    page URL and no API route), so it carries no sentence.
    """
    entry = next((entry for entry in entries if entry[0] == slug), None)
    if entry is None:
        raise NotFound()
    return entry


__all__ = [
    "NotFound",
    "archive",
    "draft",
    "meeting",
    "meeting_in",
    "project",
    "run",
    "run_state",
    "settings_section",
    "tape",
    "task_kind",
    "term",
]
