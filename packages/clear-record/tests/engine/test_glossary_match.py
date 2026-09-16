"""The glossary near-match guard: one-way, conservative, script-aware.

This predicate decides whether a chunk the scope *excluded* is pulled back into
the re-decode set, so its two properties are what the tests pin: a plausible
match is found (never skipped), and the rule errs toward matching rather than
away from it. A false positive only costs a decode; a false negative is the
failure this exists to prevent.
"""

from __future__ import annotations

import pytest

from clear_record.engine import changed_terms, glossary_terms, term_could_affect


@pytest.mark.parametrize(
    ("term", "transcript"),
    [
        ("Acme", "the Acme deal"),  # exact token, any case
        ("Acme", "ACME"),
        ("Goldman", "we met Goldmen"),  # one substitution
        ("Kubernetes", "kubernets is up"),  # one deletion
        ("ProjectX", "Project X shipped"),  # decoded as two tokens
        ("张三", "今天张三来"),  # exact CJK
        ("张三", "张 三 来了"),  # CJK decoded with spaces between characters
        ("王小明", "王 小 明"),  # a two-character run survives
    ],
)
def test_a_plausible_match_is_found(term: str, transcript: str) -> None:
    assert term_could_affect(term, transcript) is True


def test_a_single_cjk_homophone_substitution_still_matches() -> None:
    """Regression: a one-character name mis-decode must not be skipped.

    ``张三`` and ``张山`` share no two-character run, so the run rule alone
    returned False and a scoped re-run would carry a stale transcript.
    """
    assert term_could_affect("张三", "今天张山来了") is True
    assert term_could_affect("王小明", "王大明") is True


@pytest.mark.parametrize(
    ("term", "transcript"),
    [
        ("Acme", "unrelated words"),
        # Whole-token comparison is what stops a two-letter term matching every
        # word that happens to contain it.
        ("AI", "the rain in spain"),
        ("AI", "said again"),
        ("张三", "completely different"),
        ("Kubernetes", "cattle ranch"),
    ],
)
def test_an_unrelated_transcript_is_not_flagged(term: str, transcript: str) -> None:
    assert term_could_affect(term, transcript) is False


def test_single_character_latin_terms_do_not_match_every_word() -> None:
    """A one-letter term is too weak a signal to justify a decode on its own."""
    assert term_could_affect("a", "banana") is False


def test_empty_inputs_never_match() -> None:
    assert term_could_affect("", "anything") is False
    assert term_could_affect("Acme", "") is False
    assert term_could_affect("  ", "Acme") is False


def test_changed_terms_counts_additions_and_removals() -> None:
    """A term that *left* the prompt can change a chunk too, so it counts."""
    assert changed_terms("", "Acme") == ("Acme",)
    assert changed_terms("Acme", "") == ("Acme",)
    assert changed_terms("Acme, Beta", "Acme, Gamma") == ("Beta", "Gamma")
    assert changed_terms("Acme, Beta", "Beta, Acme") == ()  # reordering is not a change


def test_glossary_terms_ignores_blank_entries() -> None:
    assert glossary_terms(" Acme , , Beta ") == ("Acme", "Beta")
    assert glossary_terms("") == ()
