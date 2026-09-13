"""End-to-end tests for the non-ASR pipeline stages on synthetic audio.

These exercise ingest -> align -> reconcile -> export + calibrate_report without
requiring an ASR backend, so they run in any environment (including CI without a
GPU framework). The ASR-requiring `transcribe` stage is tested separately when a
backend is available (see the @skip conditions in test_transcriber).
"""

from __future__ import annotations

import numpy as np
import soundfile as sf

from cr_cli import stages
from cr_providers import BackendBase


def _write_tone(
    path, sr: int = 8000, seconds: float = 6.0, freq: float = 220.0
) -> None:
    # aperiodic chirp so cross-correlation alignment is unambiguous
    t = np.arange(int(seconds * sr), dtype=np.float64) / sr
    f1 = freq * 6.0
    phase = 2 * np.pi * (freq * t + (f1 - freq) * t * t / (2.0 * seconds))
    sig = (0.4 * np.sin(phase)).astype(np.float32)
    sf.write(str(path), sig, sr)


def _workspace(tmp_path) -> str:
    wd = tmp_path / "rec"
    wd.mkdir()
    _write_tone(wd / "a.wav", freq=220.0)
    _write_tone(wd / "b.wav", freq=220.0)
    return str(wd)


def test_ingest_align_reconcile_export(tmp_path) -> None:
    wd = _workspace(tmp_path)
    sources = stages.ingest(wd)
    assert len(sources) == 2
    alignment = stages.align(wd)
    assert alignment.reference == sources[0].id

    # Seed segments "by hand" (transcribe stage is backend-gated).
    from cr_core import Segment

    per_source = {
        sources[0].id: [
            Segment(
                start=0.0,
                end=1.5,
                text="first line",
                source=sources[0].id,
                confidence=0.9,
            )
        ],
        sources[1].id: [
            Segment(
                start=0.0,
                end=1.5,
                text="first line",
                source=sources[1].id,
                confidence=0.6,
            )
        ],
    }
    from cr_cli import workspace as ws

    ws.write_segments(
        tmp_path / "rec", per_source, {"backend": "none", "model": "none"}
    )

    record = stages.reconcile(wd)
    assert record.segments and record.segments[0].speaker == sources[0].label

    written = stages.export(wd)
    assert "md" in written and written["md"].exists()

    report = stages.calibrate_report(wd)
    assert report["coverage"] is not None


def test_ingest_splits_multichannel_sources(tmp_path) -> None:
    """A 4-channel meeting/DJI capture becomes four per-channel sources (auto),
    and `--mix-down` collapses it to one."""
    sr = 8000
    t = np.arange(int(3.0 * sr), dtype=np.float64) / sr
    chans = [np.sin(2 * np.pi * f * t).astype(np.float32) for f in (200, 300, 400, 500)]
    quad = tmp_path / "meeting.wav"
    sf.write(str(quad), np.stack(chans, axis=1), sr)

    wd = tmp_path / "sess"
    wd.mkdir()
    (wd / "meeting.wav").unlink(missing_ok=True)
    import shutil

    shutil.copy(str(quad), str(wd / "meeting.wav"))

    split_sources = stages.ingest(str(wd), split="auto")
    assert len(split_sources) == 4
    assert all(s.id.endswith(("ch1", "ch2", "ch3", "ch4")) for s in split_sources)

    mixed = stages.ingest(str(wd), split="mix")
    assert len(mixed) == 1
    assert not mixed[0].id.endswith("ch1")


