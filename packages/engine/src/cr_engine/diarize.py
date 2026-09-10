"""Dependency-free multi-speaker diarization (baseline).

For a **single mixed stream** there are no per-speaker channels, so speaker
attribution has to come from the audio itself. Real diarization systems use deep
speaker embeddings (x-vectors / ECAPA); to stay dependency-light and vendor-free
this module provides a transparent **baseline**: per-segment log-mel statistics
(a cheap spectral fingerprint) clustered with k-means, with the number of
speakers either given or estimated by silhouette score.

It is not a state-of-the-art diarizer and is honest about that — it is the
default that always works offline, behind a seam a stronger provider can replace.
"""

from __future__ import annotations

import numpy as np

__all__ = ["diarize", "logmel_stats", "pitch_stats"]

# Minimum silhouette score for the automatic model count to accept more than one
# speaker. This is deliberately a *strong* margin: a measured single-speaker
# scene (``make_scene(n_speakers=1)``) scores only ~0.2, so a lower floor still
# split one voice into two. A clear two-voice separation scores >~0.5.
_MIN_SILHOUETTE = 0.45


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
def _hz_to_mel(f: np.ndarray | float) -> np.ndarray:
    return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)


def _mel_to_hz(m: np.ndarray | float) -> np.ndarray:
    return 700.0 * (10.0 ** (np.asarray(m) / 2595.0) - 1.0)


