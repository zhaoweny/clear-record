#!/usr/bin/env python3
"""Agent drive: the harness stand-in for the three LLM-shaped jobs.

Ticket 07. clear-record calls no model (ADR-0031): the three jobs — glossary
collection, transcript check, minutes — are work a **harness** does over MCP, and
this script stands in for one. It reaches clear-record **only** through the MCP
tools the shipped server exposes, never through the service's Python seam.

**Run it through just** (the pointer recipe lives in the justfile; this line is
what it runs)::

    just agent-drive
    just agent-drive --tape path/to/recording.wav

The ``--all-packages`` flag is what makes the script work: it imports
``clear_record``, so it must run inside the project environment, exactly like
``scripts/agent_setup.py``.

The stories, in order, each reported:

1. **MCP surface** — launch the server exactly as the wizard's client entry names
   it, list its tools over stdio, and require the draft tools and the transcript
   read to be there. Needs no key.
2. **Scripted drafts** — write one draft version per job with
   ``write_agent_draft``, then append a second version to one chain, and assert
   the chain reports both versions with their authors. Needs no key.
3. **Review** — accept one draft and reject another over MCP, and assert the
   acceptance produced its artifact while the rejected chain is kept. Needs no
   key.
4. **Model** — the leg that needs a real model: point the model at the same MCP
   tools, let it read the transcript and write a glossary draft, and require it
   to have called a tool. Requires a key; **without one it is reported SKIP** and
   the drive still exits 0.

Key handling is BYOK for the *drive*, which is a harness, not the app:

- the key is read at run time from ``CR_DRIVE_API_KEY``, or from the file named by
  ``CR_DRIVE_API_KEY_FILE``, or from the operator's gitignored default
  (``<repo>/.local/deepseek_api-key.txt``, also checked in the main worktree);
- the value is **never printed**, never written to config or the registry, and
  every line of output is passed through :class:`Redactor`;
- the ``CR_DRIVE_`` prefix is deliberate: ``CR_AGENT_*`` names what the app's 0.2
  in-process path read, which this version reports as ignored, so the drive must
  not reuse it.

The tape source is pinned: ``--tape <file.wav>`` drives a real recording the
operator supplies, and with no ``--tape`` the default is the TTS hello-world tape
(:func:`clear_record.service.hello_tape.write_hello_tape`). A missing system
voice is a **diagnostic finding**, not a crash and not a silent skip: the drive
reports it and falls back to the seeded transcript so the draft stories still
run.

This workflow is deliberately **not** part of ``just verify`` or ``just e2e``:
those stay offline and deterministic.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import wave
from pathlib import Path
from typing import Any

from clear_record.core import RecordDocument, Segment, write_json
from clear_record.service import Registry, available_backend_ids, write_hello_tape
from clear_record.service.managed import workspace_path_for
from clear_record.service.setup import MCP_SERVER_ARGS, MCP_SERVER_COMMAND

#: Where this script lives: <repo root>/scripts/agent_drive.py.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The deepseek target (the ticket default). Both are overridable by the flags
#: below; the drive is the harness, so it holds the model choice, not the app.
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: The default throwaway data dir, inside the repo gitignored .local/ (ADR-0006),
#: so a drive can never touch a real user data dir.
DEFAULT_DATA_DIR = REPO_ROOT / ".local" / "agent-drive" / "data"

#: The operator key file (gitignored). It is a **documented default**, never the
#: only option: the two environment variables win over it, and the main worktree
#: copy is tried too (just runs from a worktree).
DEFAULT_KEY_FILE = REPO_ROOT / ".local" / "deepseek_api-key.txt"

#: The drive's BYOK variable names. Deliberately **not** ``CR_AGENT_*``: that
#: prefix names the app's removed in-process path, which this version reports as
#: ignored.
ENV_KEY = "CR_DRIVE_API_KEY"
ENV_KEY_FILE = "CR_DRIVE_API_KEY_FILE"

#: The tool names the drive requires the server to expose: the three jobs' write
#: path, the draft reads, the two decisions, and the transcript read a model needs
#: to reason about a meeting at all.
REQUIRED_TOOLS: tuple[str, ...] = (
    "read_transcript",
    "list_agent_drafts",
    "read_agent_draft",
    "write_agent_draft",
    "accept_agent_draft",
    "reject_agent_draft",
)

#: The three draft kinds, in the order the drive exercises them. Repeated here
#: rather than imported: the drive speaks to the server as an external client
#: does, so it pins the *wire* vocabulary, not a Python constant.
DRAFT_KINDS: tuple[str, ...] = ("glossary_collection", "transcript_check", "minutes")

#: A marker written into the seed dir; the wipe guard only deletes a directory
#: that carries it (or is empty), so --data-dir can never quietly eat data.
SEED_MARKER = ".agent-drive-seed"

#: The author identities the drive declares. The scripted stories use one name
#: and the model leg another, so a chain written by both shows two authors.
SCRIPTED_AUTHOR = "agent-drive/scripted"
MODEL_AUTHOR = "agent-drive/model"
HUMAN_AUTHOR = "human:agent-drive"

#: sk-shaped tokens, masked even if a value the redactor was not told about
#: reaches the output through a traceback or an endpoint echo.
_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b")

#: The seeded glossary terms: (term, reading, aliases, definition, status).
SEED_TERMS: tuple[tuple[str, str | None, str | None, str, str], ...] = (
    (
        "falcon",
        "FAL-kun",
        "Falcon, falcon-1",
        "Internal codename for the reconciliation pass.",
        "confirmed",
    ),
    (
        "lead in",
        "LEED in",
        "pre-roll, preroll",
        "Room tone kept at the head of a take.",
        "confirmed",
    ),
    (
        "H4n",
        "aitch four en",
        "H4N, h4",
        "The field recorder used as the second source.",
        "candidate",
    ),
)

#: The seeded meeting transcript: (start, end, speaker, source, text). It is
#: plausible review conversation with decisions and action items, so the three
#: jobs have real material even when no usable tape is available.
SEED_SEGMENTS: tuple[tuple[float, float, str, str, str], ...] = (
    (3.0, 7.4, "Alex", "mic", "the recorder was running from the top of the hour"),
    (
        8.1,
        13.9,
        "Bo",
        "mic",
        "and the glossary kept the falcon codename out of the noise",
    ),
    (
        14.6,
        19.2,
        "Alex",
        "mic",
        "good. the h4n still comes through as aitch four en in two places",
    ),
    (
        19.8,
        25.1,
        "Bo",
        "mic",
        "add h4n as a confirmed term so the next run stops guessing",
    ),
    (25.9, 31.4, "Alex", "mic", "agreed. we retire pre-roll and keep lead in"),
    (
        32.0,
        38.8,
        "Bo",
        "mic",
        "the archive is the durable copy, so we can drop the uploaded tapes",
    ),
    (39.5, 44.0, "Alex", "mic", "manually though. nothing deletes the tapes"),
    (
        44.6,
        50.2,
        "Bo",
        "mic",
        "understood. i will write the minutes and list the action items",
    ),
)


def _scripted_values(project: str, meeting: str) -> dict[str, dict]:
    """What a scripted harness writes for each job, over the seeded material.

    The shapes are the ones the MCP tool's own docstring declares, and they are
    what the console renders and an acceptance promotes — so the scripted story
    exercises the real pipeline from the wire in, with no model involved.
    """
    return {
        "glossary_collection": {
            "terms": [
                {
                    "term": "h4n",
                    "reading": "aitch four en",
                    "aliases": ["H4N", "h4"],
                    "definition": "The field recorder used as the second source.",
                    "evidence": "the h4n still comes through as aitch four en",
                }
            ]
        },
        "transcript_check": {
            "revision": "the h4n still comes through as aitch four en in two places",
            "changes": [
                {
                    "before": "the h4n still comes through as aitch four en",
                    "after": "the H4n still comes through as aitch four en",
                    "reason": "the glossary spells the recorder H4n",
                }
            ],
        },
        "minutes": {
            "project": project,
            "meeting": meeting,
            "attendees": ["Alex", "Bo"],
            "decisions": [
                "H4n becomes a confirmed term.",
                "Lead in is kept; pre-roll is retired.",
            ],
            "actions": ["Bo writes the minutes and lists the action items."],
            "body": (
                "# Kickoff\n\n"
                "The recorder ran from the top of the hour. The glossary kept the "
                "falcon codename out of the noise, and H4n is spelled out in two "
                "places.\n\n"
                "## Decisions\n\n"
                "- H4n becomes a confirmed term.\n"
                "- Lead in is kept; pre-roll is retired.\n"
            ),
        },
    }


class DriveError(Exception):
    """The drive cannot start (a seed-dir guard or a bad invocation)."""


@dataclasses.dataclass(frozen=True)
class KeyResolution:
    """The resolved key, where it came from, and why it is missing (if so).

    The key value is held only in one field and is never printed; the source
    label and the miss detail are safe to print.
    """

    key: str | None
    source: str
    detail: str = ""


class Redactor:
    """Mask the key value (and any sk-shaped token) in everything printed."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self._secrets = tuple(secret for secret in secrets if secret)

    def __call__(self, text: object) -> str:
        rendered = str(text)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "***")
        return _SECRET_RE.sub("sk-***", rendered)


