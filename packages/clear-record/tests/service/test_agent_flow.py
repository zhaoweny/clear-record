"""The hello-world acceptance check: success path and every finding (ticket 05).

The slow stages are **stubbed** on purpose: the check's value is that it works
with no system voice, no ASR backend and no model on the test machine, and that
each missing piece becomes a *finding naming the leg* rather than an exception.
The success path is asserted with a stub pipeline that writes the transcript the
real stages would; nothing here runs TTS or ASR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clear_record.pipeline.tts import TtsError, TtsUnavailable
from clear_record.service import agent_flow
from clear_record.service.hello_tape import HelloTape
from clear_record.service.setup import SetupError
from clear_record.service.transcript import TranscriptSlice


def _tape(destination: Path, *, lang: str = "en") -> HelloTape:
    """A written clip stand-in (the pipeline stub never reads the bytes)."""
    path = destination / f"hello-world-{lang}.wav"
    destination.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"RIFF....")
    return HelloTape(path=path, engine="stub", lang=lang, phrase="Hello world.")


def _page(text: str = "00:00:00.000 [mic] hello world") -> TranscriptSlice:
    return TranscriptSlice(
        meeting_id=0,
        path="/scratch/segments.json",
        source="transcript",
        total=1,
        offset=0,
        returned=1,
        next=None,
        text=text,
    )


def _success_deps(tmp_path, calls: list[dict]):
    """The seams that make the success path run with no voice, backend or model."""
    checkpoint = tmp_path / "models" / "ggml-small.bin"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.write_bytes(b"model")

    def pipeline(workspace, tape, *, backend_id, model, language):
        calls.append(
            {
                "workspace": Path(workspace),
                "tape": Path(tape),
                "backend_id": backend_id,
                "model": model,
                "language": language,
            }
        )

    return {
        "write_tape": _tape,
        "backends": lambda: ("apple",),
        "checkpoint": lambda: checkpoint,
        "pipeline": pipeline,
        "transcript": lambda meeting: _page(),
        "entry": lambda: {"command": "clear-record", "args": ["mcp"]},
    }


# --- the success path -------------------------------------------------------- #


def test_a_working_chain_returns_the_transcript_and_the_mcp_entry(tmp_path) -> None:
    calls: list[dict] = []
    result = agent_flow.run_hello_check(
        destination=tmp_path / "ws",
        lang="en",
        state={},
        **_success_deps(tmp_path, calls),
    )

    assert result.ok is True
    assert result.leg == agent_flow.LEG_OK
    assert result.segments == 1
    assert "hello world" in result.transcript
    # The tape is created inside the caller's destination, never committed.
    assert result.tape is not None
    assert result.tape.path.parent.parent == tmp_path / "ws"
    # The pipeline ran over the scratch workspace with the concrete checkpoint.
    assert len(calls) == 1
    assert calls[0]["tape"] == result.tape.path
    assert calls[0]["model"].endswith("ggml-small.bin")
    assert calls[0]["backend_id"] == "apple"
    # The MCP leg is the client entry plus the tool the server exposes.
    assert result.tool == agent_flow.TRANSCRIPT_TOOL
    assert result.entry == {"command": "clear-record", "args": ["mcp"]}


def test_the_check_owns_a_scratch_workspace_and_never_registers_a_project(
    tmp_path,
) -> None:
    destination = tmp_path / "ws"
    (destination / "stale").mkdir(parents=True)
    (destination / "stale" / "old.wav").write_bytes(b"old")

    calls: list[dict] = []
    agent_flow.run_hello_check(
        destination=destination, state={}, **_success_deps(tmp_path, calls)
    )

    # A re-run wipes the workspace: no accumulated clips, no stale transcript.
    assert not (destination / "stale").exists()
    assert calls[0]["workspace"] == destination


def test_the_check_reads_the_state_record_for_the_mcp_leg(tmp_path) -> None:
    calls: list[dict] = []
    result = agent_flow.run_hello_check(
        destination=tmp_path / "ws",
        state={"mcp_config": "/home/u/mcp.json", "harness": "/usr/bin/pi-agent"},
        **_success_deps(tmp_path, calls),
    )

    assert result.mcp_config == "/home/u/mcp.json"
    assert result.harness == "/usr/bin/pi-agent"


# --- the findings: one leg named each --------------------------------------- #


def test_no_system_voice_is_a_tts_finding(tmp_path) -> None:
    deps = _success_deps(tmp_path, [])

    def no_engine(destination, *, lang):
        raise TtsUnavailable("no engine")

    deps["write_tape"] = no_engine
    result = agent_flow.run_hello_check(destination=tmp_path / "ws", **deps)

    assert result.leg == agent_flow.LEG_TTS
    assert "no system voice" in str(result.message)


def test_a_voice_that_wrote_no_audio_is_still_a_tts_finding(tmp_path) -> None:
    deps = _success_deps(tmp_path, [])

    def broken(destination, *, lang):
        raise TtsError("say produced no audio")

    deps["write_tape"] = broken
    result = agent_flow.run_hello_check(destination=tmp_path / "ws", **deps)

    assert result.leg == agent_flow.LEG_TTS
    assert "produced no audio" in str(result.message)


def test_no_backend_is_a_backend_finding_with_the_clis_own_message(tmp_path) -> None:
    deps = _success_deps(tmp_path, [])
    deps["backends"] = lambda: ()
    result = agent_flow.run_hello_check(destination=tmp_path / "ws", **deps)

    assert result.leg == agent_flow.LEG_BACKEND
    assert "no ASR backend is available" in str(result.message)
    # The tape was still created; only the decode is missing.
    assert result.tape is not None


def test_a_ggml_backend_with_no_model_is_a_model_finding(tmp_path) -> None:
    deps = _success_deps(tmp_path, [])
    deps["checkpoint"] = lambda: None
    result = agent_flow.run_hello_check(destination=tmp_path / "ws", **deps)

    assert result.leg == agent_flow.LEG_MODEL
    assert "no transcription model is on disk" in str(result.message)


def test_a_model_free_backend_needs_no_checkpoint(tmp_path) -> None:
    calls: list[dict] = []
    deps = _success_deps(tmp_path, calls)
    deps["backends"] = lambda: ("apple-speech",)
    deps["checkpoint"] = lambda: None
    result = agent_flow.run_hello_check(destination=tmp_path / "ws", **deps)

    assert result.leg == agent_flow.LEG_OK
    assert calls[0]["model"] is None


def test_a_failed_decode_is_a_transcribe_finding(tmp_path) -> None:
    deps = _success_deps(tmp_path, [])

    def broken(workspace, tape, *, backend_id, model, language):
        raise RuntimeError("whisper-cli exited 1")

    deps["pipeline"] = broken
    result = agent_flow.run_hello_check(destination=tmp_path / "ws", **deps)

    assert result.leg == agent_flow.LEG_TRANSCRIBE
    assert "whisper-cli exited 1" in str(result.message)


def test_an_empty_transcript_is_a_transcribe_finding(tmp_path) -> None:
    deps = _success_deps(tmp_path, [])

    def empty(meeting):
        page = _page()
        return TranscriptSlice(
            meeting_id=0,
            path=page.path,
            source="transcript",
            total=0,
            offset=0,
            returned=0,
            next=None,
            text="",
        )

    deps["transcript"] = empty
    result = agent_flow.run_hello_check(destination=tmp_path / "ws", **deps)

    assert result.leg == agent_flow.LEG_TRANSCRIBE
    assert "no segments" in str(result.message)


# --- the checkpoint probe never downloads ----------------------------------- #


def test_the_checkpoint_probe_prefers_the_default_model(tmp_path, monkeypatch) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "ggml-large-v3-q5_0.bin").write_bytes(b"m")
    (models / "ggml-small.bin").write_bytes(b"m")
    monkeypatch.setenv("CR_MODELS_DIR", str(models))

    assert agent_flow._on_disk_checkpoint() == models / "ggml-small.bin"


def test_the_checkpoint_probe_returns_the_concrete_quantized_path(
    tmp_path, monkeypatch
) -> None:
    models = tmp_path / "models"
    models.mkdir()
    (models / "ggml-large-v3-q5_0.bin").write_bytes(b"m")
    monkeypatch.setenv("CR_MODELS_DIR", str(models))

    # The path, not the normalized size name: passing "large-v3" would send the
    # backend looking for ggml-large-v3.bin and trigger a download.
    assert agent_flow._on_disk_checkpoint() == models / "ggml-large-v3-q5_0.bin"


def test_the_checkpoint_probe_is_none_without_a_model(tmp_path, monkeypatch) -> None:
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setenv("CR_MODELS_DIR", str(models))

    assert agent_flow._on_disk_checkpoint() is None


def test_the_default_language_comes_from_the_request_locale(tmp_path) -> None:
    calls: list[dict] = []
    result = agent_flow.run_hello_check(
        destination=tmp_path / "ws", lang="zh_CN", **_success_deps(tmp_path, calls)
    )
    assert result.ok
    assert calls[0]["language"] == "zh_CN"


# --- transcription readiness (the setup wizard's second step) ---------------- #


def test_readiness_is_ok_for_a_model_free_backend_with_no_checkpoint(tmp_path) -> None:
    status = agent_flow.transcription_status(
        backends=lambda: ("apple-speech",),
        checkpoint=lambda: None,
        model_dir=str(tmp_path / "models"),
    )

    assert status.state == agent_flow.LEG_OK
    assert status.ready is True
    assert status.backend == "apple-speech"
    assert status.model is None
    assert status.models_dir == str(tmp_path / "models")


def test_readiness_is_a_backend_state_when_none_is_available(tmp_path) -> None:
    status = agent_flow.transcription_status(
        backends=lambda: (),
        checkpoint=lambda: None,
        model_dir=str(tmp_path / "models"),
    )

    assert status.state == agent_flow.LEG_BACKEND
    assert status.ready is False
    assert status.backend is None
    assert status.model is None


def test_readiness_is_a_model_state_when_a_ggml_backend_has_no_checkpoint(
    tmp_path,
) -> None:
    status = agent_flow.transcription_status(
        backends=lambda: ("apple",),
        checkpoint=lambda: None,
        model_dir=str(tmp_path / "models"),
    )

    assert status.state == agent_flow.LEG_MODEL
    assert status.ready is False
    assert status.backend == "apple"
    assert status.model is None


def test_readiness_names_the_checkpoint_for_a_ggml_backend(tmp_path) -> None:
    checkpoint = tmp_path / "models" / "ggml-small.bin"

    status = agent_flow.transcription_status(
        backends=lambda: ("apple",),
        checkpoint=lambda: checkpoint,
        model_dir=str(tmp_path / "models"),
    )

    assert status.state == agent_flow.LEG_OK
    assert status.ready is True
    assert status.backend == "apple"
    assert status.model == str(checkpoint)


def test_readiness_lists_what_is_on_disk(tmp_path) -> None:
    models = tmp_path / "models"
    models.mkdir()
    for name in ("ggml-small.bin", "ggml-large-v3-q5_0.bin"):
        (models / name).write_bytes(b"m")

    status = agent_flow.transcription_status(
        backends=lambda: ("apple",),
        checkpoint=lambda: models / "ggml-small.bin",
        model_dir=str(models),
    )

    assert status.models_present == ("ggml-large-v3-q5_0.bin", "ggml-small.bin")


# --- explicit model downloads ------------------------------------------------- #


def test_a_named_download_targets_the_ggml_checkpoint_not_the_backend(
    tmp_path, monkeypatch
) -> None:
    """A named model is a ggml checkpoint even when apple-speech is preferred.

    On macOS 26 the default backend resolves to ``apple-speech``, whose
    ``prepare`` provisions a *language asset* -- so routing a named download
    through the backend reported success while no ``ggml-medium.bin`` landed.
    The download must go straight to the ggml downloader.
    """
    from clear_record.providers import backends
    from clear_record.providers.apple_speech import AppleSpeechBackend

    models = tmp_path / "models"
    models.mkdir()
    downloaded: list[tuple[str, str, str, str]] = []

    def fake_download(model: str, name: str, base: str, candidate: str) -> str:
        downloaded.append((model, name, base, candidate))
        Path(candidate).write_bytes(b"ggml")
        return candidate

    monkeypatch.setattr(backends, "_download_ggml_model", fake_download)

    def no_apple_prepare(*_args, **_kwargs):  # pragma: no cover - a call is failure
        raise AssertionError("a named model download went through the backend")

    monkeypatch.setattr(AppleSpeechBackend, "prepare", no_apple_prepare)
    # The machine's preferred backend is the model-free native path.
    monkeypatch.setattr(agent_flow, "available_backend_ids", lambda: ("apple-speech",))

    result = agent_flow.download_transcription_model("medium", model_dir=str(models))

    assert len(downloaded) == 1
    called_model, called_name, _base, candidate = downloaded[0]
    assert (called_model, called_name) == ("medium", "ggml-medium.bin")
    assert candidate == result
    assert result.endswith("ggml-medium.bin")


def test_a_named_download_lands_the_checkpoint_the_provider_fetched(
    tmp_path, monkeypatch
) -> None:
    """The model leg, through the pipeline layer the service may import.

    Only the provider's network edge is replaced, so the real path resolver above
    it runs: the service asks ``pipeline.stages``, which asks ``providers``, and
    what comes back is the checkpoint landed under the models directory.
    """
    from clear_record.providers import backends

    models = tmp_path / "models"
    models.mkdir()
    fetched: list[tuple[str, str]] = []

    def fake_fetch(model: str, name: str, base: str, candidate: str) -> str:
        fetched.append((model, candidate))
        Path(candidate).write_bytes(b"ggml")
        return candidate

    monkeypatch.setattr(backends, "_download_ggml_model", fake_fetch)

    result = agent_flow.download_transcription_model("small", model_dir=str(models))

    assert fetched == [("small", str(models / "ggml-small.bin"))]
    assert result == str(models / "ggml-small.bin")
    assert Path(result).read_bytes() == b"ggml"


def test_an_unknown_model_is_a_setup_error_not_a_value_error(tmp_path) -> None:
    """Regression: it raised an untranslated ValueError, not a SetupError."""
    with pytest.raises(SetupError) as excinfo:
        agent_flow.download_transcription_model("gigantic", model_dir=str(tmp_path))

    message = str(excinfo.value.message)
    assert "unknown model" in message
    assert "tiny, base, small, medium, large-v3" in message


def test_readiness_carries_the_clis_own_missing_backend_message(tmp_path) -> None:
    status = agent_flow.transcription_status(
        backends=lambda: (),
        checkpoint=lambda: None,
        model_dir=str(tmp_path / "models"),
    )

    assert status.state == agent_flow.LEG_BACKEND
    assert status.message is not None
    assert "no ASR backend is available" in str(status.message)


def test_the_check_reuses_the_wizards_readiness_for_a_model_free_backend(
    tmp_path,
) -> None:
    """Regression: one resolver decides "needs no checkpoint" for both."""
    calls: list[dict] = []
    deps = _success_deps(tmp_path, calls)
    deps["backends"] = lambda: ("apple-speech",)

    def no_checkpoint_probe():
        raise AssertionError("a model-free backend must not probe for a checkpoint")

    deps["checkpoint"] = no_checkpoint_probe
    result = agent_flow.run_hello_check(destination=tmp_path / "ws", **deps)

    assert result.leg == agent_flow.LEG_OK
    assert calls[0]["model"] is None
