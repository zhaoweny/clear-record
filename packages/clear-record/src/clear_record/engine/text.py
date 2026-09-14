"""Text-level cleaning of ASR output (pure, dependency-free).

Whisper-family models emit bracketed non-speech markers (``[S]``, ``[MUSIC]``,
``♪``) on music/ambient audio, and can fall into repetition loops on non-speech.
Neither must become transcript. This module is deliberately pure, audio-free
logic so it can be unit-tested and reused by the transcribe and reconcile stages.

It also owns the **glossary near-match** used by scoped re-runs: whether a changed
glossary term could plausibly appear in a cached transcript. That predicate is
deliberately one-way — a ``True`` only ever causes *more* re-decoding — so it can
widen an explicit scope but can never skip a chunk that should have changed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from clear_record.core import Segment

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


# --- glossary near-match (scoped re-runs) ---------------------------------- #
#
# A scoped re-run reuses the chunks outside its scope. The predicate below is the
# *only* thing that may pull a reused chunk back into the re-decode set, and it
# is conservative on purpose: a wrong ``True`` costs one decode, a wrong
# ``False`` could ship a stale transcript. It never suppresses a decode.

# CJK ideographs, kana and hangul: scripts that do not separate words with
# spaces, so the whole line is the comparison unit.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
# Any run of word characters, and the complement (everything to drop).
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_TIGHT_RE = re.compile(r"[\W_]+", re.UNICODE)


def _within_one_edit(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` differ by at most one insert/delete/substitute."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    # ``b`` is one character longer: skip the single extra character.
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1 :]


def term_could_affect(term: str, transcript: str) -> bool:
    """True when a glossary ``term`` could plausibly appear in ``transcript``.

    Two shapes, because the scripts tokenize differently:

    - A term containing CJK/kana/hangul has no word boundaries to rely on, so it
      is compared against the transcript with whitespace and punctuation
      removed. A one-character term must occur outright; a term of two to four
      characters must share a two-character run; a longer term a three-character
      run. (A partial run is a plausible mis-decode of the still-unknown term.)
    - Any other term is compared token by token (case-folded). A term token of
      two or more characters must equal a transcript token; one of three or more
      characters also matches a transcript token within one edit (a mis-spelled
      name). Comparing whole tokens is what keeps ``AI`` from matching ``rain``.
    """
    term = (term or "").strip()
    transcript = transcript or ""
    if not term or not transcript:
        return False

    if _CJK_RE.search(term):
        tight_term = _TIGHT_RE.sub("", term.casefold())
        tight_text = _TIGHT_RE.sub("", transcript.casefold())
        if not tight_term or not tight_text:
            return False
        run = 1 if len(tight_term) <= 1 else (2 if len(tight_term) <= 4 else 3)
        return any(
            tight_term[i : i + run] in tight_text
            for i in range(len(tight_term) - run + 1)
        )

    tokens = [token for token in _WORD_RE.findall(term.casefold()) if len(token) >= 2]
    if not tokens:
        return False
    text_tokens = _WORD_RE.findall(transcript.casefold())
    if not text_tokens:
        return False
    for token in tokens:
        if token in text_tokens:
            return True
        if len(token) >= 3 and any(
            len(other) >= 3 and _within_one_edit(token, other) for other in text_tokens
        ):
            return True
    return False


def glossary_terms(prompt: str) -> tuple[str, ...]:
    """The non-empty terms of a joined glossary prompt (``", "``-separated)."""
    return tuple(term.strip() for term in (prompt or "").split(",") if term.strip())


def changed_terms(previous: str, current: str) -> tuple[str, ...]:
    """Terms added to, or removed from, the glossary between two runs.

    Removals count: a term that *left* the prompt may equally have been steering
    the decode of a chunk whose text resembles it, so both directions widen the
    re-decode set. Returns a sorted tuple so a run's decisions are deterministic.
    """
    before = set(glossary_terms(previous))
    after = set(glossary_terms(current))
    return tuple(sorted(after ^ before))


__all__ = [
    "changed_terms",
    "clean_segments",
    "collapse_repetitions",
    "glossary_terms",
    "is_non_speech",
    "term_could_affect",
]
