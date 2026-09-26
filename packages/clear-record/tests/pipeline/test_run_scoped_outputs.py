"""A run's outputs are its own copy; the workspace publishes the newest.

ADR-0033's retention rule, at the workspace level: rewrite-in-place is
destructive in kind, so every run writes its manifest, transcript segments,
reconciled record and exports into ``<workspace>/runs/<run id>/`` — its own copy,
retained — and a **finished** run publishes that copy at the workspace root,
which stays the default read. A later run writes its own copy and leaves the
earlier run's readable; a run that dies half-way publishes nothing, so neither
the workspace's copy nor another run's can be touched by it. What a run
**reads** (its tapes, the ``glossary.txt`` a hand-edit lands in, the
``.clear-record-ignore`` declaration) stays the workspace's; three things a node
run writes at the workspace root all the same — the normalized ``audio/``, the
``glossary.txt`` a confirmed-term registry publishes, and ``transcribe.log``.
The app-owned chunk cache is neither: it lives in the app's own cache directory,
keyed per workspace, and a run only advances it, so a resume keeps its cache.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from clear_record.core import RecordDocument, Segment, Source, load_json
from clear_record.pipeline import stages
from clear_record.pipeline.workspace import (
    MANIFEST,
    RUN_MARKER,
    RECORD,
    RUNS_DIR,
    SEGMENTS,
    Workspace,
    discover_audio,
    publish_run,
)


def _tone(path: Path, *, sr: int = 8000, seconds: float = 2.0) -> None:
    """One recording of a tone, so ``ingest`` has real audio to normalize."""
    time = np.arange(int(seconds * sr), dtype=np.float64) / sr
    sf.write(str(path), (0.4 * np.sin(2 * np.pi * 220.0 * time)).astype(np.float32), sr)


def _source(source_id: str) -> Source:
    return Source(id=source_id, path=f"audio/{source_id}.wav", label="Speaker 1")


def _finish(scope: Workspace, text: str) -> Workspace:
    """Leave the documents a finished run leaves in *scope*, keyed by *text*."""
    scope.write_manifest([_source("a")])
    scope.write_segments({"a": [Segment(0.0, 1.0, text, "a")]}, {"backend": text})
    scope.write_record(
        RecordDocument(
            sources=(_source("a"),),
            alignment=None,
            segments=(Segment(0.0, 1.0, text, "a"),),
        )
    )
    scope.export_dir.mkdir(parents=True, exist_ok=True)
    (scope.export_dir / "record.md").write_text(text, encoding="utf-8")
    return scope


def _published(home: Workspace) -> dict[str, bytes]:
    """The workspace's published documents, byte for byte."""
    return {
        name: (home.root / name).read_bytes() for name in (MANIFEST, SEGMENTS, RECORD)
    }


def _record_text(workspace: Workspace) -> str:
    return workspace.load_record().segments[0].text


# --- the layout ------------------------------------------------------------- #
def test_a_run_scope_writes_beside_the_workspace_and_keeps_the_shared_state(
    tmp_path: Path,
) -> None:
    home = Workspace.at(tmp_path / "ws")
    scope = _finish(home.run_scope(7), "seven")

    assert scope.outputs == home.root / RUNS_DIR / "7"
    assert scope.manifest_path == home.root / RUNS_DIR / "7" / MANIFEST
    assert scope.segments_path == home.root / RUNS_DIR / "7" / SEGMENTS
    assert scope.record_path == home.root / RUNS_DIR / "7" / RECORD
    assert scope.export_dir == home.root / RUNS_DIR / "7" / "export"
    # The inputs and the shared state are the workspace's, not the run's.
    assert scope.audio_dir == home.audio_dir
    assert scope.glossary_path == home.glossary_path
    assert scope.ignore_path == home.ignore_path
    assert scope.chunks_dir == home.chunks_dir
    # Nothing is published until the run finishes.
    assert not home.manifest_path.exists()
    assert not home.record_path.exists()


def test_a_run_scope_resolves_back_to_its_workspace(tmp_path: Path) -> None:
    home = Workspace.at(tmp_path / "ws")
    scope = home.begin_scope(7)

    opened = Workspace.at(scope.outputs)
    assert opened.root == home.root
    assert opened.run_id == 7
    assert opened.outputs == scope.outputs
    assert Workspace.at(home.root).run_id is None


