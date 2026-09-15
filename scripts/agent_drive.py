#!/usr/bin/env python3
"""Agentic test-drive: the optional, on-demand agent story against a real endpoint.

Ticket 07. The three agent tasks of ADR-0018 (glossary collection, transcript
check, minutes) are normally proven offline against a fake endpoint, which is
what "just verify" must stay. This script is the **other** half: drive the same
tasks against a real OpenAI-compatible endpoint the operator brings, over a
throwaway seeded meeting, so the agent experience can be exercised on demand.

**Run it through just** (the pointer recipe lives in the justfile; this line is
what it runs)::

    just agent-drive
    just agent-drive --tape path/to/recording.wav

The "--all-packages" flag is what makes the script work: it imports
"clear_record", so it must run inside the project environment, exactly like
"scripts/agent_setup.py".

The stories, in order, each reported:

1. **Endpoint** - point at the OpenAI-compatible target (deepseek by default)
   and prove it with a real test call through the same runner the tasks use.
2. **Agent tasks** - over a seeded meeting, run glossary collection, transcript
   check and minutes; assert each produced a draft, and print a redacted summary.
3. **Accept one draft** - promote it and assert the promoted artifact exists.
4. **MCP round-trip** - a later story; reported as skipped here, not faked.

Key handling is BYOK (ADR-0018) and is a hard rule:

- the key is read at run time from "CR_AGENT_API_KEY", or from the file named by
  "CR_AGENT_API_KEY_FILE", or from the operator's gitignored default
  ("<repo>/.local/deepseek_api-key.txt", also checked in the main worktree);
- the value is **never printed**, never written to config or the registry, and
  every line of output is passed through :class:`Redactor`;
- without a key the drive prints a clear message and **exits 0** - it never
  hangs and never crashes.

The tape source is pinned: "--tape <file.wav>" drives a real recording the
operator supplies, and with no "--tape" the default is the TTS hello-world tape
(:func:`clear_record.service.hello_tape.write_hello_tape`). A missing system
voice is a **diagnostic finding**, not a crash and not a silent skip: the drive
reports it and falls back to the seeded transcript so the agent stories still
run. On a machine whose "say" writes zero-frame audio (this one), that finding
is the correct no-argument outcome, not a bug to fix.

This workflow is deliberately **not** part of "just verify" or "just e2e": those
stay offline and deterministic.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from clear_record.core import RecordDocument, Segment, write_json
from clear_record.service import (
    TASK_KINDS,
    EndpointRunner,
    MeetingAgent,
    Registry,
    available_backend_ids,
    write_hello_tape,
)
from clear_record.service.managed import workspace_path_for
from clear_record.service.setup import verify_endpoint

#: Where this script lives: <repo root>/scripts/agent_drive.py.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: The deepseek target (the ticket default). Both are overridable by the
#: CR_AGENT_ENDPOINT / CR_AGENT_MODEL variables or the flags below.
DEFAULT_ENDPOINT = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: The default throwaway data dir, inside the repo gitignored .local/ (ADR-0006),
#: so a drive can never touch a real user data dir.
DEFAULT_DATA_DIR = REPO_ROOT / ".local" / "agent-drive" / "data"

#: The operator key file (gitignored). It is a **documented default**, never the
#: only option: the two environment variables win over it, and the main
#: worktree copy is tried too (just runs from a worktree).
DEFAULT_KEY_FILE = REPO_ROOT / ".local" / "deepseek_api-key.txt"

#: BYOK variable names (ADR-0018).
ENV_KEY = "CR_AGENT_API_KEY"
ENV_KEY_FILE = "CR_AGENT_API_KEY_FILE"

#: The environment variable name the **resolved value** is handed to the runner
#: under. It lives only in an in-memory mapping, never in os.environ and never on
#: disk; the runner own config stores only a name.
KEY_ENV_NAME = "CR_AGENT_DRIVE_KEY"

#: A marker written into the seed dir; the wipe guard only deletes a directory
#: that carries it (or is empty), so --data-dir can never quietly eat data.
SEED_MARKER = ".agent-drive-seed"

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
#: tasks have real material even when no usable tape is available.
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


# --- key resolution (BYOK, never printed) ----------------------------------- #


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
    """Resolve the BYOK key: environment, then a named file, then the default.

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
    reported plainly, but the agent stories still run on the seeded transcript.
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
        from clear_record.cli import stages

        stages.ingest(str(workspace), [str(tape)])
        stages.transcribe(str(workspace), backend_id, language=args.lang)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - transcript fallback
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
    report.add("tape", "PASS", f"{label}: transcribed {count} segment(s)")
    return "tape"


