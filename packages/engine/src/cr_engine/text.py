"""Text-level cleaning of ASR output (pure, dependency-free).

Whisper-family models emit bracketed non-speech markers (``[S]``, ``[MUSIC]``,
``♪``) on music/ambient audio, and can fall into repetition loops on non-speech.
Neither must become transcript. This module is deliberately pure, audio-free
logic so it can be unit-tested and reused by the transcribe and reconcile stages.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from cr_core import Segment

# A short token that is entirely a bracketed/parenthesised expression.
_MARKER_RE = re.compile(r"^\s*[\[\(（【]\s*[^\[\]\(\)（）【】]*\s*[\]\)）】]\s*$")
# A token made only of music/symbol/punctuation characters.
_MUSIC_RE = re.compile(r"^[\s♪♫♬♩#*\-–—._]+$")
_MARKER_MAX_LEN = 40


def is_non_speech(text: str) -> bool:
    """True when ``text`` carries no speech: blank, a short bracketed marker, or
    only music/symbol characters."""
    t = (text or "").strip()
    if not t:
        return True
    if _MARKER_RE.match(t):
        return len(t) <= _MARKER_MAX_LEN
    return bool(_MUSIC_RE.match(t))


def collapse_repetitions(segments: Sequence[Segment]) -> list[Segment]:
    """Drop consecutive segments that repeat the same text from the same
    source/speaker (decoder loops).

    Repetition is a per-source, contiguous phenomenon: identical words arriving
    from *different* sources are separate simultaneous events, so they are kept
    for the overlap resolver to arbitrate. A genuine short repeat is retained as
    a single occurrence rather than dropped entirely.
    """
    out: list[Segment] = []
    for seg in segments:
        if out:
            prev = out[-1]
            if (
                prev.text.strip() == seg.text.strip()
                and prev.source == seg.source
                and (prev.speaker or "") == (seg.speaker or "")
            ):
                continue
        out.append(seg)
    return out


def clean_segments(segments: Iterable[Segment]) -> list[Segment]:
    """Remove non-speech markers and collapse repetition loops (order preserved)."""
    kept = [s for s in segments if not is_non_speech(s.text)]
    return collapse_repetitions(kept)


__all__ = ["clean_segments", "collapse_repetitions", "is_non_speech"]
