"""The diagnostics **bundle**: one redacted file a user can hand us.

The log record shape and the rotating sink live in
:mod:`clear_record.core.diagnostics` (``core`` is the one layer every surface may
import, so the CLI leaves a trail too). This module owns what the service layer
adds on top:

* **Redaction** — no transcript text, no audio, no glossary terms, and file
  basenames replaced by **stable hashes** while the path shape is kept.
* **Bundle assembly** — version, platform, backend availability **and
  reasons**, the resolved model/knobs, the last run's status and error, and the
  recent log lines, rendered as one plain-text file.
* The ``clear-record diagnose`` subcommand (registered through the
  ``clear_record.commands`` entry point) and the console download.

This is **not telemetry.** Nothing is transmitted anywhere: the user creates the
file, reads it, and chooses whether to attach it. There is no network call in
this module.

Layering: this is a ``service`` module, so it must not import ``providers``. It
reaches the provider probe through :mod:`clear_record.pipeline.auto`: the
``pipeline`` layer may import ``providers``, this module may not.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import logging
import os
import platform as _platform
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import click

from clear_record.core.diagnostics import effective_level, log_event
from clear_record.core.message import _english, render_message
from clear_record.core.paths import registry_path

# --- bundle --------------------------------------------------------------- #

#: The bundle a user attaches to a report. Plain text: it opens anywhere and is
#: easy to eyeball before sending. The format is additive-only.
BUNDLE_FILENAME = "clear-record-diagnostics.txt"
BUNDLE_FORMAT_VERSION = 1
#: Bounds on the private detail an explicit opt-in may pull from a workspace.
_PRIVATE_SECTION_BYTES = 64 * 1024
#: How many recent log lines the bundle carries by default.
DEFAULT_LOG_LINES = 200


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


# --- redaction ------------------------------------------------------------ #

#: Length of the stable hex digest that replaces a path component.
_HASH_LEN = 12
#: File extensions worth keeping visible (the *shape* of a path); the name that
#: carries the identity is hashed away. Model checkpoints (``.bin``) are public
#: names, not user files, so they are deliberately absent: a bare model name in
#: the resolved knobs stays readable, while a path to one is still redacted.
_FILE_SUFFIXES = (
    "wav",
    "mp3",
    "m4a",
    "aac",
    "flac",
    "ogg",
    "opus",
    "aiff",
    "aif",
    "json",
    "txt",
    "log",
    "srt",
    "vtt",
    "md",
    "csv",
    "toml",
)
#: One pass over both shapes (a path with separators, or a bare filename), so a
#: replacement is never re-hashed by a second rule. The path branch runs to the
#: next line break, quote, comma, semicolon, angle bracket, pipe or close paren,
#: so a component containing a space (My Meeting) is redacted as one token; a
#: space alone does not end the path. The bare branch likewise admits interior
#: spaces (My Meeting.wav), so a filename is hashed whole rather than only from
#: its last word; that can also hash the words leading up to a filename in free
#: text, which is the safe direction for a privacy redactor.
_TOKEN = re.compile(
    r"(?:[A-Za-z]:)?(?:[\\/][^\n\"'<>|,;)]+)+"
    r"|(?<![\w/\\])[\w][\w.\-]*(?: [\w.\-]+)*\.(?:"
    + "|".join(_FILE_SUFFIXES)
    + r")(?![\w])",
    re.IGNORECASE,
)


def hash_component(name: str) -> str:
    """A stable, deterministic replacement for one file/directory name.

    Stable means the same name always hashes to the same token, so a path is
    still correlatable inside one bundle (and across bundles) — the *identity*
    is what is dropped, not the structure. A leading dot and the file suffix are
    preserved so the shape reads naturally (``.env`` → ``.<hash>``,
    ``take1.wav`` → ``<hash>.wav``).
    """
    if not name:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_HASH_LEN]
    prefix = "." if name.startswith(".") and name not in (".", "..") else ""
    suffix = Path(name).suffix
    if any(char.isspace() for char in suffix):
        # A path token may run to the end of the line, so its suffix can be
        # trailing prose (for example notes.txt because it is locked); never
        # let that survive as though it were a file extension.
        suffix = ""
    return f"{prefix}{digest}{suffix}"


def redact_path(value: str) -> str:
    """Hash every component name in a path, keeping separators and suffixes."""
    if not value:
        return value
    parts = re.split(r"([/\\])", value)
    out: list[str] = []
    first = True
    for token in parts:
        if token in ("/", "\\"):
            out.append(token)
            continue
        if token == "":
            continue
        if first and len(token) == 2 and token[1] == ":":
            out.append(token)  # a Windows drive prefix is not identifying
        else:
            out.append(hash_component(token))
        first = False
    return "".join(out)


def redact_text(text: str) -> str:
    """Replace every path-like or filename-like token with stable hashes."""

    def _replace(match: re.Match) -> str:
        token = match.group(0)
        if "/" in token or "\\" in token:
            return redact_path(token)
        return hash_component(token)

    return _TOKEN.sub(_replace, text)


#: What a credential value becomes when one reaches a log line. The *key* — or
#: the word "password" — survives: knowing a credential was sent is useful, its
#: value is the one thing this bundle must never carry (ADR-0033).
_CREDENTIAL_PLACEHOLDER = "[redacted]"

#: A credential-shaped **field name**, matched whole: a structured log record
#: whose key is one of these has its value replaced regardless of its shape.
_CREDENTIAL_KEY = re.compile(
    r"(?i)(?:password|passwd|passphrase|secret|token|credential)"
)

#: A credential **value in free text**: a key word followed by `:`/`=`, as a query
#: string (`password=x`), a form post (`password: x`) or a JSON body
#: (`"password":"x"`). The credential is stored hashed and is never logged, so
#: this is the second line — the one that keeps a future change which logged a
#: request body from putting a password into a bundle.
_CREDENTIAL_FIELD = re.compile(
    r"""(?i)\b(password|passwd|passphrase|secret|token|credential)"""
    r"""(\"?\s*[:=]\s*\"?)([^\s\"&,;}]+)"""
)


