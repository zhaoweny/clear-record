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
        {"author": "mcp", "written_at": "…", "value": {…},
         "decision": null, "reviewed_by": null, "reviewed_at": null,
         "promotion": null}
      ]
    }

Properties the design holds to:

- **Every version records who wrote it.** ``author`` is the **actor** the writing
  transport supplied — ``mcp`` for the stdio adapter; the surface that asked, for
  any other caller (ADR-0033) — and it is the only provenance this store can
  honestly claim: the app did not produce the value and never saw the model, and a
  string the caller *declares* looks like evidence without being any (ADR-0033
  supersedes ADR-0031's declared identity). No console route writes a version;
  the field's name is the stored shape's, and what it holds is an actor.
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
  than failing it. One chain is one file written whole, so a writer **holds the
  chain** while it reads it and writes it back: the console and the MCP server are
  two processes over one workspace, and neither may drop the other's version
  (ADR-0031: *one chain, one writer at a time*; *a decision names the version it
  decides*).
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from clear_record.service.audit import require_actor

if os.name == "nt":  # pragma: no cover - the Windows build's own branch
    import msvcrt

    def _lock(handle: Any) -> None:
        """Take ``handle``'s lock, waiting for whoever holds it (Windows).

        ``msvcrt`` locks a byte *range*, so the lock file is given a byte to hold
        first; ``LK_LOCK`` retries for about ten seconds and then raises, which is
        a bound rather than a hang.
        """
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: Any) -> None:
        """Take ``handle``'s lock, waiting for whoever holds it (POSIX)."""
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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

#: The file a writer holds while it reads a chain and writes it back. It lives in
#: the chain's own directory, so it appears and goes with the chain, and a listing
#: never mistakes it for one (a chain is a directory holding ``DRAFT_FILENAME``).
LOCK_FILENAME = "draft.lock"

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


class UnreadableChain(ValueError):
    """A chain that cannot be read, so it cannot be decided or written over.

    Nothing this store writes can produce one: a chain is replaced whole, by
    rename. What reaches it is a file a hand-edit or a crash left unusable —
    which a listing skips, so the console and the MCP tools can only meet it if
    the file is damaged between a surface's read and the store's read under the
    hold. Both of those reads raise it: the append (a re-run) and the decision.
    """


@dataclasses.dataclass(frozen=True)
class Provenance:
    """Who wrote one version of a draft, and which chain it belongs to.

    The app can claim nothing else: no model, no prompt, no endpoint — it did
    not write the value. ``author`` is the **actor** the writing transport
    supplied, not an identity the writer declared (ADR-0033).
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
        actor: str,
        at: str,
        promotion: dict | None = None,
    ) -> Draft:
        """A copy with ``decision`` recorded on the newest version.

        That the newest version is the one the human read is the caller's
        question, asked against the chain **on disk** (:func:`record_review`,
        :class:`StaleVersion`): this method records the decision it is handed.

        A decision is written **once** — the guard is :func:`record_review`, which
        returns the stored chain before its ``apply`` runs — so a repeated accept
        can never run a promotion twice; this method only records the decision it
        is handed.
        """
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"unknown draft decision {decision!r}")
        if not self.current.pending:
            return self
        current = dataclasses.replace(
            self.current,
            decision=decision,
            reviewed_by=actor,
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


@contextlib.contextmanager
def _held(path: Path) -> Iterator[None]:
    """Hold one chain while this writer reads it and writes it back.

    The lock is advisory and local, which is what this store is: the console and
    the MCP server are two processes over one workspace, and a chain is one
    document written whole — a writer that read it and writes it back has to keep
    the other's **chain write** out of the middle, or one of the two versions is
    silently gone. It is held by the writers that take it (``write_draft``,
    ``start_draft``, ``append_version``, ``record_review``): a reader that takes
    no lock is unaffected by it, and a hand-edit takes no lock and is held by
    none.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / LOCK_FILENAME, "a+b") as handle:
        _lock(handle)
        try:
            yield
        finally:
            _unlock(handle)


