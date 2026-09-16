"""A managed workspace: app-owned tapes, guarded uploads and storage (ADR-0024).

Two workspace modes share one abstraction. A *user-chosen* workspace (the CLI's
``--dir`` and a meeting's ``workspace_path``) stays the user's document
(ADR-0007). A *managed* workspace is app-owned **additionally**: the console can
provision it, receive tapes into it and transcribe them, so a self-hosted node
does not need the user to place multi-GB files by hand first. To the pipeline the
two are indistinguishable — a managed meeting is just a meeting whose
``workspace_path`` points inside the managed root, and its uploaded tapes are
input recordings discovered and run exactly like user-typed paths.

The managed root itself lives in :mod:`clear_record.service.paths` (the one
platformdirs-backed resolver, ``CR_WORKSPACE_ROOT`` override, default
``<data>/workspaces/``).

Upload is a **write surface**, so every guard is here and each failure is a
:class:`UploadRejected` (a user-facing message, never a traceback):

- the requested filename must be a bare name (no traversal, no absolute path,
  no separators);
- its extension must be audio, by the CLI's one allow-list
  (:func:`clear_record.cli.workspace.is_audio`);
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

import errno
import hashlib
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import BinaryIO

from clear_record.cli.workspace import AUDIO_SUFFIXES, is_audio
from clear_record.core.i18n import deferred
from clear_record.service.models import Meeting, Tape
from clear_record.service.paths import resolve_workspace_root
from clear_record.service.store import Registry

#: Subdirectory of a managed workspace holding the uploaded tapes. It is not one
#: of ``Workspace``'s own directories and not in ``SKIP_DIRS``, so
#: ``discover_audio`` finds the tapes: to the pipeline an upload is an input
#: recording like any other.
TAPES_DIRNAME = "tapes"

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
) -> Meeting:
    """Create the meeting's managed workspace and point the meeting at it.

    Idempotent: an already-provisioned managed meeting keeps its directory. The
    shape is the ordinary :class:`~clear_record.cli.workspace.Workspace` shape,
    so nothing downstream can tell it apart.
    """
    resolved_root = managed_root(root)
    path = workspace_path_for(resolved_root, meeting)
    _safe_mkdir(path, root=resolved_root)
    if meeting.workspace_path != str(path):
        meeting = registry.set_meeting_workspace(meeting.id, str(path))
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
) -> Meeting:
    """Refuse an upload *before* its body is read, where the guard can.

    ``declared_bytes`` is the request's ``Content-Length`` (the whole multipart
    body, an upper bound on the file). It is checked against the cap and against
    free space on the managed root. ``upload_id`` is the client's optional
    upload id: it is validated here, and an id whose scratch file is already on
    disk is refused as unsupported (resume is not built). Returns the (possibly
    newly provisioned) meeting so the caller need not resolve it twice.
    """
    meeting = _upload_workspace(registry, meeting, root)
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
    filename: str | None,
    declared_bytes: int | None = None,
    root: str | os.PathLike | None = None,
    upload_id: str | None = None,
) -> Tape:
    """Stream one uploaded tape to the meeting's managed workspace.

    Guards run first (filename, extension, cap, disk, upload id). The body is
    then copied in blocks to a sibling ``.part`` file, ``fsync``-ed and
    atomically renamed; the tape is recorded — checksum and size — only after
    the rename. Any failure (a truncated body included) removes the partial file
    and leaves no tape behind.

    ``upload_id``, when given, names that scratch file, so it is the transfer's
    identity for a future resume layer. This slice still starts every upload at
    zero: an id whose scratch file exists is refused by the precheck.
    """
    meeting = precheck_upload(registry, meeting, declared_bytes, root, upload_id)
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
    tapes_dir = _safe_mkdir(workspace / TAPES_DIRNAME, root=resolved_root)
    target = _unique_target(tapes_dir, name)
    if target.is_symlink():
        raise UploadRejected(
            deferred("refusing to write to the symlink {path}"), path=str(target)
        )

    cap = max_upload_bytes()
    # A client id names its own scratch file (so a resume layer can find it); a
    # request without one gets a short random name that cannot collide with a
    # sibling upload. Either way the name is not the tape name, which may be near
    # the filesystem's 255-byte limit.
    part = _scratch_path(tapes_dir, token)
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
        _fsync_dir(tapes_dir)
        return registry.register_tape(
            meeting.id, path=str(target), sha256=digest.hexdigest(), bytes=written
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


def meeting_storage(
    registry: Registry,
    meeting: Meeting,
    root: str | os.PathLike | None = None,
) -> dict:
    """The meeting's workspace size, its uploaded tapes and the root's free space.

    ``free_bytes`` is the same accounting the upload guard checks (one function,
    :func:`root_free_bytes`), so the console's panel and the guard cannot drift;
    it is ``None`` when the root cannot report it, or when the meeting is not
    managed (its disk is not this app's concern).
    """
    resolved_root = managed_root(root)
    managed_here = is_managed(meeting, resolved_root)
    free_bytes: int | None = None
    if managed_here:
        try:
            free_bytes = root_free_bytes(resolved_root)
        except OSError:  # pragma: no cover - a root that cannot be measured
            free_bytes = None
    return {
        "workspace_path": meeting.workspace_path,
        "managed": managed_here,
        "managed_root": str(resolved_root),
        "bytes": workspace_usage(meeting),
        "free_bytes": free_bytes,
        "tapes": [
            {
                "id": tape.id,
                "path": tape.path,
                "name": Path(tape.path).name,
                "sha256": tape.sha256,
                "bytes": tape.bytes,
            }
            for tape in registry.list_tapes(meeting.id)
        ],
    }


def delete_tape(
    registry: Registry,
    meeting: Meeting,
    tape_id: int,
    root: str | os.PathLike | None = None,
) -> Tape:
    """Delete one **managed** tape's file and record.

    Only a tape inside the meeting's managed workspace may be deleted: a
    user-chosen path is the user's document, not app-owned data (ADR-0007). The
    archive is the durable copy, so deleting workspace tapes is safe by design
    (ADR-0024).
    """
    tape = registry.get_tape(tape_id)
    if tape is None or tape.meeting_id != meeting.id:
        raise KeyError(tape_id)
    resolved_root = managed_root(root)
    if not is_managed(meeting, resolved_root):
        raise UploadRejected(
            deferred(
                "this meeting uses a user-chosen workspace; only a managed tape can "
                "be deleted here"
            )
        )
    path = Path(tape.path)
    _require_within(path, resolved_root)
    path.unlink(missing_ok=True)
    return registry.forget_tape(tape_id)


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
) -> Meeting:
    """The managed workspace an upload may write into.

    No workspace yet -> provision one. A user-chosen workspace (outside the
    managed root) is refused: the app writes these files, so they must not land
    in a user document (ADR-0007's rule still holds).
    """
    if not meeting.workspace_path:
        return ensure_managed_workspace(registry, meeting, root)
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
    partial = _scratch_path(Path(meeting.workspace_path) / TAPES_DIRNAME, upload_id)
    if partial.is_symlink() or partial.exists():
        raise _resume_not_supported(upload_id)


def _unique_target(directory: Path, name: str) -> Path:
    """A destination filename that does not collide, mirroring the archive's
    disagreement-free suffixes (``a.wav`` -> ``a-2.wav``)."""
    candidate = directory / name
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while True:
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        n += 1


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
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DISK_HEADROOM_BYTES",
    "DisallowedExtension",
    "InsufficientSpace",
    "InvalidUploadId",
    "MAX_UPLOAD_ID_LENGTH",
    "ResumeNotSupported",
    "TAPES_DIRNAME",
    "UnsafeFilename",
    "UploadRejected",
    "UploadTooLarge",
    "delete_tape",
    "ensure_managed_workspace",
    "is_managed",
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
