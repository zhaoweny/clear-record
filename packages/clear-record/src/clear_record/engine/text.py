"""Text-level cleaning of ASR output (pure, dependency-free).

Whisper-family models emit bracketed non-speech markers (``[S]``, ``[MUSIC]``,
``♪``) on music/ambient audio, and can fall into repetition loops on non-speech.
Neither must become transcript. This module is deliberately pure, audio-free
logic so it can be unit-tested and reused by the transcribe and reconcile stages.

It also owns the **glossary near-match** used by scoped re-runs: whether a changed
glossary term could plausibly appear in a cached transcript. That predicate is
deliberately one-way — a ``True`` only ever causes *more* re-decoding — so it can
widen an explicit scope but can never skip a chunk that should have changed.

Finally it owns the **Han-script classifier** (Traditional vs Simplified), which
the transcribe stage uses to name the script a source came out in: nothing
rewrites a script, so a workspace that mixes backends would otherwise interleave
the two without a marker.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Sequence

from clear_record.core import Segment

# A short token that is entirely a bracketed/parenthesized expression.
_MARKER_RE = re.compile(r"^\s*[\[\(（【]\s*[^\[\]\(\)（）【】]*\s*[\]\)）】]\s*$")
# A token made only of music/symbol/punctuation characters.
_MUSIC_RE = re.compile(r"^[\s♪♫♬♩#*\-–—._]+$")
_MARKER_MAX_LEN = 40
# A run of full-width sentence punctuation and whitespace at the very start of a
# segment. ASR occasionally begins a cue with the previous sentence's full stop
# ("。我认为..."); it carries no meaning and must not become transcript.
_LEADING_CJK_PUNCT_RE = re.compile(r"^[\s，。！？；：、]+")
# Whitespace the decoder inserted before full-width punctuation ("就个人而言 ，需要"):
# CJK punctuation hugs the character before it.
_SPACE_BEFORE_CJK_PUNCT_RE = re.compile(r"\s+([，。！？；：、）】》」』])")


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


def _tidy(text: str) -> str:
    """Drop a stray leading sentence mark and the spaces before CJK punctuation."""
    tidy = _LEADING_CJK_PUNCT_RE.sub("", text)
    tidy = _SPACE_BEFORE_CJK_PUNCT_RE.sub(r"\1", tidy)
    return tidy.strip()


def clean_segments(segments: Iterable[Segment]) -> list[Segment]:
    """Remove non-speech markers and collapse repetition loops (order preserved).

    Each segment's text is tidied first, so a stray leading ``。`` or a space
    before a CJK comma never reaches the transcript, the export or the minutes.
    """
    tidied = [dataclasses.replace(seg, text=_tidy(seg.text)) for seg in segments]
    kept = [s for s in tidied if not is_non_speech(s.text)]
    return collapse_repetitions(kept)


# --- Han script (Traditional vs Simplified) -------------------------------- #
#
# A Mandarin source can arrive in either script: whisper.cpp's ``-l zh`` writes
# Traditional, Apple's on-device transcriber writes Simplified, and a workspace
# that mixes backends across sources (``--rerun-source``, an ensemble path) then
# interleaves them. Nothing downstream rewrites a script, so the record has to
# *name* it instead of silently mixing -- and that needs a classifier that adds
# no dependency.
#
# The two sets below hold characters whose counterpart in the other script is a
# **different character** (個/个, 這/这, 標/标). The sets are disjoint, so a text
# is Traditional when it carries a left-hand form, Simplified when it carries a
# right-hand one -- and *both* when it carries one of each, which is exactly the
# interleaving this classifier exists to catch. Characters that exist in both
# scripts with a different meaning (里, 后, 干, 几, 系) are deliberately absent:
# they would make the answer a guess. Neither list has to be complete to be
# useful -- one distinctive character settles a script -- so a text made only of
# shared characters reports the empty set ("undetermined") rather than a coin
# flip.
_TRADITIONAL_ONLY = frozenset(
    "個這們為說時對開會學習書語議論記認識試應該讓請謝誰買賣貴費資產業務員團"
    "國圖場報導轉邊過進運遠遲適錯錢鐘長門陽難靜頁頭顯風飛飯館馬驗體點齊龍沒"
    "問題麼樣標籤規據與讀寫聽親愛關車東見現間電話機錄銀鐵鏡鍵憂慮擔練樂藥醫"
    "數網壞實際內兩並從來決兒結總裡裏"
)
_SIMPLIFIED_ONLY = frozenset(
    "个这们为说时对开会学习书语议论记认识试应该让请谢谁买卖贵费资产业务员团"
    "国图场报导转边过进运远迟适错钱钟长门阳难静页头显风飞饭馆马验体点齐龙没"
    "问题么样标签规据与读写听亲爱关车东见现间电话机录银铁镜键忧虑担练乐药医"
    "数网坏实际内两并从来决儿结总"
)


def han_scripts(text: str) -> tuple[str, ...]:
    """The Han scripts ``text`` carries, ordered: ``()`` when it carries no
    distinctive character, else one or both of ``"simplified"``/``"traditional"``.

    The rule is the two sets above and nothing else: a character of the
    simplified set puts Simplified in the answer, one of the traditional set puts
    Traditional in it, and a text holding one of each holds **both**. Collapsing
    that to a single label would hide half of what the text shows -- the mixed
    decode is the case this exists to catch -- so a caller is handed the set and
    decides what its own comparison means.

    ``()`` is *undetermined*, never a guess: a couple of digits, a Latin name, or
    a run of characters both scripts spell settles nothing. Reading it as either
    script would invent evidence; reading it as "no difference" is what a caller
    must not do when it is comparing descriptions of two sources.
    """
    if not text:
        return ()
    found: list[str] = []
    if any(char in _SIMPLIFIED_ONLY for char in text):
        found.append("simplified")
    if any(char in _TRADITIONAL_ONLY for char in text):
        found.append("traditional")
    return tuple(found)


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
      A same-length window within one edit is also accepted, so a single
      homophone substitution (``张三`` decoded as ``张山``) is not missed.
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
        if any(
            tight_term[i : i + run] in tight_text
            for i in range(len(tight_term) - run + 1)
        ):
            return True
        # A single homophone substitution destroys every run above (张三 -> 张山),
        # so compare the whole term against each equally long transcript window.
        width = len(tight_term)
        return any(
            _within_one_edit(tight_term, tight_text[j : j + width])
            for j in range(len(tight_text) - width + 1)
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
    "han_scripts",
    "is_non_speech",
    "term_could_affect",
]