def redact_credential(text: str) -> str:
    """Replace any credential-shaped value in ``text`` with a placeholder."""
    return _CREDENTIAL_FIELD.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_CREDENTIAL_PLACEHOLDER}",
        text,
    )


def redact_log_line(line: str) -> str:
    """Redact a structured log line's string values (or free text, if not JSON).

    Paths and filenames are hashed; a credential-shaped key or value is replaced
    outright, whatever it looks like — the one thing the bundle must never carry
    is the console's own password, and a log line is the only route by which it
    could reach one.
    """
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return redact_credential(redact_text(line))
    if not isinstance(record, dict):
        return redact_credential(redact_text(line))
    for key, value in list(record.items()):
        if not isinstance(value, str):
            continue
        if key and _CREDENTIAL_KEY.fullmatch(key):
            record[key] = _CREDENTIAL_PLACEHOLDER
        else:
            record[key] = redact_credential(redact_text(value))
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


# --- backend availability ------------------------------------------------- #


def backend_status() -> dict[str, dict]:
    """Each catalog backend's verdict with its reason (via the pipeline layer).

    ``service`` may not import ``providers``; ``pipeline`` may, and its
    ``auto.backend_availability`` is the one probe. This is never imported at
    module load, so a plain ``import clear_record.service`` stays light.
    """
    from clear_record.pipeline.auto import backend_availability
    from clear_record.core.diagnostics import log_event

    status = backend_availability()
    result = {
        backend_id: {
            "available": verdict.available,
            "reason": verdict.reason.as_json() if verdict.reason else None,
        }
        for backend_id, verdict in status.items()
    }
    log_event(
        "info",
        "backends",
        "backend.probe",
        count=len(result),
        available=sum(1 for verdict in result.values() if verdict["available"]),
    )
    return result


# --- bundle assembly ------------------------------------------------------ #


def machine_description() -> str:
    """The machine identity the run record stores (OQ-3).

    A *description*, not a fingerprint: the hostname plus the CPU/accelerator
    facts the diagnostics bundle already collects, composed from the same stdlib
    ``platform`` probe. Nothing is uploaded anywhere (there is no telemetry), and
    no dependency is added for it.
    """
    node = _platform.node() or "unknown host"
    cpu = _platform.machine() or "unknown CPU"
    processor = _platform.processor()
    detail = f"{cpu} {processor}".strip() if processor else cpu
    return f"{node} ({_platform.platform()}; {detail})"


@dataclasses.dataclass(frozen=True)
class BundleFacts:
    """Everything :func:`build_bundle` renders — assembled, never gathered here.

    Keeping the facts explicit makes the bundle **pure over its inputs**, which
    is what lets the redaction test assert the exact output without a running
    registry, provider probe or logs on disk.
    """

    version: str
    python: str
    platform: str
    machine: str
    backends: Mapping[str, Mapping[str, object]]
    options: Mapping[str, object]
    run: Mapping[str, object] | None = None
    workspace: str | None = None
    log_lines: Sequence[str] = ()
    include_private: bool = False
    private_sections: Mapping[str, str] = dataclasses.field(default_factory=dict)


