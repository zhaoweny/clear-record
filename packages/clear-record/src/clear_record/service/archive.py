"""Archive a meeting's tapes and record into an immutable, checksummed copy.

Archiving is the **record of what was received**: the incoming tape set and the
pipeline's derivative artifacts are *copied* — never moved, never overwritten —
into a timestamped directory under the project's archive root, together with an
``archive.json`` manifest that lists every file with its size and ``sha256``.

The archive root is **user-chosen** (ADR-0006/ADR-0007): an explicit argument,
else the project's ``default_archive_root``. There is deliberately no
app-owned fallback — an archive the app invented a home for would not be the
user's document.

Layout::

    <root>/<project_slug>/<YYYYMMDD-HHMMSS>-<meeting_slug>/
        tapes/        the incoming recordings, by original filename
        record/       the pipeline artifacts (record, transcript, exports)
        archive.json  the manifest

A second archive of the same meeting gets a fresh timestamped directory (with a
numeric suffix if it lands in the same second); the first is left untouched.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
from pathlib import Path

from clear_record.service.models import Archive, Meeting
from clear_record.service.store import Registry
from clear_record.service.webhooks import (
    ARCHIVE_CREATED,
    WebhookEmitter,
    default_emitter,
)

#: Section holding the incoming tapes inside an archive.
TAPES_DIRNAME = "tapes"
#: Section holding the pipeline's derivative artifacts inside an archive.
RECORD_DIRNAME = "record"
#: The manifest filename; also the first thing :func:`verify_archive` reads.
MANIFEST_FILENAME = "archive.json"

#: ``kind`` recorded for a manifest entry copied from the tape set.
TAPE_KIND = "tape"


def tool_version() -> str:
    """The installed ``clear-record`` version, or ``"unknown"`` if not installed."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("clear-record")
    except PackageNotFoundError:  # pragma: no cover - source tree without install
        return "unknown"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _timestamp() -> str:
    """Local wall-clock stamp for the archive directory name."""
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_root(
    registry: Registry, meeting: Meeting, root: str | Path | None
) -> Path:
    """The archive root: explicit argument, else the project's default.

    Raises :class:`ValueError` when neither is set: the archive root is a
    user-chosen directory, so it is never invented (ADR-0007).
    """
    if root is not None:
        return Path(root).expanduser().resolve()
    project = registry.require_project(meeting.project_slug)
    if project.default_archive_root:
        return Path(project.default_archive_root).expanduser().resolve()
    raise ValueError(
        f"no archive root for {meeting.project_slug!r}: pass one explicitly or "
        "set the project's default_archive_root"
    )


def _archive_dir(root: Path, meeting: Meeting, timestamp: str) -> Path:
    """A fresh, never-before-used archive directory.

    The timestamp has second resolution, so two archives in the same second are
    disambiguated with a numeric suffix rather than reusing (and thus
    overwriting) the first.
    """
    parent = root / meeting.project_slug
    candidate = parent / f"{timestamp}-{meeting.slug}"
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{timestamp}-{meeting.slug}-{suffix}"
        suffix += 1
    return candidate


