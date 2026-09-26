"""A managed workspace: app-owned tapes, guarded uploads and storage (ADR-0024).

Two workspace modes share one abstraction. A *user-chosen* workspace (the CLI's
``--dir`` and a meeting's ``workspace_path``) stays the user's document
(ADR-0007). A *managed* workspace is app-owned **additionally**: the console can
provision it, receive tapes into it and transcribe them, so a self-hosted node
does not need the user to place multi-GB files by hand first. To the pipeline the
two are indistinguishable — a managed meeting is just a meeting whose
``workspace_path`` points inside the managed root, and its uploaded tapes are
input recordings discovered and run exactly like user-typed paths.

The managed root itself lives in :mod:`clear_record.core.paths` (the one
platformdirs-backed resolver, ``CR_WORKSPACE_ROOT`` override, default
``<data>/workspaces/``).

Upload is a **write surface**, so every guard is here and each failure is a
:class:`UploadRejected` (a user-facing message, never a traceback):

- the requested filename must be a bare name (no traversal, no absolute path,
  no separators);
- its extension must be audio, by the pipeline's one allow-list
  (:func:`clear_record.pipeline.workspace.is_audio`);
- the declared size must be within ``CR_MAX_UPLOAD_BYTES``;
- free space on the managed root is checked **before** the body is read;
- no symlink is followed — the destination directory is verified to resolve
  inside the managed root, and the ``.part`` file is created ``O_EXCL |
  O_NOFOLLOW``.

An upload may carry a client-supplied **upload id**: a bare, bounded token that
names the transfer. It is validated and honoured as the ``.part`` scratch file's
identity, so a resumable layer can be added later without changing the request's
shape. **Resuming itself is not built**: a fresh id still starts at zero, and an
id whose ``.part`` is already on disk (an interrupted or in-flight upload) is
refused as explicitly unsupported, with an actionable message.

The stream is copied in fixed-size blocks to a ``.part`` file beside its
destination, ``fsync``-ed, then atomically renamed; only then is the tape
recorded (with its ``sha256`` and size). A partial or failed upload never
becomes a tape.

**Limitation (deliberate, this slice):** the POST is a single stream. A dropped
multi-GB upload restarts from zero — resumable upload is out of scope until it
is asked for; the upload id is the seam that layer will use.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import BinaryIO

from clear_record.pipeline.workspace import (
    AUDIO_DIR,
    AUDIO_SUFFIXES,
    EXPORT_DIR,
    RUN_MARKER,
    RUNS_DIR,
    Workspace,
    is_audio,
)
from clear_record.core.i18n import deferred
from clear_record.core.paths import resolve_models_dir, resolve_workspace_root
from clear_record.service import audit, tapestore
from clear_record.service.agent_review import AGENT_DIRNAME
from clear_record.service.archive import verify_archive
from clear_record.service.models import Archive, Meeting, Tape
from clear_record.service.schemas import Shape
from clear_record.service.store import Registry

#: Upload cap when ``CR_MAX_UPLOAD_BYTES`` is unset (8 GiB).
DEFAULT_MAX_UPLOAD_BYTES = 8 * 1024**3

#: Free space left untouched when checking the disk before a transfer.
DISK_HEADROOM_BYTES = 64 * 1024**2

#: Multipart framing (boundaries, part headers, the field name) beyond the file
#: body; a declared ``Content-Length`` is checked against the cap with this much
#: slack so framing never rejects a file that is itself within the cap.
_FORM_OVERHEAD_BYTES = 1 << 20

#: Longest client-supplied upload id accepted. The id is a bare token, never a
#: path: ``[A-Za-z0-9][A-Za-z0-9._-]*`` up to this length, so it can name the
#: scratch file for a future resume layer without escaping the tapes directory.
MAX_UPLOAD_ID_LENGTH = 64
_UPLOAD_ID_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_UPLOAD_ID_LENGTH - 1}}}"
)

#: The in-flight scratch file: ``.cr-upload-<token>.part``. The ``.part`` suffix
#: keeps it out of ``discover_audio``, and the prefix marks it as ours.
_SCRATCH_PREFIX = ".cr-upload-"
_SCRATCH_SUFFIX = ".part"

_READ_BLOCK = 1 << 20
#: The storage buckets STO-01's machine total is made of, in display order:
#: ``id``, the English label (marked for the catalog with ``deferred``; the
#: template translates it at render time) and the kind: ``source`` is the
#: irreplaceable tape, ``derived`` is recomputable from it. ``models`` is
#: machine-wide (every project decodes with the same weights), so it is counted
#: once in the machine total and left out of a project's own total.
STORAGE_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ("tapes", deferred("Tapes"), "source"),
    ("records", deferred("Transcripts and records"), "derived"),
    ("audio", deferred("Audio copies"), "derived"),
    ("exports", deferred("Exports"), "derived"),
    ("agent_runs", deferred("Agent run directories"), "derived"),
    ("chunks", deferred("Chunk cache"), "derived"),
    ("models", deferred("Model weights"), "derived"),
)

#: Buckets that belong to the machine, not to one project.
MACHINE_BUCKETS = frozenset({"models"})

#: Every bucket a meeting's workspace can hold (everything but the models).
WORKSPACE_BUCKETS: tuple[str, ...] = tuple(
    key for key, _label, _kind in STORAGE_BUCKETS if key not in MACHINE_BUCKETS
)

#: The workspace subdirectories that name their own bucket during the walk;
#: anything else is a source tape (input audio) or the meeting's record.
_BUCKET_DIRS = {
    AUDIO_DIR: "audio",
    EXPORT_DIR: "exports",
    AGENT_DIRNAME: "agent_runs",
}


class UploadRejected(ValueError):
    """An upload the guards refused. ``str(exc)`` is the English message.

    The message is a **stable ID plus parameters** — :func:`deferred` marks the
    ID for the catalog — so a presentation boundary can render it in the user's
    locale with ``tr(exc.msgid, **exc.params)``. ``str(exc)`` stays the English
    form, which is what machine surfaces (the JSON API, logs) keep.
    """

    def __init__(self, msgid: str, **params: object) -> None:
        self.msgid = msgid
        self.params = params
        super().__init__(msgid.format(**params) if params else msgid)


class UnsafeFilename(UploadRejected):
    """The requested filename is not a bare name (traversal/absolute/separator)."""


class DisallowedExtension(UploadRejected):
    """The requested filename is not an audio file per ``workspace.is_audio``."""


class UploadTooLarge(UploadRejected):
    """The upload exceeds ``CR_MAX_UPLOAD_BYTES``."""


class InsufficientSpace(UploadRejected):
    """The managed root has too little free space for the declared upload."""


class InvalidUploadId(UploadRejected):
    """The client's upload id is not a valid bare token."""