def _render_options(options: Mapping[str, object], redact) -> list[str]:
    out: list[str] = []
    for key in sorted(options):
        value = options[key]
        if value is None:
            continue
        if isinstance(value, str):
            # Redact every string value: redact_text leaves a bare non-file
            # value (a model size, a language, a sha) readable, but hashes a
            # filename whether or not it carries a separator.
            rendered = redact(value)
        else:
            rendered = str(value)
        out.append(f"{key}: {rendered}")
    return out


def build_bundle(facts: BundleFacts) -> str:
    """Render the diagnostics bundle. Redacted unless ``include_private``."""
    redact = (lambda value: value) if facts.include_private else redact_text
    redact_line = (lambda line: line) if facts.include_private else redact_log_line

    out: list[str] = []
    out.append("clear-record diagnostics bundle")
    out.append(
        f"format: {BUNDLE_FORMAT_VERSION} (additive-only: fields are added, never removed)"
    )
    out.append(f"generated: {_now()}")
    out.append("")
    out.append(
        "NOT TELEMETRY. Nothing in this file is transmitted anywhere. You create it,"
    )
    out.append(
        "read it, and decide whether to attach it to a bug report yourself; there is"
    )
    out.append("no network call. Redaction is on unless you passed --include-private.")
    out.append("")
    if facts.include_private:
        out.append(
            "PRIVATE DETAIL IS INCLUDED (--include-private): real file paths, the run's"
        )
        out.append(
            "workspace log, the transcript and the glossary. Review before sharing."
        )
    else:
        out.append(
            "WITHHELD by default: transcript text, audio, glossary terms, and file names."
        )
        out.append(
            "Every path keeps its shape (separators, depth, suffixes) but each component"
        )
        out.append(
            "name is replaced by a stable hash, so it is recognizable, not identifying."
        )
    out.append("")
    out.append("# version")
    out.append(f"clear-record: {facts.version}")
    out.append(f"python: {facts.python}")
    out.append(f"platform: {facts.platform}")
    out.append(f"machine: {facts.machine}")
    out.append("")
    out.append("# backend availability (and why)")
    if facts.backends:
        width = max(len(str(backend_id)) for backend_id in facts.backends)
        for backend_id, verdict in facts.backends.items():
            state = "available" if verdict.get("available") else "unavailable"
            reason_node = verdict.get("reason")
            reason = (
                redact(render_message(reason_node, _english) or "")
                if reason_node
                else ""
            )
            out.append(f"{str(backend_id):<{width}}  {state:<11} {reason}".rstrip())
    else:
        out.append("(backend probe unavailable)")
    out.append("")
    out.append("# resolved model / knobs")
    rendered_options = _render_options(facts.options, redact)
    out.extend(rendered_options or ["(none recorded)"])
    out.append("")
    out.append("# last run")
    if facts.workspace:
        workspace = (
            facts.workspace if facts.include_private else redact(facts.workspace)
        )
        out.append(f"workspace: {workspace}")
    if facts.run:
        for key in (
            "run_id",
            "meeting_id",
            "status",
            "backend",
            "model",
            "language",
            "started_at",
            "ended_at",
        ):
            value = facts.run.get(key)
            if value is not None:
                out.append(f"{key}: {redact(str(value))}")
        if facts.run.get("error"):
            out.append(f"error: {redact(str(facts.run['error']))}")
    else:
        out.append("(no run recorded)")
    out.append("")
    out.append("# recent log lines (oldest first)")
    if facts.log_lines:
        out.extend(redact_line(line) for line in facts.log_lines)
    else:
        out.append("(no log lines)")
    out.append("")
    for title, body in facts.private_sections.items():
        out.append(f"# {title} (included by --include-private)")
        out.append(body.rstrip("\n"))
        out.append("")
    out.append("# withheld")
    if facts.include_private:
        out.append("nothing further (audio is never read or included).")
    else:
        out.append(
            "transcript text; audio; glossary terms; file names (basenames hashed)."
        )
    out.append("")
    return "\n".join(out)


def _read_tail(path: Path, max_bytes: int = _PRIVATE_SECTION_BYTES) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def _private_sections(
    workspace: str | os.PathLike | None, include_private: bool
) -> dict:
    """The private detail ``--include-private`` opts into (empty otherwise).

    Audio is never read; the transcript and glossary are read only when the user
    explicitly asked for them.
    """
    if not include_private or workspace is None:
        return {}
    from clear_record.pipeline.workspace import GLOSSARY, TRANSCRIBE_LOG, Workspace

    ws = Workspace.at(workspace)
    sections: dict[str, str] = {}
    for title, candidate in (
        ("workspace log (transcribe.log)", ws.root / TRANSCRIBE_LOG),
        ("transcript (segments.json)", ws.segments_path),
        ("glossary (glossary.txt)", ws.root / GLOSSARY),
    ):
        if candidate.is_file():
            sections[title] = _read_tail(candidate)
    return sections


