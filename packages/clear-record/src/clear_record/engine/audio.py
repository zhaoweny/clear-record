"""Audio I/O: decode any supported file to a mono float32 array + sample rate.

Uses ``soundfile`` for formats libsndfile understands (wav, flac, ogg, opus,
aiff…); for phone/handheld formats soundfile cannot decode (m4a, mp3 in some
builds) it shells out to ``ffmpeg`` and re-reads the resulting wav — and to the
``ffprobe`` beside it when a container libsndfile refuses must be *counted*
rather than decoded (``channel_count``). Everything is still environment-local;
nothing is written to audio unless a decode helper explicitly asks for it.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from clear_record.core.process import SubprocessRunner

_FFMPEG = shutil.which("ffmpeg")
#: The count-only probe beside ``ffmpeg``, from the same install: a container
#: ``soundfile`` refuses is counted through the decoder that reads it at all.
_FFPROBE = shutil.which("ffprobe")

#: The seam ``ffmpeg`` is launched through: a one-shot decode with no grace or
#: cancellation of its own, whose non-zero exit stays a ``CalledProcessError``.
_RUNNER = SubprocessRunner()


class AudioDecodeError(RuntimeError):
    """Raised when a file cannot be decoded to PCM."""


def _decode_with_ffmpeg(
    path: Path, target_sr: int | None, channel: int | None = None
) -> tuple[np.ndarray, int]:
    if _FFMPEG is None:
        raise AudioDecodeError(
            f"cannot decode {path.name!r}: no decoder available. "
            "Install FFmpeg (or use wav/flac/ogg which soundfile reads natively)."
        )
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "decoded.wav"
        cmd = [_FFMPEG, "-v", "error", "-y", "-i", str(path)]
        if channel is None:
            cmd += ["-ac", "1"]
        else:
            # Select one channel instead of downmixing, the way ``sf.read``'s own
            # ``channel`` does for the containers it opens: a multichannel tape
            # in a container libsndfile refuses must still split per channel,
            # rather than be silently collapsed to a mono mix of them all.
            cmd += ["-af", f"pan=mono|c0=c{channel}"]
        if target_sr:
            cmd += ["-ar", str(target_sr)]
        cmd += [str(out)]
        # Bytes output and ``check`` both keep what this decode has always done:
        # a failed ``ffmpeg`` raises ``subprocess.CalledProcessError``.
        _RUNNER.run(cmd, capture_output=True, text=False, check=True)
        return read_audio(out, target_sr=target_sr)


def read_audio(
    path: str | Path,
    target_sr: int | None = None,
    channel: int | None = None,
) -> tuple[np.ndarray, int]:
    """Return ``(mono float32 in [-1, 1], sample_rate)``.

    If ``channel`` is given, that channel is selected (0-based) instead of
    downmixing a multi-channel file. If ``target_sr`` is given, the returned
    sample rate is exactly ``target_sr``.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    try:
        data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    except (RuntimeError, sf.LibsndfileError):  # libsndfile can't read it
        if channel is not None:
            # The range check ``sf.read`` would have made, against the count the
            # decoder's own probe reports — a container libsndfile refuses has a
            # channel count too, and selecting one of its channels is what the
            # split path asks for.
            channels = channel_count(path)
            if channel < 0 or channel >= channels:
                raise AudioDecodeError(
                    f"channel {channel} out of range for {path.name!r} "
                    f"({channels} channel(s))"
                )
        return _decode_with_ffmpeg(path, target_sr, channel)

    if data.ndim > 1:
        if channel is not None:
            if channel < 0 or channel >= data.shape[1]:
                raise AudioDecodeError(
                    f"channel {channel} out of range for {path.name!r} "
                    f"({data.shape[1]} channel(s))"
                )
            data = data[:, channel]
        else:
            data = data.mean(axis=1)  # downmix to mono
    data = np.ascontiguousarray(data, dtype=np.float32)

    if target_sr is not None and sr != target_sr:
        data = _resample(data, sr, target_sr)
        sr = target_sr
    return data, sr


def channel_count(path: str | Path) -> int:
    """Number of channels in an audio file (1 on any probe failure).

    A container libsndfile opens is counted from its header, which costs nothing.
    A container it **refuses** — WavPack, m4a/aac, the field tapes ``read_audio``
    decodes through ffmpeg — is counted through that same decoder, whose probe
    reports what the container really holds. Counting those as one is what let a
    multichannel WavPack be silently downmixed: the split decision (and
    ``resolve_auto``'s ``max_channels``) read a mono that was never there, so a
    four-channel tape became one source and `--split-channels` did nothing.
    """
    try:
        info = sf.info(str(path))
        return int(info.channels)
    except Exception:
        try:
            return _ffmpeg_channel_count(Path(path))
        except Exception:
            return 1


def _ffmpeg_channel_count(path: Path) -> int:
    """The channel count of a container libsndfile refuses, from the decoder's
    own probe (the ``ffprobe`` beside the ``ffmpeg`` ``read_audio`` shells out
    to). Raises ``AudioDecodeError`` when that probe is not installed, and any
    ``CalledProcessError``/parse failure the caller's fallback catches."""
    if _FFPROBE is None:
        raise AudioDecodeError(
            f"cannot count the channels of {path.name!r}: no prober available. "
            "Install FFmpeg (or use wav/flac/ogg which soundfile reads natively)."
        )
    probe = _RUNNER.run(
        [
            _FFPROBE,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(probe.stdout.strip().splitlines()[0])


def _resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Linear-interpolation resampling (adequate for 16 kHz ASR prep)."""
    if x.size == 0:
        return x.astype(np.float32)
    n_out = int(round(x.size * dst_sr / src_sr))
    if n_out <= 0:
        return x[:0].astype(np.float32)
    src_pos = np.linspace(0.0, x.size - 1.0, n_out, dtype=np.float64)
    lo = np.floor(src_pos).astype(np.int64)
    hi = np.minimum(lo + 1, x.size - 1)
    frac = (src_pos - lo).astype(np.float32)
    return (x[lo] * (1.0 - frac) + x[hi] * frac).astype(np.float32)


def rms(x: np.ndarray) -> float:
    """Root-mean-square energy of a signal."""
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


# whisper.cpp requires 16 kHz mono WAV; normalize once at ingest.
ASR_SAMPLE_RATE = 16000


def prepare_16k_wav(
    src: str | Path, dst: str | Path, channel: int | None = None
) -> None:
    """Decode ``src`` to a 16 kHz mono WAV at ``dst``.

    ``channel`` selects one channel of a multi-channel source (0-based); when
    omitted the source is downmixed to mono.
    """
    data, sr = read_audio(src, ASR_SAMPLE_RATE, channel=channel)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), data, ASR_SAMPLE_RATE)


__all__ = [
    "ASR_SAMPLE_RATE",
    "AudioDecodeError",
    "channel_count",
    "prepare_16k_wav",
    "read_audio",
    "rms",
]
