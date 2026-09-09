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
