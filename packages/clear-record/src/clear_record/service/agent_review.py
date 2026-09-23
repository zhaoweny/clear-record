"""The meeting-scoped draft surface: what a harness writes, and what a human accepts.

:mod:`clear_record.service.agent_drafts` owns the store — a version chain with
author provenance. This module is the second half: **what accepting a version of
each kind actually produces**, and the meeting-scoped operations a console or an
MCP client actually calls.

Two halves:

- :class:`MeetingAgent` reads a meeting's chains under ``<workspace>/agent/``,
  opens one with what a harness wrote, and decides a version on the human's
  behalf.
- :func:`promote_draft` is the typed promotion behind an acceptance, and it is
  what makes an acceptance **mean** something per kind:

  - ``glossary_collection`` → the proposed terms become ``candidate`` registry
    terms (``added_by="agent"``). They do **not** become ``confirmed``: only a
    confirmed term biases the decoder (:mod:`clear_record.service.glossary`), and
    confirming is the owner's per-term act, not a bulk side effect of accepting a
    draft full of proposals.
  - ``transcript_check`` → the corrected revision is written as a **new file**
    (``revision.txt`` beside the chain) plus its change list (``changes.json``),
    and registered as a ``transcript_revision`` artifact. The reconciled
    ``record.json`` is never overwritten in place.
  - ``minutes`` → the Markdown minutes body is written beside the chain
    (``minutes.md``) and registered as the meeting's ``minutes`` artifact.

**Promotion is explicit and idempotent.** The decision and its outcome are one
write into the chain's newest version, and a version that already carries a
decision is never decided again — so a repeated accept cannot run a side effect
twice. The write helpers are independently idempotent too (a term already in the
registry is skipped, an artifact path already registered is reused), so a
promotion interrupted between its side effect and its record is still safe to
repeat.

Depends only on ``core`` and ``service`` (the layering DAG); nothing here names a
model, an endpoint or a runtime.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clear_record.core.i18n import deferred
from clear_record.service.agent_drafts import (
    DRAFT_FILENAME,
    Draft,
    StaleVersion,
    TASK_KINDS,
    append_version,
    list_drafts,
    record_review,
    require_shape,
    start_draft,
)
from clear_record.service.models import Meeting
from clear_record.service.schemas import ArtifactOut, ProvenanceOut, Shape
from clear_record.service.store import Registry

#: The subdirectory of a meeting workspace holding its agent drafts. It is not
#: the pipeline's (``AUDIO_DIR``/``export``/``chunks``) and holds no audio, so
#: ``discover_audio`` never sees it.
AGENT_DIRNAME = "agent"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


class MeetingAgentError(RuntimeError):
    """A meeting cannot open a draft, in the service's own words.

    The message is a **stable ID plus parameters** (:func:`deferred` marks the ID
    for the catalog), so a presentation boundary renders it with
    ``tr(exc.msgid, **exc.params)``. ``str(exc)`` stays the English form.
    """

    def __init__(self, msgid: str, **params: object) -> None:
        self.msgid = msgid
        self.params = params
        super().__init__(msgid.format(**params) if params else msgid)


class PromotionError(MeetingAgentError):
    """A draft cannot be promoted (no workspace, or a value its kind cannot use)."""


def _stale(exc: StaleVersion) -> MeetingAgentError:
    """A decision that named a version the harness has since superseded."""
    return MeetingAgentError(
        deferred(
            "the version you read ({version}) is not the newest one ({newest}), so "
            "the decision was not recorded"
        ),
        version=exc.version,
        newest=exc.newest,
    )


def _agent_dir(meeting: Meeting) -> Path:
    if not meeting.workspace_path:
        raise MeetingAgentError(
            deferred(
                "meeting {meeting} has no workspace; set one before writing an "
                "agent draft"
            ),
            meeting=meeting.slug,
        )
    return Path(meeting.workspace_path) / AGENT_DIRNAME


class MeetingAgent:
    """The draft operations for one meeting.

    Holding the meeting and the registry here keeps every operation a one-liner at
    the call site and gives the console and the MCP adapter one shared
    implementation.
    """

    def __init__(self, registry: Registry, meeting: Meeting) -> None:
        self.registry = registry
        self.meeting = meeting

    # --- reading ----------------------------------------------------------- #
    @property
    def directory(self) -> Path:
        """The meeting's draft directory (``<workspace>/agent``)."""
        return _agent_dir(self.meeting)

    def drafts(self) -> list[Draft]:
        """Every draft chain this meeting owns, newest version first.

        A directory written by another meeting (or a corrupt one) is skipped
        rather than failing the whole listing: this is a view over files.
        """
        return [
            draft
            for draft in list_drafts(self.directory)
            if draft.project == self.meeting.project_slug
            and draft.meeting == self.meeting.slug
        ]

    def draft(self, draft_id: str) -> Draft | None:
        """The meeting's chain with this id, or ``None``."""
        return next(
            (draft for draft in self.drafts() if draft.draft_id == draft_id), None
        )

    def minutes_artifact(self):
        """The meeting's newest accepted minutes artifact, or ``None``."""
        return self.registry.latest_artifact(self.meeting.id, "minutes")

    # --- writing ----------------------------------------------------------- #
    def write(
        self,
        kind: str,
        value: Any,
        *,
        author: str,
        draft_id: str | None = None,
        clock: Callable[[], str] = _now,
    ) -> Draft:
        """Record what a harness (or a human) produced for ``kind``.

        ``value`` is stored as it came — the app did not produce it and does not
        second-guess it. ``draft_id`` appends to an existing chain; without it a
        new chain is opened. The author identity is the writer's own declaration
        and is recorded on the version.
        """
        directory = self.directory
        if draft_id is None:
            if kind not in TASK_KINDS:
                raise MeetingAgentError(
                    deferred("unknown draft kind {kind}; known kinds: {known}"),
                    kind=kind,
                    known=", ".join(TASK_KINDS),
                )
            problem = require_shape(kind, value)
            if problem is not None:
                raise MeetingAgentError(
                    deferred("the {kind} draft cannot be written: {detail}"),
                    kind=kind,
                    detail=problem,
                )
            return start_draft(
                directory,
                kind=kind,
                project=self.meeting.project_slug,
                meeting=self.meeting.slug,
                value=value,
                author=author,
                clock=clock,
            )
        target = self.draft(draft_id)
        if target is None:
            raise MeetingAgentError(
                deferred("no draft {draft} for {meeting}"),
                draft=draft_id,
                meeting=self.meeting.slug,
            )
        problem = require_shape(target.kind, value)
        if problem is not None:
            raise MeetingAgentError(
                deferred("the {kind} draft cannot be written: {detail}"),
                kind=target.kind,
                detail=problem,
            )
        return append_version(target.path, value=value, author=author, clock=clock)

    # --- reviewing --------------------------------------------------------- #
    def promote(
        self,
        draft: Draft,
        *,
        author: str = "human",
        version: int | None = None,
        clock: Callable[[], str] = _now,
    ) -> Draft:
        """Accept ``draft``'s ``version``; produce what its acceptance means."""
        try:
            return promote_draft(
                draft,
                registry=self.registry,
                meeting=self.meeting,
                author=author,
                version=version,
                clock=clock,
            )
        except StaleVersion as exc:
            raise _stale(exc) from exc

    def reject(
        self,
        draft: Draft,
        *,
        author: str = "human",
        version: int | None = None,
        clock: Callable[[], str] = _now,
    ) -> Draft:
        """Reject a version, keeping the whole chain on disk as history."""
        try:
            return record_review(
                draft, "rejected", author=author, version=version, clock=clock
            )
        except StaleVersion as exc:
            raise _stale(exc) from exc

    def legacy_drafts(self) -> tuple[str, ...]:
        """The 0.2 agent runs still on disk under this meeting's agent directory.

        The 0.2 in-process path wrote one directory per run
        (``<workspace>/agent/<kind>-<run_id>/``, with ``run.json``). ADR-0031's
        store does not read those and does not migrate them: they are named here
        so a surface can say **what** is not part of the chain instead of leaving
        it silently invisible.
        """
        directory = self.directory
        if not directory.is_dir():
            return ()
        return tuple(
            sorted(
                child.name
                for child in directory.iterdir()
                if (child / "run.json").is_file()
                and not (child / DRAFT_FILENAME).is_file()
            )
        )


