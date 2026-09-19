"""System text-to-speech, behind a provider seam (ADR-0027).

The setup walkthrough needs a short spoken clip **generated on the user's own
machine** so the pipeline can ingest and transcribe it and a first-run user sees
the product work in their own language. This module is that seam: it detects the
platform's TTS engines and synthesizes text to an audio file using only the
stdlib :mod:`subprocess` module -- no Python TTS dependency is added.

Engines
-------
Detection is a plain ``PATH`` lookup, in preference order:

1. macOS ``say`` (``--data-format=LEI16@16000`` gives a plain 16 kHz mono WAV)
2. Linux ``espeak-ng`` (``-w`` writes a WAV)
3. Linux ``espeak`` (same CLI shape)
4. Linux ``spd-say`` (speech-dispatcher) -- **detected but playback-only**: the
   tool sends text to the speech-dispatcher daemon and has no file output, so it
   is reported as an engine but never selected to write a clip.

A missing voice for the requested language is a **state, not an exception**: the
provider prefers the requested language's voice and otherwise falls back to
English (voice and, when the caller supplies one, phrase). With no engine at all
the provider raises :class:`TtsUnavailable`, which a setup surface renders as a
plain "no system voice here" state rather than a crash.

Provenance
----------
:func:`synthesize_clip` returns a :class:`Synthesis` naming the engine, the
language actually spoken, the voice and whether the language fell back. The
pinned :func:`synthesize` returns only the path for callers that do not need the
record. Vendors stay in :mod:`clear_record.providers`; ``clear_record.core``
never imports this module (the layering guard, ADR-0012).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from clear_record.core.process import SubprocessRunner

#: How long a voice listing may take. It is a local probe, not synthesis.
_PROBE_TIMEOUT_S = 10.0
#: How long a synthesis may take. Generous: a slow VM or a long phrase is still
#: a valid clip, and a false "unavailable" is worse than a slow one.
_SYNTH_TIMEOUT_S = 120.0

#: The seam this module launches its engines through (see ``_run``).
_RUNNER = SubprocessRunner()

#: The WAV data format macOS ``say`` needs. Without it, ``say -o out.wav`` fails
#: with ``Opening output file failed: fmt?`` -- the extension alone does not pick
#: a container format. ``LEI16`` is 16-bit little-endian PCM; 16 kHz mono is what
#: speech recognizers expect.
_SAY_WAV_DATA_FORMAT = "LEI16@16000"

#: Engine names in preference order, with the family that decides command shape.
_ENGINE_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("say", "say"),
    ("espeak-ng", "espeak"),
    ("espeak", "espeak"),
    ("spd-say", "spd-say"),
)

#: A locale tag as the engines print it: ``en``, ``en_US``, ``zh-CN``.
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[_-][A-Za-z0-9]{2,})?$")


class TtsUnavailable(RuntimeError):
    """No system TTS engine can write a clip here, or none could be used."""


class TtsError(TtsUnavailable):
    """A detected engine was launched and failed to produce the clip.

    A subtype so a caller that only knows :class:`TtsUnavailable` still catches
    it, while a caller that wants to tell "nothing installed" from "it ran and
    broke" can.
    """


@dataclass(frozen=True)
class TtsVoice:
    """One voice an engine offers: its selector and the locale it speaks."""

    name: str
    lang: str


@dataclass(frozen=True)
class TtsEngine:
    """A detected system TTS engine."""

    name: str
    path: str
    kind: str

    @property
    def writes_file(self) -> bool:
        """True when this engine can synthesize to an audio file.

        ``spd-say`` hands text to the speech-dispatcher daemon and has no file
        output, so it is detected and reportable but never chosen to write a
        clip.
        """
        return self.kind != "spd-say"


@dataclass(frozen=True)
class Synthesis:
    """A written clip plus where it came from (the provenance ADR-0027 wants)."""

    path: Path
    engine: str
    lang: str
    voice: str | None = None
    fallback: bool = False


def detect() -> tuple[TtsEngine, ...]:
    """Every system TTS engine found on ``PATH``, in preference order.

    Cheap: a handful of :func:`shutil.which` calls, no subprocess and no model.
    """
    found: list[TtsEngine] = []
    for name, kind in _ENGINE_CANDIDATES:
        path = shutil.which(name)
        if path:
            found.append(TtsEngine(name=name, path=path, kind=kind))
    return tuple(found)


def synthesize(text: str, *, lang: str, out_path: Path) -> Path:
    """Speak ``text`` into ``out_path`` and return the written path.

    The requested language's voice is preferred; when it is missing the text is
    spoken with the English voice (or the engine's default) rather than failing.
    Raises :class:`TtsUnavailable` when no engine can write a file.
    """
    return synthesize_clip(text, lang=lang, out_path=out_path).path


def synthesize_clip(
    text: str,
    *,
    lang: str,
    out_path: Path,
    fallback_text: str | None = None,
    engines: Sequence[TtsEngine] | None = None,
) -> Synthesis:
    """Synthesize ``text`` to ``out_path`` and report the provenance.

    ``lang`` is the requested language tag (``en``, ``zh_CN``, ``zh-CN``...).
    ``fallback_text`` is the English phrase to speak instead when ``lang`` has
    no voice, so an English fallback is English words in an English voice rather
    than foreign words read by one. ``engines`` is injectable (tests, an
    already-detected snapshot); the default re-detects.

    Each file-writing engine is tried in preference order, so a broken first
    engine falls through to the next rather than failing the clip.
    """
    out_path = Path(out_path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TtsUnavailable(
            f"could not create the clip directory {out_path.parent}: {exc}"
        ) from exc

    available = tuple(engines) if engines is not None else detect()
    writers = tuple(engine for engine in available if engine.writes_file)
    if not writers:
        names = ", ".join(engine.name for engine in available)
        if names:
            raise TtsUnavailable(
                f"found {names}, but none can write an audio file; install "
                "espeak-ng (Linux) or use macOS say"
            )
        raise TtsUnavailable(
            "no system text-to-speech engine found; install espeak-ng on Linux, "
            "or use macOS say"
        )

    requested = _normalize_lang(lang) or "en"
    display_lang = (lang or "").strip() or "en"
    failures: list[str] = []
    for engine in writers:
        voices = _list_voices(engine)
        voice = _match_voice(voices, requested)
        spoken_lang = display_lang
        spoken_text = text
        fallback = False
        if voice is None and requested != "en":
            english = _match_voice(voices, "en")
            if english is not None:
                # Only claim English when an English voice was actually chosen:
                # with none, the engine's default voice speaks the original
                # text and the provenance must not invent a language.
                voice = english
                spoken_lang = "en"
                fallback = True
                if fallback_text is not None:
                    spoken_text = fallback_text
        command = _command(engine, spoken_text, voice=voice, out_path=out_path)
        try:
            proc = _run(command, timeout=_SYNTH_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{engine.name}: {type(exc).__name__}: {exc}")
            continue
        if proc.returncode != 0:
            failures.append(
                f"{engine.name}: exit {proc.returncode}{_detail(proc.stderr)}"
            )
            continue
        if not _has_audio(out_path):
            failures.append(f"{engine.name}: produced no audio")
            continue
        return Synthesis(
            path=out_path,
            engine=engine.name,
            lang=spoken_lang,
            voice=voice.name if voice is not None else None,
            fallback=fallback,
        )
    raise TtsError("every system text-to-speech engine failed: " + "; ".join(failures))


def _normalize_lang(lang: str) -> str:
    """A language tag as ``en_us``/``zh``: strip encoding and unify separators.

    An empty tag normalises to ``""`` -- "no tag" -- so a caller that wants the
    English default must apply it explicitly and a voice carrying no locale can
    never be mistaken for an English one. ``C``/``POSIX`` is the process default
    and keeps the English rendering.
    """
    code = (lang or "").strip()
    if not code:
        return ""
    if code in {"C", "POSIX"}:
        return "en"
    code = code.split(".")[0].split("@")[0]
    return code.replace("-", "_").lower()


def _match_voice(voices: Sequence[TtsVoice], lang: str) -> TtsVoice | None:
    """The best voice for ``lang``: exact locale first, then base language.

    ``lang`` arrives normalised, but a voice tag is kept as the engine printed
    it, so both sides are normalised here -- ``en-GB`` and ``zh-HK`` must match
    ``en_gb`` and ``zh_hk`` rather than silently falling through to the engine
    default.
    """
    target = _normalize_lang(lang)
    if not target:
        return None
    base = target.split("_")[0]
    for voice in voices:
        if _normalize_lang(voice.lang) == target:
            return voice
    for voice in voices:
        if _normalize_lang(voice.lang).split("_")[0] == base:
            return voice
    return None


def _list_voices(engine: TtsEngine) -> tuple[TtsVoice, ...]:
    """Ask ``engine`` for its voices; an unreadable list is simply empty."""
    if engine.kind == "say":
        command = [engine.path, "-v", "?"]
    elif engine.kind == "espeak":
        command = [engine.path, "--voices"]
    else:
        return ()
    try:
        proc = _run(command, timeout=_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ()
    if proc.returncode != 0:
        return ()
    return _parse_voices(engine.kind, proc.stdout or "")


def _parse_voices(kind: str, output: str) -> tuple[TtsVoice, ...]:
    """Parse ``say -v '?'`` or ``espeak --voices`` output into voices."""
    voices: list[TtsVoice] = []
    if kind == "say":
        for raw in output.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            name, sep, locale = line.rpartition(" ")
            if not sep or not _LOCALE_RE.match(locale):
                continue
            voices.append(TtsVoice(name=name.strip(), lang=locale))
    elif kind == "espeak":
        for raw in output.splitlines():
            parts = raw.split()
            if len(parts) < 2 or not parts[0].isdigit():
                continue
            code = parts[1]
            if code.startswith("mb") or not _LOCALE_RE.match(code):
                continue
            voices.append(TtsVoice(name=code, lang=code))
    return tuple(voices)


def _command(
    engine: TtsEngine,
    text: str,
    *,
    voice: TtsVoice | None,
    out_path: Path,
) -> list[str]:
    """The argv that speaks ``text`` into ``out_path`` for ``engine``."""
    if engine.kind == "say":
        command = [engine.path]
        if voice is not None:
            command += ["-v", voice.name]
        if out_path.suffix.lower() in {".wav", ".wave"}:
            command += [f"--data-format={_SAY_WAV_DATA_FORMAT}"]
        return [*command, "-o", str(out_path), text]
    if engine.kind == "espeak":
        command = [engine.path]
        if voice is not None:
            command += ["-v", voice.name]
        return [*command, "-w", str(out_path), text]
    raise TtsUnavailable(f"{engine.name} cannot write an audio file")


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run one engine command through the seam, capturing text output.

    Kept as a module-level function (rather than inlining ``_RUNNER.run`` at the
    two call sites) because it is this module's process seam for tests: every
    voice listing and synthesis in the suite is driven through a fake here.
    """
    return _RUNNER.run(command, capture_output=True, text=True, timeout=timeout)


def _has_audio(path: Path) -> bool:
    """True when ~path~ holds a non-empty audio stream.

    A WAV is checked with the stdlib :mod:~wave~ reader: macOS ~say~ exits 0 and
    writes a header-only file when the speech service is unavailable, which a
    plain file-size check would wrongly accept as a clip. Other containers (a
    caller's ~.aiff~) fall back to "exists and is not empty".
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
    except OSError:
        return False
    if path.suffix.lower() in {".wav", ".wave"}:
        try:
            with wave.open(str(path), "rb") as handle:
                return handle.getnframes() > 0
        except (wave.Error, EOFError, OSError):
            return False
    return True


def _detail(stderr: str | None, limit: int = 400) -> str:
    text = (stderr or "").strip()[-limit:]
    return f": {text}" if text else ""


__all__ = [
    "Synthesis",
    "TtsEngine",
    "TtsError",
    "TtsUnavailable",
    "TtsVoice",
    "detect",
    "synthesize",
    "synthesize_clip",
]
