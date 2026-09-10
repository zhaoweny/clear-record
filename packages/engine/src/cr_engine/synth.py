"""Synthetic multi-view recorder generator (owner strategy: synthesize badness).

The owner's reasoning (logbook `10-19-projects/15-clear-record`): genuinely bad
published multi-track audio is scarce, and when it exists it usually has no
*clean correct answer* to score recovery against. So the **`align`** test set is
synthesized: take a clean scene, then degrade it *per recorder* with a known
time offset (and optionally clock drift, gain/FR mismatch, reverberation, noise,
dropouts), while **retaining the exact aligned ground truth**.

This module builds that scene and the degraded per-device recordings. It is
numpy-only (no scipy) and vendor-free. The output is:
  - ``scene``      the clean, multi-speaker event (16 kHz mono float32)
  - ``speaker_events``  exact (speaker, start_s, end_s) ground truth
  - per-recorder ``record()`` -> (audio, true_offset_s) with the offset relative
    to a reference recorder that starts at scene time ``0``.

It also models **close-microphone cross-talk**: ``make_speaker_stems`` exposes the
per-speaker contributions, ``mix_crosstalk`` renders one device with a designated
dominant speaker plus attenuated bleed from the others, and
``make_crosstalk_scene`` builds one lav-style device per speaker. Each event keeps
its true speaker, so per-segment attribution can be scored against ground truth
(the cross-talk failure mode behind ticket 09).
"""

from __future__ import annotations

import numpy as np

SR = 16000


# --------------------------------------------------------------------------- #
# Scene construction
# --------------------------------------------------------------------------- #
def _one_pole_lowpass(x: np.ndarray, alpha: float) -> np.ndarray:
    """Simple IIR lowpass (no scipy): y[n] = alpha*x[n] + (1-alpha)*y[n-1]."""
    if x.size == 0:
        return x
    a = np.clip(alpha, 1e-6, 1.0)
    b = 1.0 - a
    y = np.empty_like(x)
    acc = 0.0
    for n in range(x.size):
        acc = a * x[n] + b * acc
        y[n] = acc
    return y


def _voice_noise(rng: np.random.Generator, n: int) -> np.ndarray:
    """A speech-shaped noise burst (band-limited, syllabic envelope)."""
    noise = rng.standard_normal(n)
    # ~1 kHz-ish lowpass smearing to make it speech-band, not white
    k = max(1, int(0.0008 * SR))
    noise = np.convolve(noise, np.ones(k) / k, mode="same")
    # syllabic envelope: a few humps over the utterance
    cycles = rng.uniform(3.0, 6.0)
    env = 0.5 + 0.5 * np.abs(np.sin(np.linspace(0.0, cycles * np.pi, n)))
    return (noise * env).astype(np.float32)


def make_speaker_stems(
    duration_s: float,
    n_speakers: int = 3,
    *,
    sr: int = SR,
    seed: int = 0,
    non_overlapping: bool = False,
) -> tuple[list[np.ndarray], list[dict]]:
    """Build clean per-speaker stems and their exact event timeline.

    Every event is rendered into its own speaker's stem, then all stems are scaled
    by one shared factor so that ``sum(stems)`` equals the :func:`make_scene`
    scene. Exposing the individual stems is what makes cross-talk testable: a
    device can be rendered as one dominant speaker plus attenuated bleed from the
    others (:func:`mix_crosstalk`, :func:`make_crosstalk_scene`).

    ``events`` carries the ground-truth ``speaker`` (index) and ``start``/``end``
    (seconds) for every utterance, so a test can assert per-segment attribution.
    By default utterances may overlap (as the existing generator does); set
    ``non_overlapping`` for a scene where every window has exactly one speaker,
    which is what lets a test score attribution without overlap ambiguity.
    """
    rng = np.random.default_rng(seed)
    n = int(duration_s * sr)
    stems = [np.zeros(n, dtype=np.float32) for _ in range(n_speakers)]
    events: list[dict] = []
    speaker_voice: dict[int, float] = {
        s: rng.uniform(180.0, 320.0) for s in range(n_speakers)
    }

    t = 0.2
    while t < duration_s - 0.6:
        speaker = int(rng.integers(0, n_speakers))
        dur = rng.uniform(0.5, 1.6)
        start_s = t
        end_s = min(duration_s - 0.1, t + dur)
        i0, i1 = int(start_s * sr), int(end_s * sr)
        if i1 > i0:
            seg = _voice_noise(rng, i1 - i0)
            # per-speaker timbre: map the speaker's base freq to a lowpass cutoff
            alpha = float(
                np.clip(0.05 + (speaker_voice[speaker] - 180.0) / 4000.0, 0.03, 0.20)
            )
            stems[speaker][i0:i1] += _one_pole_lowpass(seg, alpha).astype(np.float32)
            events.append(
                {"speaker": speaker, "start": round(start_s, 4), "end": round(end_s, 4)}
            )
        t += rng.uniform(0.25, 0.9)
        if non_overlapping:
            t = max(t, end_s)

    # One shared normalisation: the sum still peaks at 0.8 exactly as before.
    mixed = np.zeros(n, dtype=np.float32)
    for stem in stems:
        mixed += stem
    peak = float(np.max(np.abs(mixed))) or 1.0
    scale = 0.8 / peak
    return [(stem * scale).astype(np.float32) for stem in stems], events


