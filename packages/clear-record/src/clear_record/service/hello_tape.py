"""The hello-world tape: a synthetic clip the setup walkthrough plays back.

ADR-0027 makes a spoken "hello world", generated on the user's machine, the
**onboarding acceptance test**: the wizard creates it, the pipeline ingests and
transcribes it, and the transcript is shown back. The same flow is a permanent
diagnostic, so a re-run localizes a fault to transcription, clear-record, MCP or
the model.

This module owns the two things the tape needs beyond the TTS provider:

- **The phrase per locale.** It is *spoken content*, not UI copy, so it lives
  here as data rather than in the gettext catalog: nothing renders it as a
  translated label, and a machine translator or a catalog update must not change
  what the clip says.
- **The provenance record.** The clip is environment-local and never committed
  (ADR-0006), but where it came from -- engine, language, phrase, voice and
  whether the language fell back -- is durable and travels with the tape.

The provider is reached through :mod:`clear_record.pipeline.tts`: the layering
DAG (ADR-0012) lets ``service`` import ``pipeline`` but not ``providers``, so the
names cross there -- the route :mod:`clear_record.service.agent_flow` takes to
:mod:`clear_record.pipeline.stages` for the model provisioning.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clear_record.pipeline.tts import Synthesis, synthesize_clip
from clear_record.core.i18n import normalize_locale

#: The spoken phrase per base language. Deliberately **not** gettext: this is
#: what the clip says, not a string a surface renders. English is the fallback
#: for any language without an entry here.
HELLO_PHRASES: dict[str, str] = {
    "en": "Hello world. This is a clear record test.",
    "zh": "你好，世界。这是一次清晰录音测试。",
    "es": "Hola mundo. Esta es una prueba de grabación clara.",
    "fr": "Bonjour le monde. Ceci est un test d'enregistrement clair.",
    "de": "Hallo Welt. Dies ist ein Test für eine klare Aufnahme.",
    "ja": "こんにちは、世界。これはクリアレコードのテストです。",
    "pt": "Olá, mundo. Este é um teste de gravação clara.",
    "it": "Ciao, mondo. Questo è un test di registrazione chiara.",
    "ko": "안녕하세요, 세계. 이것은 선명한 녹음 테스트입니다.",
    "ru": "Привет, мир. Это тест чёткой записи.",
}

#: The clip's basename stem; the language is appended so two locales can share
#: one destination directory without overwriting each other.
_CLIP_STEM = "hello-world"


@dataclass(frozen=True)
class HelloTape:
    """A generated hello-world clip plus its provenance.

    ``lang`` is the language actually spoken -- the requested one when its voice
    exists, otherwise English -- and ``phrase`` is the text actually spoken.
    ``fallback`` is True when the requested language had no voice and English was
    used instead.
    """

    path: Path
    engine: str
    lang: str
    phrase: str
    voice: str | None = None
    fallback: bool = False


def _base_lang(lang: str) -> str:
    """The base language code of ``lang`` (``zh_CN`` -> ``zh``), or ``en``."""
    code = normalize_locale(lang) or "en"
    return code.replace("-", "_").split("_")[0].lower() or "en"


def hello_phrase(lang: str) -> str:
    """The clip's phrase for ``lang``, falling back to the English phrase."""
    return HELLO_PHRASES.get(_base_lang(lang), HELLO_PHRASES["en"])


def _clip_path(destination: Path, lang: str) -> Path:
    token = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in _base_lang(lang)
    )
    return destination / f"{_CLIP_STEM}-{token}.wav"


def write_hello_tape(destination: Path, *, lang: str) -> HelloTape:
    """Synthesize the locale's hello-world clip under ``destination``.

    ``destination`` is a **directory**; it is created if missing and the clip is
    written inside it as ``hello-world-<lang>.wav``. Returns a :class:`HelloTape`
    naming the path and the provenance (engine, language actually spoken, phrase,
    voice, fallback).

    A missing voice for ``lang`` is not an error: the provider speaks the English
    phrase with the English voice and ``HelloTape.fallback`` is True. With no
    TTS engine at all the provider's :class:`TtsUnavailable` propagates -- a
    state a setup surface renders, never an unhandled crash.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    requested_phrase = hello_phrase(lang)
    english_phrase = HELLO_PHRASES["en"]
    synthesis: Synthesis = synthesize_clip(
        requested_phrase,
        lang=lang,
        out_path=_clip_path(destination, lang),
        fallback_text=english_phrase,
    )
    phrase = english_phrase if synthesis.fallback else requested_phrase
    return HelloTape(
        path=synthesis.path,
        engine=synthesis.engine,
        lang=synthesis.lang,
        phrase=phrase,
        voice=synthesis.voice,
        fallback=synthesis.fallback,
    )


__all__ = [
    "HELLO_PHRASES",
    "HelloTape",
    "hello_phrase",
    "write_hello_tape",
]
