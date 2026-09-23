"""The `synth` development command: build the badness, keep the ground truth.

Genuine bad published multi-track is scarce and usually has no correct
answer, so this command **synthesizes the badness** and keeps the clean
aligned ground truth to score recovery against. It is a development command,
not a pipeline stage: it produces the fixture a `calibrate` run is scored on,
and its output is the only artifact the pipeline never reads back.
"""

from __future__ import annotations

import random

import soundfile as sf

from clear_record.core import Source
from clear_record.engine import SYNTH_SR, make_scene, record as synth_record
from clear_record.pipeline.workspace import Workspace


# --------------------------------------------------------------------------- #
# synthesize (owner strategy: build the badness, keep the ground truth)
# --------------------------------------------------------------------------- #
def synth(
    directory: str,
    devices: int = 4,
    duration_s: float = 20.0,
    speakers: int = 4,
    seed: int = 0,
) -> dict:
    """Generate a clean multi-speaker scene + degraded per-device recordings,
    plus an exact ground-truth alignment/event timeline.

    This realizes the owner's strategy: genuine bad published multi-track is
    scarce and usually has no correct answer, so we **synthesize the badness**
    and keep the clean aligned ground truth to score recovery against — the
    reliable way to calibrate `align`/`reconcile`.
    """
    w = Workspace.at(directory)
    d = w.root
    rng = random.Random(seed)
    scene, events = make_scene(duration_s, speakers, seed=seed)
    audio_dir = w.audio_dir
    audio_dir.mkdir(parents=True, exist_ok=True)

    sources: list[Source] = []
    devices_meta: list[dict] = []
    for i in range(devices):
        dev_id = f"device_{i}"
        start_s = 0.0 if i == 0 else round(rng.uniform(0.2, 1.4), 3)
        # mild but realistic degradation; kept recoverable so align is scoreable
        deg = dict(
            start_s=start_s,
            drift_ppm=rng.uniform(-60, 60),
            gain=rng.uniform(0.75, 1.25),
            noise=rng.uniform(0.0005, 0.006),
            lowpass_ms=rng.uniform(0.0, 1.2),
            rir_s=rng.uniform(0.04, 0.16),
            dropout_frac=rng.uniform(0.0, 0.02),
            seed=seed * 1000 + i,
        )
        audio, true_offset = synth_record(scene, **deg)
        wav = audio_dir / f"{dev_id}.wav"
        sf.write(str(wav), audio, SYNTH_SR)
        sources.append(
            Source(id=dev_id, path=str(wav), label=f"device{i}", clock_domain="wall")
        )
        devices_meta.append(
            {"id": dev_id, "true_offset_s": round(true_offset, 4), **deg}
        )

    w.write_manifest(sources)
    gt = {
        "scene_duration_s": round(duration_s, 4),
        "devices": devices_meta,
        "events": events,
    }
    w.write_ground_truth(gt)

    print(f"[synth] {len(sources)} device(s), {len(events)} speaker event(s) -> {d}")
    for m in devices_meta:
        print(f"  {m['id']:10s} true_offset={m['true_offset_s']:+.4f}s")
    print(f"  ground truth -> {w.ground_truth_path}")
    return gt
