"""Cross-correlation alignment of two recordings, using numpy FFT.

Given a reference recording and another recording of the same event, estimate the
constant time offset that best maps the source onto the reference's clock:

    reference_time = source_time + offset

The estimate is **windowed** — we take a short, high-energy reference window and
locate it in the source — which is both cheaper and far more robust than
correlating two hours of audio wholesale. The result is approximate and
explicitly not a high-precision clock-sync service (architecture §7).

Correlation convention: ``cross_corr(a, b)[lag] = Σ_t a[t] * b[t + lag]``, i.e.
positive ``lag`` means ``b`` is *delayed* relative to ``a`` (content in ``b``
appears later). We locate a window taken from the reference within the source;
the peak lag is the source position of that window, giving
``offset = ref_window_start - lag``.
"""

from __future__ import annotations

import numpy as np

from cr_core import Alignment
from cr_engine.audio import AudioDecodeError, read_audio

_WINDOW_S = 4.0
_MAX_LAG_S = 120.0
# Alignment runs at 1 kHz: ~1 ms resolution is ample for the *approximate*
# alignment this project promises, while keeping the FFT small. At 8 kHz a
# multi-hour meeting tape would need multi-GB FFT buffers; at 1 kHz a 3 h file
# is ~10.8 M samples (~hundreds of MB), so long tapes stay processable.
_ALIGN_SR = 1000


def _normalized(x: np.ndarray) -> np.ndarray:
    x = x - x.mean()
    n = np.linalg.norm(x)
    return x / n if n > 1e-12 else x


def cross_correlate(sig: np.ndarray, window: np.ndarray) -> np.ndarray:
    """Linear cross-correlation ``c[k] = Σ_t sig[t] * window[t - k]``.

    ``window`` (length ``m``) is matched against ``sig`` (length ``n``). The peak
    at index ``k`` locates the window starting at position ``k`` in ``sig``, so
    the output index *is* the lag/source position directly. Output length is
    ``n + m - 1``. Verified by ``test_cross_correlate_recovers_known_shift``.
    """
    m = window.size
    n = sig.size
    if n == 0 or m == 0:
        return np.zeros(1, dtype=np.float64)
    size = 1 << (n + m - 1).bit_length()
    fw = np.fft.rfft(window, size)
    fs = np.fft.rfft(sig, size)
    # correlation = irfft( conj(fft(window)) * fft(sig) )
    corr = np.fft.irfft(np.conj(fw) * fs, size)[: n + m - 1]
    # normalize by signal energy so peaks are comparable across sources
    return corr / max(1.0, np.linalg.norm(sig) * np.linalg.norm(window))


def estimate_offset(
    reference_path: str,
    source_path: str,
    *,
    window_s: float = _WINDOW_S,
    max_lag_s: float = _MAX_LAG_S,
) -> tuple[float, float]:
    """Estimate ``offset`` (seconds) mapping ``source`` onto ``reference``.

    Returns ``(offset, confidence)`` where ``offset = ref_time - source_time``.
    If no meaningful peak is found, returns ``(0.0, 0.0)`` so callers can fall
    back to "assume simultaneous start".
    """
    try:
        ref, _ = read_audio(reference_path, _ALIGN_SR)
        src, _ = read_audio(source_path, _ALIGN_SR)
    except AudioDecodeError:
        return 0.0, 0.0

    if ref.size < _ALIGN_SR // 2 or src.size < _ALIGN_SR // 2:
        return 0.0, 0.0

    n_win = min(int(window_s * _ALIGN_SR), ref.size)
    # pick the most energetic window in the reference (more likely to be speech)
    if ref.size > n_win:
        half = n_win // 2
        starts = range(0, ref.size - n_win + 1, half)
        energies = [float(np.mean(np.square(ref[s : s + n_win]))) for s in starts]
        win_start = list(starts)[int(np.argmax(energies))]
    else:
        win_start = 0
    window = _normalized(ref[win_start : win_start + n_win])

    corr = cross_correlate(_normalized(src), window)
    lag_index = int(np.argmax(corr))
    lag = lag_index  # output index IS the source position of the window
    value = float(corr[lag_index])

    # offset = ref_window_start - source_position (in samples at _ALIGN_SR)
    offset = (win_start - lag) / _ALIGN_SR
    # Reject peak if the window can't plausibly be located within max_lag.
    if lag > int(max_lag_s * _ALIGN_SR):
        return 0.0, 0.0
    confidence = float(min(1.0, max(0.0, value)))
    return float(offset), confidence


def align_sources(sources, reference_id: str | None = None) -> Alignment:
    """Align a list of ``Source`` objects against a reference.

    The first source is the reference by default. Any source that cannot be
    loaded/aligned is given offset ``0.0``; the alignment's ``confidence`` is the
    mean over the aligned (non-reference) sources.
    """
    if not sources:
        raise ValueError("no sources to align")
    reference = reference_id if reference_id else sources[0].id
    ref_path = next((s.path for s in sources if s.id == reference), sources[0].path)

    offsets: dict[str, float] = {}
    confs: list[float] = []
    for src in sources:
        if src.id == reference:
            offsets[src.id] = 0.0
            continue
        off, conf = estimate_offset(reference_path=ref_path, source_path=src.path)
        offsets[src.id] = round(off, 4)
        confs.append(conf)

    mean_conf = float(np.mean(confs)) if confs else None
    return Alignment(
        reference=reference,
        offsets=offsets,
        method="windowed-cross-correlation",
        confidence=mean_conf,
    )


__all__ = ["cross_correlate", "estimate_offset", "align_sources"]