def _mel_filterbank(
    sr: int, n_fft: int, n_mels: int, fmin: float, fmax: float
) -> np.ndarray:
    fmax = min(fmax, sr / 2.0)
    mels = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2)
    hz = _mel_to_hz(mels)
    bins = np.clip(np.floor((n_fft + 1) * hz / sr).astype(int), 0, n_fft // 2)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    for m in range(1, n_mels + 1):
        left, center, right = int(bins[m - 1]), int(bins[m]), int(bins[m + 1])
        if center <= left:
            center = left + 1
        if right <= center:
            right = center + 1
        for k in range(left, min(center, fb.shape[1])):
            fb[m - 1, k] = (k - left) / (center - left)
        for k in range(center, min(right, fb.shape[1])):
            fb[m - 1, k] = (right - k) / (right - center)
    return fb


def logmel_stats(
    x: np.ndarray,
    sr: int,
    *,
    n_mels: int = 40,
    frame: int = 400,
    hop: int = 160,
    n_fft: int = 512,
    max_seconds: float = 20.0,
) -> np.ndarray:
    """Return a fixed-length spectral fingerprint: mean+std of log-mel energies."""
    x = x.astype(np.float64)
    cap = int(max_seconds * sr)
    if x.size > cap:
        start = (x.size - cap) // 2
        x = x[start : start + cap]
    if x.size < frame:
        x = np.pad(x, (0, frame - x.size))
    n_frames = 1 + (x.size - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(frame)[None, :]
    spec = np.fft.rfft(frames, n=n_fft, axis=1)
    power = spec.real**2 + spec.imag**2
    fb = _mel_filterbank(sr, n_fft, n_mels, 20.0, 8000.0)
    mel = power @ fb.T
    logmel = np.log(mel + 1e-10)
    return np.concatenate([logmel.mean(axis=0), logmel.std(axis=0)]).astype(np.float64)


def pitch_stats(
    x: np.ndarray,
    sr: int,
    *,
    frame_ms: float = 40.0,
    hop_ms: float = 20.0,
    fmin: float = 60.0,
    fmax: float = 400.0,
    max_seconds: float = 20.0,
) -> np.ndarray:
    """Return ``(median_f0, f0_iqr, voiced_fraction)`` via autocorrelation.

    Fundamental frequency is the single most discriminative cheap speaker cue
    (e.g. male vs female), so it is combined with the spectral fingerprint.
    """
    x = x.astype(np.float64)
    cap = int(max_seconds * sr)
    if x.size > cap:
        start = (x.size - cap) // 2
        x = x[start : start + cap]
    frame = int(frame_ms / 1000.0 * sr)
    hop = max(1, int(hop_ms / 1000.0 * sr))
    if x.size < frame:
        x = np.pad(x, (0, frame - x.size))
    n_frames = 1 + (x.size - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(frame)[None, :]
    frames = frames - frames.mean(axis=1, keepdims=True)
    n_fft = 1 << (2 * frame - 1).bit_length()
    spec = np.fft.rfft(frames, n_fft, axis=1)
    ac = np.fft.irfft(spec * np.conj(spec), n_fft, axis=1)[:, :frame]
    ac = ac / (ac[:, :1] + 1e-9)
    lo, hi = max(1, int(sr / fmax)), min(frame - 1, int(sr / fmin))
    if hi <= lo:
        return np.array([0.0, 0.0, 0.0])
    region = ac[:, lo : hi + 1]
    peaks = region.argmax(axis=1) + lo
    strength = region.max(axis=1)
    voiced = strength > 0.3
    if not np.any(voiced):
        return np.array([0.0, 0.0, 0.0])
    f0 = sr / peaks[voiced]
    return np.array(
        [
            float(np.median(f0)),
            float(np.percentile(f0, 75) - np.percentile(f0, 25)),
            float(voiced.mean()),
        ]
    )


def _embedding(x: np.ndarray, sr: int) -> np.ndarray:
    return np.concatenate([logmel_stats(x, sr), pitch_stats(x, sr)])


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #
def _kmeans(
    x: np.ndarray, k: int, *, iters: int = 100, n_init: int = 5, seed: int = 0
) -> tuple[np.ndarray, np.ndarray, float]:
    rng = np.random.default_rng(seed)
    n = x.shape[0]
    k = max(1, min(k, n))
    best: tuple[float, np.ndarray, np.ndarray] | None = None

    def _dist(centers: np.ndarray) -> np.ndarray:
        return np.linalg.norm(x[:, None, :] - centers[None, :, :], axis=2)

    for _ in range(n_init):
        centers = [x[rng.integers(n)]]
        for _ in range(1, k):
            d = np.min(
                np.stack([np.sum((x - c) ** 2, axis=1) for c in centers]), axis=0
            )
            probs = d / (d.sum() + 1e-12)
            centers.append(x[rng.choice(n, p=probs)])
        c = np.array(centers)
        labels = np.zeros(n, dtype=int)
        for _ in range(iters):
            new = _dist(c).argmin(axis=1)
            if np.array_equal(new, labels):
                break
            labels = new
            for j in range(k):
                if np.any(labels == j):
                    c[j] = x[labels == j].mean(axis=0)
        inertia = float(np.sum(np.min(_dist(c), axis=1) ** 2))
        if best is None or inertia < best[0]:
            best = (inertia, labels.copy(), c.copy())
    assert best is not None
    return best[1], best[2], best[0]


def _silhouette(x: np.ndarray, labels: np.ndarray, *, max_n: int = 400) -> float:
    n = x.shape[0]
    if n > max_n:
        idx = np.linspace(0, n - 1, max_n).astype(int)
        x, labels, n = x[idx], labels[idx], max_n
    uniq = np.unique(labels)
    if uniq.size < 2:
        return -1.0
    d = np.linalg.norm(x[:, None, :] - x[None, :, :], axis=2)
    scores = []
    for i in range(n):
        same = labels == labels[i]
        same[i] = False
        if not np.any(same):
            scores.append(0.0)
            continue
        a = float(d[i, same].mean())
        b = min(float(d[i, labels == c].mean()) for c in uniq if c != labels[i])
        scores.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return float(np.mean(scores))


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def diarize(
    audio: np.ndarray,
    sr: int,
    segments: list[tuple[float, float]],
    n_speakers: int | None = None,
    *,
    seed: int = 0,
) -> list[int]:
    """Assign a speaker index (0-based, ordered by first appearance) per segment.

    ``segments`` are ``(start_s, end_s)`` pairs into ``audio``. Returns one label
    per input segment (segments too short to fingerprint get label 0).
    """
    labels = [0] * len(segments)
    if not segments:
        return labels

    embeddings: list[np.ndarray] = []
    keep: list[int] = []
    for i, (a, b) in enumerate(segments):
        s, e = max(0, int(a * sr)), min(audio.size, int(b * sr))
        if e - s < int(0.2 * sr):
            continue
        embeddings.append(_embedding(audio[s:e], sr))
        keep.append(i)
    if len(keep) < 2:
        return labels

    x = np.array(embeddings)
    x = (x - x.mean(axis=0)) / (x.std(axis=0) + 1e-8)
    # Balance the feature blocks: without this, 80 spectral dimensions drown out
    # the 3 pitch dimensions in Euclidean distance, even though pitch is the
    # most discriminative cheap cue. Normalize each block to equal total weight
    # and give pitch a modest boost.
    n_pitch = 3
    n_spec = x.shape[1] - n_pitch
    if n_spec > 0:
        spec = x[:, :n_spec] / np.sqrt(n_spec)
        pitch = x[:, n_spec:] / np.sqrt(n_pitch) * 1.5
        x = np.concatenate([spec, pitch], axis=1)

    if n_speakers and n_speakers >= 2:
        lab, _, _ = _kmeans(x, min(n_speakers, len(keep)), seed=seed)
    else:
        best_lab: np.ndarray | None = None
        best_score = -2.0
        kmax = min(6, len(keep) // 2)
        for k in range(2, kmax + 1):
            candidate, _, _ = _kmeans(x, k, seed=seed)
            score = _silhouette(x, candidate)
            if score > best_score:
                best_score, best_lab = score, candidate
        if best_lab is None or best_score < _MIN_SILHOUETTE:
            # No clear multi-speaker structure: stay at one speaker rather than
            # inventing a split (single-narrator and non-speech over-split before).
            lab = np.zeros(len(keep), dtype=int)
        else:
            lab = best_lab

    for j, orig in enumerate(keep):
        labels[orig] = int(lab[j])

    # Relabel by first appearance so "Speaker 1" speaks first.
    remap: dict[int, int] = {}
    ordered: list[int] = []
    for value in labels:
        if value not in remap:
            remap[value] = len(remap)
            ordered.append(value)
    return [remap[value] for value in labels]