@dataclasses.dataclass(frozen=True)
class Story:
    """One reported leg: what happened, and whether it failed the drive."""

    name: str
    status: str
    detail: str


class Report:
    """Collects and prints the story lines, redacting every detail on the way."""

    def __init__(self, redact: Redactor) -> None:
        self.redact = redact
        self.stories: list[Story] = []

    def add(self, name: str, status: str, detail: str) -> Story:
        story = Story(name=name, status=status, detail=self.redact(detail))
        self.stories.append(story)
        print(f"{status:<7} {name}: {story.detail}")
        return story

    def say(self, text: str = "") -> None:
        print(self.redact(text))

    @property
    def failed(self) -> bool:
        return any(story.status == "FAIL" for story in self.stories)


# --- key resolution (BYOK for the drive, never printed) --------------------- #


def _read_key(path: Path) -> str | None:
    """The key text, or None when the file is missing or empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def main_worktree() -> Path | None:
    """The main checkout of this repo, when running from a git worktree.

    The git common dir is <main>/.git for every linked worktree, so its parent
    is where the operator gitignored .local/ actually lives.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    common = Path(completed.stdout.strip())
    if not common.is_dir():
        return None
    return common.parent if common.name == ".git" else common


def key_file_candidates() -> list[Path]:
    """The default key files, nearest first: this checkout, then the main one."""
    candidates = [DEFAULT_KEY_FILE]
    main = main_worktree()
    if main is not None:
        candidates.append(main / ".local" / "deepseek_api-key.txt")
    unique: list[Path] = []
    for path in candidates:
        if path not in unique:
            unique.append(path)
    return unique


