"""Archive behaviour: the manifest, immutability, root resolution, verification.

These exercise the service seam directly (temp DB, temp archive root, no web
app), so an archive is trusted independently of any adapter.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from clear_record.service import Registry, archive_meeting, verify_archive
from clear_record.service.archive import MANIFEST_FILENAME
from clear_record.service.store import _alembic_config


def _registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _seeded(tmp_path, *, default_archive_root: bool = True):
    """A project with one meeting, one tape and one record artifact."""
    registry = _registry(tmp_path)
    root = tmp_path / "archive"
    registry.create_project(
        "Ops", default_archive_root=str(root) if default_archive_root else None
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    tape = tmp_path / "a.wav"
    tape.write_bytes(b"RIFFfake-audio")
    meeting = registry.create_meeting(
        "ops", "Kickoff", recorded_at="2026-09-14", workspace_path=str(workspace)
    )
    registry.set_recording_set(meeting.id, [str(tape)])
    record = workspace / "record.json"
    record.write_bytes(b'{"segments": []}')
    registry.add_artifact(
        meeting.id,
        kind="record",
        path=str(record),
        sha256=hashlib.sha256(record.read_bytes()).hexdigest(),
        bytes=record.stat().st_size,
    )
    return registry, meeting, tape, record, root


def test_manifest_lists_every_file_with_checksums(tmp_path) -> None:
    registry, meeting, tape, record, root = _seeded(tmp_path)
    archive = archive_meeting(registry, meeting)

    assert archive.meeting_id == meeting.id
    assert archive.project_id == meeting.project_id
    assert registry.get_archive(archive.id) == archive
    assert registry.list_archives(meeting.id) == [archive]

    archive_dir = Path(archive.root_path)
    assert archive_dir.parent == root.resolve() / "ops"
    assert archive_dir.name.endswith("-kickoff")
    assert archive.manifest_path == str(archive_dir / MANIFEST_FILENAME)

    manifest_path = archive_dir / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["tool"] == "clear-record"
    assert manifest["tool_version"]
    assert manifest["created_at"]
    assert manifest["project_id"] == meeting.project_id
    assert manifest["project_slug"] == "ops"
    assert manifest["meeting_id"] == meeting.id
    assert manifest["meeting_slug"] == "kickoff"
    assert manifest["recording_set_id"] == registry.latest_recording_set(meeting.id).id
    assert manifest["artifact_ids"] == [
        artifact.id for artifact in registry.list_artifacts(meeting.id)
    ]

    files = {entry["path"]: entry for entry in manifest["files"]}
    assert set(files) == {"tapes/a.wav", "record/record.json"}
    assert files["tapes/a.wav"]["kind"] == "tape"
    assert files["tapes/a.wav"]["bytes"] == tape.stat().st_size
    assert (
        files["tapes/a.wav"]["sha256"] == hashlib.sha256(tape.read_bytes()).hexdigest()
    )
    assert files["record/record.json"]["kind"] == "record"
    assert files["record/record.json"]["bytes"] == record.stat().st_size
    assert (
        files["record/record.json"]["sha256"]
        == hashlib.sha256(record.read_bytes()).hexdigest()
    )

    # The copies are byte-identical and the manifest is sealed in the registry.
    assert (archive_dir / "tapes" / "a.wav").read_bytes() == tape.read_bytes()
    assert (archive_dir / "record" / "record.json").read_bytes() == record.read_bytes()
    assert (
        archive.manifest_sha256
        == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )


def test_rearchiving_creates_a_new_directory(tmp_path, monkeypatch) -> None:
    registry, meeting, _tape, _record, _root = _seeded(tmp_path)
    # Pin the timestamp so the second call would collide without the suffix.
    monkeypatch.setattr(
        "clear_record.service.archive._timestamp", lambda: "20260914-120000"
    )

    first = archive_meeting(registry, meeting)
    first_manifest = Path(first.manifest_path).read_bytes()

    second = archive_meeting(registry, meeting)

    assert second.root_path != first.root_path
    assert Path(second.root_path).is_dir()
    assert Path(first.root_path).is_dir()
    # The first archive is untouched byte-for-byte.
    assert Path(first.manifest_path).read_bytes() == first_manifest
    assert Path(first.root_path).name == "20260914-120000-kickoff"
    assert Path(second.root_path).name == "20260914-120000-kickoff-2"
    assert len(registry.list_archives(meeting.id)) == 2


def test_a_concurrent_archive_winner_is_never_deleted(tmp_path, monkeypatch) -> None:
    """A name another archive won between the probe and mkdir is retried, not deleted."""
    from clear_record.service import archive as archive_mod

    registry, meeting, _tape, _record, root = _seeded(tmp_path)
    winner = root / "ops" / "20260914-120000-kickoff"
    winner.mkdir(parents=True)
    (winner / MANIFEST_FILENAME).write_text('{"winner": true}', encoding="utf-8")

    calls = {"n": 0}
    real = archive_mod._archive_dir

    def racy(root_path, meeting, timestamp):
        calls["n"] += 1
        if calls["n"] == 1:
            return winner
        return real(root_path, meeting, timestamp)

    monkeypatch.setattr(archive_mod, "_archive_dir", racy)

    archive = archive_meeting(registry, meeting)

    # The concurrent winner is byte-for-byte untouched...
    assert (winner / MANIFEST_FILENAME).read_text(
        encoding="utf-8"
    ) == '{"winner": true}'
    # ...and this archive landed beside it, under a free name.
    assert Path(archive.root_path) != winner
    assert Path(archive.root_path).parent == winner.parent
    assert verify_archive(Path(archive.root_path)).ok is True


def test_verify_archive_detects_tampering_and_missing_files(tmp_path) -> None:
    registry, meeting, _tape, _record, _root = _seeded(tmp_path)
    archive = archive_meeting(registry, meeting)
    archive_dir = Path(archive.root_path)

    assert verify_archive(archive_dir).ok is True

    # A same-length tamper is caught by the digest alone.
    tape_copy = archive_dir / "tapes" / "a.wav"
    tape_copy.write_bytes(bytes(tape_copy.stat().st_size))
    (archive_dir / "record" / "record.json").unlink()

    result = verify_archive(archive_dir)
    assert result.ok is False
    assert result.mismatched == ["tapes/a.wav"]
    assert result.missing == ["record/record.json"]

    with pytest.raises(FileNotFoundError):
        verify_archive(tmp_path / "nothing-here")


def test_archive_root_must_be_chosen(tmp_path) -> None:
    registry, meeting, _tape, _record, _root = _seeded(
        tmp_path, default_archive_root=False
    )
    with pytest.raises(ValueError, match="no archive root"):
        archive_meeting(registry, meeting)

    explicit = tmp_path / "somewhere-else"
    archive = archive_meeting(registry, meeting, root=explicit)
    assert Path(archive.root_path).parent == explicit.resolve() / "ops"


def test_a_registry_at_revision_two_gains_the_archive_table(tmp_path) -> None:
    """An existing registry at revision two gains the archive table on open."""
    db = tmp_path / "registry.sqlite3"
    command.upgrade(_alembic_config(db), "0002")
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute(
            "INSERT INTO project (slug, name, notes, created_at)"
            " VALUES ('ops', 'Ops', '', 'now')"
        )

    registry = Registry(db)
    assert [p.slug for p in registry.list_projects()] == ["ops"]
    meeting = registry.create_meeting("ops", "Kickoff")
    archive = registry.add_archive(
        meeting.id,
        meeting.project_id,
        root_path="/archives/ops/kickoff",
        manifest_path="/archives/ops/kickoff/archive.json",
        manifest_sha256="0" * 64,
    )
    assert registry.list_archives(meeting.id) == [archive]
    assert registry.get_archive(archive.id) == archive
