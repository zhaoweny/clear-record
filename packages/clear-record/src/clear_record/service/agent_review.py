"""The meeting-scoped agent-task surface: launch, review, promote.

:mod:`clear_record.service.agent` owns the runner seam and
:mod:`clear_record.service.agent_tasks` the task kinds. This module is the third
piece ADR-0018 implies but the seam alone does not provide: **what an accepted
draft of each kind actually produces**, and the meeting-scoped operations a
console or an agent actually calls.

Two halves:

- :class:`MeetingAgent` packages a meeting's three tasks from its workspace
  transcript and the project's confirmed-term glossary snapshot, launches one
  with the configured runner (or an injected one), and lists/reads the drafts
  written under ``<workspace>/agent/``.
- :func:`promote_draft` is the typed promotion behind an acceptance. A bare
  ``accept_draft`` flips a flag and changes nothing (which is exactly the gap
  this module closes); promotion makes the acceptance **mean** something per
  kind:

  - ``glossary_collection`` → the proposed terms become ``candidate`` registry
    terms (``added_by="agent"``). They do **not** become ``confirmed``: only a
    confirmed term biases the decoder (:mod:`clear_record.service.glossary`), and
    confirming is the owner's per-term act, not a bulk side effect of accepting a
    draft full of proposals.
  - ``transcript_check`` → the corrected revision is written as a **new file**
    (``revision.txt`` beside the draft) plus its change list (``changes.json``),
    and registered as a ``transcript_revision`` artifact. The reconciled
    ``record.json`` is never overwritten in place.
  - ``minutes`` → the Markdown minutes body is written beside the draft
    (``minutes.md``) and registered as the meeting's ``minutes`` artifact.

**Promotion is explicit and idempotent.** The outcome is recorded in the run's
``run.json`` under ``promotion`` and read back through
:attr:`clear_record.service.agent.Draft.promotion`; a second
:func:`promote_draft` on the same draft returns the recorded outcome without
repeating a side effect. The write helpers are independently idempotent too (a
term already in the registry is skipped, an artifact path already registered is
reused), so a promotion interrupted between its side effect and its record is
still safe to repeat.

Depends only on ``core``, ``service`` and ``cli`` (the layering DAG); nothing
here names an agent runtime or a vendor.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from clear_record.core.i18n import deferred
from clear_record.service.agent import (
    AgentConfig,
    AgentTaskError,
    Draft,
    Runner,
    default_config,
    read_draft,
    reject_draft,
    run_task,
)
from clear_record.service.agent_tasks import (
    glossary_collection_task,
    minutes_task,
    transcript_check_task,
    unknown_kind,
)
from clear_record.service.glossary import project_snapshot
from clear_record.service.models import Meeting
from clear_record.service.store import Registry
from clear_record.service.transcript import read_transcript

#: The subdirectory of a meeting workspace holding its agent-task runs. It is
#: not the pipeline's (``AUDIO_DIR``/``export``/``chunks``) and holds no audio,
#: so ``discover_audio`` never sees it.
AGENT_DIRNAME = "agent"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


class MeetingAgentError(AgentTaskError):
    """A meeting cannot launch the task, in the service's own words.

    The message is a **stable ID plus parameters** (:func:`deferred` marks the ID
    for the catalog), so a presentation boundary renders it with
    ``tr(exc.msgid, **exc.params)``. ``str(exc)`` stays the English form.
    """

    def __init__(self, msgid: str, **params: object) -> None:
        self.msgid = msgid
        self.params = params
        super().__init__(msgid.format(**params) if params else msgid)


class PromotionError(AgentTaskError):
    """A draft cannot be promoted (no workspace, or no promoter for its kind)."""


def _agent_dir(meeting: Meeting) -> Path:
    if not meeting.workspace_path:
        raise MeetingAgentError(
            deferred(
                "meeting {meeting} has no workspace; set one before running an "
                "agent task"
            ),
            meeting=meeting.slug,
        )
    return Path(meeting.workspace_path) / AGENT_DIRNAME


# --- how each kind's task is packaged --------------------------------------- #
# A uniform signature over the declared context sections, so ``MeetingAgent.task``
# is a lookup and never a fallthrough: a kind added to ``TASK_KINDS`` without a
# builder here fails loudly instead of being silently packaged as another kind.


def _glossary_collection(
    project: str, meeting: str, *, transcript: str, glossary: str, context: str
):
    return glossary_collection_task(
        project, meeting, transcript=transcript, glossary=glossary
    )


def _transcript_check(
    project: str, meeting: str, *, transcript: str, glossary: str, context: str
):
    return transcript_check_task(
        project, meeting, transcript=transcript, glossary=glossary, context=context
    )


def _minutes(
    project: str, meeting: str, *, transcript: str, glossary: str, context: str
):
    return minutes_task(
        project, meeting, transcript=transcript, glossary=glossary, context=context
    )


#: The task builder per kind — the one place a kind and its packaged sections
#: meet, mirroring :data:`PROMOTERS` for the review half.
TASKS: dict[str, Callable[..., object]] = {
    "glossary_collection": _glossary_collection,
    "transcript_check": _transcript_check,
    "minutes": _minutes,
}


class MeetingAgent:
    """The agent-task operations for one meeting.

    ``runner`` (tests, embedders) wins over ``config``, which wins over the
    process's resolved default (:func:`~clear_record.service.agent.default_config`).
    Holding the meeting and the registry here keeps every operation a one-liner at
    the call site and gives the console and the MCP adapter one shared
    implementation.
    """

    def __init__(
        self,
        registry: Registry,
        meeting: Meeting,
        *,
        runner: Runner | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        self.registry = registry
        self.meeting = meeting
        self.runner = runner
        self.config = config

    # --- reading ----------------------------------------------------------- #
    @property
    def directory(self) -> Path:
        """The meeting's agent-run directory (``<workspace>/agent``)."""
        return _agent_dir(self.meeting)

    def configured(self) -> bool:
        """Whether a runner is available for a launch, without calling one."""
        if self.runner is not None:
            return True
        config = self.config if self.config is not None else default_config()
        return bool(config.endpoint or config.commands)

    def drafts(self) -> list[Draft]:
        """Every agent draft this meeting produced, newest first.

        A run directory written by another meeting (or a corrupt one) is skipped
        rather than failing the whole listing: this is a view over files.
        """
        directory = self.directory
        if not directory.is_dir():
            return []
        found: list[Draft] = []
        for run_dir in sorted(directory.iterdir()):
            if not (run_dir / "run.json").is_file():
                continue
            try:
                draft = read_draft(run_dir)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if (
                draft.project == self.meeting.project_slug
                and draft.meeting == self.meeting.slug
            ):
                found.append(draft)
        found.sort(
            key=lambda draft: (draft.provenance.started_at, draft.provenance.run_id),
            reverse=True,
        )
        return found

    def draft(self, run_id: str) -> Draft | None:
        """The meeting's draft with this run id, or ``None``."""
        return next(
            (draft for draft in self.drafts() if draft.provenance.run_id == run_id),
            None,
        )

    def minutes_artifact(self):
        """The meeting's newest accepted minutes artifact, or ``None``."""
        return self.registry.latest_artifact(self.meeting.id, "minutes")

    # --- launching --------------------------------------------------------- #
    def task(self, kind: str):
        """Package ``kind`` from the meeting's transcript, glossary and context.

        A lookup in :data:`TASKS`, never a fallthrough: an unknown kind (including
        one a future ``TASK_KINDS`` adds without a builder) is a named error, so a
        new kind can never be silently packaged as another.
        """
        builder = TASKS.get(kind)
        if builder is None:
            problem = unknown_kind(kind)
            raise MeetingAgentError(problem.msgid, **dict(problem.params))
        transcript = self.transcript_text()
        glossary = project_snapshot(self.registry, self.meeting.project_slug).text
        return builder(
            self.meeting.project_slug,
            self.meeting.slug,
            transcript=transcript,
            glossary=glossary,
            context=self.context_text(),
        )

    def transcript_text(self) -> str:
        """The meeting's transcript text, or an actionable error naming the meeting."""
        try:
            return read_transcript(self.meeting).text
        except FileNotFoundError as exc:
            raise MeetingAgentError(
                deferred("no transcript for {project}/{meeting} yet: {detail}"),
                project=self.meeting.project_slug,
                meeting=self.meeting.slug,
                detail=str(exc),
            ) from exc

    def context_text(self) -> str:
        """The meeting/project metadata and notes, as the task's ``context``."""
        project = self.registry.get_project(self.meeting.project_slug)
        lines = [
            f"project: {self.meeting.project_slug}"
            + (f" ({project.name})" if project is not None else ""),
            f"meeting: {self.meeting.slug} ({self.meeting.title})",
        ]
        if self.meeting.recorded_at:
            lines.append(f"recorded_at: {self.meeting.recorded_at}")
        if project is not None and project.notes.strip():
            lines.append(f"project notes: {project.notes.strip()}")
        if self.meeting.notes.strip():
            lines.append(f"meeting notes: {self.meeting.notes.strip()}")
        return "\n".join(lines)

    def launch(self, kind: str) -> Draft:
        """Run ``kind`` and return its draft (nothing is promoted here)."""
        return run_task(
            self.task(kind),
            self.directory,
            runner=self.runner,
            config=self.config,
        )

    # --- reviewing --------------------------------------------------------- #
    def promote(self, draft: Draft) -> Draft:
        """Accept ``draft`` and produce what its kind's acceptance means."""
        return promote_draft(draft, registry=self.registry, meeting=self.meeting)

    def reject(self, draft: Draft) -> Draft:
        """Reject ``draft``, keeping it and its provenance on disk."""
        return reject_draft(draft)