class ResumeNotSupported(UploadRejected):
    """The id names an interrupted/in-flight upload, and resume is not built."""


class ArchiveRequired(UploadRejected):
    """A managed tape cannot be deleted: the meeting has no verified archive.

    The delete would destroy data with no durable copy, so it is refused with a
    message naming the archive action instead (ADR-0033).
    """


def managed_root(explicit: str | os.PathLike | None = None) -> Path:
    """The managed workspace root (see :func:`paths.resolve_workspace_root`)."""
    return resolve_workspace_root(explicit)


def is_managed(meeting: Meeting, root: str | os.PathLike | None = None) -> bool:
    """True when the meeting's workspace is inside the managed root."""
    if not meeting.workspace_path:
        return False
    return _within(Path(meeting.workspace_path), managed_root(root))


def workspace_path_for(root: Path, meeting: Meeting) -> Path:
    """Where a managed meeting's workspace lives: ``<root>/<project>/<meeting>``.

    Both parts are registry slugs ([a-z0-9-]): the store validates an explicit
    slug, so the path cannot escape the root by construction. _safe_mkdir still
    re-checks containment before it creates anything.
    """
    return Path(root) / meeting.project_slug / meeting.slug


def ensure_managed_workspace(
    registry: Registry,
    meeting: Meeting,
    root: str | os.PathLike | None = None,
    *,
    actor: str,
) -> Meeting:
    """Create the meeting's managed workspace and point the meeting at it.

    Idempotent: an already-provisioned managed meeting keeps its directory. The
    shape is the ordinary :class:`~clear_record.pipeline.workspace.Workspace` shape,
    so nothing downstream can tell it apart.

    ``actor`` is the surface this provisioning is done for, and is recorded
    against the meeting it points at (ADR-0033): pointing a meeting at a
    workspace is a registry write like any other.
    """
    resolved_root = managed_root(root)
    path = workspace_path_for(resolved_root, meeting)
    _safe_mkdir(path, root=resolved_root)
    if meeting.workspace_path != str(path):
        meeting = registry.set_meeting_workspace(meeting.id, str(path), actor=actor)
    return meeting


def root_free_bytes(root: str | os.PathLike | None = None) -> int:
    """Free bytes on the managed root.

    The **one accounting** of this fact: the upload guard checks it before the
    body is read, and :func:`meeting_storage` reports it for the console, so a
    panel can never say there is room while the guard refuses. Creates the root
    when it is missing (the guard needs it to exist to measure it) and lets an
    ``OSError`` propagate when the filesystem cannot report it; a *reporting*
    caller turns that into "unknown" rather than a failure.
    """
    resolved_root = managed_root(root)
    resolved_root.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(resolved_root).free


