"""The agent setup flow and its hello-world acceptance check.

One reusable flow — **Harness -> MCP config -> Try it** — answers the
question "is this machine ready for an agent to work with clear-record?". It is
reached two ways (the setup wizard's Agent step and Settings -> Agent), and the
same **Try it** check is the permanent diagnostic Settings -> Status re-runs.

The check proves the local chain once, in one synchronous call:

1. create the locale's hello-world tape with the system voice
   (:func:`clear_record.service.hello_tape.write_hello_tape`);
2. ingest and transcribe it with the existing pipeline
   (``clear_record.cli.stages`` — the same stages ``scripts/agent_drive.py``
   drives, never a second implementation);
3. read the transcript back with the same read the MCP tool and the review use
   (:func:`clear_record.service.transcript.read_transcript`);
4. report the MCP entry that exposes that read, so the user can point a harness
   at it and prove the agent-answers leg with a real model (``just agent-drive``,
   ticket 07 — deliberately **not** part of ``just verify``/``just e2e``).

Every anticipated failure is a **finding**, not an exception: the result names
the leg that stopped — ``tts`` (no system voice), ``backend`` (no ASR backend),
``model`` (no checkpoint on disk), ``transcribe`` (the decode failed) — which is
the diagnostic's whole value. The finding carries the CLI's/service's own
:class:`~clear_record.cli.auto.Message`; the console renders it with ``tr`` and
never restates the rule.

The scratch workspace is app-owned state under ``<state>/hello-check``
(ADR-0006): the generated clip and its transcript are environment-local and are
never committed, never registered as a project, and never touched by a real
user's data.
"""

from __future__ import annotations

import dataclasses
import shutil
import threading
from collections.abc import Callable, Mapping
from pathlib import Path

from clear_record.cli import auto as _auto
from clear_record.cli.auto import Message
from clear_record.cli.tts import TtsError, TtsUnavailable
from clear_record.core.i18n import current_locale, deferred
from clear_record.core.paths import resolve_models_dir, resolve_state_dir
from clear_record.service.auto import (
    MODEL_LADDER,
    available_backend_ids,
    model_paths_on_disk,
)
from clear_record.service.hello_tape import HelloTape, write_hello_tape
from clear_record.service.models import Meeting
from clear_record.service.setup import SetupError, mcp_server_entry, read_setup_state
from clear_record.service.transcript import TranscriptSlice, read_transcript

#: The legs the check reports. ``ok`` means the transcript was produced; the
#: other four name the piece that stopped the chain, so a re-run localizes a
#: fault rather than only saying "failed".
LEG_OK = "ok"
LEG_TTS = "tts"
LEG_BACKEND = "backend"
LEG_MODEL = "model"
LEG_TRANSCRIBE = "transcribe"

#: The MCP tool that returns a meeting's transcript text. It is what the
#: "expose it over MCP" leg advertises, and a test pins it to the real MCP
#: surface (``clear_record.mcp.server.TOOL_NAMES``) so the two cannot drift.
TRANSCRIPT_TOOL = "read_transcript"

#: The scratch workspace's basename under the state dir. Fixed (not per-run):
#: the check wipes and recreates it, so a re-run never accumulates clips.
HELLO_CHECK_DIRNAME = "hello-check"

#: The fake project/meeting the transcript read is given. The read only needs a
#: meeting's workspace; this one is never registered, so it can never surface in
#: the console as a project.
_SCRATCH_PROJECT = "hello-world-check"
_SCRATCH_MEETING = "hello-world"

#: Serializes the scratch workspace against concurrent "Try it" clicks. The
#: console is local and single-user (ADR-0013); this is belt-and-braces so two
#: requests cannot interleave ingest/transcribe in one directory.
_CHECK_LOCK = threading.Lock()

WriteTape = Callable[..., HelloTape]
Backends = Callable[[], tuple[str, ...]]
Checkpoint = Callable[[], "Path | None"]
Pipeline = Callable[..., None]
Transcript = Callable[..., TranscriptSlice]
Entry = Callable[[], dict]


