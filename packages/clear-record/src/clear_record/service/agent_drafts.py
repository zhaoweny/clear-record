"""The draft store: a **version chain with author provenance** (ADR-0031).

clear-record does not call a model (ADR-0031 supersedes ADR-0018). A harness the
user runs reads a meeting over MCP and **writes** what it produced as a draft;
this module is that store, and the only thing the app keeps of the agent
conversation.

One draft is one chain, on disk under ``<workspace>/agent/<draft_id>/draft.json``::

    {
      "draft_id": "…", "kind": "minutes", "project": "…", "meeting": "…",
      "created_at": "…",
      "versions": [
        {"author": "pi-agent", "written_at": "…", "value": {…},
         "decision": null, "reviewed_by": null, "reviewed_at": null,
         "promotion": null}
      ]
    }

Properties the design holds to:

- **Every version records who wrote it.** ``author`` is the identity the harness
  declares (an agent, or a human), and it is the only provenance this store can
  honestly claim: the app did not produce the value and never saw the model.
- **The chain is the state.** There is no separate review-state machine: a
  version is *pending* until a human records ``accepted`` or ``rejected`` on it,
  and the draft's review state is its newest version's decision. A harness that
  writes a new version therefore re-opens a decided draft — which is what makes
  tuning a conversation (write, review, write again) and not a one-shot run.
- **Accepting happens once per version, and it is recorded with who did it.**
  A review write also carries the promotion's own outcome (``promotion``), so
  the decision and what it produced can never disagree on disk.
- **A plain file, no database.** A draft can be read, copied and reviewed with no
  registry and no network, and an unreadable one is skipped by a listing rather
  than failing it.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: The three jobs a harness does for a meeting (ADR-0018's task kinds, now the
#: draft kinds). Every kind's acceptance means something specific — see
#: :data:`clear_record.service.agent_review.PROMOTERS` — so the list is closed.
TASK_KINDS: tuple[str, ...] = ("glossary_collection", "transcript_check", "minutes")

#: The decisions a human may record on one version. ``pending`` is the absence
#: of either, not a value: :data:`DRAFT_STATE` names it for a surface.
DRAFT_STATE = "draft"
REVIEW_DECISIONS: tuple[str, ...] = ("accepted", "rejected")

#: The file one chain is stored in, inside its own directory.
DRAFT_FILENAME = "draft.json"

#: What each kind's ``value`` must carry, and what type each key must be. The app
#: does **not** grade a harness's prose — it refuses a payload that cannot be the
#: thing it claims to be, which is what keeps an acceptance from registering an
#: empty minutes document as the meeting's minutes.
DRAFT_SHAPE: dict[str, dict[str, type]] = {
    "glossary_collection": {"terms": list},
    "transcript_check": {"revision": str, "changes": list},
    "minutes": {"body": str},
}

#: The list-of-objects field each kind has, and the keys every item must carry.
DRAFT_ITEM_SHAPE: dict[str, tuple[str, tuple[str, ...]]] = {
    "glossary_collection": ("terms", ("term",)),
    "transcript_check": ("changes", ("before", "after")),
}

_TYPE_NAMES: dict[type, str] = {list: "a list", str: "a string", dict: "an object"}


def require_shape(kind: str, value: object) -> str | None:
    """Why ``value`` cannot be a ``kind`` draft, or ``None`` when it can.

    The check is structural and deliberately shallow: the keys the acceptance
    reads have to be there and have to be the right kind of thing, and every item
    of a kind's list field has to carry its keys. A required string has to carry
    something too — a blank ``body`` or ``revision`` is the empty document the
    key exists to rule out, not a value the human can review. Anything else about
    the content is the harness's claim, which the human reviews.
    """
    if kind not in DRAFT_SHAPE:  # pragma: no cover - callers check the kind first
        return f"unknown draft kind {kind!r}"
    if not isinstance(value, dict):
        return f"the value must be a JSON object, not {type(value).__name__}"
    problems: list[str] = []
    for key, expected in DRAFT_SHAPE[kind].items():
        if key not in value:
            problems.append(f"{key!r} is missing")
        elif not isinstance(value[key], expected):
            problems.append(
                f"{key!r} must be {_TYPE_NAMES[expected]}, "
                f"not {type(value[key]).__name__}"
            )
        elif expected is str and not value[key].strip():
            problems.append(f"{key!r} is empty")
    field, keys = DRAFT_ITEM_SHAPE.get(kind, ("", ()))
    if field and not problems:
        items = value.get(field) or []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"{field}[{index}] must be an object")
                continue
            for key in keys:
                found = item.get(key)
                if not isinstance(found, str) or not found.strip():
                    problems.append(f"{field}[{index}].{key} is missing")
    return "; ".join(problems) if problems else None


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


class StaleVersion(ValueError):
    """A decision named a version that is no longer the newest one.

    A harness may append a version between the moment a human read a draft and
    the moment they decided it. The decision names the version it was made on, so
    that race is refused rather than silently applied to text nobody read.
    """

    def __init__(self, version: int, newest: int) -> None:
        self.version = version
        self.newest = newest
        super().__init__(
            f"version {version} is not the newest version ({newest}) and cannot be "
            "decided"
        )


@dataclasses.dataclass(frozen=True)
class Provenance:
    """Who wrote one version of a draft, and which chain it belongs to.

    The app can claim nothing else: no model, no prompt, no endpoint — it did
    not write the value. ``author`` is the identity the writer declared.
    """

    draft_id: str
    kind: str
    project: str
    meeting: str
    author: str
    written_at: str


@dataclasses.dataclass(frozen=True)
class Version:
    """One link of the chain: a value, its author, and the human's decision."""

    provenance: Provenance
    value: Any
    decision: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    promotion: dict | None = None

    @property
    def pending(self) -> bool:
        """True until a human accepts or rejects this version."""
        return self.decision is None

    def as_dict(self) -> dict:
        return {
            "author": self.provenance.author,
            "written_at": self.provenance.written_at,
            "value": self.value,
            "decision": self.decision,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
            "promotion": self.promotion,
        }