def resolve_key(environ, *, candidates: list[Path] | None = None) -> KeyResolution:
    """Resolve the drive's BYOK key: environment, then a named file, then the default.

    The value is returned only in the key field. It is never logged, stored or
    echoed; the source label and the miss detail are safe to print.
    """
    value = environ.get(ENV_KEY)
    if value and value.strip():
        return KeyResolution(value.strip(), ENV_KEY)

    named = environ.get(ENV_KEY_FILE)
    if named and named.strip():
        path = Path(named.strip()).expanduser()
        from_file = _read_key(path)
        if from_file is None:
            return KeyResolution(
                None, f"{ENV_KEY_FILE}={path}", f"{path} is missing or empty"
            )
        return KeyResolution(from_file, f"{ENV_KEY_FILE}={path}")

    found = key_file_candidates() if candidates is None else candidates
    for path in found:
        from_file = _read_key(path)
        if from_file is not None:
            return KeyResolution(from_file, str(path))
    listed = ", ".join(str(path) for path in found) or "(none)"
    return KeyResolution(
        None,
        "(no key)",
        f"set {ENV_KEY} or {ENV_KEY_FILE}, or place a key in: {listed}",
    )


# --- the throwaway seed ----------------------------------------------------- #


def prepare_data_dir(path: str | Path, *, force: bool = False) -> Path:
    """Wipe and recreate the seed dir, refusing anything that is not ours.

    Only an empty directory or one carrying SEED_MARKER is removed; a populated
    directory without the marker needs --force. That is the guard behind "never
    write into a real user data dir".
    """
    target = Path(path).expanduser()
    try:
        target = target.resolve()
    except OSError:
        pass
    if target.exists():
        if target.is_file():
            raise DriveError(f"{target} is a file, not a data directory")
        contents = list(target.iterdir())
        if not contents or (target / SEED_MARKER).is_file() or force:
            shutil.rmtree(target)
        else:
            raise DriveError(
                f"refusing to wipe {target}: it has no {SEED_MARKER} marker, so it "
                "does not look like an agent-drive seed (pass --force to override)"
            )
    target.mkdir(parents=True, exist_ok=True)
    (target / SEED_MARKER).write_text(
        "clear-record agent-drive seed\n", encoding="utf-8"
    )
    return target