def describe_draft(draft: Draft) -> dict:
    """A draft as plain JSON data (the console's and MCP's shared view).

    Provenance and value are included whole: a reviewer needs the runner, model
    and hashes that justify a draft as much as its content, and a machine caller
    needs the structured value, not a rendering of it.
    """
    return {
        "run_id": draft.provenance.run_id,
        "kind": draft.kind,
        "review_state": draft.review_state,
        "accepted": draft.accepted,
        "rejected": draft.rejected,
        "promotion": draft.promotion,
        "provenance": dataclasses.asdict(draft.provenance),
        "value": draft.value,
        "run_dir": str(draft.run_dir),
    }


# --- promotion: what an acceptance produces --------------------------------- #


def _register_file(
    registry: Registry,
    meeting: Meeting,
    path: Path,
    *,
    kind: str,
):
    """Record ``path`` as a final agent artifact; idempotent by (kind, path)."""
    for existing in registry.list_artifacts(meeting.id):
        if existing.kind == kind and existing.path == str(path):
            return existing
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return registry.add_artifact(
        meeting.id,
        kind=kind,
        path=str(path),
        sha256=digest,
        bytes=path.stat().st_size,
        produced_by="agent",
        review_state="final",
    )


def _promote_glossary(
    draft: Draft,
    *,
    registry: Registry,
    meeting: Meeting,
) -> dict:
    """The proposed terms become **candidate** registry terms (never confirmed)."""
    value = draft.value if isinstance(draft.value, dict) else {}
    proposed = value.get("terms") or []
    existing = {term.term for term in registry.list_terms(meeting.project_slug)}
    note = f"agent draft {draft.provenance.run_id}"
    added: list[str] = []
    skipped: list[str] = []
    for item in proposed:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", "")).strip()
        if not term or term in existing:
            if term:
                skipped.append(term)
            continue
        aliases = item.get("aliases") or []
        registry.add_term(
            meeting.project_slug,
            term,
            reading=item.get("reading"),
            aliases=", ".join(str(alias) for alias in aliases) or None,
            definition=item.get("definition"),
            status="candidate",
            added_by="agent",
            notes=note,
        )
        existing.add(term)
        added.append(term)
    return {"added": added, "skipped": skipped, "status": "candidate"}