@dataclasses.dataclass(frozen=True)
class HelloCheck:
    """One hello-world check's outcome — the transcript, or the leg that failed.

    ``message`` is a stable ID plus parameters; a boundary renders it with
    ``tr`` (:meth:`clear_record.cli.auto.Message.render`) and ``str`` stays the
    English form for logs. On success ``transcript`` is the rendered page and
    ``segments`` its size; on a finding they are empty. ``entry``/``tool`` are
    the MCP leg the console shows — the client config entry and the transcript
    read the server exposes.
    """

    leg: str
    message: Message
    tape: HelloTape | None = None
    backend: str | None = None
    model: str | None = None
    transcript: str = ""
    segments: int = 0
    mcp_config: str | None = None
    harness: str | None = None
    tool: str = TRANSCRIPT_TOOL
    entry: dict = dataclasses.field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the whole local chain (tape -> transcript) worked once."""
        return self.leg == LEG_OK


def hello_check_workspace() -> Path:
    """The scratch workspace the check owns (``<state>/hello-check``)."""
    return resolve_state_dir() / HELLO_CHECK_DIRNAME


def _requested_lang(lang: str | None) -> str:
    """The language to speak: the request's, else the process's, else English."""
    return (lang or current_locale() or "en").strip() or "en"


def _on_disk_checkpoint() -> Path | None:
    """A ggml checkpoint already on disk, preferring the backend's default size.

    **Never downloads.** A missing checkpoint is the ``model`` finding, reported
    before the pipeline runs so a web request can never trigger a first-use
    download. The concrete path — not the size name — is returned, so a
    quantized file (``ggml-large-v3-q5_0.bin``) resolves as-is.
    """
    candidates = model_paths_on_disk()
    if not candidates:
        return None
    default = resolve_models_dir() / f"ggml-{_auto.DEFAULT_MODEL}.bin"
    if default.is_file():
        return default
    return candidates[0]


@dataclasses.dataclass(frozen=True)
class TranscriptionStatus:
    """Whether transcription can run here, and what is missing when it cannot.

    ``state`` is one of the check's legs: ``ok`` (a backend is available and,
    where one is needed, a checkpoint is on disk), ``backend`` (no ASR backend),
    or ``model`` (the backend needs a checkpoint and none is on disk). It is the
    decision :func:`run_hello_check` makes before it transcribes, exposed on its
    own so the setup wizard can state readiness instead of describing theory.
    ``message`` is the CLI's own finding for a ``backend`` state, so the
    acceptance check reports the same words the resolver chose.
    """

    state: str
    backend: str | None
    model: str | None
    models_dir: str
    models_present: tuple[str, ...]
    message: Message | None = None

    @property
    def ready(self) -> bool:
        """True when a run could transcribe without fetching anything."""
        return self.state == LEG_OK


def transcription_status(
    *,
    backends: Backends = available_backend_ids,
    checkpoint: Checkpoint = _on_disk_checkpoint,
    model_dir: str | None = None,
) -> TranscriptionStatus:
    """Report transcription readiness without running the pipeline.

    Never downloads: a missing checkpoint is the ``model`` state, not a fetch.
    It is the backend/model choice :func:`run_hello_check` makes, so the wizard
    and the acceptance check cannot disagree about what is ready.
    """
    models_dir = resolve_models_dir(model_dir)
    present = tuple(sorted(path.name for path in model_paths_on_disk(model_dir)))
    try:
        choice = _auto.resolve_backend(backends())
    except _auto.NoBackendAvailable as exc:
        return TranscriptionStatus(
            LEG_BACKEND, None, None, str(models_dir), present, exc.message
        )
    backend_id = choice.backend
    if backend_id == _auto.APPLE_SPEECH_BACKEND_ID:
        return TranscriptionStatus(LEG_OK, backend_id, None, str(models_dir), present)
    found = checkpoint()
    return TranscriptionStatus(
        LEG_OK if found is not None else LEG_MODEL,
        backend_id,
        str(found) if found is not None else None,
        str(models_dir),
        present,
    )


def download_transcription_model(
    model: str | None = None, *, model_dir: str | None = None
) -> str:
    """Download (and verify) a transcription checkpoint, on request.

    The console's explicit remediation for the ``model`` readiness state, and
    its model picker. It reuses the CLI's pinned, checksum-verified downloader,
    so a click here and a first transcription install identical bytes -- and it
    is reached only by a click: the acceptance check stays side-effect-free.

    ``model`` is a size from the ladder (tiny .. large-v3) or ``None`` for the
    resolved backend's own default. A named model is fetched as that exact ggml
    checkpoint **independently of the preferred backend** -- on macOS 26 the
    default resolves ``apple-speech``, whose ``prepare`` provisions a language
    asset rather than a ``.bin`` -- so the click always lands the chosen file;
    ``None`` still asks the resolved backend for its own default. Returns the
    model path, or the empty string when ``model`` is ``None`` and no backend
    can transcribe here.
    """
    from clear_record.cli import stages

    if model is not None and model not in MODEL_LADDER:
        raise SetupError(
            Message(
                deferred("unknown model {model!r}; expected one of {known}"),
                (("model", model), ("known", ", ".join(MODEL_LADDER))),
            )
        )
    if model is not None:
        # A named model is a ggml checkpoint request, whatever backend is
        # preferred here: on macOS 26 the default resolves ``apple-speech``,
        # whose ``prepare`` provisions a language asset, not a ``.bin``, so
        # routing through the backend would report success without fetching.
        return stages.download_ggml_model(model, model_dir)
    status = transcription_status(model_dir=model_dir)
    if status.backend is None:
        return ""
    if status.state != LEG_MODEL:
        return status.model or ""
    return stages.prepare_model(status.backend, model, model_dir)


def _run_pipeline(
    workspace: Path,
    tape: Path,
    *,
    backend_id: str,
    model: str | None,
    language: str,
) -> None:
    """Ingest ``tape`` and transcribe it with the existing CLI stages.

    Imported lazily so a plain ``import clear_record.service`` stays light, and
    so the service reuses the **one** stage wiring ``scripts/agent_drive.py``
    uses rather than reimplementing ingest/transcribe.
    """
    from clear_record.cli import stages

    stages.ingest(str(workspace), [str(tape)])
    stages.transcribe(str(workspace), backend_id, model=model, language=language)


def _scratch_meeting(workspace: Path) -> Meeting:
    """A never-registered meeting for :func:`read_transcript` to read."""
    return Meeting(
        id=0,
        project_id=0,
        project_slug=_SCRATCH_PROJECT,
        slug=_SCRATCH_MEETING,
        title="Hello World",
        recorded_at=None,
        workspace_path=str(workspace),
        notes="",
        status="recorded",
        created_at="",
    )


def _transcribe_finding(exc: BaseException) -> Message:
    """The ``transcribe`` finding, naming what the pipeline actually said."""
    detail = str(exc).strip() or type(exc).__name__
    return Message(
        deferred("transcription failed: {detail}"), (("detail", detail[:400]),)
    )


def run_hello_check(
    *,
    destination: Path | None = None,
    lang: str | None = None,
    state: Mapping | None = None,
    write_tape: WriteTape = write_hello_tape,
    backends: Backends = available_backend_ids,
    checkpoint: Checkpoint = _on_disk_checkpoint,
    pipeline: Pipeline = _run_pipeline,
    transcript: Transcript = read_transcript,
    entry: Entry = mcp_server_entry,
) -> HelloCheck:
    """Run the hello-world acceptance check and return its outcome.

    The parameters after ``state`` are the test seams: a caller can stub the
    slow pipeline (and the voice/backend probes) so the success path is asserted
    with no system voice, no ASR backend and no model — exactly what
    ``tests/service/test_agent_flow.py`` does. The web route uses every default.
    """
    requested = _requested_lang(lang)
    record = dict(state) if state is not None else read_setup_state()
    mcp_config = record.get("mcp_config")
    harness = record.get("harness")
    entry_value = entry()

    def finding(leg: str, message: Message, **extra) -> HelloCheck:
        return HelloCheck(
            leg=leg,
            message=message,
            mcp_config=mcp_config,
            harness=harness,
            tool=TRANSCRIPT_TOOL,
            entry=entry_value,
            **extra,
        )

    workspace = (
        Path(destination) if destination is not None else hello_check_workspace()
    )

    with _CHECK_LOCK:
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)

        # 1. The tape: the system voice in the requested locale.
        try:
            tape = write_tape(workspace / "tape", lang=requested)
        except TtsUnavailable as exc:
            if isinstance(exc, TtsError):
                message = Message(
                    deferred(
                        "the system voice ran but produced no audio ({detail}), so "
                        "the hello-world tape could not be created"
                    ),
                    (("detail", str(exc)[:400]),),
                )
            else:
                message = Message(
                    deferred(
                        "no system voice is installed, so the hello-world tape "
                        "could not be created"
                    )
                )
            return finding(LEG_TTS, message)

        # 2. Transcription readiness, from the wizard's own resolver, so the
        #    wizard and this check cannot disagree about what is ready.
        status = transcription_status(backends=backends, checkpoint=checkpoint)
        if status.state == LEG_BACKEND:
            # A ``backend`` state always carries the CLI's own finding.
            assert status.message is not None
            return finding(LEG_BACKEND, status.message, tape=tape)

        backend_id = status.backend
        model: str | None = status.model
        if status.state == LEG_MODEL:
            return finding(
                LEG_MODEL,
                Message(
                    deferred(
                        "no transcription model is on disk at {models_dir}; "
                        "the first transcription run downloads one, or place a "
                        "checkpoint there, then run the check again"
                    ),
                    (("models_dir", status.models_dir),),
                ),
                tape=tape,
                backend=backend_id,
            )

        # 3. Ingest -> transcribe, and read the transcript the same way the MCP
        #    tool and the review do.
        try:
            pipeline(
                workspace,
                Path(tape.path),
                backend_id=backend_id,
                model=model,
                language=requested,
            )
        except Exception as exc:  # noqa: BLE001 - every failure is a finding
            return finding(
                LEG_TRANSCRIBE,
                _transcribe_finding(exc),
                tape=tape,
                backend=backend_id,
                model=model,
            )

        try:
            page = transcript(_scratch_meeting(workspace))
        except Exception as exc:  # noqa: BLE001 - no segments is a finding too
            return finding(
                LEG_TRANSCRIBE,
                _transcribe_finding(exc),
                tape=tape,
                backend=backend_id,
                model=model,
            )

        if page.total <= 0:
            return finding(
                LEG_TRANSCRIBE,
                Message(
                    deferred(
                        "transcription produced no segments, so there is no "
                        "transcript to show"
                    )
                ),
                tape=tape,
                backend=backend_id,
                model=model,
            )

    return HelloCheck(
        leg=LEG_OK,
        message=Message(
            deferred(
                "the hello-world tape ran through ingest and transcription; the "
                "transcript is below"
            )
        ),
        tape=tape,
        backend=backend_id,
        model=model,
        transcript=page.text,
        segments=page.total,
        mcp_config=mcp_config,
        harness=harness,
        tool=TRANSCRIPT_TOOL,
        entry=entry_value,
    )


__all__ = [
    "HELLO_CHECK_DIRNAME",
    "HelloCheck",
    "LEG_BACKEND",
    "LEG_MODEL",
    "LEG_OK",
    "LEG_TRANSCRIBE",
    "LEG_TTS",
    "TRANSCRIPT_TOOL",
    "TranscriptionStatus",
    "download_transcription_model",
    "hello_check_workspace",
    "run_hello_check",
    "transcription_status",
]
