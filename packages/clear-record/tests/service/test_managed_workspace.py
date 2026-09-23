"""The managed workspace: root resolution, guarded uploads and storage (ADR-0024).

Exercised through the service seam (temp registry, temp root, no web app). The
point of these tests is the *indistinguishability* claim: a managed workspace is
an ordinary workspace, and its uploaded tapes are inputs like any other, so the
pipeline is never told which mode it is running.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from clear_record.pipeline.workspace import Workspace, discover_audio
from clear_record.service.agent_review import AGENT_DIRNAME
from clear_record.core import paths
from clear_record.service import Registry
from clear_record.service import managed


@pytest.fixture(autouse=True)
def _no_real_config(tmp_path, monkeypatch):
    """Keep a developer's real config.toml out of every resolution here."""
    monkeypatch.setattr(paths, "config_path", lambda: tmp_path / "absent.toml")


@pytest.fixture()
def registry(tmp_path) -> Registry:
    return Registry.open(db_path=tmp_path / "registry.sqlite3")


def _managed_meeting(registry: Registry, monkeypatch, tmp_path, title="Kickoff"):
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", title)
    return managed.ensure_managed_workspace(registry, meeting)


class _DroppedStream(io.BytesIO):
    """A body that fails part-way, like a dropped connection."""

    def __init__(self, data: bytes, fail_after: int):
        super().__init__(data)
        self._fail_after = fail_after

    def read(self, size: int = -1) -> bytes:
        if self.tell() >= self._fail_after:
            raise OSError("connection dropped")
        return super().read(min(size, 2) if size >= 0 else size)


class _MustNotBeRead(io.BytesIO):
    def read(self, size: int = -1) -> bytes:  # pragma: no cover - asserts fail if hit
        raise AssertionError("the body must not be read before the guard refuses")


# --- root resolution ------------------------------------------------------- #
def test_workspace_root_precedence(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CR_DATA_DIR", raising=False)
    monkeypatch.delenv("CR_WORKSPACE_ROOT", raising=False)

    # explicit beats everything
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "env"))
    assert paths.resolve_workspace_root(tmp_path / "explicit") == tmp_path / "explicit"

    # env beats the config file and the platform default
    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\nworkspace_root = "{tmp_path / "configured"}"\n')
    monkeypatch.setattr(paths, "config_path", lambda: config)
    assert paths.resolve_workspace_root() == tmp_path / "env"

    # the config beats the default
    monkeypatch.delenv("CR_WORKSPACE_ROOT", raising=False)
    assert paths.resolve_workspace_root() == tmp_path / "configured"


def test_workspace_root_defaults_under_the_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CR_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("CR_DATA_DIR", str(tmp_path / "data"))
    assert paths.resolve_workspace_root() == tmp_path / "data" / "workspaces"

    # With no CR_DATA_DIR the platform default under the injected data dir is used.
    monkeypatch.delenv("CR_DATA_DIR", raising=False)
    assert paths.resolve_workspace_root() == tmp_path / "app" / "data" / "workspaces"


# --- provisioning ---------------------------------------------------------- #
def test_a_managed_meeting_gets_a_workspace_inside_the_root(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)

    root = tmp_path / "managed"
    assert meeting.workspace_path == str(root / "ops" / "kickoff")
    assert Path(meeting.workspace_path).is_dir()
    assert managed.is_managed(meeting)
    # The workspace is the ordinary shape: a Workspace opens cleanly on it.
    assert Workspace.at(meeting.workspace_path).manifest_path.parent == Path(
        meeting.workspace_path
    )


def test_a_path_escaping_slug_creates_nothing_outside_the_root(
    registry, tmp_path, monkeypatch
) -> None:
    """Containment is checked before mkdir, so nothing escapes the managed root."""
    import dataclasses

    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")
    escaped = dataclasses.replace(meeting, project_slug="../escaped")

    with pytest.raises(managed.UploadRejected, match="outside the managed root"):
        managed.ensure_managed_workspace(registry, escaped)

    assert not (tmp_path / "escaped").exists()
    assert registry.meeting_by_id(meeting.id).workspace_path is None