def _promote_transcript_check(
    draft: Draft,
    *,
    registry: Registry,
    meeting: Meeting,
) -> dict:
    """Write the corrected revision as a **new** file plus its change list."""
    value = draft.value if isinstance(draft.value, dict) else {}
    revision_path = draft.run_dir / "revision.txt"
    revision_path.write_text(str(value.get("revision", "")) + "\n", encoding="utf-8")
    changes = value.get("changes") or []
    changes_path = draft.run_dir / "changes.json"
    changes_path.write_text(
        json.dumps(
            {
                "changes": changes,
                "provenance": dataclasses.asdict(draft.provenance),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = _register_file(
        registry, meeting, revision_path, kind="transcript_revision"
    )
    return {
        "revision_path": str(revision_path),
        "changes_path": str(changes_path),
        "changes": len(changes),
        "artifact_id": artifact.id,
    }


def _promote_minutes(
    draft: Draft,
    *,
    registry: Registry,
    meeting: Meeting,
) -> dict:
    """Write the Markdown minutes and register them as the meeting's artifact."""
    value = draft.value if isinstance(draft.value, dict) else {}
    path = draft.run_dir / "minutes.md"
    path.write_text(str(value.get("body", "")) + "\n", encoding="utf-8")
    artifact = _register_file(registry, meeting, path, kind="minutes")
    return {
        "path": str(path),
        "artifact_id": artifact.id,
        "decisions": len(value.get("decisions") or []),
        "actions": len(value.get("actions") or []),
    }


#: What an acceptance produces, per kind. Adding a kind means adding a promoter
#: here (and a test), not editing the seam.
PROMOTERS: dict[str, Callable[..., dict]] = {
    "glossary_collection": _promote_glossary,
    "transcript_check": _promote_transcript_check,
    "minutes": _promote_minutes,
}


def promote_draft(
    draft: Draft,
    *,
    registry: Registry,
    meeting: Meeting,
    clock: Callable[[], str] = _now,
) -> Draft:
    """Accept ``draft`` and promote it into what its kind produces.

    Idempotent: a draft whose ``run.json`` already carries a ``promotion`` record
    is returned as-is, with no second side effect. The flag flip and the record
    are one write, so the accepted state and its outcome never disagree.
    """
    if draft.promotion is not None:
        return draft
    promoter = PROMOTERS.get(draft.kind)
    if promoter is None:  # pragma: no cover - TASK_KINDS and PROMOTERS agree
        raise PromotionError(
            deferred("no promotion is defined for task kind {kind}"), kind=draft.kind
        )
    _agent_dir(meeting)  # refuses a meeting with no workspace
    summary = promoter(draft, registry=registry, meeting=meeting)
    promotion = {"kind": draft.kind, "at": clock(), "summary": summary}
    document = json.loads(draft.provenance_path.read_text(encoding="utf-8"))
    document["review_state"] = "accepted"
    document["promotion"] = promotion
    draft.provenance_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return dataclasses.replace(draft, review_state="accepted", promotion=promotion)


__all__ = [
    "AGENT_DIRNAME",
    "MeetingAgent",
    "MeetingAgentError",
    "PROMOTERS",
    "PromotionError",
    "TASKS",
    "describe_draft",
    "promote_draft",
]