# --- the boundary views (the console's and MCP's shared shape) -------------- #


class VersionView(Shape):
    """One link of a chain, as a reader is told about it.

    The provenance and the decision, not the value: a caller that wants the text
    a version holds reads the draft's ``value`` (the newest), and a chain's
    history is there to say **who wrote it and what was decided**, which is what
    the store records about a run it did not perform.
    """

    version: int
    author: str
    written_at: str
    decision: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    promotion: dict[str, Any] | None


class DraftView(Shape):
    """A draft chain as the console and the MCP tools both report it (ADR-0030).

    ``provenance`` and ``value`` are the newest version's — what a reviewer acts
    on — and ``versions`` is the whole chain. ``value`` and ``promotion`` stay
    JSON-shaped: their schema belongs to the kind, not to this view.
    """

    draft_id: str
    kind: str
    version: int
    review_state: str
    accepted: bool
    rejected: bool
    promotion: dict[str, Any] | None
    provenance: ProvenanceOut
    value: Any
    versions: list[VersionView]
    draft_dir: str


class AgentDraftsOut(Shape):
    """A meeting's draft surface: the kinds, the chains, the minutes.

    Shared by the JSON API and the MCP tool, so the browser and an agent read one
    shape. ``minutes`` is the accepted minutes artifact, or ``None`` until one
    exists.
    """

    kinds: list[str]
    drafts: list[DraftView]
    minutes: ArtifactOut | None