@dataclasses.dataclass(frozen=True)
class Draft:
    """A whole chain: every version written to one draft, newest last.

    ``value``/``provenance``/``promotion`` are the **newest** version's, which is
    what a reviewer and a promotion act on; ``versions`` is the history a
    reviewer may want to read back.
    """

    path: Path
    created_at: str
    versions: tuple[Version, ...]

    @property
    def current(self) -> Version:
        return self.versions[-1]

    @property
    def version(self) -> int:
        """The newest version's number, counting from 1 (a decision names it)."""
        return len(self.versions)

    @property
    def value(self) -> Any:
        return self.current.value

    @property
    def provenance(self) -> Provenance:
        return self.current.provenance

    @property
    def promotion(self) -> dict | None:
        return self.current.promotion

    @property
    def draft_id(self) -> str:
        return self.current.provenance.draft_id

    @property
    def kind(self) -> str:
        return self.current.provenance.kind

    @property
    def project(self) -> str:
        return self.current.provenance.project

    @property
    def meeting(self) -> str:
        return self.current.provenance.meeting

    @property
    def review_state(self) -> str:
        """The newest version's decision, or ``draft`` while it is pending."""
        return self.current.decision or DRAFT_STATE

    @property
    def accepted(self) -> bool:
        return self.review_state == "accepted"

    @property
    def rejected(self) -> bool:
        return self.review_state == "rejected"

    @property
    def run_dir(self) -> Path:
        """The directory this chain owns (a promotion writes its artifacts here)."""
        return self.path.parent

    def as_dict(self) -> dict:
        """The persisted document — what :func:`read_draft` reads back."""
        return {
            "draft_id": self.draft_id,
            "kind": self.kind,
            "project": self.project,
            "meeting": self.meeting,
            "created_at": self.created_at,
            "versions": [version.as_dict() for version in self.versions],
        }

    def reviewed(
        self,
        decision: str,
        *,
        author: str,
        at: str,
        promotion: dict | None = None,
        version: int | None = None,
    ) -> Draft:
        """A copy with ``decision`` recorded on the newest version.

        ``version`` is the version the decision was made **on** — what a reader
        saw. A caller that names an older one is refused
        (:class:`StaleVersion`), because a harness may have appended a version in
        between; ``None`` means "the newest", which is what a caller deciding what
        it just read wants.

        A decision is written **once**: re-deciding the same version returns the
        draft unchanged, so a repeated accept can never run a promotion twice.
        """
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"unknown draft decision {decision!r}")
        if version is not None and version != self.version:
            raise StaleVersion(version=version, newest=self.version)
        if not self.current.pending:
            return self
        current = dataclasses.replace(
            self.current,
            decision=decision,
            reviewed_by=author,
            reviewed_at=at,
            promotion=promotion,
        )
        return dataclasses.replace(self, versions=(*self.versions[:-1], current))