def test_attribute_stage_corrects_crosstalk_then_reconcile_preserves(tmp_path) -> None:
    """`attribute` re-labels bleed-dominated segments from relative energy, and
    `reconcile` keeps the corrected speaker (composability)."""
    import soundfile as sf

    from cr_core import Segment
    from cr_engine import SYNTH_SR, make_crosstalk_scene
    from cr_cli import workspace as ws

    devices, events = make_crosstalk_scene(
        duration_s=16.0, n_speakers=2, bleed_db=-6.0, seed=7, non_overlapping=True
    )
    wd = tmp_path / "ct"
    wd.mkdir()
    for i, device in enumerate(devices):
        sf.write(str(wd / f"lav{i}.wav"), device, SYNTH_SR)

    sources = stages.ingest(str(wd))
    labels = {s.id: s.label for s in sources}
    truth = {f"w{i}": labels[f"lav{e['speaker']}"] for i, e in enumerate(events)}
    # Every segment is taken from the bleed channel and given the naive label.
    per_source: dict[str, list[Segment]] = {s.id: [] for s in sources}
    for i, e in enumerate(events):
        wrong = f"lav{1 - e['speaker']}"
        per_source[wrong].append(
            Segment(
                start=e["start"],
                end=e["end"],
                text=f"w{i}",
                source=wrong,
                speaker=labels[wrong],
            )
        )
    ws.write_segments(wd, per_source, {"backend": "none", "model": "none"})

    stages.attribute(str(wd))
    per_source, _ = ws.load_segments(wd)
    fixed = [s for sid in per_source for s in per_source[sid]]
    assert sorted(s.speaker for s in fixed) == sorted(truth.values())

    record = stages.reconcile(str(wd))
    assert record.segments
    # Joins may merge adjacent cues, but every token keeps the corrected speaker.
    for seg in record.segments:
        for token in seg.text.split():
            assert seg.speaker == truth[token]


def test_attribute_stage_windowed_tracks_gain_and_writes_confidence(tmp_path) -> None:
    """`attribute --window-s` uses the rolling per-source level and persists the
    calibrated confidence (not just the corrected speaker), so reconcile sees it."""
    import soundfile as sf

    from cr_core import Segment
    from cr_engine import SYNTH_SR, make_crosstalk_scene

    from cr_cli import workspace as ws

    devices, events = make_crosstalk_scene(
        duration_s=20.0, n_speakers=2, bleed_db=-9.0, seed=5, non_overlapping=True
    )
    gains = [10.0 ** (6.0 / 20.0), 10.0 ** (-6.0 / 20.0)]
    wd = tmp_path / "gain"
    wd.mkdir()
    for i, device in enumerate(devices):
        sf.write(
            str(wd / f"lav{i}.wav"), (device * gains[i]).astype("float32"), SYNTH_SR
        )

    sources = stages.ingest(str(wd))
    labels = {s.id: s.label for s in sources}
    per_source: dict[str, list[Segment]] = {s.id: [] for s in sources}
    for i, e in enumerate(events):
        wrong = f"lav{1 - e['speaker']}"  # the bleed-dominated channel
        per_source[wrong].append(
            Segment(e["start"], e["end"], f"w{i}", wrong, labels[wrong])
        )
    ws.write_segments(wd, per_source, {"backend": "none", "model": "none"})

    stages.attribute(str(wd), window_s=15.0)

    per_source, _ = ws.load_segments(wd)
    fixed = [s for sid in per_source for s in per_source[sid]]
    assert sorted(s.speaker for s in fixed) == sorted(
        labels[f"lav{e['speaker']}"] for e in events
    )
    assert all(s.confidence is not None for s in fixed)
    assert all(0.0 < s.confidence <= 1.0 for s in fixed)