def describe_draft(draft: Draft) -> DraftView:
    """A draft chain as declared data (the console's and MCP's shared view)."""
    return DraftView(
        draft_id=draft.draft_id,
        kind=draft.kind,
        version=draft.version,
        review_state=draft.review_state,
        accepted=draft.accepted,
        rejected=draft.rejected,
        promotion=draft.promotion,
        provenance=ProvenanceOut(**dataclasses.asdict(draft.provenance)),
        value=draft.value,
        versions=[
            VersionView(
                version=number,
                author=version.provenance.author,
                written_at=version.provenance.written_at,
                decision=version.decision,
                reviewed_by=version.reviewed_by,
                reviewed_at=version.reviewed_at,
                promotion=version.promotion,
            )
            for number, version in enumerate(draft.versions, start=1)
        ],
        draft_dir=str(draft.run_dir),
    )


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
    note = f"agent draft {draft.draft_id} by {draft.provenance.author}"
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
                "author": draft.provenance.author,
                "written_at": draft.provenance.written_at,
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
#: here (and a test), not editing the store.
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
    author: str = "human",
    version: int | None = None,
    clock: Callable[[], str] = _now,
) -> Draft:
    """Accept ``draft``'s ``version`` and promote it into what its kind produces.

    ``version`` names the version the human read; a draft whose harness has since
    appended another one is refused (:class:`~clear_record.service.StaleVersion`)
    **before** any side effect runs, so a promotion can never apply text nobody
    reviewed.

    Idempotent: a version that already carries a decision is returned as-is, with
    no second side effect. The decision and the promotion's outcome are one
    write, so the accepted state and what it produced never disagree.
    """
    if version is not None and version != draft.version:
        raise StaleVersion(version=version, newest=draft.version)
    if not draft.current.pending:
        return draft
    problem = require_shape(draft.kind, draft.value)
    if problem is not None:
        # A chain written before this check existed, or by hand: promoting it
        # would register an artifact that is not the thing its kind promises.
        raise PromotionError(
            deferred("the {kind} draft cannot be accepted: {detail}"),
            kind=draft.kind,
            detail=problem,
        )
    promoter = PROMOTERS.get(draft.kind)
    if promoter is None:  # pragma: no cover - TASK_KINDS and PROMOTERS agree
        raise PromotionError(
            deferred("no promotion is defined for draft kind {kind}"), kind=draft.kind
        )
    _agent_dir(meeting)  # refuses a meeting with no workspace
    summary = promoter(draft, registry=registry, meeting=meeting)
    promotion = {"kind": draft.kind, "at": clock(), "summary": summary}
    return record_review(
        draft,
        "accepted",
        author=author,
        promotion=promotion,
        version=version,
        clock=clock,
    )


__all__ = [
    "AGENT_DIRNAME",
    "AgentDraftsOut",
    "DraftView",
    "MeetingAgent",
    "MeetingAgentError",
    "PROMOTERS",
    "PromotionError",
    "TASK_KINDS",
    "VersionView",
    "describe_draft",
    "promote_draft",
]