def precheck_upload(
    registry: Registry,
    meeting: Meeting,
    declared_bytes: int | None = None,
    root: str | os.PathLike | None = None,
    upload_id: str | None = None,
    *,
    actor: str,
) -> Meeting:
    """Refuse an upload *before* its body is read, where the guard can.

    ``declared_bytes`` is the request's ``Content-Length`` (the whole multipart
    body, an upper bound on the file). It is checked against the cap and against
    free space on the managed root. ``upload_id`` is the client's optional
    upload id: it is validated here, and an id whose scratch file is already on
    disk is refused as unsupported (resume is not built). Returns the (possibly
    newly provisioned) meeting so the caller need not resolve it twice — and
    ``actor`` is the upload's surface, recorded if provisioning turns out to be
    needed (ADR-0033).
    """
    meeting = _upload_workspace(registry, meeting, root, actor=actor)
    token = validate_upload_id(upload_id)
    if token is not None:
        _refuse_occupied_upload_id(meeting, token)
    cap = max_upload_bytes()
    if declared_bytes is None:
        return meeting
    if declared_bytes > cap + _FORM_OVERHEAD_BYTES:
        raise _too_large(declared_bytes, cap)
    resolved_root = managed_root(root)
    # The root is operator-chosen, so a symlink there is allowed (it is not a
    # component the upload creates); the *destination* dirs are what is checked.
    free = root_free_bytes(resolved_root)
    needed = declared_bytes + DISK_HEADROOM_BYTES
    if free < needed:
        raise _no_space(free, needed, resolved_root)
    return meeting


def upload_tape(
    registry: Registry,
    meeting: Meeting,
    stream: BinaryIO,
    *,
    actor: str,
    filename: str | None,
    declared_bytes: int | None = None,
    root: str | os.PathLike | None = None,
    upload_id: str | None = None,
) -> Tape:
    """Stream one uploaded tape to the meeting's managed workspace.

    Guards run first (the upload id, the declared size, free space, then the
    filename and extension). The body is
    then copied in blocks to a sibling ``.part`` file, ``fsync``-ed and
    atomically renamed; the tape is recorded — checksum and size — only after
    the rename. Any failure (a truncated body included) removes the partial file
    and leaves no tape behind.

    ``upload_id``, when given, names that scratch file, so it is the transfer's
    identity for a future resume layer. This slice still starts every upload at
    zero: an id whose scratch file exists is refused by the precheck.
    """
    meeting = precheck_upload(
        registry, meeting, declared_bytes, root, upload_id, actor=actor
    )
    assert meeting.workspace_path is not None  # precheck provisioned it
    token = validate_upload_id(upload_id)
    name = sanitize_filename(filename)
    if not is_audio(Path(name)):
        raise DisallowedExtension(
            deferred("{name!r} is not an audio tape; allowed extensions: {extensions}"),
            name=name,
            extensions=", ".join(sorted(AUDIO_SUFFIXES)),
        )

    resolved_root = managed_root(root)
    workspace = Path(meeting.workspace_path)
    _require_within(workspace, resolved_root)
    tapes = _safe_mkdir(tapestore.tapes_dir(workspace), root=resolved_root)
    # `tapestore.unique_path` never hands back a symlinked name (its own
    # on-disk check treats one as taken), so there is no reachable symlink
    # guard to restate here.
    target = tapestore.unique_path(tapes, name)

    cap = max_upload_bytes()
    # A client id names its own scratch file (so a resume layer can find it); a
    # request without one gets a short random name that cannot collide with a
    # sibling upload. Either way the name is not the tape name, which may be near
    # the filesystem's 255-byte limit.
    part = _scratch_path(tapes, token)
    digest = hashlib.sha256()
    written = 0
    created = False
    replaced = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(part, flags, 0o600)
        except FileExistsError as exc:
            # Only a client id can land here (a random name cannot collide): the
            # slot holds an interrupted or in-flight upload, which this node
            # cannot resume. The existing file is left untouched.
            if token is not None:
                raise _resume_not_supported(token) from exc
            raise
        created = True
        with os.fdopen(fd, "wb") as handle:
            while True:
                block = stream.read(_READ_BLOCK)
                if not block:
                    break
                written += len(block)
                if written > cap:
                    raise _too_large(written, cap)
                digest.update(block)
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, target)
        replaced = True
        _fsync_dir(tapes)
        return registry.register_tape(
            meeting.id,
            actor=actor,
            path=str(target),
            sha256=digest.hexdigest(),
            bytes=written,
        )
    except OSError as exc:
        if created:
            part.unlink(missing_ok=True)
        if replaced:
            target.unlink(missing_ok=True)
        if exc.errno == errno.ENOSPC:
            # The disk filled between the precheck and now (a race, or a body
            # whose size was not declared). Still a clear refusal, not a 500.
            raise InsufficientSpace(
                deferred(
                    "the disk filled while writing the upload; free space or point "
                    "CR_WORKSPACE_ROOT at a larger disk"
                )
            ) from exc
        raise
    except BaseException:
        if created:
            part.unlink(missing_ok=True)
        if replaced:
            target.unlink(missing_ok=True)
        raise


def workspace_usage(meeting: Meeting) -> int:
    """Total bytes under the meeting's workspace; symlinks are never followed."""
    if not meeting.workspace_path:
        return 0
    root = Path(meeting.workspace_path)
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:  # pragma: no cover - a file raced away
            continue
    return total


class StorageTape(Shape):
    """One of a meeting's uploaded tapes as the storage panel publishes it."""

    id: int
    path: str
    name: str
    sha256: str
    bytes: int


