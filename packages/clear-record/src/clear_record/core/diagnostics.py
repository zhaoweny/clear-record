"""Structured log records and a bounded rotating sink (stdlib only).

The sink lives in ``core`` — the lowest layer, which every surface may import —
so the CLI, the service and the web console all write the same records and the
diagnostics bundle reads them back. A bare ``clear-record run`` therefore leaves
a trail without the CLI importing the service layer (which the layer DAG
forbids).

A record is a stable, additive-only JSON line::

    {"ts": ..., "level": ..., "component": ..., "event": ..., <scalar fields>}

User content is never dumped into a record: fields are short scalars (and a
short ``message`` at most). The sink rotates at :data:`MAX_LOG_BYTES`, keeping
:data:`RETAINED_LOGS` files beside the current one, under ``$CR_LOG_DIR`` or the
platform-native log directory (``~/Library/Logs/clear-record`` on macOS —
ADR-0007/ADR-0025's *state/logs*).

The location itself is resolved by :mod:`clear_record.core.paths`; this module
imports no third-party code, so it never computes a platform path.

Vendor-free: stdlib only, like the rest of ``clear_record.core``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path

from clear_record.core.paths import ENV_LOG_DIR
from clear_record.core.paths import resolve_logs_dir as _resolve_logs_dir

#: The rotating log file inside :func:`logs_dir`.
LOG_FILENAME = "clear-record.log"

#: Rotate when the current file would exceed this many bytes, keeping
#: :data:`RETAINED_LOGS` rotated files beside it (``.1`` newest, ``.N`` oldest).
MAX_LOG_BYTES = 1 << 20  # 1 MiB
RETAINED_LOGS = 3
#: How many recent lines :func:`read_recent` returns by default.
DEFAULT_LOG_LINES = 200

#: The accepted levels, weakest first; a record is written when its level is at
#: least the configured one.
LEVELS: tuple[str, ...] = ("debug", "info", "warning", "error")
_LEVEL_NUMBERS = {name: index for index, name in enumerate(LEVELS)}
#: ``CR_LOG_LEVEL`` overrides this; an unknown value falls back to the default.
DEFAULT_LEVEL = "info"
ENV_LOG_LEVEL = "CR_LOG_LEVEL"
# ``CR_LOG_DIR`` (the documented sink escape hatch) is defined in core.paths and
# re-exported here, so a reader of diagnostics still sees it.

#: A process-wide level set programmatically (the CLI's ``--verbose``). It beats
#: the environment for the life of the process; ``set_level(None)`` clears it.
_level_override: str | None = None
#: Rotation + append must be atomic across threads (the web/service use workers).
_append_lock = threading.Lock()


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


# --- level ---------------------------------------------------------------- #


def _normalize_level(level: str) -> str | None:
    level = (level or "").strip().lower()
    if level == "warn":
        level = "warning"
    return level if level in _LEVEL_NUMBERS else None


def set_level(level: str | None) -> None:
    """Set (or clear) a process-wide level; ``--verbose`` uses this."""
    global _level_override
    _level_override = _normalize_level(level) if level is not None else None


def effective_level(environ: Mapping[str, str] | None = None) -> str:
    """The configured level: the programmatic override, else ``CR_LOG_LEVEL``,
    else :data:`DEFAULT_LEVEL`."""
    if _level_override is not None:
        return _level_override
    env = os.environ if environ is None else environ
    return _normalize_level(env.get(ENV_LOG_LEVEL) or "") or DEFAULT_LEVEL


def enabled(level: str, environ: Mapping[str, str] | None = None) -> bool:
    """True when ``level`` is at least the configured level."""
    current = _normalize_level(level) or "info"
    return _LEVEL_NUMBERS[current] >= _LEVEL_NUMBERS[effective_level(environ)]


# --- the sink path -------------------------------------------------------- #


def logs_dir(explicit: str | os.PathLike | None = None) -> Path:
    """Resolve the rotating log directory.

    Precedence: explicit argument > ``CR_LOG_DIR`` > the platform-native log
    directory. Delegates to the one resolver (:mod:`clear_record.core.paths`), so
    a writer and a reader can never disagree about where the log lives.
    """
    return _resolve_logs_dir(explicit)


def log_path(explicit: str | os.PathLike | None = None) -> Path:
    """The current rotating log file."""
    return logs_dir(explicit) / LOG_FILENAME


# --- the record ----------------------------------------------------------- #


def _scalar(value: object) -> object:
    """Coerce a field to a JSON scalar (the record carries scalars only)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def make_record(level: str, component: str, event: str, fields: Mapping) -> dict:
    """Build one stable record: ``ts``, ``level``, ``component``, ``event``, then
    the scalar fields in sorted order. Fields are additive; nothing is removed."""
    record: dict = {
        "ts": _now(),
        "level": level,
        "component": component,
        "event": event,
    }
    for key in sorted(fields):
        record[key] = _scalar(fields[key])
    return record