def _latest_run(registry) -> object | None:
    """The most recent run across every project (diagnose has no run context)."""
    latest = None
    for project in registry.list_projects():
        for meeting in registry.list_meetings(project.slug):
            runs = registry.list_runs(meeting.id)
            if runs and (latest is None or runs[0].id > latest.id):
                latest = runs[0]
    return latest


def _run_facts(registry, run_id: int | None, meeting_id: int | None) -> dict | None:
    if registry is None:
        return None
    run = None
    if run_id is not None:
        run = registry.get_run(run_id)
    elif meeting_id is not None:
        runs = registry.list_runs(meeting_id)
        run = runs[0] if runs else None
    else:
        run = _latest_run(registry)
    return dataclasses.asdict(run) if run is not None else None


def _workspace_for(registry, meeting_id, run_facts) -> str | None:
    if registry is None:
        return None
    target = meeting_id
    if target is None and run_facts is not None:
        target = run_facts.get("meeting_id")
    if target is None:
        return None
    meeting = registry.meeting_by_id(int(target))
    return meeting.workspace_path if meeting is not None else None


def _resolved_options(run_facts: Mapping[str, object] | None) -> dict:
    """The resolved model and knobs, plus the recorded glossary identity."""
    from clear_record.core import PipelineOptions, resolve_options

    resolved = resolve_options(PipelineOptions())
    options: dict = {
        "backend": (run_facts or {}).get("backend") or resolved.backend,
        "model": (run_facts or {}).get("model") or resolved.model,
        "language": (run_facts or {}).get("language") or resolved.language,
        "profile": resolved.profile,
        "chunk_seconds": resolved.chunk_seconds,
        "overlap_seconds": resolved.overlap_seconds,
        "jobs": resolved.jobs,
    }
    options.update(resolved.decoder_knobs())
    recorded = (run_facts or {}).get("options")
    if isinstance(recorded, Mapping):
        for key in ("glossary", "glossary_sha256"):
            if recorded.get(key):
                options[key] = recorded[key]
    return options


def collect_bundle(
    *,
    registry=None,
    run_id: int | None = None,
    meeting_id: int | None = None,
    workspace: str | os.PathLike | None = None,
    include_private: bool = False,
    limit: int = DEFAULT_LOG_LINES,
    log_dir: str | os.PathLike | None = None,
) -> str:
    """Gather the facts and render one bundle.

    Everything a bundle can leak is redacted in :func:`build_bundle`; this only
    gathers (and never reads transcript, audio or glossary unless the user opted
    in). ``registry`` is optional: without one there is no run context.
    """
    from clear_record.core.diagnostics import read_recent
    from clear_record.service.archive import tool_version

    run_facts = _run_facts(registry, run_id, meeting_id)
    if workspace is None:
        workspace = _workspace_for(registry, meeting_id, run_facts)
    facts = BundleFacts(
        version=tool_version(),
        python=_platform.python_version(),
        platform=_platform.platform(),
        machine=_platform.machine(),
        backends=backend_status(),
        options=_resolved_options(run_facts),
        run=run_facts,
        workspace=str(workspace) if workspace else None,
        log_lines=read_recent(limit, explicit=log_dir),
        include_private=include_private,
        private_sections=_private_sections(workspace, include_private),
    )
    return build_bundle(facts)


# --- console (uvicorn) logging into the same sink -------------------------- #

#: ``logging`` level names mapped to the sink's four levels.
_CONSOLE_LEVELS = {
    "CRITICAL": "error",
    "ERROR": "error",
    "WARNING": "warning",
    "WARN": "warning",
    "INFO": "info",
    "DEBUG": "debug",
    "NOTSET": "info",
}