def test_attribute_stage_never_emits_room_as_speaker(tmp_path) -> None:
    """B1 regression: the mixed/room source lives in the manifest, so the CLI must
    keep it out of the candidate set -- it is a witness, never a speaker.

    Three speakers, only two lavs, and the room recorded as a manifest source.
    Without the exclusion the loud room wins the energy comparison and is emitted
    as a speaker for unmiked speech, contradicting the documented contract."""
    import numpy as np
    import soundfile as sf

    from cr_core import Segment
    from cr_engine import SYNTH_SR, make_speaker_stems, mix_crosstalk

    from cr_cli import workspace as ws

    stems, events = make_speaker_stems(
        duration_s=16.0, n_speakers=3, seed=7, non_overlapping=True
    )
    wd = tmp_path / "ctroom"
    wd.mkdir()
    for i in range(2):
        sf.write(
            str(wd / f"mic{i}.wav"), mix_crosstalk(stems, i, bleed_db=-12.0), SYNTH_SR
        )
    sf.write(str(wd / "room.wav"), np.sum(stems, axis=0).astype(np.float32), SYNTH_SR)

    sources = stages.ingest(str(wd))
    labels = {s.id: s.label for s in sources}
    per_source: dict[str, list[Segment]] = {s.id: [] for s in sources}
    expected: dict[str, str] = {}
    for i, e in enumerate(events):
        if e["speaker"] < 2:  # covered: correct the bleed-dominated channel
            wrong = f"mic{1 - e['speaker']}"
            expected[f"w{i}"] = labels[f"mic{e['speaker']}"]
        else:  # unmiked: the room gate keeps the incoming speaker
            wrong = "mic1"
            expected[f"w{i}"] = labels["mic1"]
        per_source[wrong].append(
            Segment(e["start"], e["end"], f"w{i}", wrong, labels[wrong])
        )
    ws.write_segments(wd, per_source, {"backend": "none", "model": "none"})

    stages.attribute(str(wd), mixed_source="room")
    per_source, _ = ws.load_segments(wd)
    emitted = [s for sid in per_source for s in per_source[sid]]

    assert labels["room"] not in {s.speaker for s in emitted}
    assert all(s.speaker != labels["room"] for s in emitted)
    assert all(s.speaker == expected[s.text] for s in emitted)


def test_merge_chunk_segments_dedupes_by_coverage() -> None:
    """A fully-covered duplicate is dropped; a boundary straddler keeps its
    unique tail; a later unique segment is kept whole."""
    from cr_core import Segment
    from cr_cli.stages import _merge_chunk_segments

    def seg(start: float, end: float, text: str) -> Segment:
        return Segment(start=start, end=end, text=text, source="src")

    first = [seg(0.0, 5.0, "alpha"), seg(5.0, 10.0, "beta")]
    # chunk 1 overlaps [2, 10]: "alpha" is a covered duplicate; "gamma"
    # straddles the emitted frontier; "delta" is entirely new.
    second = [
        seg(2.0, 5.0, "alpha"),
        seg(8.0, 13.0, "gamma"),
        seg(13.0, 15.0, "delta"),
    ]

    out = _merge_chunk_segments([first, second])
    assert [(s.start, s.end, s.text) for s in out] == [
        (0.0, 5.0, "alpha"),
        (5.0, 10.0, "beta"),
        (10.0, 13.0, "gamma"),
        (13.0, 15.0, "delta"),
    ]
    assert out[-1].text == "delta"  # the unique tail survived
    for earlier, later in zip(out, out[1:]):
        assert earlier.end <= later.start + 1e-9


def test_align_reports_unresolved_source(tmp_path, capsys) -> None:
    """`align` names the sources it could not place instead of silently
    recording a fake zero offset."""
    import dataclasses
    from pathlib import Path

    from cr_cli import workspace as ws

    wd = _workspace(tmp_path)
    sources = stages.ingest(wd)
    broken = [
        dataclasses.replace(s, path=str(tmp_path / "missing.wav")) if s.id == "b" else s
        for s in sources
    ]
    ws.write_manifest(Path(wd), broken)

    alignment = stages.align(wd)
    assert "b" in alignment.unresolved
    assert "UNRESOLVED" in capsys.readouterr().out