class MeetingStorage(Shape):
    """A meeting's workspace size, its uploaded tapes and the root's free space.

    Declared where it is computed (ADR-0030), because both surfaces read it: the
    console's panel and `/api/meetings/{id}/storage`.
    """

    workspace_path: str | None
    managed: bool
    managed_root: str
    bytes: int
    free_bytes: int | None
    tapes: list[StorageTape]


def meeting_storage(
    registry: Registry,
    meeting: Meeting,
    root: str | os.PathLike | None = None,
) -> MeetingStorage:
    """The meeting's workspace size, its uploaded tapes and the root's free space.

    ``free_bytes`` is the same accounting the upload guard checks (one function,
    :func:`root_free_bytes`), so the console's panel and the guard cannot drift;
    it is ``None`` when the root cannot report it, or when the meeting is not
    managed (its disk is not this app's concern).

    ``bytes`` is deliberately the **workspace alone** (the machine-wide surface
    is :func:`machine_storage`, whose rows are workspace + that workspace's
    chunk cache + the models); do not "fix" the two into one number.
    """
    resolved_root = managed_root(root)
    managed_here = is_managed(meeting, resolved_root)
    free_bytes: int | None = None
    if managed_here:
        try:
            free_bytes = root_free_bytes(resolved_root)
        except OSError:  # pragma: no cover - a root that cannot be measured
            free_bytes = None
    return MeetingStorage(
        workspace_path=meeting.workspace_path,
        managed=managed_here,
        managed_root=str(resolved_root),
        bytes=workspace_usage(meeting),
        free_bytes=free_bytes,
        tapes=[
            StorageTape(
                id=tape.id,
                path=tape.path,
                name=Path(tape.path).name,
                sha256=tape.sha256,
                bytes=tape.bytes,
            )
            for tape in registry.list_tapes(meeting.id)
        ],
    )


def machine_storage(
    registry: Registry,
    root: str | os.PathLike | None = None,
) -> dict:
    """STO-01's one accounting: the machine total with a per-project breakdown.

    Every byte the app holds is in one of ``STORAGE_BUCKETS``, each marked
    **source** (the tape, irreplaceable) or **derived** (recomputable). The
    chunk cache (app-owned, outside every workspace) and the model directory are
    **inside** the total; a *user-chosen* workspace is measured exactly like a
    managed one, and its row says which disk it is on (``managed``).

    Each bucket carries the bytes it could **measure** plus a ``partial`` flag:
    a component that cannot be read contributes whatever was readable and is
    named in ``unknown``, so ``total_bytes`` stays a true lower bound (the
    console shows it as ">=") instead of collapsing to zero. One physical file
    is counted once per call, so a tape registered for two meetings, a symlink
    beside its target, or a workspace two meetings share cannot double count.

    ``meeting_storage``'s ``bytes`` keeps its original, narrower meaning (the
    meeting workspace's own bytes); this is a separate, wider surface.
    """
    resolved_root = managed_root(root)
    machine: dict[str, _Measure] = {key: _ZERO for key, _l, _k in STORAGE_BUCKETS}
    seen: set[str] = set()
    projects: list[dict] = []
    for project in registry.list_projects():
        measured: dict[str, _Measure] = {key: _ZERO for key in WORKSPACE_BUCKETS}
        workspaces: list[dict] = []
        for meeting in registry.list_meetings(project.slug):
            one = _meeting_buckets(registry, meeting, seen)
            for key, value in one.items():
                measured[key] = _add(measured[key], value)
                machine[key] = _add(machine[key], value)
            known = _sum_known(one)
            workspaces.append(
                {
                    "meeting": meeting.title,
                    "path": meeting.workspace_path,
                    "managed": is_managed(meeting, resolved_root),
                    "total_bytes": known,
                    "total_size": _human_bytes(known),
                    "partial": bool(_unknown(one)),
                }
            )
        project_total = _sum_known(measured)
        projects.append(
            {
                "slug": project.slug,
                "name": project.name,
                "buckets": _bucket_rows(measured),
                "total_bytes": project_total,
                "total_size": _human_bytes(project_total),
                "unknown": _unknown(measured),
                "partial": bool(_unknown(measured)),
                "workspaces": workspaces,
            }
        )
    machine["models"] = _dir_bytes(resolve_models_dir(), seen)
    total = _sum_known(machine)
    unknown = _unknown(machine)
    return {
        "buckets": _bucket_rows(machine),
        "total_bytes": total,
        "total_size": _human_bytes(total),
        "unknown": unknown,
        "partial": bool(unknown),
        "projects": projects,
    }


@dataclasses.dataclass(frozen=True)
class TapeDeletion:
    """A deleted tape and the verified archive that made the delete safe."""

    tape: Tape
    archive: Archive