def _draft_id(kind: str, project: str, meeting: str, created_at: str) -> str:
    seed = "|".join([kind, project, meeting, created_at])
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _provenance(document: dict, entry: dict) -> Provenance:
    return Provenance(
        draft_id=_text(document, "draft_id"),
        kind=_text(document, "kind"),
        project=_text(document, "project"),
        meeting=_text(document, "meeting"),
        author=_text(entry, "author"),
        written_at=_text(entry, "written_at"),
    )


def _text(document: dict, key: str) -> str:
    value = document.get(key)
    return value if isinstance(value, str) else ""


def _version(document: dict, entry: object) -> Version:
    """One recorded version, or a useful error naming the field that is wrong."""
    if not isinstance(entry, dict):
        raise ValueError("a draft version must be a JSON object")
    decision = entry.get("decision")
    if decision is not None and decision not in REVIEW_DECISIONS:
        raise ValueError(f"unknown draft decision {decision!r}")
    promotion = entry.get("promotion")
    return Version(
        provenance=_provenance(document, entry),
        value=entry.get("value"),
        decision=decision,
        reviewed_by=entry.get("reviewed_by"),
        reviewed_at=entry.get("reviewed_at"),
        promotion=promotion if isinstance(promotion, dict) else None,
    )


def read_draft(path: str | Path) -> Draft:
    """Reload one chain from its ``draft.json`` (or raise :class:`ValueError`)."""
    source = Path(path)
    document = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{source} does not hold a draft object")
    versions = document.get("versions")
    if not isinstance(versions, list) or not versions:
        raise ValueError(f"{source} holds no draft versions")
    created_at = _text(document, "created_at")
    entries = tuple(_version(document, entry) for entry in versions)
    if not created_at:
        created_at = entries[0].provenance.written_at
    return Draft(path=source, created_at=created_at, versions=entries)