def _replace(draft: Draft) -> Path:
    """Write ``draft`` over its chain's file, whole, from a temporary name.

    The rename is what makes a torn ``draft.json`` impossible to produce: a reader
    sees the chain before the write or after it, never half of either.
    """
    parent = draft.path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = parent / f"{DRAFT_FILENAME}.tmp"
    temporary.write_text(
        json.dumps(draft.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, draft.path)
    return draft.path


def write_draft(draft: Draft) -> Path:
    """Persist a chain, replacing the file wholesale (the chain is the document).

    This is the chain's own replace, not a **service entry point**: the three
    entry points above it (:func:`start_draft`, :func:`append_version`,
    :func:`record_review`) are what a surface calls, and the audit record holds
    their rows (ADR-0033) — a second row here would attribute the same write
    twice and name no actor of its own.

    The chain is held for the write. A caller that *read* the chain and writes it
    back must hold it across both halves — :func:`append_version` and
    :func:`record_review` do; calling this directly is for a chain whose content
    the caller owns.
    """
    with _held(draft.path):
        return _replace(draft)


def start_draft(
    directory: str | Path,
    *,
    kind: str,
    project: str,
    meeting: str,
    value: Any,
    actor: str,
    clock: Callable[[], str] = _now,
) -> Draft:
    """Open a chain with its first version and write it.

    ``value`` is whatever the harness produced — the app stores it as it came.
    Only the ``kind`` is checked: the chain's kinds are the three jobs, and a
    typo has to fail at the write rather than at a later acceptance.

    ``actor`` is the transport's own word for the surface that wrote this version
    (:data:`~clear_record.service.lifecycle.ACTORS`) — ``mcp`` for the adapter that
    is the only writer today — and it is what the version records as its author:
    ADR-0033 supersedes ADR-0031's *declared* identity, because a string a caller
    chooses looks like evidence and is not.

    The directory **is** the claim on the id: it is created exclusively, so two
    writers that collide inside one clock second — the console and the MCP server
    are two processes — cannot both take the first candidate, and the loser takes
    the next one instead of writing over the winner's chain.
    """
    if kind not in TASK_KINDS:
        raise ValueError(
            f"unknown draft kind {kind!r}; known kinds: {', '.join(TASK_KINDS)}"
        )
    actor = require_actor(actor)
    written_at = clock()
    root = Path(directory)
    # The id is a digest of the facts plus a disambiguator, so two chains opened
    # for one meeting inside the same clock second are still two chains.
    disambiguator = 0
    while True:
        seed = written_at if disambiguator == 0 else f"{written_at}#{disambiguator}"
        draft_id = _draft_id(kind, project, meeting, seed)
        chain_dir = root / draft_id
        try:
            chain_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            disambiguator += 1
            continue
        break
    version = Version(
        provenance=Provenance(
            draft_id=draft_id,
            kind=kind,
            project=project,
            meeting=meeting,
            author=actor,
            written_at=written_at,
        ),
        value=value,
    )
    draft = Draft(
        path=chain_dir / DRAFT_FILENAME, created_at=written_at, versions=(version,)
    )
    write_draft(draft)
    return draft


def _read_chain(path: Path) -> Draft:
    """The chain at ``path``, or :class:`UnreadableChain` naming the file.

    Every read of a chain a writer is about to write back goes through this: the
    file is one document, and a writer that cannot parse it must not write over
    what is there.
    """
    try:
        return read_draft(path)
    except (OSError, ValueError) as exc:
        raise UnreadableChain(f"{path} cannot be read: {exc}") from exc


def append_version(
    path: str | Path,
    *,
    value: Any,
    actor: str,
    clock: Callable[[], str] = _now,
) -> Draft:
    """Add a version to an existing chain (a re-run, or a human's edit).

    Read, extended and written as one operation on the chain: a decision that
    lands meanwhile waits for the lock instead of being overwritten, and a version
    another appender wrote in between is appended **after**, never lost. The
    chain's kind and meeting are the ones it was opened with — a caller cannot
    retarget a draft by appending to it — and a chain that cannot be read raises
    :class:`UnreadableChain` rather than being written over. ``actor`` is the
    surface the appended version's author is recorded as (ADR-0033).
    """
    actor = require_actor(actor)
    source = Path(path)
    with _held(source):
        draft = _read_chain(source)
        version = Version(
            provenance=dataclasses.replace(
                draft.provenance, author=actor, written_at=clock()
            ),
            value=value,
        )
        updated = dataclasses.replace(draft, versions=(*draft.versions, version))
        _replace(updated)
    return updated


def record_review(
    draft: Draft,
    decision: str,
    *,
    actor: str,
    version: int,
    clock: Callable[[], str] = _now,
    apply: Callable[[Draft], dict | None] | None = None,
) -> Draft:
    """Record a human's decision on a chain's **named** version, and write it.

    One operation on the chain, decided on **what is stored**, not on the caller's
    snapshot: the chain is read under its lock, ``version`` — the version the
    human read, and there is no default — is checked against it, ``apply`` (an
    acceptance's own work — :func:`~clear_record.service.agent_review.promote_draft`)
    runs inside the same hold and its summary is recorded as the promotion, and
    the decided chain is written back before the lock is released. So no other
    writer's chain write can land between the decision and its record, and a
    decision refused for naming a version that is not the newest one is refused
    **before** ``apply`` runs.

    A version that already carries a decision is **not** an error and not a second
    write: the chain as it stands is returned, because a decision is recorded once
    (ADR-0031). A chain that cannot be read at all — gone, or damaged by a
    hand-edit or a crash — raises :class:`UnreadableChain` rather than being
    written over; the console and the MCP tools reach that only if the file is
    damaged between their read and this call.
    """
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"unknown draft decision {decision!r}")
    actor = require_actor(actor)
    source = Path(draft.path)
    with _held(source):
        stored = _read_chain(source)
        if version != stored.version:
            raise StaleVersion(version=version, newest=stored.version)
        if not stored.current.pending:
            return stored
        summary = apply(stored) if apply is not None else None
        promotion = (
            None
            if summary is None
            else {"kind": stored.kind, "at": clock(), "summary": summary}
        )
        decided = stored.reviewed(
            decision, actor=actor, at=clock(), promotion=promotion
        )
        _replace(decided)
    return decided


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
    "UnreadableChain",
    "Version",
    "append_version",
    "list_drafts",
    "read_draft",
    "record_review",
    "require_shape",
    "start_draft",
    "write_draft",
]