def durable_archive(registry: Registry, meeting: Meeting) -> Archive:
    """The meeting's **verified** archive: the durable copy a delete leans on.

    The newest archive (the registry lists them newest first) that verifies is
    the one named; an archive whose manifest is gone, or whose files no longer
    match their recorded sizes/digests, does not count. Raises
    :class:`ArchiveRequired` when none verifies — the message names the archive
    action, since archiving is what makes the delete reconstructible.
    """
    for archive in registry.list_archives(meeting.id):
        try:
            verification = verify_archive(archive.root_path)
        except FileNotFoundError:
            continue
        if verification.ok:
            return archive
    raise ArchiveRequired(
        deferred(
            "this meeting has no verified archive; archive the meeting first, "
            "then delete its tapes"
        )
    )


def delete_tapes(
    registry: Registry,
    meeting: Meeting,
    tape_ids: list[int],
    root: str | os.PathLike | None = None,
    *,
    actor: str,
) -> list[TapeDeletion]:
    """Delete **managed** tapes' files and records, when that is reversible.

    Only tapes inside the meeting's managed workspace may be deleted: a
    user-chosen path is the user's document, not app-owned data (ADR-0007). And
    every delete stands on a durable copy: the meeting must have a **verified**
    archive, or the whole batch is refused with a message naming the archive
    action and nothing is unlinked (ADR-0033). The durable copy is one archive
    for the meeting, so it is verified **once**, and every precondition — the
    actor's own gate included — passes before the first unlink: a bad id, or a
    word the record cannot attribute a row to, refuses the batch whole with the
    filesystem untouched.

    ``actor`` is the transport's word for the surface that asked for the
    deletion. Each tape's own row is dropped against it, and the two refusals
    below — a user-chosen workspace, or no verified archive for the meeting —
    each leave one failed ``tape.forget`` row for the meeting (the batch's
    subject: one refusal covers every id asked for), so a tape that is gone is
    still accounted for (ADR-0033). The containment guard's refusal (a tape
    resolving outside the managed root) is not one of them: it names the path it
    refuses and leaves no row.
    """
    audit.require_actor(actor)
    tapes: list[Tape] = []
    for tape_id in tape_ids:
        tape = registry.get_tape(tape_id)
        if tape is None or tape.meeting_id != meeting.id:
            raise KeyError(tape_id)
        tapes.append(tape)
    resolved_root = managed_root(root)
    if not is_managed(meeting, resolved_root):
        # A policy answer, not a key miss, so the refusal leaves a row — one for
        # the batch, naming the meeting whose tapes were refused (ADR-0033).
        registry.record_audit(
            actor,
            "tape.forget",
            f"meeting:{meeting.id}",
            outcome=audit.FAILED,
        )
        raise UploadRejected(
            deferred(
                "this meeting uses a user-chosen workspace; only a managed tape can "
                "be deleted here"
            )
        )
    for tape in tapes:
        _require_within(Path(tape.path), resolved_root)
    try:
        archive = durable_archive(registry, meeting)
    except ArchiveRequired:
        # A policy answer like the workspace refusal above, and the one that
        # guards the destroy: the refusal leaves a row (ADR-0033) — one for the
        # batch, naming the meeting whose archive is missing.
        registry.record_audit(
            actor,
            "tape.forget",
            f"meeting:{meeting.id}",
            outcome=audit.FAILED,
        )
        raise
    deletions: list[TapeDeletion] = []
    for tape in tapes:
        Path(tape.path).unlink(missing_ok=True)
        deletions.append(
            TapeDeletion(
                tape=registry.forget_tape(tape.id, actor=actor), archive=archive
            )
        )
    return deletions


def delete_tape(
    registry: Registry,
    meeting: Meeting,
    tape_id: int,
    root: str | os.PathLike | None = None,
    *,
    actor: str,
) -> TapeDeletion:
    """Delete one managed tape; see :func:`delete_tapes` for the rule.

    ``actor`` is the transport's word for the surface that asked, and every
    drop this delegates to is recorded against it (ADR-0033).
    """
    return delete_tapes(registry, meeting, [tape_id], root, actor=actor)[0]


def sanitize_filename(filename: str | None) -> str:
    """Return a bare audio filename, or raise :class:`UnsafeFilename`.

    Rejects an empty name, ``.``/``..``, anything with a path separator (so no
    relative traversal and no absolute path) and control characters. The
    survivors are single components that cannot escape their directory.
    """
    name = (filename or "").strip()
    if not name:
        raise UnsafeFilename(
            deferred(
                "the upload has no filename; attach the tape as a file part named "
                "'file' with a name ending in an audio extension"
            )
        )
    if name in {".", ".."} or ".." in Path(name).parts:
        raise UnsafeFilename(
            deferred(
                "refusing the filename {name!r}: it contains a path-traversal component"
            ),
            name=name,
        )
    if (
        "/" in name
        or "\\" in name
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
    ):
        raise UnsafeFilename(
            deferred(
                "refusing the filename {name!r}: a tape name must be a bare "
                "filename, with no directory or path separator"
            ),
            name=name,
        )
    if any(ord(char) < 32 for char in name):
        raise UnsafeFilename(
            deferred("refusing the filename {name!r}: it contains a control character"),
            name=name,
        )
    if len(name.encode("utf-8")) > 255:
        raise UnsafeFilename(
            deferred("refusing the filename {name!r}: it is longer than 255 bytes"),
            name=name,
        )
    return name