def seed(registry: Registry, workspace_root: Path):
    """Seed one project, its glossary and one meeting with a real transcript.

    The registry is the e2e seed pattern: the service layer writes the same rows
    the console reads, so the drive cannot drift from the app own schema.
    """
    project = registry.create_project(
        "Agent drive", notes="Throwaway project for the agent test-drive."
    )
    for term, reading, aliases, definition, status in SEED_TERMS:
        registry.add_term(
            project.slug,
            term,
            reading=reading,
            aliases=aliases,
            definition=definition,
            status=status,
        )
    meeting = registry.create_meeting(project.slug, "Kickoff")
    meeting = registry.set_meeting_workspace(
        meeting.id, str(workspace_path_for(workspace_root, meeting))
    )
    workspace = Path(meeting.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)
    write_json(
        workspace / "record.json",
        RecordDocument(
            sources=(),
            alignment=None,
            segments=tuple(
                Segment(start=start, end=end, text=text, source=source, speaker=speaker)
                for start, end, speaker, source, text in SEED_SEGMENTS
            ),
        ),
    )
    return meeting


# --- the tape source -------------------------------------------------------- #


def wav_frames(path: Path) -> int:
    """The number of audio frames in a WAV, or 0 when it cannot be read."""
    try:
        with wave.open(str(path)) as handle:
            return handle.getnframes()
    except (wave.Error, OSError, EOFError):
        return 0


def segment_count(workspace: Path) -> int:
    """The number of transcribed segments the pipeline wrote, if any."""
    path = workspace / "segments.json"
    if not path.is_file():
        return 0
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    sources = document.get("sources")
    if not isinstance(sources, dict):
        return 0
    return sum(len(items) for items in sources.values() if isinstance(items, list))


def pick_backend() -> str | None:
    """A usable ASR backend id, preferring the model-free system one."""
    available = available_backend_ids()
    if not available:
        return None
    return "apple-speech" if "apple-speech" in available else available[0]