# --- the stories ------------------------------------------------------------ #


def summarize_draft(draft) -> str:
    """A redacted, one-line summary of what a draft holds."""
    value = draft.value if isinstance(draft.value, dict) else {}
    if draft.kind == "glossary_collection":
        terms = value.get("terms") or []
        names = ", ".join(str(item.get("term", "")) for item in terms if item)
        body = f"{len(terms)} proposed term(s)"
        if names:
            body += f": {names}"
    elif draft.kind == "transcript_check":
        changes = value.get("changes") or []
        body = (
            f"revision {len(str(value.get('revision', '')))} chars, "
            f"{len(changes)} change(s)"
        )
    elif draft.kind == "minutes":
        body = (
            f"body {len(str(value.get('body', '')))} chars, "
            f"{len(value.get('decisions') or [])} decision(s), "
            f"{len(value.get('actions') or [])} action(s)"
        )
    else:
        body = f"{len(draft.text)} bytes"
    return (
        f"{body}; run={draft.provenance.run_id} runner={draft.provenance.runner} "
        f"model={draft.provenance.model or '?'} bytes={draft.artifact_path.stat().st_size}"
    )


def drive_endpoint(endpoint: str, model: str, key: str, timeout: float, report: Report):
    """Story 1: prove the endpoint with a real test call through the runner."""
    result = verify_endpoint(
        endpoint,
        model=model,
        api_key_env=KEY_ENV_NAME,
        environ={KEY_ENV_NAME: key},
        timeout=timeout,
    )
    if result.ok:
        report.add(
            "1 endpoint",
            "PASS",
            f"{endpoint} answered the test call with model "
            f"{result.model or model or 'its own model'}",
        )
        return True
    detail = str(result.detail) if result.detail is not None else "no detail"
    report.add("1 endpoint", "FAIL", f"{endpoint} did not answer: {detail}")
    return False


def drive_tasks(agent: MeetingAgent, report: Report) -> list:
    """Story 2: run each task kind and require every one to produce a draft."""
    drafts: list = []
    for kind in TASK_KINDS:
        try:
            draft = agent.launch(kind)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - one leg, reported
            report.add(
                f"2 task {kind}", "FAIL", f"launch failed: {type(exc).__name__}: {exc}"
            )
            continue
        if draft.review_state != "draft":
            report.add(
                f"2 task {kind}",
                "FAIL",
                f"expected a draft, got review_state={draft.review_state!r}",
            )
            continue
        drafts.append(draft)
        report.add(f"2 task {kind}", "PASS", summarize_draft(draft))
    return drafts


def drive_accept(agent: MeetingAgent, drafts: list, report: Report) -> tuple[bool, str]:
    """Story 3: promote one draft and assert its promoted artifact exists."""
    target = next((d for d in drafts if d.kind == "minutes"), None)
    if target is None:
        target = next((d for d in drafts if d.kind == "transcript_check"), None)
    if target is None and drafts:
        target = drafts[0]
    if target is None:
        report.add("3 accept", "FAIL", "no draft was produced to accept")
        return False, ""
    try:
        promoted = agent.promote(target)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - one leg, reported
        report.add(
            "3 accept",
            "FAIL",
            f"promoting the {target.kind} draft failed: {type(exc).__name__}: {exc}",
        )
        return False, target.kind
    promotion = promoted.promotion or {}

    if target.kind == "minutes":
        artifact = agent.registry.latest_artifact(agent.meeting.id, "minutes")
        ok = artifact is not None and Path(artifact.path).is_file()
        detail = (
            f"accepted minutes -> {artifact.path}"
            if ok
            else "accepted minutes, but no minutes artifact was registered"
        )
    elif target.kind == "transcript_check":
        artifact = agent.registry.latest_artifact(
            agent.meeting.id, "transcript_revision"
        )
        ok = artifact is not None and Path(artifact.path).is_file()
        detail = (
            f"accepted transcript_check -> {artifact.path}"
            if ok
            else "accepted transcript_check, but no revision artifact was registered"
        )
    else:
        added = (promotion.get("summary") or {}).get("added") or []
        ok = bool(added)
        names = ", ".join(str(name) for name in added)
        detail = (
            f"accepted glossary_collection -> added {len(added)} candidate term(s)"
            + (f": {names}" if names else "")
            if ok
            else "accepted glossary_collection, but no candidate term was added"
        )
    report.add("3 accept", "PASS" if ok else "FAIL", detail)
    return ok, target.kind