def validate_upload_id(upload_id: str | None) -> str | None:
    """Return a well-formed client upload id, or ``None`` when none was sent.

    The id is a **bare token**, never a path: it must match
    :data:`MAX_UPLOAD_ID_LENGTH`-bounded ``[A-Za-z0-9][A-Za-z0-9._-]*``. An id
    is optional — the console sends none — but a malformed one is refused rather
    than silently ignored, because the whole point of accepting one is that a
    resumable layer can later trust it.
    """
    if upload_id is None:
        return None
    if _UPLOAD_ID_RE.fullmatch(upload_id) is None:
        raise InvalidUploadId(
            deferred(
                "the upload id {id!r} is not valid: use 1-{max_length} characters "
                "from A-Z, a-z, 0-9, dot, underscore or hyphen, starting with a "
                "letter or digit"
            ),
            id=upload_id,
            max_length=MAX_UPLOAD_ID_LENGTH,
        )
    return upload_id


def max_upload_bytes() -> int:
    """The upload cap: ``CR_MAX_UPLOAD_BYTES`` bytes, else 8 GiB.

    An unset, non-integer or non-positive value falls back to the default so a
    typo cannot silently remove the cap.
    """
    raw = os.environ.get("CR_MAX_UPLOAD_BYTES")
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES
    return value if value > 0 else DEFAULT_MAX_UPLOAD_BYTES


# --- internals ------------------------------------------------------------- #
def _upload_workspace(
    registry: Registry,
    meeting: Meeting,
    root: str | os.PathLike | None,
    *,
    actor: str,
) -> Meeting:
    """The managed workspace an upload may write into.

    No workspace yet -> provision one. A user-chosen workspace (outside the
    managed root) is refused: the app writes these files, so they must not land
    in a user document (ADR-0007's rule still holds).
    """
    if not meeting.workspace_path:
        return ensure_managed_workspace(registry, meeting, root, actor=actor)
    if not is_managed(meeting, root):
        raise UploadRejected(
            deferred(
                "this meeting uses a user-chosen workspace, which has no managed "
                "place to upload to; create a managed meeting, or set the tapes by "
                "path instead"
            )
        )
    _safe_mkdir(Path(meeting.workspace_path), root=managed_root(root))
    return meeting


def _within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:  # pragma: no cover - a path that cannot be resolved
        return False


def _require_within(path: Path, root: Path) -> None:
    if not _within(path, root):
        raise UploadRejected(
            deferred(
                "refusing to use {path}: it resolves outside the managed root "
                "{root} (a symlink?)"
            ),
            path=str(path),
            root=str(root),
        )


def _safe_mkdir(path: Path, *, root: Path | None = None) -> Path:
    """mkdir -p that refuses to use (or follow) a symlink at path.

    Containment is checked *before* the directory is created, so a path that
    escapes the root (an unvalidated slug, an absolute path) creates nothing
    outside it; the check is repeated after creation to close the
    symlink-race window.
    """
    if root is not None:
        _require_within(path, root)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise UploadRejected(
            deferred("refusing to use {path}: it is a symlink"), path=str(path)
        )
    if root is not None:
        _require_within(path, root)
    return path


def _scratch_path(tapes_dir: Path, upload_id: str | None) -> Path:
    """The in-flight scratch file for one upload.

    A client id names its own scratch file (the id's identity on disk, for a
    future resume layer); a request without one gets a short random name that
    cannot collide with a sibling upload.
    """
    token = upload_id if upload_id is not None else secrets.token_hex(8)
    return tapes_dir / f"{_SCRATCH_PREFIX}{token}{_SCRATCH_SUFFIX}"


def _refuse_occupied_upload_id(meeting: Meeting, upload_id: str) -> None:
    """Refuse an id whose scratch file already exists.

    An id names one transfer. A file already at that slot is an interrupted or
    in-flight upload, and this node cannot resume either, so it is refused with
    an actionable message rather than overwritten or silently restarted. A
    symlink at the slot is refused the same way (and the write stays
    ``O_EXCL | O_NOFOLLOW`` regardless).
    """
    if not meeting.workspace_path:
        return
    workspace = Path(meeting.workspace_path)
    partial = _scratch_path(tapestore.tapes_dir(workspace), upload_id)
    if partial.is_symlink() or partial.exists():
        raise _resume_not_supported(upload_id)


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync so the rename is durable across a crash."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform without directory fsync
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - e.g. a filesystem that refuses it
        pass
    finally:
        os.close(fd)


#: One measured component: the bytes that could be read, plus whether any part
#: of it could not. (0, True) is "unknown", never a silent zero.
_Measure = tuple[int, bool]

_ZERO: _Measure = (0, False)


