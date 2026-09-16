"""The system-TTS provider, driven with every engine stubbed.

No test here runs a real voice: the engine lookup is faked and the process seam
(:func:`clear_record.providers.tts._run`) is replaced with a fake that answers
voice listings and records the synthesis command. That keeps the suite green on
a machine with no system voice at all.
"""

from __future__ import annotations

import io
import subprocess
import wave
from pathlib import Path

import pytest

import clear_record.providers.tts as tts
from clear_record.providers.tts import (
    Synthesis,
    TtsEngine,
    TtsUnavailable,
    TtsVoice,
    detect,
    synthesize,
    synthesize_clip,
)

SAY = TtsEngine(name="say", path="/usr/bin/say", kind="say")
ESPEAK = TtsEngine(name="espeak-ng", path="/usr/bin/espeak-ng", kind="espeak")
SPD = TtsEngine(name="spd-say", path="/usr/bin/spd-say", kind="spd-say")

SAY_VOICES = (
    "Tingting            zh_CN    # 你好！我叫婷婷。\n"
    "Eddy (中文（中国大陆）)     zh_CN    # 你好！我叫Eddy。\n"
    "Albert              en_US    # Hello! My name is Albert.\n"
    "Samantha            en_US    # Hello! My name is Samantha.\n"
)
ESPEAK_VOICES = (
    "Pty Language Age/Gender VoiceName          File          Other Languages\n"
    " 5  en             M  english              en            (en-us 2)\n"
    " 5  zh             M  Chinese (Mandarin)   asia/zh\n"
    " 5  mb-en1         M  english-mbrola       mbrola/en1\n"
)