class DiagnosticsHandler(logging.Handler):
    """A ``logging`` handler that writes records into the diagnostics sink.

    The console's own logs (uvicorn's startup/error/access records) belong in
    the same rotating file ``clear-record diagnose`` reads, not only on stderr,
    so a service node's log trail survives a reboot. Records are stored as the
    sink's usual structured lines (a short ``message`` scalar), so the bundle's
    redaction applies to them unchanged.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a log line must never raise
            message = str(record.msg)
        log_event(
            _CONSOLE_LEVELS.get(record.levelname.upper(), "info"),
            "console",
            "console.log",
            logger=record.name,
            message=message,
        )


def console_log_config(level: str | None = None) -> dict:
    """A uvicorn ``log_config`` that routes the console's logs to the sink.

    Used by ``clear-record serve`` (the headless, service-facing entry point) so
    a systemd/launchd unit's logs land beside every other clear-record record
    instead of only in the journal. ``level`` defaults to the configured sink
    level (``CR_LOG_LEVEL`` / the programmatic override).
    """
    chosen = (level or effective_level()).upper()
    loggers = {
        name: {"handlers": ["diagnostics"], "level": chosen, "propagate": False}
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"diagnostics": {"()": DiagnosticsHandler}},
        "loggers": loggers,
    }


# --- the `clear-record diagnose` subcommand ------------------------------- #

_PRIVATE_NOTE = (
    "[diagnose] --include-private ADDS: real file paths (not hashed); the run's\n"
    "  workspace log (transcribe.log); the transcript (segments.json); the glossary\n"
    "  (glossary.txt). Audio is still never read. Review the bundle before sharing."
)

_CONTENTS_NOTE = (
    "[diagnose] the bundle contains: version/platform, backend availability with\n"
    "  reasons, the resolved model/knobs, the last run's status and error, and recent\n"
    "  log lines. Redaction is on by default. This is NOT telemetry: nothing was\n"
    "  transmitted — attach the file to your report yourself."
)

_DRY_RUN_NOTE = (
    "\n[dry run] nothing was written and nothing is transmitted. "
    "Re-run without --list to write the file."
)


def register(group: click.Group) -> None:
    """Add the ``diagnose`` subcommand (called by the CLI entry-point discovery).

    ``group`` is the CLI's Click group (ADR-0022). Registered through
    ``clear_record.commands`` (ADR-0013), so the CLI never imports this module
    directly and the base CLI stays surface-free.
    """

    @group.command(
        name="diagnose",
        help="write a redacted feedback bundle you can attach to a bug report",
    )
    @click.option(
        "--list",
        "dry_run",
        is_flag=True,
        help="print the bundle to stdout and write nothing (dry run)",
    )
    @click.option(
        "--include-private",
        is_flag=True,
        help="opt in to private detail (real paths, workspace log, transcript, "
        "glossary); prints exactly what it adds",
    )
    @click.option("--run-id", type=int, default=None, help="a specific run to report")
    @click.option(
        "--meeting-id",
        type=int,
        default=None,
        help="a meeting whose last run to report",
    )
    @click.option(
        "--output",
        "-o",
        default=None,
        help=f"output file (default ./{BUNDLE_FILENAME})",
    )
    @click.option(
        "--data-dir",
        default=None,
        envvar="CR_DATA_DIR",
        show_envvar=True,
        help="override the app data directory (default: CR_DATA_DIR / platform dir)",
    )
    def _diagnose(**kwargs) -> int:
        return run_diagnose(SimpleNamespace(**kwargs))


def run_diagnose(args) -> int:
    """The ``diagnose`` handler: gather, print what it adds, write (or dry-run)."""
    from clear_record.service.store import Registry

    registry = None
    data_dir = getattr(args, "data_dir", None)
    if registry_path(data_dir).is_file():
        registry = Registry.open(data_dir=data_dir)

    run_id = getattr(args, "run_id", None)
    meeting_id = getattr(args, "meeting_id", None)
    run_facts = _run_facts(registry, run_id, meeting_id)
    workspace = _workspace_for(registry, meeting_id, run_facts)

    include_private = bool(getattr(args, "include_private", False))
    if include_private:
        print(_PRIVATE_NOTE)

    bundle = collect_bundle(
        registry=registry,
        run_id=run_id,
        meeting_id=meeting_id,
        workspace=workspace,
        include_private=include_private,
    )

    if getattr(args, "dry_run", False):
        print(bundle)
        print(_DRY_RUN_NOTE)
        return 0

    output = getattr(args, "output", None)
    target = Path(output).expanduser() if output else Path.cwd() / BUNDLE_FILENAME
    target.write_text(bundle, encoding="utf-8")
    print(f"[diagnose] wrote {target}")
    print(_CONTENTS_NOTE)
    return 0


__all__ = [
    "BUNDLE_FILENAME",
    "BUNDLE_FORMAT_VERSION",
    "DEFAULT_LOG_LINES",
    "BundleFacts",
    "DiagnosticsHandler",
    "backend_status",
    "build_bundle",
    "collect_bundle",
    "console_log_config",
    "hash_component",
    "redact_credential",
    "redact_log_line",
    "redact_path",
    "redact_text",
    "register",
    "run_diagnose",
]
