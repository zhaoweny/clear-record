"""Project glossary snapshots: registry terms → the pipeline's glossary file.

The glossary is the ASR decoder's initial prompt, read from
``<workspace>/glossary.txt`` as one term/phrase per line. The MCP tuning loop
(ADR-0031) lets an agent edit the project's glossary and then re-run; this module
is the **bridge** that turns the registry's terms into that file, so a run after
a glossary edit actually applies it (before this, nothing generated the file).

Two invariants keep the loop honest:

- **Only ``confirmed`` terms bias the decoder.** ``candidate`` is an unreviewed
  agent draft and ``retired`` is history; neither may reach a run, or an
  unreviewed suggestion would silently change transcription.
- **A snapshot has a stable identity.** The same set of terms — in any input
  order — renders the same text and the same sha256, so a run can record which
  snapshot it used and a re-run is explainable.

The ``service → pipeline`` import edge is allowed (ADR-0012); ``Workspace`` owns
the glossary path so this module never composes it itself.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

from clear_record.pipeline.workspace import Workspace
from clear_record.service.models import GlossaryTerm
from clear_record.service.store import Registry

#: The one term status that may reach a decoder's initial prompt. The others
#: (``candidate``, ``retired``) are deliberately excluded.
CONFIRMED = "confirmed"


@dataclasses.dataclass(frozen=True)
class GlossarySnapshot:
    """A rendered glossary and its stable identity.

    ``text`` is the workspace ``glossary.txt`` body (one term per line, trailing
    newline), ``terms`` the canonical order it was rendered from, and ``sha256``
    the hash of ``text`` — the snapshot's identity, recorded with a run.
    """

    text: str
    sha256: str
    terms: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not self.terms


def canonical_terms(terms: Iterable[str]) -> tuple[str, ...]:
    """Terms as one stable, order-independent sequence: stripped, deduped, sorted.

    Sorting is case-insensitive with an exact-spelling tie-break, so the result
    never depends on the input order (or on the registry's row order) and the
    same set of terms always hashes the same.
    """
    cleaned = {term.strip() for term in terms if term.strip()}
    return tuple(sorted(cleaned, key=lambda term: (term.casefold(), term)))


def _render(terms: Iterable[str]) -> str:
    return "".join(f"{term}\n" for term in terms)


def _snapshot(terms: tuple[str, ...]) -> GlossarySnapshot:
    """A snapshot from already-canonical terms (its text is derived, never passed)."""
    text = _render(terms)
    return GlossarySnapshot(
        text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), terms=terms
    )


def build_snapshot(terms: Iterable[GlossaryTerm]) -> GlossarySnapshot:
    """The snapshot a project's terms produce: **confirmed terms only**."""
    return _snapshot(canonical_terms(t.term for t in terms if t.status == CONFIRMED))


def filter_terms(terms: Iterable[str], blocked: Iterable[str]) -> GlossarySnapshot:
    """``terms`` minus ``blocked``, as a snapshot.

    The workspace ``glossary.txt`` is the user's file and is never rewritten when
    the registry confirms nothing, but a term the registry has retired or left
    unconfirmed must not bias the decoder from it either (ADR-0033): the run
    hands the pipeline this filtered set instead.
    """
    drop = {term.strip().casefold() for term in blocked}
    return _snapshot(
        tuple(term for term in canonical_terms(terms) if term.casefold() not in drop)
    )


def snapshot_from_text(text: str) -> GlossarySnapshot:
    """The snapshot a glossary body represents.

    Used for an explicit glossary file — the same one-term-per-line format, with
    ``#`` comments and blanks ignored — so an explicit glossary's identity is
    comparable to a project snapshot's.
    """
    lines = (line.strip() for line in text.splitlines())
    return _snapshot(
        canonical_terms(line for line in lines if not line.startswith("#"))
    )


def project_snapshot(registry: Registry, project_slug: str) -> GlossarySnapshot:
    """The confirmed-term snapshot of one project, straight from the registry."""
    return build_snapshot(registry.list_terms(project_slug, status=CONFIRMED))


def write_snapshot(workspace: Workspace, snapshot: GlossarySnapshot) -> Path:
    """Publish ``snapshot`` at the workspace's ``glossary.txt``; return its path.

    Written atomically (``.tmp`` + :func:`os.replace`) so a run that starts
    concurrently never reads a half-written prompt.
    """
    workspace.root.mkdir(parents=True, exist_ok=True)
    path = workspace.glossary_path
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(snapshot.text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_run_snapshot(workspace: Workspace, snapshot: GlossarySnapshot) -> Path:
    """Publish ``snapshot`` at the workspace's app-owned *run* glossary.

    Written atomically like :func:`write_snapshot`, but at
    :attr:`Workspace.run_glossary_path`, so the run's bias never lands in the
    user's ``glossary.txt``.
    """
    path = workspace.run_glossary_path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(snapshot.text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_project_snapshot(
    registry: Registry, project_slug: str, workspace: Workspace
) -> tuple[Path, GlossarySnapshot]:
    """Write a project's confirmed-term snapshot into ``workspace``.

    Returns the written path and the snapshot, so the caller can record the
    hash alongside the run it is about to start.
    """
    snapshot = project_snapshot(registry, project_slug)
    return write_snapshot(workspace, snapshot), snapshot


__all__ = [
    "filter_terms",
    "CONFIRMED",
    "GlossarySnapshot",
    "build_snapshot",
    "canonical_terms",
    "project_snapshot",
    "snapshot_from_text",
    "write_project_snapshot",
    "write_run_snapshot",
    "write_snapshot",
]