def test_run_threads_reference_source(tmp_path, monkeypatch) -> None:
    """`run` must measure alignment against the chosen reference, not the
    first source (the stages below are stubbed so no ASR backend is needed)."""
    wd = _workspace(tmp_path)
    seen: dict[str, str] = {}
    real_align = stages.align

    def spy_align(directory, reference=None):
        alignment = real_align(directory, reference=reference)
        seen["reference"] = alignment.reference
        return alignment

    monkeypatch.setattr(stages, "align", spy_align)
    monkeypatch.setattr(stages, "transcribe", lambda *a, **k: None)
    monkeypatch.setattr(stages, "diarize", lambda *a, **k: None)
    monkeypatch.setattr(stages, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(stages, "export", lambda *a, **k: None)

    stages.run(wd, backend="fake", reference="b")
    assert seen["reference"] == "b"


def test_run_attribute_energy_selects_energy_path(tmp_path, monkeypatch) -> None:
    """`run --attribute-energy` routes to energy attribution and leaves the
    default spectral `diarize` path untouched when the flag is off."""
    wd = _workspace(tmp_path)
    calls = {"attribute": 0, "diarize": 0}

    def spy_attribute(directory, mixed_source=None, window_s=None):
        calls["attribute"] += 1
        return {}

    def spy_diarize(directory, speakers=None):
        calls["diarize"] += 1

    monkeypatch.setattr(stages, "transcribe", lambda *a, **k: None)
    monkeypatch.setattr(stages, "reconcile", lambda *a, **k: None)
    monkeypatch.setattr(stages, "export", lambda *a, **k: None)
    monkeypatch.setattr(stages, "attribute", spy_attribute)
    monkeypatch.setattr(stages, "diarize", spy_diarize)

    stages.run(wd, backend="fake", attribute_energy=True, mixed_source="b")
    assert calls == {"attribute": 1, "diarize": 0}

    stages.run(wd, backend="fake", do_diarize=True)
    assert calls == {"attribute": 1, "diarize": 1}


def test_transcribe_chunks_resume_and_glossary_invalidation(
    tmp_path, monkeypatch
) -> None:
    """Chunked transcription is resumable, and adding a glossary invalidates the
    chunk cache so a background first pass can be corrected."""
    import soundfile as sf

    from cr_core import Segment, TranscriptionResult
    from cr_providers import BackendInfo

    from cr_cli import stages

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    class Fake(BackendBase):
        calls = 0
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
        )

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            type(self).calls += 1
            data, file_sr = sf.read(audio_path)
            dur = len(data) / file_sr
            return TranscriptionResult(
                source="fake",
                segments=(
                    Segment(
                        start=0.0,
                        end=round(dur, 3),
                        text="chunk",
                        source="fake",
                        confidence=0.5,
                    ),
                ),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=dur,
            )

    fake = Fake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fake)

    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    first = Fake.calls
    assert first > 1  # the 8 s tape was chunked

    # resume: every chunk cached -> no backend calls
    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    assert Fake.calls == first

    # a new glossary invalidates the cache -> re-transcribes
    stages.glossary(str(wd), add=["ProjectX"])
    stages.transcribe(str(wd), "fake", chunk_seconds=3.0, overlap_seconds=1.0)
    assert Fake.calls > first


def test_resolve_jobs_is_adaptive() -> None:
    from cr_cli.stages import _resolve_jobs

    assert _resolve_jobs(False, 10, 4) == 1  # in-process -> serialized
    assert _resolve_jobs(True, 1, 4) == 1  # no work to parallelize
    assert _resolve_jobs(True, 10, 2) == 2  # explicit request wins
    assert 1 <= _resolve_jobs(True, 10, 0) <= 4  # bounded adaptive default