def test_a_marker_rewrite_is_published_atomically(tmp_path: Path, monkeypatch) -> None:
    """A crash mid-write leaves the marker a reader opens whole.

    ``begin_scope`` runs again for a run that begins in a scope that already
    exists (a re-claim), and every stage of the run opens the scope by reading
    its marker. Written in place, a writer killed mid-body would leave a torn
    marker and ``Workspace.at`` would silently open the scope as *its own*
    workspace — the run's shared state (the published manifest, ``audio/``, the
    chunk cache key) read from the wrong directory. The marker is published by
    rename instead, so the torn body never reaches the path a reader opens.
    """
    home = Workspace.at(tmp_path / "ws")
    scope = home.begin_scope(1)

    def dying_write_json(path, payload) -> None:
        # A writer killed mid-write: the body it was writing is partial.
        Path(path).write_text('{"workspace": "/torn', encoding="utf-8")
        raise OSError("killed mid-write")

    monkeypatch.setattr("clear_record.pipeline.workspace.write_json", dying_write_json)
    with pytest.raises(OSError):
        scope.begin_scope(1)

    assert load_json(scope.outputs / RUN_MARKER)["run_id"] == 1
    reopened = Workspace.at(scope.outputs)
    assert reopened.run_id == 1
    assert reopened.root == home.root


def test_only_a_marked_run_scope_resolves_back(tmp_path: Path) -> None:
    """The **mark**, not the name, is what makes a scope.

    ``runs`` is an ordinary word, so a directory merely *named* like one — a
    workspace that happens to sit at ``…/runs/7``, an operator's own ``runs``
    folder — stays a workspace: inferring a scope from the leaf name put a
    meeting's outputs in a different directory, left its tapes outside the walk
    and made ``read_transcript`` raise.
    """
    plain = Workspace.at(tmp_path / "docs" / "7")
    assert plain.run_id is None
    assert plain.outputs == tmp_path / "docs" / "7"
    named = Workspace.at(tmp_path / "runs" / "not-an-id")
    assert named.run_id is None
    assert named.root == tmp_path / "runs" / "not-an-id"

    # A workspace whose own path ends in ``runs/<digits>`` is a workspace.
    workspace = tmp_path / "runs" / "7"
    workspace.mkdir(parents=True)
    (workspace / "a.wav").write_bytes(b"RIFFfake")
    here = Workspace.at(workspace)
    assert here.run_id is None
    assert here.root == workspace
    assert here.outputs == workspace
    # ... and its own run scopes are under it, marked, and resolve back to it.
    scope = here.begin_scope(1)
    assert scope.outputs == workspace / "runs" / "1"
    assert Workspace.at(scope.outputs).root == workspace
    assert Workspace.at(scope.outputs).run_id == 1


# --- the documents name their run ------------------------------------------- #
def test_each_document_of_a_run_names_the_run_that_wrote_it(tmp_path: Path) -> None:
    home = Workspace.at(tmp_path / "ws")
    scope = _finish(home.begin_scope(7), "seven")

    assert load_json(scope.manifest_path)["run_id"] == 7
    assert scope.load_segments()[1]["run_id"] == 7
    assert scope.load_record().metadata["run_id"] == 7

    # A workspace's own documents name no run: the stage commands and
    # ``calibrate`` write in place at the workspace root, and what they leave
    # there names no run at all.
    plain = Workspace.at(tmp_path / "plain")
    _finish(plain, "plain")
    assert "run_id" not in load_json(plain.manifest_path)
    assert "run_id" not in plain.load_segments()[1]
    assert "run_id" not in plain.load_record().metadata


def test_a_run_reads_the_workspaces_published_manifest_for_declarations(
    tmp_path: Path,
) -> None:
    """The declaration an operator hand-edits lives at the root, not in a run.

    A run scope's own manifest is written fresh by its ``ingest``, so the
    declarations carried over (`_manifest_starts` / `_manifest_roles`) are read
    from the workspace's published manifest — and a scope, having none of its own
    yet, does not silently read the published one as its own.
    """
    home = Workspace.at(tmp_path / "ws")
    home.write_manifest([_source("a")])

    scope = home.run_scope(3)
    assert [source.id for source in scope.load_published_manifest()[0]] == ["a"]
    with pytest.raises(FileNotFoundError):
        scope.load_manifest()


# --- publication: the newest run is the default read ------------------------ #
def test_publishing_makes_a_runs_own_copy_the_workspaces_default_read(
    tmp_path: Path,
) -> None:
    home = Workspace.at(tmp_path / "ws")
    first = _finish(home.run_scope(1), "first")
    publish_run(first)
    assert _record_text(home) == "first"
    assert home.load_segments()[1]["run_id"] == 1

    second = _finish(home.run_scope(2), "second")
    publish_run(second)

    # The newest run is the default read, documents and exports both.
    assert _record_text(home) == "second"
    assert home.load_segments()[1]["run_id"] == 2
    assert (home.export_dir / "record.md").read_text(encoding="utf-8") == "second"
    # The earlier run's own copy is intact and readable.
    assert _record_text(first) == "first"
    assert first.load_segments()[1]["run_id"] == 1
    assert load_json(first.manifest_path)["run_id"] == 1
    assert (first.export_dir / "record.md").read_text(encoding="utf-8") == "first"