def _identity(path: Path) -> str:
    """A stable key for one physical file (its resolved path, else absolute)."""
    try:
        return str(path.resolve())
    except OSError:  # pragma: no cover - a path that cannot be resolved
        return str(path.absolute())


def _scan(root: Path) -> tuple[list[tuple[Path, int, str]], bool]:
    """Every file under *root*, its size and its identity, plus a partial flag.

    The identity is the **resolved** path, so one physical file reached twice
    (two links, a link and its target, two meetings) is counted once. A symlink
    pointing back inside *root* is skipped -- the walk reaches its target
    itself -- while a link to a target outside *root* is a file the pipeline
    can read (discover_audio follows it), so it is counted under the target's
    identity. Directory symlinks are never followed.

    The flag is True when a part of the tree could not be read (a permission
    error, a mount gone away): the caller marks the bucket partial rather than
    letting the missing bytes vanish silently.
    """
    files: list[tuple[Path, int, str]] = []
    unreadable = False

    def _note_error(_exc: OSError) -> None:
        nonlocal unreadable
        unreadable = True

    for dirpath, _dirnames, filenames in os.walk(
        root, onerror=_note_error, followlinks=False
    ):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.is_symlink() and _within(path.resolve(), root):
                    continue  # the walk reaches the real file on its own
                files.append((path, path.stat().st_size, _identity(path)))
            except FileNotFoundError:
                continue  # raced away: it holds nothing here now
            except OSError:
                unreadable = True
    return files, unreadable


def _dir_bytes(root: Path, seen: set[str]) -> _Measure:
    """The bytes under *root* not already counted, plus a partial flag.

    The existence probe is a stat: Path.exists swallows a permission error and
    answers False, which would report an unreadable directory as an empty one.
    An absent directory is genuinely 0; any other OSError is unknown (partial).
    """
    try:
        root.stat()
    except FileNotFoundError:
        return _ZERO
    except OSError:
        return (0, True)
    files, unreadable = _scan(root)
    total = 0
    for _path, size, identity in files:
        if identity in seen:
            continue
        seen.add(identity)
        total += size
    return (total, unreadable)


def _file_bytes(path: Path, seen: set[str]) -> _Measure:
    """One file's bytes if this call has not counted it, plus a partial flag.

    A file that is gone contributes nothing (a vanished source is not an
    unreadable one); any other OSError is unknown.
    """
    identity = _identity(path)
    if identity in seen:
        return _ZERO
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return _ZERO
    except OSError:
        return (0, True)
    seen.add(identity)
    return (size, False)


def _run_file_bucket(root: Path, rel: tuple[str, ...]) -> str | None:
    """The bucket a file inside a **marked** run scope belongs to, or ``None``.

    A run keeps its own copy of its documents under
    ``<workspace>/runs/<run id>/`` (ADR-0033), and the walk must attribute them
    where they belong: a run's **exports** are exports, and its manifest,
    segments and record are records — the same buckets the workspace root's
    published copies land in, so the machine total stays true and a run's export
    directory is not silently counted as a transcript (STO-01).

    Only a **marked** directory is a run scope (:data:`RUN_MARKER`), which is the
    rule the walk itself reads: ``runs`` is an ordinary word, so everything else
    under it is the operator's, classified like any other file — discovery takes
    its audio as an input, and the buckets must count that file as the source
    tape it is.
    """
    if rel[:1] != (RUNS_DIR,) or len(rel) < 2:
        return None
    if not (root / RUNS_DIR / rel[1] / RUN_MARKER).is_file():
        return None
    if len(rel) > 2 and rel[2] == EXPORT_DIR:
        return "exports"
    return "records"


def _workspace_buckets(root: Path, seen: set[str]) -> dict[str, _Measure]:
    """One walk over a meeting's workspace, split into STO-01's buckets.

    A file under tapes/ -- or input audio the pipeline would discover, symlinks
    included -- is a **source** tape. audio/, export/ and agent/ are the app's
    derived output, and the manifest, segments, transcript and minutes are the
    meeting's records; a file inside a **marked** run scope's ``runs/<id>/`` is
    attributed by :func:`_run_file_bucket` (a run's own exports are exports, and
    an operator's unmarked ``runs/`` is classified like any other file). Files
    this call already counted are skipped, so a shared workspace or a linked
    target cannot double count; every bucket carries the walk's partial flag.
    """
    files, unreadable = _scan(root)
    buckets = {key: 0 for key in WORKSPACE_BUCKETS}
    for path, size, identity in files:
        if identity in seen:
            continue
        seen.add(identity)
        rel = path.relative_to(root).parts
        top = rel[0] if rel else ""
        bucket = _BUCKET_DIRS.get(top)
        if bucket is None:
            bucket = _run_file_bucket(root, rel) or (
                "tapes"
                if top == tapestore.TAPES_DIRNAME or is_audio(path)
                else "records"
            )
        buckets[bucket] += size
    return {key: (value, unreadable) for key, value in buckets.items()}


