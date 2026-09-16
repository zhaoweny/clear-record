"""The hello-world tape, with the TTS provider stubbed.

No test here needs a system voice: the service's one provider call
(synthesize_clip) is replaced, so the test asserts phrase selection, the
fallback handshake and the provenance record without touching an engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clear_record.providers.tts import Synthesis, TtsUnavailable
from clear_record.service import hello_tape
from clear_record.service.hello_tape import (
    HELLO_PHRASES,
    HelloTape,
    hello_phrase,
    write_hello_tape,
)


def _stub(monkeypatch, result):
    """Replace the provider call; capture its arguments."""
    calls: dict = {}

    def fake(text, *, lang, out_path, fallback_text=None, engines=None):
        calls["text"] = text
        calls["lang"] = lang
        calls["fallback_text"] = fallback_text
        calls["out_path"] = Path(out_path)
        Path(out_path).write_bytes(b"RIFF0000")
        return result(Path(out_path))

    monkeypatch.setattr(hello_tape, "synthesize_clip", fake)
    return calls


def _provenance(**overrides) -> object:
    def make(path: Path) -> Synthesis:
        fields = {
            "path": path,
            "engine": "say",
            "lang": "zh_CN",
            "voice": "Tingting",
            "fallback": False,
        }
        fields.update(overrides)
        return Synthesis(**fields)

    return make


# --------------------------------------------------------------------------- #
# phrase selection
# --------------------------------------------------------------------------- #
def test_hello_phrase_localizes_by_base_language() -> None:
    assert hello_phrase("zh_CN") == HELLO_PHRASES["zh"]
    assert hello_phrase("zh-TW") == HELLO_PHRASES["zh"]
    assert hello_phrase("fr_FR.UTF-8") == HELLO_PHRASES["fr"]
    assert hello_phrase("en_US") == HELLO_PHRASES["en"]


def test_hello_phrase_defaults_to_english() -> None:
    assert hello_phrase("xx_YY") == HELLO_PHRASES["en"]
    assert hello_phrase("") == HELLO_PHRASES["en"]


# --------------------------------------------------------------------------- #
# the tape
# --------------------------------------------------------------------------- #
def test_write_hello_tape_lands_under_the_destination(monkeypatch, tmp_path) -> None:
    destination = tmp_path / "tape"
    calls = _stub(monkeypatch, _provenance())

    tape = write_hello_tape(destination, lang="zh_CN")

    assert isinstance(tape, HelloTape)
    assert destination.is_dir()
    assert tape.path.is_file()
    assert destination in tape.path.parents
    assert tape.path.suffix == ".wav"
    # It asked the provider for the locale's phrase and handed it the English
    # phrase as the fallback text.
    assert calls["text"] == HELLO_PHRASES["zh"]
    assert calls["fallback_text"] == HELLO_PHRASES["en"]
    assert calls["lang"] == "zh_CN"
    assert calls["out_path"] == tape.path


def test_write_hello_tape_records_provenance(monkeypatch, tmp_path) -> None:
    _stub(monkeypatch, _provenance(engine="espeak-ng", lang="zh", voice="zh"))

    tape = write_hello_tape(tmp_path, lang="zh")

    assert tape.engine == "espeak-ng"
    assert tape.lang == "zh"
    assert tape.phrase == HELLO_PHRASES["zh"]
    assert tape.voice == "zh"
    assert tape.fallback is False


def test_a_language_fallback_switches_the_phrase_to_english(
    monkeypatch, tmp_path
) -> None:
    calls = _stub(
        monkeypatch,
        _provenance(lang="en", voice="Albert", fallback=True),
    )

    tape = write_hello_tape(tmp_path, lang="ja")

    # The requested phrase was Japanese, but the spoken one is English.
    assert calls["text"] == HELLO_PHRASES["ja"]
    assert calls["fallback_text"] == HELLO_PHRASES["en"]
    assert tape.phrase == HELLO_PHRASES["en"]
    assert tape.lang == "en"
    assert tape.fallback is True


def test_write_hello_tape_surfaces_an_unavailable_provider(
    monkeypatch, tmp_path
) -> None:
    def boom(*args, **kwargs):
        raise TtsUnavailable("no system text-to-speech engine found")

    monkeypatch.setattr(hello_tape, "synthesize_clip", boom)

    with pytest.raises(TtsUnavailable):
        write_hello_tape(tmp_path, lang="en")


def test_two_locales_do_not_overwrite_each_other(monkeypatch, tmp_path) -> None:
    _stub(monkeypatch, _provenance())
    first = write_hello_tape(tmp_path, lang="zh_CN")
    _stub(monkeypatch, _provenance(lang="en", voice="Albert", fallback=True))
    second = write_hello_tape(tmp_path, lang="en")

    assert first.path != second.path
    assert first.path.is_file()
    assert second.path.is_file()