def format_record(record: Mapping) -> str:
    """Serialize one record as a single JSON line (stable key order, UTF-8)."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def log_event(level: str, component: str, event: str, **fields) -> dict | None:
    """Append one record to the rotating sink, if ``level`` is enabled.

    Returns the record (or ``None`` when suppressed). Writing is best-effort: an
    unwritable sink never breaks the pipeline, which is why this swallows
    :class:`OSError` rather than raising.
    """
    if not enabled(level):
        return None
    record = make_record(level, component, event, fields)
    try:
        append_line(log_path(), format_record(record))
    except OSError:
        pass
    return record


# --- rotation ------------------------------------------------------------- #


def _rotated(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def rotate(path: Path, *, retained: int | None = None) -> None:
    """Shift ``path`` to ``.1``, ``.1`` to ``.2`` … dropping the oldest.

    ``retained`` is the number of rotated files kept *beside* the current one.
    """
    keep = RETAINED_LOGS if retained is None else retained
    _rotated(path, keep).unlink(missing_ok=True)
    for index in range(keep - 1, 0, -1):
        older = _rotated(path, index)
        if older.exists():
            older.replace(_rotated(path, index + 1))
    if path.exists():
        path.replace(_rotated(path, 1))


def append_line(
    path: Path,
    line: str,
    *,
    max_bytes: int | None = None,
    retained: int | None = None,
) -> None:
    """Append one line, rotating first when the file would exceed the cap."""
    cap = MAX_LOG_BYTES if max_bytes is None else max_bytes
    path.parent.mkdir(parents=True, exist_ok=True)
    with _append_lock:
        if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > cap:
            rotate(path, retained=retained)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if path.stat().st_size > cap:  # a single over-long line never loops
            rotate(path, retained=retained)


def read_recent(
    limit: int = DEFAULT_LOG_LINES,
    *,
    explicit: str | os.PathLike | None = None,
) -> list[str]:
    """The most recent log lines, oldest first (rotated files are included)."""
    if limit <= 0:
        return []
    path = log_path(explicit)
    ordered = [_rotated(path, index) for index in range(RETAINED_LOGS, 0, -1)] + [path]
    lines: list[str] = []
    for candidate in ordered:
        try:
            lines.extend(
                candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            )
        except OSError:
            continue
    return lines[-limit:]


__all__ = [
    "DEFAULT_LEVEL",
    "DEFAULT_LOG_LINES",
    "ENV_LOG_DIR",
    "ENV_LOG_LEVEL",
    "LEVELS",
    "LOG_FILENAME",
    "MAX_LOG_BYTES",
    "RETAINED_LOGS",
    "append_line",
    "effective_level",
    "enabled",
    "format_record",
    "log_event",
    "log_path",
    "logs_dir",
    "make_record",
    "read_recent",
    "rotate",
    "set_level",
]