def make_scene(
    duration_s: float,
    n_speakers: int = 3,
    *,
    sr: int = SR,
    seed: int = 0,
) -> tuple[np.ndarray, list[dict]]:
    """Build a clean multi-speaker scene and its exact event timeline."""
    stems, events = make_speaker_stems(duration_s, n_speakers, sr=sr, seed=seed)
    scene = np.zeros(int(duration_s * sr), dtype=np.float32)
    for stem in stems:
        scene += stem
    return scene, events


def mix_crosstalk(
    stems: list[np.ndarray],
    dominant: int,
    *,
    bleed_db: float = -12.0,
) -> np.ndarray:
    """Render one close-microphone device from per-speaker ``stems``.

    The device's designated ``dominant`` speaker is at 0 dB; every other speaker
    bleeds in at ``bleed_db`` (e.g. ``-6`` for strong lav cross-talk, very
    negative for an effectively isolated channel). This is the "each mic hears
    more than one speaker" model the energy attributor must undo.
    """
    if not stems:
        raise ValueError("mix_crosstalk needs at least one stem")
    if not 0 <= dominant < len(stems):
        raise IndexError(f"dominant {dominant} out of range for {len(stems)} stems")
    gain = float(10.0 ** (bleed_db / 20.0))
    out = np.zeros_like(stems[0], dtype=np.float32)
    for i, stem in enumerate(stems):
        out += (stem if i == dominant else stem * gain).astype(np.float32)
    return out


def make_crosstalk_scene(
    duration_s: float,
    n_speakers: int = 2,
    *,
    bleed_db: float = -9.0,
    sr: int = SR,
    seed: int = 0,
    non_overlapping: bool = False,
) -> tuple[list[np.ndarray], list[dict]]:
    """Build one lav-style device per speaker, with modelled cross-talk.

    Device ``s`` has speaker ``s`` dominant (0 dB) and every other speaker present
    at ``bleed_db``. Returns ``(device_audio, events)`` where each event's
    ``speaker`` is the **true** source, so a test can score attribution against
    it. Pair with :func:`record` for per-device degradation if wanted.
    """
    stems, events = make_speaker_stems(
        duration_s, n_speakers, sr=sr, seed=seed, non_overlapping=non_overlapping
    )
    devices = [
        mix_crosstalk(stems, speaker, bleed_db=bleed_db)
        for speaker in range(n_speakers)
    ]
    return devices, events


# --------------------------------------------------------------------------- #
# Per-recorder degradation
# --------------------------------------------------------------------------- #
def _resample_lin(x: np.ndarray, factor: float) -> np.ndarray:
    """Linear resample by ``factor`` (>1 = longer, i.e. slower clock drift)."""
    if x.size == 0 or factor <= 0:
        return x
    n_out = int(round(x.size * factor))
    src = np.linspace(0.0, x.size - 1.0, n_out, dtype=np.float64)
    lo = np.floor(src).astype(np.int64)
    hi = np.minimum(lo + 1, x.size - 1)
    frac = (src - lo).astype(np.float32)
    return (x[lo] * (1.0 - frac) + x[hi] * frac).astype(np.float32)


def _add_reverb(x: np.ndarray, tau_s: float, sr: int) -> np.ndarray:
    """Convolve with an exponentially decaying (room) impulse response."""
    if tau_s <= 0 or x.size == 0:
        return x
    n_rir = int(tau_s * 4.0 * sr)
    t = np.arange(n_rir, dtype=np.float64) / sr
    h = np.exp(-t / tau_s)
    h /= np.sum(h)
    return np.convolve(x, h.astype(np.float32), mode="same")


def record(
    scene: np.ndarray,
    *,
    start_s: float,
    sr: int = SR,
    drift_ppm: float = 0.0,
    gain: float = 1.0,
    noise: float = 0.0,
    lowpass_ms: float = 0.0,
    rir_s: float = 0.0,
    dropout_frac: float = 0.0,
    seed: int = 0,
) -> tuple[np.ndarray, float]:
    """Capture ``scene`` as a device that starts recording at scene-time
    ``start_s``, degraded by drift/gain/noise/reverb/dropout.

    Returns ``(audio, true_offset_s)`` where ``true_offset_s`` is the offset
    ``reference_time - source_time`` relative to a reference recorder that starts
    at scene time ``0`` (i.e. exactly ``start_s``).
    """
    rng = np.random.default_rng(seed)
    n = scene.size
    i0 = int(start_s * sr)
    if i0 < 0 or i0 >= n:
        raise ValueError(f"start_s {start_s} out of range for {n / sr:.1f}s scene")
    audio = scene[i0:].astype(np.float32)
    true_offset = start_s

    if drift_ppm:
        audio = _resample_lin(audio, 1.0 + drift_ppm * 1e-6).astype(np.float32)
    if lowpass_ms > 0:
        k = max(1, int(lowpass_ms / 1000.0 * sr))
        audio = np.convolve(audio, np.ones(k) / k, mode="same").astype(np.float32)
    if rir_s > 0:
        audio = _add_reverb(audio, rir_s, sr)
    if noise > 0:
        audio = audio + (rng.standard_normal(audio.size) * noise).astype(np.float32)
    if dropout_frac > 0:
        drop_len = int(0.3 * sr)
        n_drops = int(dropout_frac * audio.size / drop_len)
        for _ in range(n_drops):
            s = int(rng.integers(0, max(1, audio.size - drop_len)))
            audio[s : s + drop_len] = 0.0

    audio = (audio * gain).astype(np.float32)
    return audio, true_offset


__all__ = [
    "SR",
    "make_crosstalk_scene",
    "make_scene",
    "make_speaker_stems",
    "mix_crosstalk",
    "record",
]