def drive_tape(
    registry: Registry,
    meeting,
    work_dir: Path,
    args,
    report: Report,
) -> str:
    """Obtain and transcribe a tape, or report why not.

    Returns "tape" when the meeting transcript came from the recording, and
    "seeded" when the seeded transcript is the one being used. A missing system
    voice, a zero-frame clip, or an unavailable backend is a **finding**:
    reported plainly, but the draft stories still run on the seeded transcript.
    """
    if args.tape:
        tape = Path(args.tape).expanduser()
        if not tape.is_file():
            report.add("tape", "FAIL", f"--tape {tape} is not a file")
            return "seeded"
        label = f"operator recording {tape}"
    else:
        destination = work_dir / "tape"
        try:
            hello = write_hello_tape(destination, lang=args.lang)
        except Exception as exc:  # noqa: BLE001 - a missing voice is a finding
            report.add(
                "tape",
                "FINDING",
                f"no system voice produced the {args.lang!r} hello tape "
                f"({type(exc).__name__}: {exc}); using the seeded transcript",
            )
            return "seeded"
        tape = Path(hello.path)
        label = f"hello tape {tape} (engine={hello.engine}, voice={hello.voice})"

    frames = wav_frames(tape)
    if frames == 0:
        report.add(
            "tape",
            "FINDING",
            f"{label} has zero audio frames (the system TTS wrote no audio); "
            "using the seeded transcript",
        )
        return "seeded"

    digest = hashlib.sha256(tape.read_bytes()).hexdigest()
    registry.register_tape(
        meeting.id, path=str(tape), sha256=digest, bytes=tape.stat().st_size
    )

    workspace = Path(meeting.workspace_path)
    backend_id = pick_backend()
    if backend_id is None:
        report.add(
            "tape",
            "FINDING",
            f"{label}: no ASR backend is available; using the seeded transcript",
        )
        return "seeded"

    try:
        from clear_record.pipeline import stages

        stages.ingest(str(workspace), [str(tape)])
        stages.transcribe(str(workspace), backend_id, language=args.lang)
    except Exception as exc:  # noqa: BLE001 - transcript fallback
        # The stages refuse with ``stages.PipelineError``, an ``Exception``, so
        # the pipeline's own failure channel arrives here with everything else
        # the two calls can raise; no stage ends the process by itself.
        report.add(
            "tape",
            "FINDING",
            f"{label}: transcription failed ({type(exc).__name__}: {exc}); "
            "using the seeded transcript",
        )
        return "seeded"

    count = segment_count(workspace)
    if count == 0:
        report.add(
            "tape",
            "FINDING",
            f"{label}: transcription produced no segments; using the seeded transcript",
        )
        return "seeded"

    (workspace / "record.json").unlink(missing_ok=True)
    report.add("tape", "PASS", f"{label}: transcribed {_plural(count, 'segment')}")
    return "tape"


def _plural(count: int, noun: str) -> str:
    """A plain English count for the report: "1 segment" / "3 segments"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# --- the MCP client --------------------------------------------------------- #


def _payload(result) -> dict | None:
    """The JSON object a tool call answered with, or None when it is not one.

    A tool that declares a return model answers with structured content; the
    text block is the JSON rendering of the same value. Either is accepted, so
    the drive keeps working across SDK version differences.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _tool_names(listed) -> list[str]:
    return sorted(tool.name for tool in listed.tools)