def _wav_bytes(frames: bytes = b"\x00\x00") -> bytes:
    """A real, decodable WAV with the given frames (default: one frame)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(frames)
    return buffer.getvalue()


class FakeRunner:
    """A process seam that never launches anything.

    Voice listings answer from the canned text; a synthesis writes a tiny
    non-empty file unless the engine is in fail_names (or write is off, for the
    "engine exited 0 but wrote nothing" case).
    """

    def __init__(
        self,
        *,
        say_voices: str = SAY_VOICES,
        espeak_voices: str = ESPEAK_VOICES,
        fail_names: tuple[str, ...] = (),
        write: bool = True,
        frames: bytes = b"\x00\x00",
        raise_on_probe: Exception | None = None,
        raise_on_synth: Exception | None = None,
    ) -> None:
        self.say_voices = say_voices
        self.espeak_voices = espeak_voices
        self.fail_names = fail_names
        self.write = write
        self.frames = frames
        self.raise_on_probe = raise_on_probe
        self.raise_on_synth = raise_on_synth
        self.calls: list[list[str]] = []

    def __call__(self, command, *, timeout):
        self.calls.append(list(command))
        if self._is_probe(command):
            if self.raise_on_probe is not None:
                raise self.raise_on_probe
            if command[1:] == ["-v", "?"]:
                return subprocess.CompletedProcess(command, 0, self.say_voices, "")
            return subprocess.CompletedProcess(command, 0, self.espeak_voices, "")
        name = Path(command[0]).name
        if name in self.fail_names:
            return subprocess.CompletedProcess(command, 1, "", f"{name} boom")
        if self.raise_on_synth is not None:
            raise self.raise_on_synth
        if self.write:
            Path(self._out(command)).write_bytes(_wav_bytes(self.frames))
        return subprocess.CompletedProcess(command, 0, "", "")

    @staticmethod
    def _is_probe(command: list[str]) -> bool:
        return command[1:] == ["-v", "?"] or "--voices" in command

    @staticmethod
    def _out(command: list[str]) -> str:
        for flag in ("-o", "-w"):
            if flag in command:
                return command[command.index(flag) + 1]
        raise AssertionError(f"no output flag in {command!r}")

    @property
    def synth_calls(self) -> list[list[str]]:
        return [call for call in self.calls if not self._is_probe(call)]


def _which(monkeypatch, mapping: dict[str, str]) -> None:
    monkeypatch.setattr(tts.shutil, "which", lambda name: mapping.get(name))


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #
def test_detect_is_preference_ordered(monkeypatch) -> None:
    _which(
        monkeypatch,
        {
            "spd-say": "/usr/bin/spd-say",
            "say": "/usr/bin/say",
            "espeak-ng": "/usr/bin/espeak-ng",
            "espeak": "/usr/bin/espeak",
        },
    )

    engines = detect()

    assert [engine.name for engine in engines] == [
        "say",
        "espeak-ng",
        "espeak",
        "spd-say",
    ]
    assert engines[0].kind == "say"
    assert engines[1].kind == "espeak"
    assert [engine.writes_file for engine in engines] == [True, True, True, False]


def test_detect_is_empty_without_engines(monkeypatch) -> None:
    _which(monkeypatch, {})
    assert detect() == ()


# --------------------------------------------------------------------------- #
# no engine / playback-only engine
# --------------------------------------------------------------------------- #
def test_synthesize_without_engines_is_a_state(monkeypatch, tmp_path) -> None:
    _which(monkeypatch, {})

    with pytest.raises(TtsUnavailable, match="no system text-to-speech engine"):
        synthesize("hello", lang="en", out_path=tmp_path / "clip.wav")


def test_only_spd_say_cannot_write_a_file(monkeypatch, tmp_path) -> None:
    _which(monkeypatch, {"spd-say": "/usr/bin/spd-say"})

    with pytest.raises(TtsUnavailable, match="none can write an audio file"):
        synthesize("hello", lang="en", out_path=tmp_path / "clip.wav")


# --------------------------------------------------------------------------- #
# macOS say
# --------------------------------------------------------------------------- #
def test_say_prefers_requested_voice_and_writes_a_wav(monkeypatch, tmp_path) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(tts, "_run", runner)
    out = tmp_path / "clip.wav"

    result = synthesize_clip("你好，世界。", lang="zh_CN", out_path=out, engines=(SAY,))

    assert result == Synthesis(
        path=out, engine="say", lang="zh_CN", voice="Tingting", fallback=False
    )
    assert out.is_file()
    command = runner.synth_calls[0]
    assert command[0] == "/usr/bin/say"
    assert command[command.index("-v") + 1] == "Tingting"
    assert "--data-format=LEI16@16000" in command
    assert command[-1] == "你好，世界。"
    assert command[command.index("-o") + 1] == str(out)


def test_say_aiff_does_not_get_the_wav_data_format(monkeypatch, tmp_path) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(tts, "_run", runner)

    synthesize_clip("hi", lang="en", out_path=tmp_path / "clip.aiff", engines=(SAY,))

    command = runner.synth_calls[0]
    assert not any(part.startswith("--data-format") for part in command)
    assert command[command.index("-o") + 1] == str(tmp_path / "clip.aiff")


def test_missing_language_falls_back_to_english_voice_and_phrase(
    monkeypatch, tmp_path
) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(tts, "_run", runner)
    out = tmp_path / "clip.wav"

    result = synthesize_clip(
        "こんにちは、世界。",
        lang="ja",
        out_path=out,
        fallback_text="Hello world.",
        engines=(SAY,),
    )

    assert result.lang == "en"
    assert result.fallback is True
    assert result.voice == "Albert"
    assert runner.synth_calls[0][-1] == "Hello world."


def test_english_request_without_a_voice_uses_the_engine_default(
    monkeypatch, tmp_path
) -> None:
    runner = FakeRunner(say_voices="")
    monkeypatch.setattr(tts, "_run", runner)

    result = synthesize_clip(
        "hello", lang="en", out_path=tmp_path / "clip.wav", engines=(SAY,)
    )

    assert result.lang == "en"
    assert result.fallback is False
    assert result.voice is None
    assert "-v" not in runner.synth_calls[0]


def test_an_unreadable_voice_list_is_not_an_error(monkeypatch, tmp_path) -> None:
    runner = FakeRunner(raise_on_probe=OSError("say exploded"))
    monkeypatch.setattr(tts, "_run", runner)
    out = tmp_path / "clip.wav"

    result = synthesize_clip("hi", lang="en", out_path=out, engines=(SAY,))

    assert result.voice is None
    assert out.is_file()


# --------------------------------------------------------------------------- #
# Linux espeak
# --------------------------------------------------------------------------- #
def test_espeak_writes_a_wav_with_the_language_voice(monkeypatch, tmp_path) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(tts, "_run", runner)
    out = tmp_path / "clip.wav"

    result = synthesize_clip("你好", lang="zh", out_path=out, engines=(ESPEAK,))

    assert result == Synthesis(
        path=out, engine="espeak-ng", lang="zh", voice="zh", fallback=False
    )
    command = runner.synth_calls[0]
    assert command[0] == "/usr/bin/espeak-ng"
    assert command[command.index("-v") + 1] == "zh"
    assert command[command.index("-w") + 1] == str(out)
    assert command[-1] == "你好"


# --------------------------------------------------------------------------- #
# failures and the engine fallback chain
# --------------------------------------------------------------------------- #
def test_an_engine_that_fails_is_reported(monkeypatch, tmp_path) -> None:
    runner = FakeRunner(fail_names=("say",))
    monkeypatch.setattr(tts, "_run", runner)

    with pytest.raises(TtsUnavailable, match="say boom"):
        synthesize_clip("hi", lang="en", out_path=tmp_path / "clip.wav", engines=(SAY,))


def test_a_broken_engine_falls_through_to_the_next(monkeypatch, tmp_path) -> None:
    runner = FakeRunner(fail_names=("say",))
    monkeypatch.setattr(tts, "_run", runner)
    out = tmp_path / "clip.wav"

    result = synthesize_clip("hi", lang="en", out_path=out, engines=(SAY, ESPEAK))

    assert result.engine == "espeak-ng"
    assert out.is_file()
    assert [Path(call[0]).name for call in runner.synth_calls] == ["say", "espeak-ng"]


def test_an_engine_that_writes_nothing_is_a_failure(monkeypatch, tmp_path) -> None:
    runner = FakeRunner(write=False)
    monkeypatch.setattr(tts, "_run", runner)

    with pytest.raises(TtsUnavailable, match="produced no audio"):
        synthesize_clip("hi", lang="en", out_path=tmp_path / "clip.wav", engines=(SAY,))


def test_a_header_only_wav_is_a_failure(monkeypatch, tmp_path) -> None:
    # macOS say exits 0 and writes a valid WAV with zero frames when the speech
    # service is unavailable; the provider must not accept it as a clip.
    runner = FakeRunner(frames=b"")
    monkeypatch.setattr(tts, "_run", runner)

    with pytest.raises(TtsUnavailable, match="produced no audio"):
        synthesize_clip("hi", lang="en", out_path=tmp_path / "clip.wav", engines=(SAY,))


def test_the_pinned_synthesize_returns_the_path(monkeypatch, tmp_path) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(tts, "_run", runner)
    # The pinned synthesize() has no engines= parameter, so it detects. Stub the
    # probe too, or a runner with no TTS engine (CI Linux) raises instead.
    _which(monkeypatch, {"say": "/usr/bin/say"})
    out = tmp_path / "clip.wav"

    assert synthesize("hello", lang="en", out_path=out) == out
    assert out.is_file()


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("en_US", "en"),
        ("zh-CN", "zh"),
        ("zh_CN.UTF-8", "zh"),
        ("C", "en"),
        ("", "en"),
        ("fr_FR@euro", "fr"),
    ],
)
def test_normalize_lang(lang, expected) -> None:
    assert tts._normalize_lang(lang).split("_")[0] == expected


def test_parse_say_voices_keeps_spaced_names() -> None:
    voices = tts._parse_voices("say", SAY_VOICES)

    assert TtsVoice(name="Tingting", lang="zh_CN") in voices
    assert TtsVoice(name="Eddy (中文（中国大陆）)", lang="zh_CN") in voices
    assert TtsVoice(name="Albert", lang="en_US") in voices


def test_parse_espeak_voices_skips_the_header_and_mbrola() -> None:
    voices = tts._parse_voices("espeak", ESPEAK_VOICES)

    assert {voice.lang for voice in voices} == {"en", "zh"}
    assert all(not voice.name.startswith("mb") for voice in voices)


def test_match_voice_prefers_an_exact_locale() -> None:
    voices = (
        TtsVoice(name="base", lang="zh"),
        TtsVoice(name="taiwan", lang="zh_TW"),
        TtsVoice(name="mainland", lang="zh_CN"),
    )

    assert tts._match_voice(voices, "zh_tw") == TtsVoice("taiwan", "zh_TW")
    assert tts._match_voice(voices, "zh") == TtsVoice("base", "zh")
    assert tts._match_voice(voices, "xx") is None


def test_match_voice_normalizes_hyphenated_voice_locales() -> None:
    """Regression: say/espeak tags use a hyphen (en-GB, zh-HK).

    Both the exact-locale and the base-language comparison split only on the
    underscore, so a hyphenated tag never matched and the requested language
    silently fell through to the engine default.
    """
    voices = (TtsVoice(name="brit", lang="en-GB"), TtsVoice(name="hk", lang="zh-HK"))

    assert tts._match_voice(voices, "en_gb") == TtsVoice("brit", "en-GB")
    assert tts._match_voice(voices, "en") == TtsVoice("brit", "en-GB")
    assert tts._match_voice(voices, "zh_hk") == TtsVoice("hk", "zh-HK")
    assert tts._match_voice(voices, "zh") == TtsVoice("hk", "zh-HK")


def test_a_missing_language_without_an_english_voice_does_not_claim_english(
    monkeypatch, tmp_path
) -> None:
    """A non-English request with no matching voice and no English voice must
    not report lang='en': the engine's default voice speaks the original text."""
    runner = FakeRunner(say_voices="Tingting  zh_CN  # x\n")
    monkeypatch.setattr(tts, "_run", runner)

    result = synthesize_clip(
        "bonjour", lang="fr", out_path=tmp_path / "clip.wav", engines=(SAY,)
    )

    assert result.lang == "fr"
    assert result.fallback is False
    assert result.voice is None
    assert runner.synth_calls[0][-1] == "bonjour"
    assert "-v" not in runner.synth_calls[0]
