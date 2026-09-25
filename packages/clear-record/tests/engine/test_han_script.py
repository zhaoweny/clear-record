"""The Han-script classifier: which scripts a source's text came out in.

whisper.cpp's ``-l zh`` writes Mandarin in Traditional characters where Apple's
on-device transcriber writes Simplified, so a workspace that mixes backends needs
the scripts *named* rather than silently interleaved — and naming them must cost
no dependency (ticket 224). Every string here is a hand-written sample, never
field content (ADR-0006).
"""

from __future__ import annotations

import pytest

from clear_record.engine import han_scripts

# The offenders the ticket measured in whisper-cli's output: 個 這 對 標 籤 …
TRADITIONAL = "這個對象規則標籤"
SIMPLIFIED = "这个对象规则标签"
# A genuinely mixed text: the Traditional forms 這/個 beside the Simplified-only
# 对 (Simplified for 對) — what one source's text holds when some of its chunks
# were decoded before a re-decode and the rest after it.
MIXED = "這個对象"


def test_a_traditional_sentence_shows_traditional_only() -> None:
    assert han_scripts(TRADITIONAL) == ("traditional",)


def test_a_simplified_sentence_shows_simplified_only() -> None:
    assert han_scripts(SIMPLIFIED) == ("simplified",)


def test_the_simplified_prompt_fixture_is_simplified() -> None:
    """The initial prompt the whisper-cli adapter sends for ``zh`` biases the
    decoder toward Simplified characters, so the fixture itself must be."""
    assert han_scripts("以下是普通话的句子。") == ("simplified",)


def test_a_text_carrying_both_scripts_shows_both() -> None:
    """The answer is the set the text shows, never one label for a mixed text:
    collapsing the two would hide the half a caller compares on. Each half alone
    is unambiguous; only the mixture needs both."""
    assert han_scripts(MIXED) == ("simplified", "traditional")
    assert han_scripts("這個") == ("traditional",)
    assert han_scripts("这个") == ("simplified",)


@pytest.mark.parametrize("text", ["", "hello", "12:30", "你好", "  "])
def test_a_text_with_no_distinctive_character_shows_nothing(text: str) -> None:
    """Nothing settled the scripts, so the answer is empty — never a guess."""
    assert han_scripts(text) == ()