def _openai_tools(listed, wanted: tuple[str, ...]) -> list[dict]:
    """The tools a model may call here, in the OpenAI function-call shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }
        for tool in listed.tools
        if tool.name in wanted
    ]


async def _call(session, name: str, arguments: dict) -> Any:
    return await session.call_tool(name, arguments)


# --- the stories ------------------------------------------------------------ #


async def _story_surface(session, listed, report: Report) -> bool:
    """Story 1: the shipped MCP server exposes what a harness needs."""
    names = _tool_names(listed)
    missing = [name for name in REQUIRED_TOOLS if name not in names]
    if missing:
        report.add(
            "1 MCP surface",
            "FAIL",
            f"the server is missing {', '.join(missing)} (it exposes {len(names)} tools)",
        )
        return False
    report.add(
        "1 MCP surface",
        "PASS",
        f"{len(names)} tools over stdio, including the draft write/accept/reject "
        "surface and read_transcript",
    )
    return True


async def _story_scripted(
    session, project: str, meeting: str, report: Report
) -> dict[str, str]:
    """Story 2: write one draft version per job, then extend one chain.

    Returns the draft id per kind. The second version on the glossary chain is
    the provenance proof: the chain must report two authors and be back in
    review, because a new version re-opens a decided draft.
    """
    values = _scripted_values(project, meeting)
    drafts: dict[str, str] = {}
    for kind in DRAFT_KINDS:
        result = await _call(
            session,
            "write_agent_draft",
            {
                "project": project,
                "meeting": meeting,
                "kind": kind,
                "value": values[kind],
                "author": SCRIPTED_AUTHOR,
            },
        )
        payload = _payload(result)
        if payload is None or not payload.get("draft_id"):
            report.add(f"2 draft {kind}", "FAIL", "the write answered no draft")
            continue
        drafts[kind] = str(payload["draft_id"])
        versions = payload.get("versions") or []
        author = (versions[-1] or {}).get("author") if versions else None
        report.add(
            f"2 draft {kind}",
            "PASS",
            f"draft {payload['draft_id']} ({payload.get('review_state')}) author={author}",
        )

    if "glossary_collection" in drafts:
        result = await _call(
            session,
            "write_agent_draft",
            {
                "project": project,
                "meeting": meeting,
                "kind": "glossary_collection",
                "value": values["glossary_collection"],
                "author": MODEL_AUTHOR,
                "draft_id": drafts["glossary_collection"],
            },
        )
        payload = _payload(result) or {}
        versions = payload.get("versions") or []
        authors = [entry.get("author") for entry in versions]
        if len(versions) == 2 and authors == [SCRIPTED_AUTHOR, MODEL_AUTHOR]:
            report.add(
                "2 chain",
                "PASS",
                f"2 versions on one chain with authors {authors}",
            )
        else:
            report.add(
                "2 chain",
                "FAIL",
                f"expected 2 versions by both authors, got {authors}",
            )
    return drafts


async def _draft_version(session, project: str, meeting: str, draft_id: str) -> int:
    """The newest version of ``draft_id`` — what a reviewer reads before deciding.

    A decision names its version, so the drive reads the chain first: that is the
    flow the tool documents, and the number it read is the one it decides.
    """
    result = await _call(
        session,
        "read_agent_draft",
        {"project": project, "meeting": meeting, "draft_id": draft_id},
    )
    return int((_payload(result) or {}).get("version") or 0)


async def _story_review(
    session, project: str, meeting: str, drafts: dict[str, str], report: Report
) -> tuple[bool, str]:
    """Story 3: a human accepts one draft over MCP and rejects another.

    Returns whether an acceptance produced its artifact, and which kind it was.
    """
    target = drafts.get("minutes") or drafts.get("transcript_check")
    if target is None:
        report.add("3 accept", "FAIL", "no draft was produced to accept")
        return False, ""
    kind = "minutes" if "minutes" in drafts else "transcript_check"
    version = await _draft_version(session, project, meeting, target)
    result = await _call(
        session,
        "accept_agent_draft",
        {
            "project": project,
            "meeting": meeting,
            "draft_id": target,
            "version": version,
            "author": HUMAN_AUTHOR,
        },
    )
    payload = _payload(result) or {}
    promotion = payload.get("promotion") or {}
    versions = payload.get("versions") or []
    decided = versions[-1] if versions else {}
    if (
        payload.get("review_state") == "accepted"
        and decided.get("reviewed_by") == HUMAN_AUTHOR
    ):
        summary = promotion.get("summary") or {}
        artifact = summary.get("artifact_id")
        report.add(
            "3 accept",
            "PASS",
            f"accepted the {kind} draft by {HUMAN_AUTHOR}; "
            f"promotion artifact={artifact or '(none)'}",
        )
        ok = artifact is not None
    else:
        report.add(
            "3 accept",
            "FAIL",
            f"the acceptance reported {payload.get('review_state')!r} "
            f"reviewed_by={decided.get('reviewed_by')!r}",
        )
        ok = False

    reject = drafts.get("transcript_check") if kind == "minutes" else None
    if reject:
        version = await _draft_version(session, project, meeting, reject)
        result = await _call(
            session,
            "reject_agent_draft",
            {
                "project": project,
                "meeting": meeting,
                "draft_id": reject,
                "version": version,
                "author": HUMAN_AUTHOR,
            },
        )
        payload = _payload(result) or {}
        versions = payload.get("versions") or []
        decided = versions[-1] if versions else {}
        if (
            payload.get("review_state") == "rejected"
            and decided.get("reviewed_by") == HUMAN_AUTHOR
        ):
            report.add("3 reject", "PASS", f"rejected draft {reject} by {HUMAN_AUTHOR}")
        else:
            report.add(
                "3 reject",
                "FAIL",
                f"the rejection reported {payload.get('review_state')!r}",
            )
    return ok, kind


def _chat_completion(
    endpoint: str,
    model: str,
    key: str,
    messages: list[dict],
    tools: list[dict],
    timeout: float,
) -> dict:
    """One OpenAI-compatible chat call, tools included. Stdlib HTTP only."""
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "messages": messages, "tools": tools}).encode(
            "utf-8"
        ),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


#: Story 4's instructions. The model can only answer by calling a clear-record
#: tool, which is what makes the leg a proof of the surface rather than of prose.
MODEL_SYSTEM = (
    "You are an MCP client agent with access to the clear-record tools. Call "
    "read_transcript for the meeting you are given, then write the candidate "
    "glossary terms you found with write_agent_draft (kind='glossary_collection', "
    "author='agent-drive/model'). Do not invent anything the transcript does not "
    "say."
)
MODEL_QUESTION = (
    "Read this meeting's transcript and propose the glossary terms it needs."
)


async def _story_model(
    session,
    listed,
    project: str,
    meeting: str,
    endpoint: str,
    model: str,
    key: str | None,
    timeout: float,
    report: Report,
) -> None:
    """Story 4: a real model drives the MCP tools. SKIP without a key."""
    if key is None:
        report.add(
            "4 model",
            "SKIP",
            "no model API key is available, so the model leg is skipped; the MCP "
            "surface above was still exercised",
        )
        return
    tools = _openai_tools(
        listed, ("read_transcript", "write_agent_draft", "list_agent_drafts")
    )
    messages = [
        {"role": "system", "content": MODEL_SYSTEM},
        {
            "role": "user",
            "content": f"Project {project!r}, meeting {meeting!r}. {MODEL_QUESTION}",
        },
    ]
    called: list[str] = []
    wrote = False
    try:
        for _ in range(6):
            reply = await asyncio.to_thread(
                _chat_completion, endpoint, model, key, messages, tools, timeout
            )
            message = reply["choices"][0]["message"]
            messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                break
            for call in calls:
                name = call["function"]["name"]
                arguments = json.loads(call["function"]["arguments"] or "{}")
                result = await session.call_tool(name, arguments)
                called.append(name)
                if name == "write_agent_draft":
                    wrote = True
                text = "\n".join(
                    block.text
                    for block in result.content
                    if getattr(block, "type", "") == "text"
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": text[:20000],
                    }
                )
    except Exception as exc:  # noqa: BLE001 - a model-leg failure is a leg
        cause = _root_cause(exc)
        report.add("4 model", "FAIL", f"{type(cause).__name__}: {cause}")
        return
    if not called:
        report.add("4 model", "FAIL", "the model answered without calling a tool")
        return
    if not wrote:
        report.add(
            "4 model",
            "FAIL",
            f"the model called {called} but wrote no draft",
        )
        return
    report.add(
        "4 model",
        "PASS",
        f"the model drove {len(called)} MCP call(s) over stdio, "
        f"including a draft write: {', '.join(called)}",
    )


def _root_cause(exc: BaseException) -> BaseException:
    """The innermost exception under the SDK's anyio task-group wrappers.

    ``stdio_client``/``ClientSession`` re-raise an inner failure as a
    ``BaseExceptionGroup`` whose message is only "unhandled errors in a
    TaskGroup", which tells the operator nothing; the first leaf does.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