def test_transcribe_runs_pending_chunks_concurrently(tmp_path, monkeypatch) -> None:
    """A process-isolated backend's pending chunks run overlapped, and the
    merged result is still per-source complete."""
    import threading
    import time

    from cr_cli import stages
    from cr_core import Segment, TranscriptionResult
    from cr_providers import BackendInfo

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.05)
                data, file_sr = sf.read(audio_path)
                dur = len(data) / file_sr
                return TranscriptionResult(
                    source="fake",
                    segments=(
                        Segment(
                            start=0.0,
                            end=round(dur, 3),
                            text="chunk",
                            source="fake",
                            confidence=0.5,
                        ),
                    ),
                    language="en",
                    backend="fake",
                    model="fake",
                    audio_duration=dur,
                )
            finally:
                with self.lock:
                    self.active -= 1

    fake = Fake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fake)

    per_source = stages.transcribe(
        str(wd), "fake", chunk_seconds=2.0, overlap_seconds=0.5, jobs=4
    )
    assert fake.max_active > 1  # chunks actually overlapped
    assert per_source["a"]  # and the merged output is complete


def test_transcribe_resolves_model_once_before_the_pool(tmp_path, monkeypatch) -> None:
    """The first-use model resolve/download runs once on the main thread, before
    any worker: a parallel backend must never race it (the provider also makes
    the download itself single-flight)."""
    import threading
    import time

    from cr_cli import stages
    from cr_core import Segment, TranscriptionResult
    from cr_providers import BackendInfo

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(8 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    events: list[str] = []

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

        def prepare(self, model, model_dir):
            events.append(f"resolve:{threading.current_thread().name}")
            return "/tmp/fake-model.bin"

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            events.append(f"transcribe:{threading.current_thread().name}")
            time.sleep(0.02)
            data, file_sr = sf.read(audio_path)
            dur = len(data) / file_sr
            return TranscriptionResult(
                source="fake",
                segments=(
                    Segment(
                        start=0.0,
                        end=round(dur, 3),
                        text="chunk",
                        source="fake",
                        confidence=0.5,
                    ),
                ),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=dur,
            )

    fake = Fake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fake)

    stages.transcribe(str(wd), "fake", chunk_seconds=2.0, overlap_seconds=0.5, jobs=4)

    # Resolved exactly once, on the main thread, before any worker ran.
    assert events[0] == "resolve:MainThread"
    assert sum(e.startswith("resolve:") for e in events) == 1
    # ...and the pool really did fan out, so this exercises the parallel path.
    assert any(
        e.startswith("transcribe:") and e != "transcribe:MainThread" for e in events
    )


def test_auto_jobs_is_capped_by_model_and_vram_and_overridable(monkeypatch) -> None:
    """The `jobs=0` default is bounded by the model's resident size against the
    GPU memory, so N large models cannot OOM the documented minimum GPU; an
    explicit `--jobs` / `CR_JOBS` still wins."""
    import os

    from cr_cli.stages import (
        _auto_jobs,
        _detect_vram_gb,
        _model_vram_gb,
        _resolve_jobs,
    )

    monkeypatch.setattr(os, "cpu_count", lambda: 16)

    # A large model cannot fan out on a small (8 GB) GPU; a medium one gets a
    # few; the 4-way fan-out cap still applies on a big (24 GB) GPU.
    assert _auto_jobs(10, "large-v3", 8.0) == 1
    assert _auto_jobs(10, "medium", 8.0) == 3
    assert _auto_jobs(10, "large-v3", 24.0) == 4
    assert _auto_jobs(10, "small", 24.0) == 4
    # Unknown / missing models are assumed large, which can only lower the cap.
    assert _auto_jobs(10, None, 8.0) == 1

    # Name parsing covers filenames, paths and quantisation suffixes.
    assert _model_vram_gb("ggml-large-v3.bin") == 3.7
    assert _model_vram_gb("/models/ggml-large-v3-q5_0.bin") == 3.7
    assert _model_vram_gb("medium") == 2.1
    assert _model_vram_gb("mystery") == 3.7

    # CR_VRAM_GB overrides the (hardware-dependent) probe for the auto path...
    monkeypatch.setenv("CR_VRAM_GB", "24")
    assert _detect_vram_gb() == 24.0
    assert _resolve_jobs(True, 10, 0, model="large-v3") == 4
    # ...but explicit --jobs and CR_JOBS override the advisory cap.
    assert _resolve_jobs(True, 10, 6, model="large-v3", vram_gb=8.0) == 6
    monkeypatch.setenv("CR_JOBS", "5")
    assert _resolve_jobs(True, 10, 0, model="large-v3", vram_gb=8.0) == 5