def _tape_bytes_outside(
    registry: Registry,
    meeting: Meeting,
    workspace: Path | None,
    seen: set[str],
) -> _Measure:
    """The bytes of this meeting's tapes that live outside its workspace.

    Tapes inside the workspace are already counted by the walk (they are input
    audio); one mounted elsewhere -- a user-chosen path registered by hand --
    is still source data and belongs in the total. Rows are deduped by resolved
    path, so the same file registered for two meetings counts once. A tape row
    whose file is gone contributes nothing; an unreadable one is partial.
    """
    total = 0
    partial = False
    for tape in registry.list_tapes(meeting.id):
        path = Path(tape.path)
        if workspace is not None and _within(path, workspace):
            continue
        size, unreadable = _file_bytes(path, seen)
        total += size
        partial = partial or unreadable
    return (total, partial)


def _chunk_bytes(meeting: Meeting, seen: set[str]) -> _Measure:
    """The workspace's app-owned chunk cache, which lives outside the workspace."""
    if not meeting.workspace_path:
        return _ZERO
    return _dir_bytes(Workspace.at(meeting.workspace_path).chunks_dir, seen)


def _meeting_buckets(
    registry: Registry, meeting: Meeting, seen: set[str]
) -> dict[str, _Measure]:
    """One meeting's slice of the buckets (everything but the machine-wide models).

    A meeting with no workspace still has registered tapes, which live outside
    any workspace by definition, so the outside-tape walk runs for it too.
    """
    workspace = Path(meeting.workspace_path) if meeting.workspace_path else None
    if workspace is None:
        measured: dict[str, _Measure] = {key: _ZERO for key in WORKSPACE_BUCKETS}
    else:
        measured = _workspace_buckets(workspace, seen)
    measured["tapes"] = _add(
        measured["tapes"], _tape_bytes_outside(registry, meeting, workspace, seen)
    )
    # The chunk cache is measured separately (it lives outside the workspace),
    # so it does not inherit the walk's partial flag.
    measured["chunks"] = _chunk_bytes(meeting, seen)
    return measured


def _add(left: _Measure, right: _Measure) -> _Measure:
    """Add one component to a bucket: the bytes sum, a partial part survives."""
    return (left[0] + right[0], left[1] or right[1])


def _sum_known(measured: dict[str, _Measure]) -> int:
    """The total of the bytes that could be measured (a lower bound)."""
    return sum(value for value, _partial in measured.values())


def _unknown(measured: dict[str, _Measure]) -> list[str]:
    """The ids of the components that could not be fully measured, in order."""
    return [
        key
        for key, _label, _kind in STORAGE_BUCKETS
        if key in measured and measured[key][1]
    ]


def _bucket_rows(measured: dict[str, _Measure]) -> list[dict]:
    """The template rows for one measured set, labels and kinds included."""
    rows = []
    for key, label, kind in STORAGE_BUCKETS:
        if key not in measured:
            continue
        count, partial = measured[key]
        rows.append(
            {
                "id": key,
                "label": label,
                "kind": kind,
                "bytes": count,
                "size": _human_bytes(count),
                "partial": partial,
            }
        )
    return rows


def _human_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{count} B"  # pragma: no cover - unreachable


def _too_large(size: int, cap: int) -> UploadTooLarge:
    return UploadTooLarge(
        deferred(
            "the upload is {size}, over the {limit} limit; raise "
            "CR_MAX_UPLOAD_BYTES to allow it"
        ),
        size=_human_bytes(size),
        limit=_human_bytes(cap),
    )


def _no_space(free: int, needed: int, root: Path) -> InsufficientSpace:
    return InsufficientSpace(
        deferred(
            "not enough free space on the managed workspace disk {root}: "
            "{free} free, about {needed} needed for this upload; free space or "
            "point CR_WORKSPACE_ROOT at a larger disk"
        ),
        root=str(root),
        free=_human_bytes(free),
        needed=_human_bytes(needed),
    )


def _resume_not_supported(upload_id: str) -> ResumeNotSupported:
    return ResumeNotSupported(
        deferred(
            "an upload with id {id!r} is already in progress or was left "
            "interrupted, and this node cannot resume one; start again with a new "
            "upload id"
        ),
        id=upload_id,
    )


__all__ = [
    "ArchiveRequired",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DISK_HEADROOM_BYTES",
    "DisallowedExtension",
    "InsufficientSpace",
    "InvalidUploadId",
    "MAX_UPLOAD_ID_LENGTH",
    "MeetingStorage",
    "ResumeNotSupported",
    "STORAGE_BUCKETS",
    "StorageTape",
    "TapeDeletion",
    "UnsafeFilename",
    "UploadRejected",
    "UploadTooLarge",
    "delete_tape",
    "delete_tapes",
    "durable_archive",
    "ensure_managed_workspace",
    "is_managed",
    "machine_storage",
    "managed_root",
    "max_upload_bytes",
    "meeting_storage",
    "precheck_upload",
    "root_free_bytes",
    "sanitize_filename",
    "upload_tape",
    "validate_upload_id",
    "workspace_path_for",
    "workspace_usage",
]