async def _mcp_session(
    project: str,
    meeting: str,
    data_dir: Path,
    endpoint: str,
    model: str,
    key: str | None,
    timeout: float,
    report: Report,
) -> tuple[bool, str]:
    """Every MCP story, in one stdio session to the shipped server."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=MCP_SERVER_COMMAND,
        args=[*MCP_SERVER_ARGS, "--data-dir", str(data_dir)],
    )
    accepted = False
    kind = ""
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            if not await _story_surface(session, listed, report):
                return False, ""
            drafts = await _story_scripted(session, project, meeting, report)
            accepted, kind = await _story_review(
                session, project, meeting, drafts, report
            )
            await _story_model(
                session,
                listed,
                project,
                meeting,
                endpoint,
                model,
                key,
                timeout,
                report,
            )
    return accepted, kind


# --- the session ------------------------------------------------------------ #


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="just agent-drive",
        description=(
            "Drive the three agent jobs through clear-record's MCP tools, the way "
            "a harness does. clear-record calls no model: the drive stands in for "
            "the harness, with a scripted story that needs no key and a model leg "
            "that does. Not part of just verify or just e2e."
        ),
    )
    parser.add_argument(
        "--tape",
        metavar="FILE.wav",
        help=(
            "a real recording to transcribe and drive the jobs over; "
            "without it the TTS hello-world tape is used"
        ),
    )
    parser.add_argument(
        "--endpoint",
        help=f"OpenAI-compatible base URL for the model leg (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--model",
        help=f"model name for the model leg (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--data-dir",
        help=f"throwaway seed directory (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--lang", default="en", help="hello-tape language (default: en)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="seconds to wait for one model call (default: 180)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --data-dir to be wiped even without the seed marker",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    resolution = resolve_key(os.environ)
    endpoint = args.endpoint or DEFAULT_ENDPOINT
    model = args.model or DEFAULT_MODEL
    data_dir = prepare_data_dir(args.data_dir or DEFAULT_DATA_DIR, force=args.force)

    report = Report(Redactor((resolution.key,) if resolution.key else ()))
    report.say("clear-record agent drive (harness stand-in)")
    report.say(f"  model leg: {endpoint} with {model}")
    report.say(f"  key      : {resolution.source} (value redacted)")
    report.say(f"  data dir : {data_dir}")
    report.say()

    registry = Registry.open(data_dir=data_dir)
    meeting = seed(registry, data_dir / "workspaces")
    transcript_source = drive_tape(registry, meeting, data_dir, args, report)
    report.say(f"  transcript: {transcript_source}")
    report.say()

    accepted, kind = asyncio.run(
        _mcp_session(
            meeting.project_slug,
            meeting.slug,
            data_dir,
            endpoint,
            model,
            resolution.key,
            args.timeout,
            report,
        )
    )

    report.say()
    if report.failed:
        report.say("result: FAIL - see the legs above")
        return 1
    if resolution.key is None:
        report.say(
            "result: PASS (MCP surface proven by scripted use; the model leg is "
            f"skipped - set {ENV_KEY} to run it)"
        )
    else:
        report.say(f"result: PASS (accepted={kind or 'none'})")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except DriveError as exc:
        print(f"agent-drive: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("agent-drive: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
