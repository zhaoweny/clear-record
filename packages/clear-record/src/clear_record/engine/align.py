"""Cross-correlation alignment of two recordings, using numpy FFT.

Given a reference recording and another recording of the same event, estimate the
constant time offset that best maps the source onto the reference's clock:

    reference_time = source_time + offset

The estimate is **windowed** — we take several reference windows spread across
the tape (the most energetic in each slice) and locate them in the source —
which is both cheaper and far more robust than correlating two hours of audio
wholesale. A single window can be a poor anchor: the most energetic 4 s stretch
may be a music bed, a dropout, or a section the other device missed entirely.
Using several windows spread across the reference, and requiring them to agree on
the same offset, survives those bad anchors; a lone window is trusted only when
its peak is decisive. The result is approximate and explicitly not a
high-precision clock-sync service (architecture §7).

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

from datetime import datetime, timezone

import numpy as np
import soundfile as sf

from clear_record.core import Alignment
from clear_record.engine.audio import AudioDecodeError, read_audio

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
# A window is kept if it carries any real signal; only essentially-silent
# windows are skipped. This is an *absolute* floor (~-80 dBFS, below 16-bit
# quantization), deliberately not relative to the loudest window: a genuine but
# attenuated shared passage must stay able to corroborate even when some other
# device cut a much louder stretch (a phone memo next to a PA, say). Dropping
# windows by relative energy created a ~10 dB recall cliff.
_SILENCE_RMS = 1e-4
# Absolute sanity floor for a correlation peak. It is *not* the noise
# discriminator — independent speech-band noise reaches ~0.13 and short white
# noise ~0.05 — it only stops a degenerate near-empty window (whose normalized
# correlation is numerical fuzz) from being treated as a match. Real
# discrimination comes from prominence plus corroboration.
_MIN_COEFFICIENT = 0.10
# A strong peak is trusted without a prominence test: clean same-device matches
# measure ~0.95-1.0, well clear of the ~0.13 noise ceiling. (The 4-device
# synthetic scenes also peak ~0.96-0.99 but, because they share one degradation
# seed, their reverberant shoulder widens MAD; the strong-peak bypass keeps them
# from depending on a thin prominence margin.) It is also the bar a *lone*
# uncorroborated vote must clear — see estimate_offset.
_STRONG_COEFFICIENT = 0.50
# Peak prominence = (peak - median) / MAD of the coefficient profile over the
# search band. The search is bounded to full-window overlaps; that bound is what
# tames this statistic. Without it, partial-overlap edge lags on short
# recordings normalize a handful of samples into a spuriously high cosine and
# pushed uncorrelated noise to prominence ~10.4 at 20 s and ~25.7 at 6 s (the
# reviewer's finding, reproduced here).
#
# Calibration with the bound in place (synthetic, measured):
#   - independent speech-band noise, 6-600 s, 300 seeds/length: per-window
#     prominence <= 9.40; low-passed noise <= 8.52; white noise never reaches the
#     0.10 coefficient floor. End-to-end (prominence + corroboration), 0 false
#     accepts over 300 seeds at 6-20 s, 200 at 60 s and 60 at 300 s, including
#     long references whose quiet tail is skipped;
#   - genuine but reverberant (dry ref vs rir_s 0.16): ~10.0-13.7;
#   - processed built-in mic (band-limited + noise): ~10.6-17.8 at peak coeff
#     0.10-0.17 (its weakest window may fall below the floor and is correctly
#     outvoted by the stronger ones — consensus, not any single window, decides);
#   - phone-like (band-limited + independent noise): ~14-24 at peak coeff
#     0.15-0.26.
#
# The floor stays at 10.0 rather than climbing above the 9.40 noise tail: the
# genuinely reverberant pair sits at ~10, so a higher floor trades away exactly
# the mic-mismatched recall ticket 10 targets (raising it breaks
# test_estimate_offset_resolves_reverberant_source). Discrimination instead
# comes from requiring corroborating votes, with a strong-peak bar for a lone
# window — the noise tail cannot produce either.
_MIN_PROMINENCE = 10.0
# An offset is accepted only when at least this many windows agree on it (within
# tolerance). A single vote is accepted only when its peak clears
# ``_STRONG_COEFFICIENT``; "the silence filter left one window" does not count as
# a one-window reference.
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
    """Several window start-positions spread across the reference.

    The reference is divided into ``_N_WINDOWS`` slices and the most energetic
    window in each is kept, so the candidates span the whole tape rather than
    clustering on one loud stretch. Only essentially-silent windows
    (``_SILENCE_RMS``) are dropped; a quieter window is *not* discarded for being
    quieter than a loud neighbour, so attenuated-but-genuine content can still
    corroborate the offset.
    """
    n = ref.size
    if n <= n_win:
        return [0] if _window_rms(ref) >= _SILENCE_RMS else []
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
    return [s for s in starts if _window_rms(ref[s : s + n_win]) >= _SILENCE_RMS]


def _window_rms(x: np.ndarray) -> float:
    """Root-mean-square of a window, for the absolute near-silence floor."""
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0


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
        #
        # Bound to full-window overlaps only (lag <= n_src - n_win): a lag where
        # the window only partly overlaps the source normalizes a handful of
        # samples into a cosine, which spikes the coefficient and its prominence
        # on short recordings. Genuine matches land at full-overlap lags.
        lo = max(0, win_start - max_lag)
        hi = min(n_src - n_win, win_start + max_lag)
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

    offset, confidence = max(best, key=lambda v: v[1])
    if len(best) < _MIN_VOTES and confidence < _STRONG_COEFFICIENT:
        # A lone, uncorroborated vote is accepted only when its peak is strong.
        # The reference can genuinely offer just one usable window — a short clip
        # or a long tape with a single content stretch — but a weak lone peak is
        # indistinguishable from an accidental match against unrelated audio.
        # Critically, "the filter left one window" must not count as "the
        # reference has only one window": a long reference whose quieter windows
        # are silence still cannot smuggle a single weak peak through.
        return 0.0, None

    return float(offset), float(min(1.0, max(0.0, confidence)))


def _length_s(path: str) -> float | None:
    """A recording's length in seconds, from its header, or ``None`` when the
    recording cannot be read at all (the estimator's own verdict is then all the
    evidence there is).

    A header read, not a decode, for every container libsndfile opens: it costs
    nothing on a long tape. A container libsndfile *refuses* — WavPack, m4a/aac,
    the field tapes ``read_audio`` decodes through ffmpeg — is measured from that
    same decode at the alignment rate instead. Its length is not unknown: it is
    one this function would otherwise miss, and reading it as unknown is what
    leaves a sequential pair's question open for the estimator's spurious peak to
    answer, the misplacement this bound exists to prevent. The decode costs what
    the estimator's own read of the same file costs, and only a pair whose
    declarations reach this path pays it.
    """
    try:
        info = sf.info(str(path))
    except (OSError, RuntimeError):
        try:
            data, sr = read_audio(str(path), _ALIGN_SR)
        except Exception:  # not decodable either: no length, and no new refusal
            return None
        if sr <= 0 or data.size <= 0:
            return None
        return float(data.size) / float(sr)
    if info.samplerate <= 0 or info.frames <= 0:
        return None
    return float(info.frames) / float(info.samplerate)


def _cannot_overlap(reference_path: str, source_path: str, gap_s: float) -> bool:
    """Whether two recordings declared *gap_s* apart hold no passage a
    correlation could find — ``True`` for the sequential parts of one recorder,
    ``False`` where only the audio can say.

    *gap_s* is the source's start **after** the reference's (``ref_time =
    source_time + gap_s``, so ``gap_s`` is ``source.start - reference.start``), and
    the two windows on the timeline are ``[0, ref_s]`` and ``[gap_s, gap_s +
    src_s]``. They touch only if the gap is shorter than the recording that began
    **first** — the earlier one's tail is what a later start can land inside,
    whatever the later one's own length. So the bound is the *earlier* length, not
    the shorter: a 20 s take beginning 40 s into a 60 s one is inside it and
    shares 20 s of passage, while two 90 s parts 90 s apart share none.

    Two parts of one recorder are the second kind — **sequential**: the later one
    begins no earlier than the earlier one lasts, so there is no common passage
    for a correlation to find, and a peak it reports over such a pair is two
    unrelated passages matching, the spurious small offset this module refuses to
    trust. Two devices armed within the same minute are the first kind: they
    overlap, and the audio measures their arming skew better than a whole-second
    name does. A rotating recorder that keeps a little pre-roll meets *inside* a
    correlation window of its own length, so that much is allowed here: the bound
    is the earlier length **less ``_WINDOW_S``**, and a pair whose gap reaches
    that is called sequential. A header either side cannot be read leaves the
    question open (``False``): the audio, not the length, then decides.
    """
    ref_s = _length_s(reference_path)
    src_s = _length_s(source_path)
    if ref_s is None or src_s is None:
        return False
    earlier_s = ref_s if gap_s >= 0 else src_s
    return abs(gap_s) >= earlier_s - _WINDOW_S


def _usable_start(value: object) -> float | None:
    """*value* as a declared start this module can use, or ``None``.

    ``Source.start_s`` is a number when ``ingest`` reads it off a file name, and
    whatever a hand-edit put in the field when an operator declares one in the
    manifest — the flow this feature exists for. ``align`` places a declared pair
    by the difference of the two declarations, so it can take any number a clock
    can render (the same measure ``ingest`` keeps, so both layers read the field
    alike) and nothing else: a name
    (``"noon"``), a bool, or ``nan``/``inf`` is no declaration at all, dropped
    here rather than raising out of the stage over a field a user typed.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
        datetime.fromtimestamp(number, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return number


def align_sources(sources, reference_id: str | None = None) -> Alignment:
    """Align a list of ``Source`` objects against a reference.

    The first source is the reference by default. Sources that cannot be placed
    are recorded in ``Alignment.unresolved`` and omitted from ``offsets`` (no fake
    zero); ``confidence`` is the mean over the sources whose offset the estimator
    found, so an alignment made only of declarations reports ``None`` — a declared
    start carries no correlation evidence, and inventing a confidence for it
    would be exactly the fake number this module refuses to write.

    **A declared start places a source the estimator cannot reach.** A recorder
    that splits one long capture into numbered files writes each part's start
    time into its name, and ``ingest`` carries it as ``Source.start_s``. For a
    pair that both declare one, the declaration places it in either of two cases:

    - the two **cannot overlap** (:func:`_cannot_overlap`): the distance between
      their declared starts reaches the recording that began first, less one
      correlation window (``_WINDOW_S``, 4 s) — a rotating recorder's pre-roll
      meets *inside* a window of its own length, and that much overlap is still a
      rotation, holding no passage a correlation window can land in. The parts
      are sequential, and a peak the estimator reports over such a pair is two
      unrelated passages matching, which is how a part at a length-multiple
      offset used to be accepted at a spurious small one;
    - the distance is **wider than ``_MAX_LAG_S``**, outside anything the
      estimator can express, and it is not asked.

    A pair that *can* overlap is the audio's to decide, and its verdict is kept
    whenever it has one: two devices armed within the same minute carry a real
    skew a filename's whole-second stamp does not resolve. When the audio has no
    verdict — no shared passage it can reach, an input it could not read, or a
    source too short for its own band — the declaration places the source, since
    the alternative is a part the record cannot place at all.

    So a declared source is reported unresolved only when it has nothing to be
    placed against: a declaration on one side alone is not a placement, and
    ``unresolved`` says so. A start reaches a source two ways — ``ingest`` reads
    it off the file name, or an operator declares it in the manifest.
    """
    if not sources:
        raise ValueError("no sources to align")
    reference = reference_id if reference_id else sources[0].id
    # One fallback for both, not two: when *reference* names no source the first
    # source stands in as the reference, and it must stand in for its declared
    # start too — otherwise every declaration on the other side is silently
    # dropped, and `clear-record align --reference <typo>` reads as "nothing
    # declared".
    ref_source = next((s for s in sources if s.id == reference), sources[0])
    ref_path = ref_source.path
    ref_start = _usable_start(ref_source.start_s)

    offsets: dict[str, float] = {}
    unresolved: list[str] = []
    confs: list[float] = []
    declared: list[str] = []
    for src in sources:
        if src.id == reference:
            offsets[src.id] = 0.0
            continue
        src_start = _usable_start(src.start_s)
        declared_gap = (
            None if src_start is None or ref_start is None else src_start - ref_start
        )
        if declared_gap is not None and (
            abs(declared_gap) > _MAX_LAG_S
            or _cannot_overlap(ref_path, src.path, declared_gap)
        ):
            offsets[src.id] = round(declared_gap, 4)
            declared.append(src.id)
            continue
        off, conf = estimate_offset(reference_path=ref_path, source_path=src.path)
        if conf is None:
            if declared_gap is not None:
                offsets[src.id] = round(declared_gap, 4)
                declared.append(src.id)
                continue
            unresolved.append(src.id)
            continue
        offsets[src.id] = round(off, 4)
        confs.append(conf)

    mean_conf = float(np.mean(confs)) if confs else None
    return Alignment(
        reference=reference,
        offsets=offsets,
        # Every offset came from a declaration and none from the audio: the
        # record must not name a method that never ran. A mixed alignment keeps
        # the estimator's name, which is what its confidence is the mean of.
        method=(
            "declared-start" if declared and not confs else "windowed-cross-correlation"
        ),
        confidence=mean_conf,
        unresolved=tuple(unresolved),
    )


__all__ = ["cross_correlate", "estimate_offset", "align_sources"]