def write_draft(draft: Draft) -> Path:
    """Persist a chain, replacing the file wholesale (the chain is the document).

    Replacing the file is what makes the chain one document, so the write is only
    safe against a chain nobody else moved: a decision write checks that first
    (:func:`_refuse_if_superseded`), because the console and the MCP server are
    two processes over one directory.
    """
    draft.path.parent.mkdir(parents=True, exist_ok=True)
    draft.path.write_text(
        json.dumps(draft.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return draft.path


def start_draft(
    directory: str | Path,
    *,
    kind: str,
    project: str,
    meeting: str,
    value: Any,
    author: str,
    clock: Callable[[], str] = _now,
) -> Draft:
    """Open a chain with its first version and write it.

    ``value`` is whatever the harness produced — the app stores it as it came.
    Only the ``kind`` is checked: the chain's kinds are the three jobs, and a
    typo has to fail at the write rather than at a later acceptance.
    """
    if kind not in TASK_KINDS:
        raise ValueError(
            f"unknown draft kind {kind!r}; known kinds: {', '.join(TASK_KINDS)}"
        )
    written_at = clock()
    # The id is a digest of the facts plus a disambiguator, so two chains opened
    # for one meeting inside the same clock second are still two chains: an
    # existing directory is never appended to by accident, which would re-open a
    # draft the human had already decided.
    draft_id = _draft_id(kind, project, meeting, written_at)
    path = Path(directory) / draft_id / DRAFT_FILENAME
    disambiguator = 1
    while path.exists():
        draft_id = _draft_id(kind, project, meeting, f"{written_at}#{disambiguator}")
        path = Path(directory) / draft_id / DRAFT_FILENAME
        disambiguator += 1
    version = Version(
        provenance=Provenance(
            draft_id=draft_id,
            kind=kind,
            project=project,
            meeting=meeting,
            author=author,
            written_at=written_at,
        ),
        value=value,
    )
    draft = Draft(path=path, created_at=written_at, versions=(version,))
    write_draft(draft)
    return draft


def append_version(
    path: str | Path,
    *,
    value: Any,
    author: str,
    clock: Callable[[], str] = _now,
) -> Draft:
    """Add a version to an existing chain (a re-run, or a human's edit).

    The chain's kind and meeting are the ones it was opened with: a caller cannot
    retarget a draft by appending to it.
    """
    draft = read_draft(path)
    version = Version(
        provenance=dataclasses.replace(
            draft.provenance, author=author, written_at=clock()
        ),
        value=value,
    )
    updated = dataclasses.replace(draft, versions=(*draft.versions, version))
    write_draft(updated)
    return updated


def _stored_versions(path: Path) -> tuple[Version, ...] | None:
    """The chain ``path`` holds right now, or ``None`` when there is none to lose."""
    try:
        return read_draft(path).versions
    except (OSError, ValueError):
        return None


def _refuse_if_superseded(draft: Draft) -> None:
    """Refuse to write a chain over a version another writer added meanwhile.

    A write replaces ``draft.json`` wholesale, so a version a harness appended
    between the reader's read and this write would be **dropped** by it; the
    stored chain has to be the one this draft holds or the write is refused
    (:class:`StaleVersion`) and the reader decides on what is actually there. A
    chain that is gone or unreadable is not a version to lose, and is written as
    before.
    """
    stored = _stored_versions(draft.path)
    if stored is None:
        return
    if [version.as_dict() for version in stored] != [
        version.as_dict() for version in draft.versions
    ]:
        raise StaleVersion(version=draft.version, newest=len(stored))


def record_review(
    draft: Draft,
    decision: str,
    *,
    author: str,
    promotion: dict | None = None,
    version: int | None = None,
    clock: Callable[[], str] = _now,
) -> Draft:
    """Record a human's accept/reject on the version they decided, and write it.

    The decision is written as the whole document, so it is refused
    (:class:`StaleVersion`) when the chain on disk gained a version since
    ``draft`` was read: that version is the harness's, and a decision must not
    drop it.
    """
    reviewed = draft.reviewed(
        decision, author=author, at=clock(), promotion=promotion, version=version
    )
    if reviewed is not draft:
        _refuse_if_superseded(draft)
        write_draft(reviewed)
    return reviewed


def list_drafts(directory: str | Path) -> list[Draft]:
    """Every chain under ``directory``, newest version first.

    A directory that is not a chain — an old run, another writer's file, a
    corrupt one — is **skipped**, not fatal: this is a view over a directory a
    user can edit by hand.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    found: list[Draft] = []
    for child in sorted(root.iterdir()):
        if not (child / DRAFT_FILENAME).is_file():
            continue
        try:
            found.append(read_draft(child / DRAFT_FILENAME))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    found.sort(
        key=lambda draft: (draft.provenance.written_at, draft.draft_id),
        reverse=True,
    )
    return found


__all__ = [
    "DRAFT_FILENAME",
    "DRAFT_ITEM_SHAPE",
    "DRAFT_SHAPE",
    "DRAFT_STATE",
    "REVIEW_DECISIONS",
    "TASK_KINDS",
    "Draft",
    "Provenance",
    "StaleVersion",
    "Version",
    "append_version",
    "list_drafts",
    "read_draft",
    "record_review",
    "require_shape",
    "start_draft",
    "write_draft",
]