def test_a_half_written_run_cannot_touch_the_published_copy_or_another_run(
    tmp_path: Path,
) -> None:
    home = Workspace.at(tmp_path / "ws")
    first = _finish(home.run_scope(1), "first")
    publish_run(first)
    published = _published(home)

    # A second run starts writing its own copy and dies before it finishes: no
    # publication happens (the run path publishes only a finished run), and what
    # it wrote is confined to its own directory.
    dying = home.run_scope(2)
    dying.outputs.mkdir(parents=True, exist_ok=True)
    dying.record_path.write_text("{half", encoding="utf-8")
    dying.segments_path.write_text('{"sources": {}}', encoding="utf-8")
    dying.export_dir.mkdir(parents=True, exist_ok=True)
    (dying.export_dir / "record.md").write_text("half", encoding="utf-8")

    assert _published(home) == published
    assert _record_text(home) == "first"
    assert (home.export_dir / "record.md").read_text(encoding="utf-8") == "first"
    assert _record_text(first) == "first"
    assert (first.export_dir / "record.md").read_text(encoding="utf-8") == "first"
    assert load_json(first.manifest_path)["run_id"] == 1


# --- resume and the cache --------------------------------------------------- #
def test_every_run_of_a_workspace_shares_its_chunk_cache(tmp_path: Path) -> None:
    """A run's own copy is its documents; the cache stays the workspace's.

    Which is what makes a resume a resume: the run after the stop decodes into
    the cache the stopped one already filled, whatever run id its documents
    carry (ADR-0007/ADR-0025).
    """
    home = Workspace.at(tmp_path / "ws")
    cache = home.chunk_cache("a").directory
    assert home.run_scope(1).chunk_cache("a").directory == cache
    assert home.begin_scope(2).chunk_cache("a").directory == cache
    assert Workspace.at(home.run_scope(2).outputs).chunk_cache("a").directory == cache


# --- the declaration an operator hand-edits --------------------------------- #
def test_a_scoped_ingest_carries_the_workspaces_declared_start_and_role(
    tmp_path: Path,
) -> None:
    """The operator's hand-edit at the root reaches the run's own copy.

    ``ingest`` **rebuilds** the manifest, so a start and a role declared by hand
    in the workspace's ``manifest.json`` are carried into the manifest the pass
    writes — which, for a run, is the manifest in the run's own copy. The
    declaration is therefore read from the workspace's **published** manifest,
    where a hand-edit lands (``Workspace.load_published_manifest``): the run's own
    copy is written fresh by this pass, so reading it would drop exactly the
    declarations the flow exists for. The scoped twin of the CLI's
    declare-then-run pin.
    """
    workspace = tmp_path / "rec"
    workspace.mkdir()
    _tone(workspace / "a.wav")
    home = Workspace.at(workspace)
    declared_start = 1_750_000_000.0
    home.write_manifest(
        [
            Source(
                id="a",
                path=str(workspace / "a.wav"),
                start_s=declared_start,
                role="mixed",
            )
        ]
    )

    scope = home.begin_scope(5)
    stages.ingest(str(scope.outputs))

    (source,) = scope.load_manifest()[0]
    assert source.id == "a"
    assert source.role == "mixed"
    assert source.start_s == declared_start
    assert load_json(scope.manifest_path)["run_id"] == 5
    # The pass wrote its own copy, and the operator's declaration is where it was.
    assert home.load_manifest()[0][0].role == "mixed"


def test_discovery_keeps_an_operators_own_runs_directory(tmp_path: Path) -> None:
    """`runs/` is not skipped by name — only a **marked** scope is the app's own.

    A capture folder that happens to be called ``runs`` (or anything else under
    the workspace's ``runs/`` that no marker claims) is the operator's: its audio
    is an input. The name-based skip dropped it without a word, and the pass then
    refused with "no audio files found".
    """
    home = Workspace.at(tmp_path / "ws")
    takes = home.root / RUNS_DIR / "my-takes"
    takes.mkdir(parents=True)
    (takes / "a.wav").write_bytes(b"RIFFfake")

    # A marked scope holds the app's own output: it is never an input.
    scope = home.begin_scope(1)
    (scope.outputs / "take.wav").write_bytes(b"RIFFfake")
    assert (scope.outputs / RUN_MARKER).is_file()

    assert [path.name for path in discover_audio(home.root)] == ["a.wav"]