def test_a_symlinked_managed_root_is_allowed(registry, tmp_path, monkeypatch) -> None:
    """The operator may point the root at a symlink (e.g. a mounted NAS)."""
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "root-link"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(link))
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Kickoff")
    meeting = managed.ensure_managed_workspace(registry, meeting)

    tape = managed.upload_tape(registry, meeting, io.BytesIO(b"RIFF"), filename="a.wav")

    assert Path(tape.path).resolve().is_relative_to(real.resolve())
    assert Path(tape.path).read_bytes() == b"RIFF"


def test_ensure_managed_workspace_is_idempotent(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    again = managed.ensure_managed_workspace(registry, meeting)
    assert again.workspace_path == meeting.workspace_path


def test_a_user_chosen_workspace_is_not_managed(
    registry, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    chosen = tmp_path / "user-docs"
    chosen.mkdir()
    meeting = registry.create_meeting("ops", "Local", workspace_path=str(chosen))

    assert not managed.is_managed(meeting)
    # An upload cannot write into a user document; it names the fix.
    with pytest.raises(managed.UploadRejected, match="user-chosen workspace"):
        managed.upload_tape(registry, meeting, io.BytesIO(b"x"), filename="a.wav")
    assert list(chosen.iterdir()) == []


# --- a successful upload --------------------------------------------------- #
def test_upload_records_a_checksummed_tape_and_the_tape_set(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    payload = b"RIFF-fake-audio-bytes"

    tape = managed.upload_tape(
        registry, meeting, io.BytesIO(payload), filename="take-one.wav"
    )

    assert tape.sha256 == hashlib.sha256(payload).hexdigest()
    assert tape.bytes == len(payload)
    assert Path(tape.path).read_bytes() == payload
    assert Path(tape.path).name == "take-one.wav"
    assert Path(tape.path).parent == Path(meeting.workspace_path) / "tapes"
    # No scratch file survives a success.
    assert not list(Path(meeting.workspace_path, "tapes").glob("*.part"))
    # The tape set the pipeline reads now holds exactly the upload.
    assert registry.latest_recording_set(meeting.id).paths == (tape.path,)
    assert registry.list_tapes(meeting.id) == [tape]


def test_an_uploaded_tape_is_discoverable_audio(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    tape = managed.upload_tape(
        registry, meeting, io.BytesIO(b"RIFF-fake"), filename="a.wav"
    )

    workspace = Workspace.at(meeting.workspace_path)
    assert discover_audio(workspace.root) == [Path(tape.path)]


def test_a_second_upload_of_the_same_name_gets_its_own_file(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    first = managed.upload_tape(registry, meeting, io.BytesIO(b"one"), filename="a.wav")
    second = managed.upload_tape(
        registry, meeting, io.BytesIO(b"two"), filename="a.wav"
    )

    assert Path(first.path) != Path(second.path)
    assert Path(first.path).read_bytes() == b"one"
    assert Path(second.path).read_bytes() == b"two"
    assert registry.latest_recording_set(meeting.id).paths == (
        first.path,
        second.path,
    )


# --- partial and failed uploads -------------------------------------------- #
def test_a_partial_upload_leaves_no_tape(registry, tmp_path, monkeypatch) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)

    with pytest.raises(OSError):
        managed.upload_tape(
            registry,
            meeting,
            _DroppedStream(b"RIFF-fake-audio", fail_after=4),
            filename="broken.wav",
        )

    assert registry.list_tapes(meeting.id) == []
    assert registry.latest_recording_set(meeting.id) is None
    tapes_dir = Path(meeting.workspace_path) / "tapes"
    assert list(tapes_dir.iterdir()) == []


def test_a_failed_registry_write_removes_the_file(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("registry down")

    monkeypatch.setattr(registry, "register_tape", boom)
    with pytest.raises(RuntimeError):
        managed.upload_tape(registry, meeting, io.BytesIO(b"RIFF"), filename="a.wav")

    assert list((Path(meeting.workspace_path) / "tapes").iterdir()) == []


# --- the upload id --------------------------------------------------------- #
def test_a_valid_upload_id_names_the_scratch_file(
    registry, tmp_path, monkeypatch
) -> None:
    """The id is honoured: it is the transfer's identity on disk."""
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    seen: list[str] = []
    real_replace = managed.os.replace

    def record(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(managed.os, "replace", record)
    tape = managed.upload_tape(
        registry, meeting, io.BytesIO(b"RIFF"), filename="a.wav", upload_id="take-01"
    )

    assert seen == [".cr-upload-take-01.part"]
    assert Path(tape.path).read_bytes() == b"RIFF"
    # Success consumed the slot: the id can be used again for a new upload.
    managed.upload_tape(
        registry, meeting, io.BytesIO(b"RIFF"), filename="b.wav", upload_id="take-01"
    )
    assert len(registry.list_tapes(meeting.id)) == 2


def test_no_upload_id_keeps_the_random_scratch_name(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    seen: list[str] = []
    real_replace = managed.os.replace

    def record(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(managed.os, "replace", record)
    managed.upload_tape(registry, meeting, io.BytesIO(b"RIFF"), filename="a.wav")

    assert len(seen) == 1
    assert seen[0].startswith(".cr-upload-")
    assert seen[0].endswith(".part")
    assert seen[0] != ".cr-upload-take-01.part"


@pytest.mark.parametrize(
    "upload_id",
    [
        "",
        "has space",
        "a/b",
        "../escape",
        ".hidden",  # leading dot would make a hidden-name token
        "-leading-dash",
        "x" * 65,
        "caf\u00e9",  # non-ASCII
    ],
)
def test_a_malformed_upload_id_is_refused_before_the_body_is_read(
    registry, tmp_path, monkeypatch, upload_id
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)

    with pytest.raises(managed.InvalidUploadId, match="upload id"):
        managed.upload_tape(
            registry,
            meeting,
            _MustNotBeRead(b"ignored"),
            filename="a.wav",
            upload_id=upload_id,
        )
    assert registry.list_tapes(meeting.id) == []
    assert list(Path(meeting.workspace_path).rglob("*.part")) == []


def test_an_occupied_upload_id_is_refused_as_unsupported(
    registry, tmp_path, monkeypatch
) -> None:
    """An id already on disk cannot be resumed here — refuse, never overwrite."""
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    tapes = Path(meeting.workspace_path) / "tapes"
    tapes.mkdir(parents=True, exist_ok=True)
    partial = tapes / ".cr-upload-take-01.part"
    partial.write_bytes(b"interrupted upload")

    with pytest.raises(managed.ResumeNotSupported, match="cannot resume"):
        managed.upload_tape(
            registry,
            meeting,
            _MustNotBeRead(b"ignored"),
            filename="a.wav",
            upload_id="take-01",
        )

    assert partial.read_bytes() == b"interrupted upload"  # untouched
    assert registry.list_tapes(meeting.id) == []


def test_a_symlink_at_an_upload_id_is_refused_the_same_way(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    tapes = Path(meeting.workspace_path) / "tapes"
    tapes.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"SECRET")
    (tapes / ".cr-upload-take-01.part").symlink_to(secret)

    with pytest.raises(managed.ResumeNotSupported):
        managed.upload_tape(
            registry, meeting, io.BytesIO(b"x"), filename="a.wav", upload_id="take-01"
        )

    assert secret.read_bytes() == b"SECRET"


def test_validate_upload_id_passes_a_token_and_none_through() -> None:
    assert managed.validate_upload_id(None) is None
    assert managed.validate_upload_id("take-01") == "take-01"
    with pytest.raises(managed.InvalidUploadId):
        managed.validate_upload_id("not a token")


# --- guards ---------------------------------------------------------------- #
@pytest.mark.parametrize(
    "filename",
    ["../escape.wav", "sub/dir/a.wav", "/absolute/a.wav", "..", ".", "a\\b.wav"],
)
def test_traversal_and_separators_are_rejected(
    registry, tmp_path, monkeypatch, filename
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    with pytest.raises(managed.UnsafeFilename):
        managed.upload_tape(registry, meeting, io.BytesIO(b"x"), filename=filename)
    assert registry.list_tapes(meeting.id) == []


def test_a_blank_filename_is_rejected(registry, tmp_path, monkeypatch) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    with pytest.raises(managed.UnsafeFilename):
        managed.upload_tape(registry, meeting, io.BytesIO(b"x"), filename="  ")


def test_a_name_over_255_utf8_bytes_is_rejected(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    name = "\u674e" * 100 + ".wav"  # 304 UTF-8 bytes, 104 characters
    assert len(name) <= 255 and len(name.encode("utf-8")) > 255
    with pytest.raises(managed.UnsafeFilename, match="255 bytes"):
        managed.upload_tape(registry, meeting, io.BytesIO(b"x"), filename=name)


def test_a_non_audio_extension_is_rejected(registry, tmp_path, monkeypatch) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    with pytest.raises(managed.DisallowedExtension, match="allowed extensions"):
        managed.upload_tape(registry, meeting, io.BytesIO(b"x"), filename="notes.txt")
    assert registry.list_tapes(meeting.id) == []


def test_an_oversize_upload_is_refused_before_the_body_is_read(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    monkeypatch.setattr(managed, "max_upload_bytes", lambda: 4)
    # A declared size over the cap is refused by the precheck; the body is never
    # touched (the stream raises if it is).
    with pytest.raises(managed.UploadTooLarge, match="CR_MAX_UPLOAD_BYTES"):
        managed.upload_tape(
            registry,
            meeting,
            _MustNotBeRead(b"ignored"),
            filename="a.wav",
            declared_bytes=10**9,
        )
    assert registry.list_tapes(meeting.id) == []


def test_an_oversize_body_is_stopped_mid_stream(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    monkeypatch.setattr(managed, "max_upload_bytes", lambda: 4)
    with pytest.raises(managed.UploadTooLarge):
        managed.upload_tape(
            registry, meeting, io.BytesIO(b"0123456789"), filename="a.wav"
        )
    assert registry.list_tapes(meeting.id) == []
    assert list((Path(meeting.workspace_path) / "tapes").iterdir()) == []


def test_a_full_disk_mid_stream_is_refused_clearly(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)

    def no_space(_fd):
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(managed.os, "fsync", no_space)
    with pytest.raises(managed.InsufficientSpace, match="disk filled"):
        managed.upload_tape(registry, meeting, io.BytesIO(b"RIFF"), filename="a.wav")
    assert registry.list_tapes(meeting.id) == []
    assert list((Path(meeting.workspace_path) / "tapes").iterdir()) == []


def test_low_disk_is_refused_before_the_body_is_read(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    monkeypatch.setattr(
        managed.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 0})(),
    )
    with pytest.raises(managed.InsufficientSpace, match="CR_WORKSPACE_ROOT"):
        managed.upload_tape(
            registry,
            meeting,
            _MustNotBeRead(b"ignored"),
            filename="a.wav",
            declared_bytes=10,
        )
    assert registry.list_tapes(meeting.id) == []


# --- symlinks -------------------------------------------------------------- #
def test_a_symlinked_destination_is_never_followed(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    tapes = Path(meeting.workspace_path) / "tapes"
    tapes.mkdir()
    secret = tmp_path / "secret.wav"
    secret.write_bytes(b"SECRET")
    (tapes / "a.wav").symlink_to(secret)

    tape = managed.upload_tape(registry, meeting, io.BytesIO(b"real"), filename="a.wav")

    assert secret.read_bytes() == b"SECRET"  # the link target is untouched
    assert (tapes / "a.wav").is_symlink()
    assert Path(tape.path).read_bytes() == b"real"
    assert Path(tape.path).name != "a.wav"


def test_a_symlinked_tapes_directory_is_refused(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (Path(meeting.workspace_path) / "tapes").symlink_to(
        elsewhere, target_is_directory=True
    )

    with pytest.raises(managed.UploadRejected, match="symlink"):
        managed.upload_tape(registry, meeting, io.BytesIO(b"x"), filename="a.wav")
    assert list(elsewhere.iterdir()) == []


# --- storage visibility ---------------------------------------------------- #
def test_storage_reports_the_workspace_size_and_the_tapes(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    managed.upload_tape(registry, meeting, io.BytesIO(b"12345"), filename="a.wav")
    managed.upload_tape(registry, meeting, io.BytesIO(b"123"), filename="b.wav")

    storage = managed.meeting_storage(registry, meeting)

    assert storage.managed is True
    assert storage.bytes == 8
    assert [tape.name for tape in storage.tapes] == ["a.wav", "b.wav"]
    assert storage.tapes[0].sha256 == hashlib.sha256(b"12345").hexdigest()


def test_storage_reports_free_space_from_the_guards_accounting(
    registry, tmp_path, monkeypatch
) -> None:
    """One fact, one home: `free_bytes` is the guard's own `root_free_bytes`."""
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    monkeypatch.setattr(
        managed.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 123_456_789})(),
    )

    assert managed.meeting_storage(registry, meeting).free_bytes == 123_456_789


def test_storage_reports_no_free_space_for_a_user_chosen_workspace(
    registry, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    chosen = tmp_path / "user-docs"
    chosen.mkdir()
    meeting = registry.create_meeting("ops", "Local", workspace_path=str(chosen))

    assert managed.meeting_storage(registry, meeting).free_bytes is None


def test_storage_reports_unknown_free_space_rather_than_failing(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)

    def unreadable(_path):
        raise OSError("the disk cannot be measured")

    monkeypatch.setattr(managed.shutil, "disk_usage", unreadable)

    storage = managed.meeting_storage(registry, meeting)

    assert storage.free_bytes is None
    assert storage.managed is True


def test_the_guard_and_the_storage_report_share_root_free_bytes(
    registry, tmp_path, monkeypatch
) -> None:
    """Patching the one function moves the guard and the report together."""
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    calls: list[object] = []

    def no_space(root=None):
        calls.append(root)
        return 0

    monkeypatch.setattr(managed, "root_free_bytes", no_space)

    with pytest.raises(managed.InsufficientSpace):
        managed.precheck_upload(registry, meeting, declared_bytes=1)
    assert managed.meeting_storage(registry, meeting).free_bytes == 0
    assert calls  # both read the seam, neither re-implements it


def test_deleting_a_tape_removes_its_file_and_the_tape_set(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    first = managed.upload_tape(registry, meeting, io.BytesIO(b"one"), filename="a.wav")
    second = managed.upload_tape(
        registry, meeting, io.BytesIO(b"two"), filename="b.wav"
    )

    deleted = managed.delete_tape(registry, meeting, first.id)

    assert deleted == first
    assert not Path(first.path).exists()
    assert Path(second.path).exists()
    assert registry.list_tapes(meeting.id) == [second]
    assert registry.latest_recording_set(meeting.id).paths == (second.path,)

    managed.delete_tape(registry, meeting, second.id)
    assert registry.latest_recording_set(meeting.id) is None


def test_deleting_a_tape_outside_the_managed_root_is_refused(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    outside = tmp_path / "user-tape.wav"
    outside.write_bytes(b"keep me")
    # A managed meeting can still carry a tape row for a path outside the root;
    # deleting that would touch a user document, so it is refused.
    tape = registry.register_tape(
        meeting.id, path=str(outside), sha256="0" * 64, bytes=7
    )
    with pytest.raises(managed.UploadRejected, match="outside the managed root"):
        managed.delete_tape(registry, meeting, tape.id)
    assert outside.read_bytes() == b"keep me"


def test_deleting_from_a_user_chosen_workspace_is_refused(
    registry, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    chosen = tmp_path / "user-docs"
    chosen.mkdir()
    tape_file = chosen / "a.wav"
    tape_file.write_bytes(b"keep me")
    meeting = registry.create_meeting("ops", "Local", workspace_path=str(chosen))
    tape = registry.register_tape(
        meeting.id, path=str(tape_file), sha256="0" * 64, bytes=7
    )
    with pytest.raises(managed.UploadRejected, match="user-chosen workspace"):
        managed.delete_tape(registry, meeting, tape.id)
    assert tape_file.read_bytes() == b"keep me"


# --- the pipeline cannot tell the difference ------------------------------- #
def test_a_managed_and_a_dir_workspace_are_indistinguishable_to_the_stages(
    registry, tmp_path, monkeypatch
) -> None:
    """The same fake pipeline, over both modes, sees the same tape and record.

    A managed meeting's tape sits under ``tapes/``; a ``--dir`` meeting's tape is
    wherever the user put it. The stage reads the tape through the run options
    and writes the record through ``Workspace``, so the *workspace layout* is the
    only difference — and the produced record is byte-identical.
    """
    from clear_record.service.runs import RunManager

    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")

    payload = b"RIFF-the-same-bytes"

    managed_meeting = registry.create_meeting("ops", "Managed")
    managed_meeting = managed.ensure_managed_workspace(registry, managed_meeting)
    managed.upload_tape(
        registry, managed_meeting, io.BytesIO(payload), filename="a.wav"
    )

    dir_ws = tmp_path / "user" / "kickoff"
    dir_ws.mkdir(parents=True)
    (dir_ws / "a.wav").write_bytes(payload)
    dir_meeting = registry.create_meeting("ops", "Dir", workspace_path=str(dir_ws))
    registry.set_recording_set(dir_meeting.id, [str(dir_ws / "a.wav")])

    records: dict[str, str] = {}

    def fake_pipeline(directory, options, on_event) -> None:
        workspace = Workspace.at(directory)
        # The stage receives the tape through the run options, exactly as it
        # would for a user-typed path.
        assert len(options.audio_files) == 1
        digest = hashlib.sha256(Path(options.audio_files[0]).read_bytes()).hexdigest()
        workspace.record_path.write_text(json.dumps({"tape": digest}))
        workspace.segments_path.write_text("{}")
        workspace.export_dir.mkdir(parents=True, exist_ok=True)
        (workspace.export_dir / "record.md").write_text("# record\n")
        records[directory] = workspace.record_path.read_text()

    for meeting in (managed_meeting, dir_meeting):
        manager = RunManager(registry, pipeline=fake_pipeline)
        run = manager.start(meeting, origin="console")
        state = manager.wait(run.id, timeout=10)
        assert state.status == "done"

    assert len(records) == 2
    assert len(set(records.values())) == 1  # identical record content
    kinds = [
        [artifact.kind for artifact in registry.list_artifacts(meeting.id)]
        for meeting in (managed_meeting, dir_meeting)
    ]
    assert kinds[0] == kinds[1] == ["export", "record", "transcript"]


# --- STO-01: the machine total, source vs derived -------------------------- #
def _seed_derived(workspace: Workspace) -> None:
    """Put one file in each derived bucket: audio, export, record, agent run."""
    workspace.audio_dir.mkdir(parents=True, exist_ok=True)
    (workspace.audio_dir / "a.wav").write_bytes(b"a" * 10)
    workspace.export_dir.mkdir(parents=True, exist_ok=True)
    (workspace.export_dir / "record.md").write_text("e" * 7)
    workspace.record_path.write_text("r" * 3)
    run_dir = workspace.root / AGENT_DIRNAME / "minutes-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "output.json").write_text("g" * 4)


def test_machine_storage_counts_every_bucket_and_marks_source_or_derived(
    registry, tmp_path, monkeypatch
) -> None:
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CR_MODELS_DIR", str(tmp_path / "models"))

    managed.upload_tape(registry, meeting, io.BytesIO(b"t" * 100), filename="a.wav")
    workspace = Workspace.at(meeting.workspace_path)
    _seed_derived(workspace)
    chunk = workspace.chunks_dir / "src" / "chunk.json"
    chunk.parent.mkdir(parents=True, exist_ok=True)
    chunk.write_bytes(b"c" * 5)
    models = tmp_path / "models"
    models.mkdir()
    (models / "ggml-base.bin").write_bytes(b"m" * 11)

    storage = managed.machine_storage(registry)
    buckets = {bucket["id"]: bucket for bucket in storage["buckets"]}

    assert [bucket["id"] for bucket in storage["buckets"]] == [
        "tapes",
        "records",
        "audio",
        "exports",
        "agent_runs",
        "chunks",
        "models",
    ]
    assert buckets["tapes"]["kind"] == "source"
    assert buckets["tapes"]["bytes"] == 100
    assert (buckets["tapes"]["label"], buckets["chunks"]["label"]) == (
        "Tapes",
        "Chunk cache",
    )
    assert buckets["models"]["label"] == "Model weights"
    assert buckets["records"]["bytes"] == 3
    assert buckets["audio"]["bytes"] == 10
    assert buckets["exports"]["bytes"] == 7
    assert buckets["agent_runs"]["bytes"] == 4
    assert buckets["chunks"]["bytes"] == 5
    assert buckets["models"]["bytes"] == 11
    for key in ("records", "audio", "exports", "agent_runs", "chunks", "models"):
        assert buckets[key]["kind"] == "derived"
    assert storage["total_bytes"] == 100 + 3 + 10 + 7 + 4 + 5 + 11
    assert storage["unknown"] == []
    assert storage["partial"] is False
    assert all(bucket["partial"] is False for bucket in storage["buckets"])

    (project,) = storage["projects"]
    assert project["slug"] == "ops"
    assert project["total_bytes"] == 100 + 3 + 10 + 7 + 4 + 5  # not the models
    assert project["unknown"] == []
    (row,) = project["workspaces"]
    assert row["managed"] is True
    assert row["path"] == meeting.workspace_path
    # The existing, narrower field keeps its workspace-only meaning.
    assert managed.meeting_storage(registry, meeting).bytes == 100 + 3 + 10 + 7 + 4


def test_machine_storage_measures_a_user_chosen_workspace_and_its_disk(
    registry, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    chosen = tmp_path / "user-docs"
    chosen.mkdir()
    (chosen / "a.wav").write_bytes(b"u" * 40)  # a loose tape: source
    (chosen / "record.json").write_bytes(b"d" * 6)
    registry.create_meeting("ops", "Local", workspace_path=str(chosen))

    storage = managed.machine_storage(registry)
    (project,) = storage["projects"]
    buckets = {bucket["id"]: bucket for bucket in project["buckets"]}

    assert buckets["tapes"]["bytes"] == 40
    assert buckets["records"]["bytes"] == 6
    assert project["total_bytes"] == 46
    (row,) = project["workspaces"]
    assert row["managed"] is False
    assert row["path"] == str(chosen)
    assert row["partial"] is False
    assert storage["unknown"] == []


def test_a_partly_unreadable_workspace_keeps_the_measured_bytes(
    registry, tmp_path, monkeypatch
) -> None:
    """One unreadable workspace must not collapse the whole bucket to zero."""
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    meetings = [
        managed.ensure_managed_workspace(
            registry, registry.create_meeting("ops", title)
        )
        for title in ("Alpha", "Beta", "Gamma")
    ]
    for meeting in meetings:
        managed.upload_tape(registry, meeting, io.BytesIO(b"t" * 10), filename="a.wav")

    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("permission bits cannot be expressed here")
    blocked = Path(meetings[1].workspace_path)
    blocked.chmod(0o000)
    try:
        storage = managed.machine_storage(registry)
    finally:
        blocked.chmod(0o755)

    buckets = {bucket["id"]: bucket for bucket in storage["buckets"]}
    assert buckets["tapes"]["bytes"] == 20  # the two readable workspaces
    assert buckets["tapes"]["partial"] is True  # Beta's tape might be in there
    assert storage["partial"] is True
    assert storage["total_bytes"] == 20  # the measured bytes survive
    (project,) = storage["projects"]
    assert project["partial"] is True
    assert set(project["unknown"]) == {
        "tapes",
        "records",
        "audio",
        "exports",
        "agent_runs",
    }


def test_a_directory_whose_parent_is_unsearchable_is_unknown(
    registry, tmp_path, monkeypatch
) -> None:
    """Path.exists() answers False here; the stat probe says unknown instead."""
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    monkeypatch.setenv("CR_CACHE_DIR", str(tmp_path / "cache"))
    cache_root = Workspace.at(meeting.workspace_path).chunks_dir
    (cache_root / "src").mkdir(parents=True)
    (cache_root / "src" / "chunk.json").write_bytes(b"c" * 5)
    parent = cache_root.parent

    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("permission bits cannot be expressed here")
    parent.chmod(0o000)
    try:
        assert not cache_root.exists()  # the trap the stat probe avoids
        storage = managed.machine_storage(registry)
    finally:
        parent.chmod(0o755)

    buckets = {bucket["id"]: bucket for bucket in storage["buckets"]}
    assert buckets["chunks"]["bytes"] == 0  # nothing readable was lost
    assert buckets["chunks"]["partial"] is True
    assert "chunks" in storage["unknown"]


def test_a_tape_registered_for_two_meetings_counts_once(
    registry, tmp_path, monkeypatch
) -> None:
    """One physical file is one bucket byte, however many rows name it."""
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    shared = tmp_path / "shared.wav"
    shared.write_bytes(b"s" * 700)
    for title in ("First", "Second"):
        meeting = registry.create_meeting("ops", title)
        registry.register_tape(meeting.id, path=str(shared), sha256="0" * 64, bytes=700)

    storage = managed.machine_storage(registry)

    buckets = {bucket["id"]: bucket for bucket in storage["buckets"]}
    assert buckets["tapes"]["bytes"] == 700
    assert storage["total_bytes"] == 700


def test_a_meeting_without_a_workspace_still_counts_its_tapes(
    registry, tmp_path, monkeypatch
) -> None:
    """No workspace is not no storage: registered tapes are still measured."""
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    meeting = registry.create_meeting("ops", "Pathless")
    tape_file = tmp_path / "a.wav"
    tape_file.write_bytes(b"t" * 12)
    registry.register_tape(meeting.id, path=str(tape_file), sha256="0" * 64, bytes=12)

    storage = managed.machine_storage(registry)

    buckets = {bucket["id"]: bucket for bucket in storage["buckets"]}
    assert buckets["tapes"]["bytes"] == 12
    (row,) = storage["projects"][0]["workspaces"]
    assert row["path"] is None


def test_a_workspace_two_meetings_share_is_counted_once(
    registry, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CR_WORKSPACE_ROOT", str(tmp_path / "managed"))
    registry.create_project("Ops")
    shared = tmp_path / "user-docs"
    shared.mkdir()
    (shared / "a.wav").write_bytes(b"u" * 40)
    (shared / "record.json").write_bytes(b"d" * 6)
    for title in ("First", "Second"):
        registry.create_meeting("ops", title, workspace_path=str(shared))

    storage = managed.machine_storage(registry)

    buckets = {bucket["id"]: bucket for bucket in storage["buckets"]}
    assert buckets["tapes"]["bytes"] == 40
    assert buckets["records"]["bytes"] == 6
    assert storage["total_bytes"] == 46


def test_a_symlinked_tape_to_an_outside_file_is_counted_once(
    registry, tmp_path, monkeypatch
) -> None:
    """discover_audio follows a link, so the walk counts its target -- once."""
    meeting = _managed_meeting(registry, monkeypatch, tmp_path)
    workspace = Path(meeting.workspace_path)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"o" * 30)
    (workspace / "linked.wav").symlink_to(outside)
    (workspace / "linked-again.wav").symlink_to(outside)

    buckets = {
        bucket["id"]: bucket for bucket in managed.machine_storage(registry)["buckets"]
    }
    assert buckets["tapes"]["bytes"] == 30  # one target, two links

    # A link pointing inside the workspace must not double its target either.
    real = workspace / "real.wav"
    real.write_bytes(b"r" * 7)
    (workspace / "alias.wav").symlink_to(real)
    buckets = {
        bucket["id"]: bucket for bucket in managed.machine_storage(registry)["buckets"]
    }
    assert buckets["tapes"]["bytes"] == 37