def _copy_into(source: Path, dest_dir: Path, used: set[str]) -> Path:
    """Copy one source file into ``dest_dir``, disambiguating a name clash.

    Returns the destination path; ``used`` accumulates the names already taken in
    this directory so two artifacts with the same basename cannot clobber each
    other.
    """
    if not source.is_file():
        raise FileNotFoundError(f"archive source is missing: {source}")
    name = source.name
    if name in used:
        n = 2
        while f"{source.stem}-{n}{source.suffix}" in used:
            n += 1
        name = f"{source.stem}-{n}{source.suffix}"
    used.add(name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    shutil.copy2(source, target)
    return target


def _entry(archive_dir: Path, path: Path, kind: str) -> dict:
    return {
        "path": path.relative_to(archive_dir).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "kind": kind,
    }


def archive_meeting(
    registry: Registry,
    meeting: Meeting,
    root: str | Path | None = None,
    *,
    webhooks: WebhookEmitter | None = None,
) -> Archive:
    """Copy ``meeting``'s tape set and pipeline artifacts into a new archive.

    The tape set is the meeting's latest selection; the artifacts are everything
    the registry knows the pipeline produced. Both are copied into
    ``<root>/<project_slug>/<YYYYMMDD-HHMMSS>-<meeting_slug>/`` and listed in
    ``archive.json``. The archive is recorded in the registry and returned.

    An existing archive is never reused or overwritten; every call makes a new
    directory. A name that a concurrent archive won between the uniqueness probe
    and the mkdir is retried, never touched. On any failure only the partial
    directory *this call* created is removed, so a registry row always points at
    a complete archive and a concurrent winner survives.

    ``webhooks`` defaults to the shared config-driven emitter; ``archive.created``
    is emitted only after the archive is complete, and delivery (off-thread)
    can never fail the archive.
    """
    root_path = _resolve_root(registry, meeting, root)

    tape_set = registry.latest_recording_set(meeting.id)
    if tape_set is None:
        raise ValueError(
            f"meeting {meeting.project_slug}/{meeting.slug} has no tape set to archive"
        )
    artifacts = registry.list_artifacts(meeting.id)

    archive_dir = _archive_dir(root_path, meeting, _timestamp())
    manifest_path = archive_dir / MANIFEST_FILENAME
    created = False
    try:
        # The uniqueness probe and this mkdir are not atomic: a concurrent
        # archive can win the same timestamped name in between. Retry with the
        # next free name rather than touching the winner's directory.
        while True:
            try:
                archive_dir.mkdir(parents=True, exist_ok=False)
                created = True
                break
            except FileExistsError:
                archive_dir = _archive_dir(root_path, meeting, _timestamp())
                manifest_path = archive_dir / MANIFEST_FILENAME

        files: list[dict] = []
        used_tapes: set[str] = set()
        for tape in tape_set.paths:
            target = _copy_into(Path(tape), archive_dir / TAPES_DIRNAME, used_tapes)
            files.append(_entry(archive_dir, target, TAPE_KIND))

        used_record: set[str] = set()
        for artifact in artifacts:
            target = _copy_into(
                Path(artifact.path), archive_dir / RECORD_DIRNAME, used_record
            )
            files.append(_entry(archive_dir, target, artifact.kind))

        manifest = {
            "tool": "clear-record",
            "tool_version": tool_version(),
            "created_at": _now(),
            "project_id": meeting.project_id,
            "project_slug": meeting.project_slug,
            "meeting_id": meeting.id,
            "meeting_slug": meeting.slug,
            "recording_set_id": tape_set.id,
            "run_ids": sorted({a.run_id for a in artifacts if a.run_id is not None}),
            "artifact_ids": [a.id for a in artifacts],
            "files": files,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        manifest_sha256 = _sha256(manifest_path)
    except Exception:
        if created:
            shutil.rmtree(archive_dir, ignore_errors=True)
        raise

    archive = registry.add_archive(
        meeting.id,
        meeting.project_id,
        root_path=str(archive_dir),
        manifest_path=str(manifest_path),
        manifest_sha256=manifest_sha256,
    )
    # Announced only after the archive is complete; delivery is off-thread, so it
    # can never fail the archive.
    emitter = webhooks if webhooks is not None else default_emitter()
    emitter.emit(ARCHIVE_CREATED, project_id=meeting.project_id, meeting_id=meeting.id)
    return archive


def verify_archive(archive_dir: str | Path) -> dict:
    """Re-read an archive's manifest and re-check every file against it.

    Returns ``{"ok", "missing", "mismatched", "checked", "archive"}``: ``ok`` is
    true only when every listed file is present with its recorded size and
    ``sha256``. A missing manifest raises :class:`FileNotFoundError` — there is
    nothing to verify.
    """
    archive_dir = Path(archive_dir)
    manifest_path = archive_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no archive manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    missing: list[str] = []
    mismatched: list[str] = []
    checked = 0
    for entry in manifest.get("files", []):
        relative = entry["path"]
        target = archive_dir / relative
        checked += 1
        if not target.is_file():
            missing.append(relative)
        elif (
            target.stat().st_size != entry["bytes"]
            or _sha256(target) != entry["sha256"]
        ):
            mismatched.append(relative)

    return {
        "ok": not missing and not mismatched,
        "missing": missing,
        "mismatched": mismatched,
        "checked": checked,
        "archive": str(archive_dir),
    }


__all__ = [
    "MANIFEST_FILENAME",
    "RECORD_DIRNAME",
    "TAPES_DIRNAME",
    "archive_meeting",
    "tool_version",
    "verify_archive",
]
