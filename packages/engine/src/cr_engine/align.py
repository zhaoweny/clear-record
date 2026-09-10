"""Cross-correlation alignment of two recordings, using numpy FFT.

Given a reference recording and another recording of the same event, estimate the
constant time offset that best maps the source onto the reference's clock:

    reference_time = source_time + offset

The estimate is **windowed** — we take several short, high-energy reference
windows spread across the tape and locate them in the source — which is both
cheaper and far more robust than correlating two hours of audio wholesale. A
single window can be a poor anchor: the most energetic 4 s stretch may be a
music bed, a dropout, or a section the other device missed entirely. Using
several windows spread across the reference, and requiring them to agree on the
same offset, survives those bad anchors. The result is approximate and
explicitly not a high-precision clock-sync service (architecture §7).

A window is "matched" by the **prominence** of its correlation peak: how far the
best peak stands above the correlation's own robust background, rather than by an
absolute coefficient alone. A genuine match — even a heavily attenuated,
band-limited one from a different mic — produces one isolated peak that towers
over the background, whereas uncorrelated audio produces a field of comparable
peaks. Prominence is therefore scale- and degradation-robust in a way a fixed
coefficient floor is not.

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

# Number of candidate windows spread across the reference. Each is the most
# energetic window in its slice of the tape, so a bad anchor in one slice cannot
# sink the estimate; the others still cover the shared content.
_N_WINDOWS = 6
# Candidate windows quieter than this fraction of the strongest candidate are
# dropped: on a tape that is mostly silence only the windows around real content
# remain, and their offsets are the ones that can be corroborated.
_MIN_WINDOW_ENERGY_FRAC = 0.05
# Absolute sanity floor. A window that is mostly silence has a correlation whose
# median/MAD collapse, inflating prominence to absurd values for what is really
# a random match; this floor rejects those. It is far below any genuine match —
# measured: genuine weak matches peak at ~0.10-0.29; independent speech-band
# noise peaks at ~0.13, and short (4 s) white noise against a quiet reference at
# ~0.05.
_MIN_COEFFICIENT = 0.10
# A strong peak is trusted without a prominence test: clean same-device matches
# measure ~0.95-1.0, well clear of the ~0.13 noise ceiling. (The 4-device
# synthetic scenes also peak ~0.96-0.99 but, because they share one degradation
# seed, their reverberant shoulder widens MAD; the strong-peak bypass keeps them
# from depending on a thin prominence margin.)
_STRONG_COEFFICIENT = 0.50
# Peak prominence = (peak - median) / MAD of the coefficient profile over the
# search band. Calibration (synthetic, measured):
#   - independent speech-band noise, 20-1200 s, 40+ seeds: prominence <= 8.1
#     (the shipped 1200 s test pair measures <= 6.9);
#   - genuine but reverberant (dry ref vs rir_s 0.16): ~19-27;
#   - phone-like (band-limited + independent noise): ~16-27 at peak coeff
#     0.17-0.26;
#   - processed built-in mic (band-limited + noise): ~12-20 at peak coeff
#     0.10-0.17;
#   - clean same-device: ~60.
# The 10.0 floor sits above the noise ceiling and below every genuine match.
# It deliberately *accepts* the dry-vs-reverberant and mic-mismatched pairs that
# the old absolute 0.25 floor rejected (ticket 04's precision-over-recall
# tradeoff is superseded by ticket 10's prominence criterion).
_MIN_PROMINENCE = 10.0
# An offset is accepted only when at least this many windows agree on it (within
# tolerance), unless the reference is too short to offer more than one window.
_MIN_VOTES = 2
_VOTE_TOLERANCE_S = 1.0


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


def _candidate_window_starts(ref: np.ndarray, n_win: int) -> list[int]:
    """Several high-energy window start-positions spread across the reference.

    The reference is divided into ``_N_WINDOWS`` slices and the most energetic
    window in each is kept, so the candidates span the whole tape rather than
    clustering on one loud stretch. Windows far quieter than the strongest
    candidate (a mostly-silent slice) are dropped.
    """
    n = ref.size
    if n <= n_win:
        return [0]
    span = n - n_win
    chosen: list[int] = []
    for b in range(_N_WINDOWS):
        b0 = b * span // _N_WINDOWS
        b1 = (b + 1) * span // _N_WINDOWS
        candidates = list(range(b0, b1 + 1, max(1, n_win // 2))) or [b0]
        energies = [
            float(np.mean(np.square(ref[s : s + n_win], dtype=np.float64)))
            for s in candidates
        ]
        chosen.append(candidates[int(np.argmax(energies))])

    starts: list[int] = []
    for s in chosen:
        if s not in starts:
            starts.append(s)
    energies = {
        s: float(np.mean(np.square(ref[s : s + n_win], dtype=np.float64)))
        for s in starts
    }
    strongest = max(energies.values()) if energies else 0.0
    return [s for s in starts if energies[s] >= _MIN_WINDOW_ENERGY_FRAC * strongest]


def _coefficient_profile(
    corr: np.ndarray, src_sq: np.ndarray, n_src: int, n_win: int
) -> np.ndarray:
    """Normalize a raw correlation into a per-lag cosine coefficient.

    ``corr`` is a raw dot product with a unit-norm window, so it scales with the
    *local* energy of the source at each lag. Dividing by the local window norm
    turns it into the normalized coefficient, which is scale-invariant (~1.0 for
    a matching window at any signal length).
    """
    lags = np.arange(corr.size)
    lo = np.minimum(lags, n_src)
    hi = np.minimum(lags + n_win, n_src)
    local = np.sqrt(np.maximum(src_sq[hi] - src_sq[lo], 0.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(local > 1e-12, corr / np.maximum(local, 1e-12), 0.0)


def estimate_offset(
    reference_path: str,
    source_path: str,
    *,
    window_s: float = _WINDOW_S,
    max_lag_s: float = _MAX_LAG_S,
) -> tuple[float, float | None]:
    """Estimate ``offset`` (seconds) mapping ``source`` onto ``reference``.

    Returns ``(offset, confidence)`` where ``offset = ref_time - source_time``.
    A ``confidence`` of ``None`` means the source could not be placed at all
    (unreadable, too short, or no peak) — callers must not read that as a zero
    offset.
    """
    try:
        ref, _ = read_audio(reference_path, _ALIGN_SR)
        src, _ = read_audio(source_path, _ALIGN_SR)
    except (AudioDecodeError, FileNotFoundError):
        return 0.0, None

    if ref.size < _ALIGN_SR // 2 or src.size < _ALIGN_SR // 2:
        return 0.0, None

    n_win = min(int(window_s * _ALIGN_SR), ref.size)
    max_lag = int(max_lag_s * _ALIGN_SR)

    # One source FFT and one prefix-sum of its squared samples are reused across
    # every candidate window, so the extra windows cost little on long tapes.
    src_n = _normalized(src)
    n_src = src_n.size
    size = 1 << (n_src + n_win - 1).bit_length()
    src_fft = np.fft.rfft(src_n, size)
    src_sq = np.concatenate([[0.0], np.cumsum(src_n.astype(np.float64) ** 2)])

    starts = _candidate_window_starts(ref, n_win)
    votes: list[tuple[float, float]] = []  # (offset_s, peak coefficient)
    for win_start in starts:
        window = _normalized(ref[win_start : win_start + n_win])
        if np.linalg.norm(window) <= 1e-12:
            continue
        fw = np.fft.rfft(window, size)
        corr = np.fft.irfft(np.conj(fw) * src_fft, size)[: n_src + n_win - 1]
        coef = _coefficient_profile(corr, src_sq, n_src, n_win)

        # Search *both* directions around the reference window position, bounded
        # by max_lag. The one-sided guard of an earlier revision rejected any
        # window found beyond max_lag into the source — which silently zeroed
        # every recording whose most energetic window sat more than max_lag into
        # the tape.
        lo = max(0, win_start - max_lag)
        hi = min(coef.size - 1, win_start + max_lag)
        if hi < lo:
            continue
        band = coef[lo : hi + 1]
        k = int(np.argmax(band))
        peak = float(band[k])
        if peak < _MIN_COEFFICIENT:
            continue
        median = float(np.median(band))
        mad = float(np.median(np.abs(band - median)))
        prominence = (peak - median) / max(mad, 1e-12)
        if prominence < _MIN_PROMINENCE and peak < _STRONG_COEFFICIENT:
            continue
        votes.append(((win_start - (lo + k)) / _ALIGN_SR, peak))

    if not votes:
        return 0.0, None

    # Keep the largest cluster of votes that agree on an offset. Uncorrelated
    # audio scatters its peaks across lags, so it cannot form a cluster; genuine
    # content places every window at (nearly) the same offset.
    best: list[tuple[float, float]] = []
    for offset, _ in votes:
        cluster = [v for v in votes if abs(v[0] - offset) <= _VOTE_TOLERANCE_S]
        if len(cluster) > len(best) or (
            len(cluster) == len(best)
            and max(v[1] for v in cluster) > max((v[1] for v in best), default=0.0)
        ):
            best = cluster
    if len(starts) > 1 and len(best) < _MIN_VOTES:
        # Only one window found a peak; without corroboration it is as likely to
        # be noise as content, so leave the source unresolved rather than record
        # a meaningless offset.
        return 0.0, None

    offset, confidence = max(best, key=lambda v: v[1])
    return float(offset), float(min(1.0, max(0.0, confidence)))


def align_sources(sources, reference_id: str | None = None) -> Alignment:
    """Align a list of ``Source`` objects against a reference.

    The first source is the reference by default. Sources that cannot be placed
    are recorded in ``Alignment.unresolved`` and omitted from ``offsets`` (no fake
    zero); ``confidence`` is the mean over the sources that were aligned.
    """
    if not sources:
        raise ValueError("no sources to align")
    reference = reference_id if reference_id else sources[0].id
    ref_path = next((s.path for s in sources if s.id == reference), sources[0].path)

    offsets: dict[str, float] = {}
    unresolved: list[str] = []
    confs: list[float] = []
    for src in sources:
        if src.id == reference:
            offsets[src.id] = 0.0
            continue
        off, conf = estimate_offset(reference_path=ref_path, source_path=src.path)
        if conf is None:
            unresolved.append(src.id)
            continue
        offsets[src.id] = round(off, 4)
        confs.append(conf)

    mean_conf = float(np.mean(confs)) if confs else None
    return Alignment(
        reference=reference,
        offsets=offsets,
        method="windowed-cross-correlation",
        confidence=mean_conf,
        unresolved=tuple(unresolved),
    )


__all__ = ["cross_correlate", "estimate_offset", "align_sources"]