# --- the session ------------------------------------------------------------ #


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="just agent-drive",
        description=(
            "Drive clear-record own agent tasks against a real "
            "OpenAI-compatible endpoint. Optional and BYOK: without a key it "
            "skips. This is not part of just verify or just e2e."
        ),
    )
    parser.add_argument(
        "--tape",
        metavar="FILE.wav",
        help=(
            "a real recording to transcribe and drive the agent tasks over; "
            "without it the TTS hello-world tape is used"
        ),
    )
    parser.add_argument(
        "--endpoint",
        help=(
            "OpenAI-compatible base URL (default: CR_AGENT_ENDPOINT or "
            f"{DEFAULT_ENDPOINT})"
        ),
    )
    parser.add_argument(
        "--model",
        help=f"model name (default: CR_AGENT_MODEL or {DEFAULT_MODEL})",
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
        help="seconds to wait for one endpoint call (default: 180)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --data-dir to be wiped even without the seed marker",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    resolution = resolve_key(os.environ)
    if resolution.key is None:
        print("agent-drive: skipping - no agent API key is available.")
        print(f"  {resolution.detail}")
        print(
            f"  Set {ENV_KEY} or {ENV_KEY_FILE}; the value is never printed, "
            "stored or committed."
        )
        return 0

    report = Report(Redactor((resolution.key,)))
    endpoint = args.endpoint or os.environ.get("CR_AGENT_ENDPOINT") or DEFAULT_ENDPOINT
    model = args.model or os.environ.get("CR_AGENT_MODEL") or DEFAULT_MODEL
    data_dir = prepare_data_dir(args.data_dir or DEFAULT_DATA_DIR, force=args.force)

    report.say("clear-record agent test-drive")
    report.say(f"  endpoint : {endpoint}")
    report.say(f"  model    : {model}")
    report.say(f"  key      : {resolution.source} (value redacted)")
    report.say(f"  data dir : {data_dir}")
    report.say()

    registry = Registry.open(data_dir=data_dir)
    meeting = seed(registry, data_dir / "workspaces")
    transcript_source = drive_tape(registry, meeting, data_dir, args, report)
    report.say(f"  transcript: {transcript_source}")

    endpoint_ok = drive_endpoint(endpoint, model, resolution.key, args.timeout, report)

    runner = EndpointRunner(
        endpoint,
        model=model,
        api_key_env=KEY_ENV_NAME,
        timeout=args.timeout,
        environ={KEY_ENV_NAME: resolution.key},
    )
    agent = MeetingAgent(registry, meeting, runner=runner)
    drafts = drive_tasks(agent, report) if endpoint_ok else []
    accepted_kind = ""
    if drafts:
        _, accepted_kind = drive_accept(agent, drafts, report)
    if not drafts:
        report.add("3 accept", "SKIP", "no draft was produced, so nothing was accepted")
    report.add(
        "4 MCP round-trip",
        "SKIP",
        "later story; this slice drives clear-record own agent tasks, not MCP",
    )

    report.say()
    if report.failed:
        report.say("result: FAIL - see the legs above")
    else:
        report.say(
            f"result: PASS ({len(drafts)}/{len(TASK_KINDS)} drafts, "
            f"accepted={accepted_kind or 'none'})"
        )
    return 1 if report.failed else 0


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