def test_transcribe_interrupt_cancels_queue_and_kills_children(
    tmp_path, monkeypatch
) -> None:
    """Ctrl-C cancels queued chunks and terminates the in-flight children
    promptly, leaving a consistent, resumable chunk cache.

    The backend is a hardware-free stub that spawns a real, killable child
    process (a stand-in for `whisper-cli`), so this exercises the pool's real
    process-termination path without a GPU or model weights.
    """
    import json
    import os
    import signal
    import subprocess
    import sys
    import threading
    import time

    import pytest

    from cr_cli import workspace as ws
    from cr_core import Segment, TranscriptionResult
    from cr_engine import plan_chunks
    from cr_providers import BackendInfo

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(10 * sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    # The pool must not monkey-patch the global Popen (ticket 08).
    original_popen_init = subprocess.Popen.__init__

    started = threading.Event()
    lock = threading.Lock()
    procs: list[subprocess.CompletedProcess] = []

    class BlockingFake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            from cr_providers import SubprocessRunner

            runner = process_runner or SubprocessRunner()
            started.set()
            # A real child process stands in for `whisper-cli`, launched through
            # the pool's injected runner (no global Popen patch): the pool must
            # find and terminate it, not merely abandon the worker thread.
            result = runner.run(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                capture_output=True,
                text=True,
            )
            with lock:
                procs.append(result)
            if result.returncode != 0:
                raise RuntimeError(f"whisper-cli killed (exit {result.returncode})")
            return TranscriptionResult(
                source="fake",
                segments=(Segment(0.0, 0.1, "x", "fake"),),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=0.1,
            )

    monkeypatch.setattr(stages, "get_backend", lambda _id: BlockingFake())

    def interrupt_once() -> None:
        started.wait(5)
        time.sleep(0.2)  # let both workers spawn while the rest queue
        os.kill(os.getpid(), signal.SIGINT)

    helper = threading.Thread(target=interrupt_once, daemon=True)
    helper.start()
    began = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        stages.transcribe(
            str(wd), "fake", chunk_seconds=2.0, overlap_seconds=0.5, jobs=2
        )
    elapsed = time.monotonic() - began
    helper.join(timeout=2)

    # No global patch was installed (or left behind) by the pool.
    assert subprocess.Popen.__init__ is original_popen_init

    # Bounded: it did not block on the 30 s children.
    assert elapsed < 5.0
    # The in-flight children were terminated and queued chunks never ran.
    assert procs, "at least one chunk should have started"
    for proc in procs:
        assert proc.returncode != 0
    n_chunks = len(plan_chunks(10.0, 2.0, 0.5))
    assert len(procs) < n_chunks

    # Cache stays consistent and resumable: meta intact, every body complete,
    # no half-written temp files.
    cache_dir = ws.chunks_dir(wd) / "a"
    meta = json.loads((cache_dir / "_meta.json").read_text(encoding="utf-8"))
    assert meta["n_chunks"] == n_chunks
    cached: list[int] = []
    for body in cache_dir.glob("*.json"):
        data = json.loads(body.read_text(encoding="utf-8"))
        if body.name != "_meta.json":
            assert isinstance(data, list)
            cached.append(int(body.stem))
    assert set(cached) <= set(range(n_chunks))
    assert not list(cache_dir.glob("*.tmp"))

    # Resuming completes the plan, recomputing only the uncached chunks.
    class FastFake(BlockingFake):
        calls = 0

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            type(self).calls += 1
            return TranscriptionResult(
                source="fake",
                segments=(Segment(0.0, 0.1, "x", "fake"),),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=0.1,
            )

    fast = FastFake()
    monkeypatch.setattr(stages, "get_backend", lambda _id: fast)
    stages.transcribe(str(wd), "fake", chunk_seconds=2.0, overlap_seconds=0.5, jobs=2)
    assert FastFake.calls == n_chunks - len(cached)
    assert len(list(cache_dir.glob("[0-9]*.json"))) == n_chunks


def test_transcribe_check_plugin_gates_on_the_load_probe(tmp_path, monkeypatch) -> None:
    """`check_plugin=True` runs the opt-in probe once and refuses to transcribe
    when the ggml plugin fails to load. The default path never probes (the
    surface test pins the opt-in flag)."""
    import pytest

    from cr_providers import BackendInfo, PluginLoadProbe

    wd = tmp_path / "rec"
    wd.mkdir()
    sr = 16000
    t = np.arange(sr, dtype=np.float64) / sr
    sf.write(
        str(wd / "a.wav"), (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32), sr
    )
    stages.ingest(str(wd), split="mix")

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

    monkeypatch.setattr(stages, "get_backend", lambda _id: Fake())
    calls: list = []

    def fake_probe(backend):
        calls.append(backend)
        return PluginLoadProbe(False, "a non-matching backend loaded: cuda")

    monkeypatch.setattr(stages, "probe_ggml_plugin_load", fake_probe)

    with pytest.raises(SystemExit, match="did not load"):
        stages.transcribe(str(wd), "fake", check_plugin=True)
    assert len(calls) == 1, "the probe must run exactly once per invocation"


def test_no_global_popen_patch_is_left_behind() -> None:
    """Regression for ticket 08: the pool must not monkey-patch ``Popen``."""
    for name in (
        "_ORIGINAL_POPEN_INIT",
        "_tracked_popen_init",
        "_track_subprocesses",
        "_SubprocessTracker",
        "_run_tracked",
        "_PoolCancel",
    ):
        assert not hasattr(stages, name), f"stale global patch helper: {name}"


def test_two_concurrent_pools_use_distinct_runners(tmp_path, monkeypatch) -> None:
    """Two pools in one process each get their own runner, so cancellation stays
    scoped; both complete with independent caches."""
    import threading
    from pathlib import Path

    from cr_cli import workspace as ws
    from cr_core import Segment, TranscriptionResult
    from cr_providers import BackendInfo

    def make_ws(name: str) -> str:
        wd = tmp_path / name
        wd.mkdir()
        sr = 8000
        t = np.arange(sr, dtype=np.float64) / sr
        sf.write(
            str(wd / "a.wav"),
            (0.2 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32),
            sr,
        )
        stages.ingest(str(wd), split="mix")
        return str(wd)

    wd1, wd2 = make_ws("one"), make_ws("two")
    runners: list = []
    lock = threading.Lock()

    class Fake(BackendBase):
        info = BackendInfo(
            id="fake",
            vendor="test",
            frameworks=(),
            description="fake",
            default_model="fake",
            parallelizable=True,
        )

        def available(self) -> bool:
            return True

        def transcribe(
            self,
            audio_path,
            *,
            language=None,
            model=None,
            model_dir=None,
            initial_prompt=None,
            process_runner=None,
        ):
            with lock:
                runners.append(process_runner)
            return TranscriptionResult(
                source="fake",
                segments=(Segment(0.0, 0.1, "x", "fake"),),
                language="en",
                backend="fake",
                model="fake",
                audio_duration=0.1,
            )

    monkeypatch.setattr(stages, "get_backend", lambda _id: Fake())
    errors: list = []

    def worker(wd: str) -> None:
        try:
            stages.transcribe(wd, "fake", jobs=1)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(wd,)) for wd in (wd1, wd2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(runners) == 2
    assert runners[0] is not runners[1], "each pool needs its own runner"
    for wd in (wd1, wd2):
        per_source, _ = ws.load_segments(Path(wd))
        assert per_source
