"""macOS 26+ system backend: Apple ``SpeechAnalyzer`` + ``SpeechTranscriber``.

This is the first **system-native** (non-``whisper-cli``) backend behind the
``Backend`` seam (ADR-0019). It drives Apple's on-device transcription service:
models are Apple's own, run on the Neural Engine, are installed/reserved through
``AssetInventory``, and are **not** bundled with clear-record.

Bridging: why a Swift helper and not ``pyobjc``
-----------------------------------------------
The macOS 26 ``SpeechAnalyzer`` / ``SpeechTranscriber`` / ``AssetInventory`` /
``AnalysisContext`` API is **Swift-only**. The ``Speech`` framework's
Objective-C headers expose only the legacy ``SFSpeechRecognizer`` classes, and
the new types carry no ``@objc`` bridging (only the shared ``@objc deinit``), so
a ``pyobjc`` wrapper cannot reach them. The bridge is therefore a small Swift
program, :file:`apple_speech_helper.swift`, compiled once and invoked as a
subprocess — the same execution shape as the shipped ``whisper-cli`` adapters.
It lives here in ``clear_record.providers``; ``clear_record.core`` stays
vendor-free (ADR-0003, ADR-0012).

The helper is **compiled from source shipped inside the wheel** and cached under
the platform cache directory (``CR_APPLE_SPEECH_HELPER`` points at a prebuilt
helper instead). Compiling needs the Swift toolchain
(``xcode-select --install``); when it is absent the backend reports
``unavailable`` with that actionable reason rather than raising ``ImportError``.
No Python dependency is introduced, so Linux/Windows installs are unaffected.

The seam's capabilities
-----------------------
- ``runtime=RUNTIME_SYSTEM`` and ``chunked=False``: the pipeline hands this
  backend each source once; the per-source chunk cache still provides coarse
  progress and resume (ADR-0019).
- ``parallelizable=False``: the analyzer is a single in-process OS session, so
  independent ``transcribe()`` calls are serialized.
- ``decoder_knobs=()``: the whisper-cli decoder knobs do not apply; a requested
  one fails loudly in the pipeline rather than being silently dropped.

Glossary, confidence and language
---------------------------------
- **Glossary: supported** via ``AnalysisContext.contextualStrings`` custom
  vocabulary (the ``--term`` arguments). Apple documents this as a *bias*, not a
  hard constraint.
- **Confidence:** populated from Apple's ``transcriptionConfidence`` attributed
  string attribute, averaged over a segment's runs; ``None`` when Apple provides
  none — it is never invented.
- **Language:** Apple serves a fixed locale set. A ``--language`` it cannot serve
  fails with an actionable error; it is never silently auto-detected.

Provisioning and the ``prepare`` seam
-------------------------------------
``available()`` never downloads: it checks the platform and macOS version, then
the helper's ``SpeechTranscriber.isAvailable`` (the helper is compiled once and
cached the first time a probe needs it). ``prepare()`` performs the
``AssetInventory`` install/reserve. The seam's ``prepare(model, model_dir)`` has
no language argument, so it provisions the **current system locale**; an
explicit ``--language`` outside it is provisioned on first ``transcribe()``
(the backend is serialized, so there is no install race). Once provisioned,
transcription is offline.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from clear_record.core import Segment, TranscriptionResult
from clear_record.core.i18n import deferred
from clear_record.core.message import Message
from clear_record.core.paths import resolve_cache_dir
from clear_record.providers.base import (
    APPLE_SPEECH_BACKEND_ID,
    Availability,
    BackendBase,
    BackendInfo,
    RUNTIME_SYSTEM,
)

#: Minimum macOS major version that ships ``SpeechAnalyzer`` / ``SpeechTranscriber``.
MIN_MACOS_MAJOR = 26

#: The Swift helper source that ships inside this package (uv_build includes the
#: module's data files, like the web templates and message catalogs).
HELPER_SOURCE = Path(__file__).with_name("apple_speech_helper.swift")
#: Point at a prebuilt helper to skip compilation (e.g. a frozen app bundle).
HELPER_ENV = "CR_APPLE_SPEECH_HELPER"

_HELPER_SUBDIR = "apple-speech"
_COMPILE_TIMEOUT_S = 300.0
#: A long tape can transcribe for a while; this is a safety net, not a limit.
_RUN_TIMEOUT_S = 24 * 60 * 60.0
#: The availability probe has no runner seam, so it is bounded in-process and
#: never inherits the 24h run ceiling.
_PROBE_TIMEOUT_S = 30.0

_PART_COUNTER = itertools.count()


class _AppleSpeechProblem(RuntimeError):
    """An Apple-speech failure with an English rendering and a translatable Message.

    ``str(exc)`` stays the English sentence (the terminal and logs print it);
    ``exc.message`` is the stable message ID plus parameters a boundary (the
    console, ``Availability.reason``) renders in the user's locale. A caller that
    passes a plain string still gets a Message wrapping it, so a dynamic detail
    is carried but never mistaken for a catalog ID.
    """

    message: Message

    def __init__(self, message: Message | str) -> None:
        self.message = (
            message if isinstance(message, Message) else Message(str(message))
        )
        super().__init__(str(self.message))


class AppleSpeechUnavailable(_AppleSpeechProblem):
    """The helper cannot be built here (absent toolchain) or the OS cannot serve it."""


class AppleSpeechError(_AppleSpeechProblem):
    """The helper ran and failed (bad locale, analysis error, unreadable output)."""


@dataclass(frozen=True)
class SpeechProbe:
    """The cheap ``probe`` verdict: ``SpeechTranscriber.isAvailable`` plus locales."""

    is_available: bool
    supported_locales: tuple[str, ...] = ()


def _macos_major() -> int | None:
    """The running macOS major version, or ``None`` when it cannot be read."""
    version = platform.mac_ver()[0]
    if not version:
        return None
    try:
        return int(version.split(".")[0])
    except ValueError:
        return None


def _swift_compiler() -> list[str] | None:
    """How to invoke ``swiftc``, or ``None`` when no toolchain is present.

    ``xcrun`` is used both as the presence check and as the launcher: invoking
    the resolved ``swiftc`` binary directly skips the SDK environment ``xcrun``
    sets up and, on a Command Line Tools-only install, fails to load the standard
    library (a ``SwiftBridging`` modulemap clash). ``xcrun --find swiftc`` is
    non-interactive: with no Command Line Tools it exits non-zero instead of
    popping the GUI installer the bare ``/usr/bin/swiftc`` shim would.
    """
    xcrun = shutil.which("xcrun")
    if xcrun:
        try:
            proc = subprocess.run(
                [xcrun, "--find", "swiftc"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return [xcrun, "swiftc"]
    swiftc = shutil.which("swiftc")
    return [swiftc] if swiftc else None


def _tail(text: str | None, limit: int = 800) -> str:
    return (text or "").strip()[-limit:]


def _glossary_terms(initial_prompt: str | None) -> tuple[str, ...]:
    """Split the pipeline's glossary prompt into Apple custom-vocabulary phrases.

    The pipeline joins terms with ``", "`` (``cli.stages._load_glossary``), so
    terms are recovered by splitting on commas and newlines. ``#`` comments and
    blanks are ignored.
    """
    if not initial_prompt:
        return ()
    terms: list[str] = []
    for chunk in initial_prompt.replace("\n", ",").split(","):
        term = chunk.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    return tuple(terms)


def _make_segment(
    start: float,
    end: float,
    text: str | None,
    *,
    source: str,
    confidence: float | None,
    language: str,
) -> Segment | None:
    """Build a normalized core ``Segment``, or ``None`` when the text is empty."""
    if end < start:
        start, end = end, start
    text = (text or "").strip()
    if not text:
        return None
    return Segment(
        start=round(start, 3),
        end=round(end, 3),
        text=text,
        source=source,
        confidence=confidence,
        language=language,
    )


def _segments_from_helper(
    entries: object, *, source: str, language: str
) -> tuple[Segment, ...]:
    """Normalize the helper's JSON ``segments`` into core ``Segment`` objects.

    The structure is validated so a malformed payload raises a clear error naming
    the backend rather than leaking an ``AttributeError``. A single segment with
    non-numeric timestamps is skipped; confidence is ``None`` when absent.
    """
    if not isinstance(entries, list):
        raise AppleSpeechError(
            Message(
                deferred(
                    "Apple SpeechTranscriber ({source}) returned non-list "
                    "'segments': {type}."
                ),
                (("source", source), ("type", type(entries).__name__)),
            )
        )
    out: list[Segment] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise AppleSpeechError(
                Message(
                    deferred(
                        "Apple SpeechTranscriber ({source}) returned a "
                        "non-object segment: {entry}."
                    ),
                    (("source", source), ("entry", repr(entry))),
                )
            )
        try:
            start = float(entry.get("start", 0.0))
            end = float(entry.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        confidence = entry.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = None
        segment = _make_segment(
            start,
            end,
            entry.get("text"),
            source=source,
            confidence=confidence,
            language=language,
        )
        if segment is not None:
            out.append(segment)
    return tuple(out)


RunCallable = Callable[..., Any]


class AppleSpeechHelper:
    """Compile/cache and drive :file:`apple_speech_helper.swift`.

    The OS call is reached only through ``run`` (a ``subprocess.run``-compatible
    callable), so tests can drive the whole backend seam with it faked and the
    suite stays green on any platform.
    """

    def __init__(
        self,
        *,
        source: Path | None = None,
        cache_dir: Path | None = None,
        run: RunCallable | None = None,
    ) -> None:
        self._source = Path(source) if source is not None else HELPER_SOURCE
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._run: RunCallable = run or subprocess.run
        self._binary: str | None = None
        self._probe: SpeechProbe | None = None

    # --- the compiled helper ------------------------------------------------ #
    def _cache_root(self) -> Path:
        base = self._cache_dir
        if base is None:
            base = Path(resolve_cache_dir())
        return base / _HELPER_SUBDIR

    def _source_hash(self) -> str:
        try:
            digest = hashlib.sha256(self._source.read_bytes()).hexdigest()
        except OSError:
            return "missing"
        return digest[:16]

    def binary_path(self) -> str | None:
        """The usable helper path, or ``None`` when it must still be compiled.

        An explicit ``CR_APPLE_SPEECH_HELPER`` wins; then a cached build for this
        source revision. This never compiles, so ``availability()`` can consult
        it cheaply.
        """
        override = os.environ.get(HELPER_ENV)
        if override and os.path.isfile(override):
            return override
        if self._binary:
            return self._binary
        cached = self._cache_root() / f"helper-{self._source_hash()}"
        if cached.is_file() and os.access(cached, os.X_OK):
            self._binary = str(cached)
            return self._binary
        return None

    def ensure_binary(self, *, runner: RunCallable | None = None) -> str:
        """Return the helper path, compiling the shipped Swift source if needed.

        ``runner`` is the caller's process runner (the pool's cancellable one),
        so an aborted compile is killed with the rest of the run. Raises
        :class:`AppleSpeechUnavailable` with an actionable reason when the Swift
        toolchain is absent or compilation fails.
        """
        existing = self.binary_path()
        if existing:
            return existing
        compiler = _swift_compiler()
        if compiler is None:
            raise AppleSpeechUnavailable(
                Message(
                    deferred(
                        "Apple SpeechTranscriber is reachable only through its "
                        "Swift-only API, so clear-record compiles a small helper "
                        "on first use. Install the Swift toolchain with "
                        "`xcode-select --install`, or set {env} to a prebuilt helper."
                    ),
                    (("env", HELPER_ENV),),
                )
            )
        cache = self._cache_root()
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppleSpeechUnavailable(
                Message(
                    deferred(
                        "could not create the Apple speech helper cache "
                        "directory {path}: {error}"
                    ),
                    (("path", str(cache)), ("error", str(exc))),
                )
            ) from exc
        dest = cache / f"helper-{self._source_hash()}"
        temp = cache / f".helper-{os.getpid()}-{next(_PART_COUNTER)}.tmp"
        run = runner or self._run
        try:
            proc = run(
                [
                    *compiler,
                    "-O",
                    "-parse-as-library",
                    str(self._source),
                    "-o",
                    str(temp),
                ],
                capture_output=True,
                text=True,
                timeout=_COMPILE_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _remove(temp)
            raise AppleSpeechUnavailable(
                Message(
                    deferred("could not compile the Apple speech helper: {detail}"),
                    (("detail", str(exc)),),
                )
            ) from exc
        if proc.returncode != 0 or not temp.is_file():
            detail = _tail(proc.stderr)
            _remove(temp)
            raise AppleSpeechUnavailable(
                Message(
                    deferred("could not compile the Apple speech helper")
                    if not detail
                    else deferred(
                        "could not compile the Apple speech helper: {detail}"
                    ),
                    () if not detail else (("detail", detail),),
                )
            )
        try:
            os.chmod(temp, 0o755)
            os.replace(temp, dest)
        except OSError as exc:
            _remove(temp)
            raise AppleSpeechUnavailable(
                Message(
                    deferred(
                        "could not install the compiled Apple speech helper: {error}"
                    ),
                    (("error", str(exc)),),
                )
            ) from exc
        self._binary = str(dest)
        return self._binary

    # --- the subcommands ---------------------------------------------------- #
    def probe(self, *, runner: RunCallable | None = None) -> SpeechProbe:
        """``SpeechTranscriber.isAvailable`` (cached per process, no download)."""
        if self._probe is None:
            data = self._result(
                ["probe"],
                stream_stderr=False,
                runner=runner,
                timeout=_PROBE_TIMEOUT_S,
            )
            supported = data.get("supportedLocales")
            self._probe = SpeechProbe(
                is_available=bool(data.get("isAvailable")),
                supported_locales=(
                    tuple(str(item) for item in supported)
                    if isinstance(supported, list)
                    else ()
                ),
            )
        return self._probe

    def prepare(
        self, language: str | None, *, runner: RunCallable | None = None
    ) -> dict:
        """Install/reserve ``language``'s asset (streaming progress to stderr)."""
        argv = ["prepare"]
        if language:
            argv += ["--locale", language]
        return self._result(argv, stream_stderr=True, runner=runner)

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        terms: tuple[str, ...] = (),
        runner: RunCallable | None = None,
    ) -> dict:
        """Transcribe ``audio_path``; return the helper's JSON payload."""
        argv = ["transcribe", "--audio", str(audio_path)]
        if language:
            argv += ["--locale", language]
        for term in terms:
            argv += ["--term", term]
        return self._result(argv, stream_stderr=False, runner=runner)

    def _result(
        self,
        argv: list[str],
        *,
        stream_stderr: bool,
        runner: RunCallable | None = None,
        timeout: float = _RUN_TIMEOUT_S,
    ) -> dict:
        binary = self.ensure_binary(runner=runner)
        run = runner or self._run
        with tempfile.TemporaryDirectory(prefix="cr-apple-speech-") as tmp:
            out = Path(tmp) / "result.json"
            cmd = [binary, *argv, "--out", str(out)]
            try:
                proc = run(
                    cmd,
                    capture_output=not stream_stderr,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise AppleSpeechError(
                    Message(
                        deferred(
                            "the Apple speech helper timed out after {seconds} seconds"
                        ),
                        (("seconds", int(timeout)),),
                    )
                ) from exc
            except OSError as exc:
                raise AppleSpeechUnavailable(
                    Message(
                        deferred("could not run the Apple speech helper: {error}"),
                        (("error", str(exc)),),
                    )
                ) from exc
            if proc.returncode != 0:
                detail = _tail(getattr(proc, "stderr", None))
                raise AppleSpeechError(
                    Message(
                        deferred(
                            "the Apple speech helper failed (exit {code}): {detail}"
                        )
                        if detail
                        else deferred("the Apple speech helper failed (exit {code})"),
                        (("code", proc.returncode), ("detail", detail))
                        if detail
                        else (("code", proc.returncode),),
                    )
                )
            if not out.is_file():
                raise AppleSpeechError(
                    Message(
                        deferred(
                            "the Apple speech helper produced no result "
                            "(it may not support SpeechAnalyzer on this OS)"
                        )
                    )
                )
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AppleSpeechError(
                    Message(
                        deferred(
                            "the Apple speech helper returned unreadable JSON: {error}"
                        ),
                        (("error", str(exc)),),
                    )
                ) from exc
            if not isinstance(data, dict):
                raise AppleSpeechError(
                    Message(
                        deferred(
                            "the Apple speech helper returned non-object JSON: {type}"
                        ),
                        (("type", type(data).__name__),),
                    )
                )
            return data


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class AppleSpeechBackend(BackendBase):
    """macOS 26+ on-device ASR via Apple ``SpeechTranscriber``.

    ``available()`` is the inherited cheap probe over :meth:`availability`, which
    names the OS/toolchain/capability it is missing; ``prepare()`` provisions the
    locale asset; ``transcribe()`` returns the same timed, source-attributed
    ``Segment`` objects as the ``whisper-cli`` backends.
    """

    def __init__(self, helper: AppleSpeechHelper | None = None) -> None:
        self.info = BackendInfo(
            id=APPLE_SPEECH_BACKEND_ID,
            vendor="Apple",
            frameworks=("Speech",),
            description=(
                "macOS 26+ on-device ASR via Apple SpeechTranscriber "
                "(SpeechAnalyzer); no third-party install, offline once provisioned."
            ),
            default_model="system",
            parallelizable=False,
            runtime=RUNTIME_SYSTEM,
            chunked=False,
            decoder_knobs=(),
        )
        self._helper = helper if helper is not None else AppleSpeechHelper()

    def availability(self) -> Availability:
        """Cheap probe: platform, macOS version, then ``SpeechTranscriber``."""
        system = platform.system()
        if system != "Darwin":
            return Availability(
                False,
                Message(
                    deferred("requires macOS {major}+ (this is {host})"),
                    (("major", MIN_MACOS_MAJOR), ("host", system)),
                ),
            )
        major = _macos_major()
        if major is None or major < MIN_MACOS_MAJOR:
            version = platform.mac_ver()[0] or "unknown"
            return Availability(
                False,
                Message(
                    deferred("requires macOS {major}+ (this is {host})"),
                    (("major", MIN_MACOS_MAJOR), ("host", version)),
                ),
            )
        try:
            probe = self._helper.probe()
        except AppleSpeechUnavailable as exc:
            return Availability(
                False,
                Message(
                    deferred("Apple Speech is unavailable: {detail}"),
                    (("detail", exc.message),),
                ),
            )
        except AppleSpeechError as exc:
            return Availability(
                False,
                Message(
                    deferred("Apple Speech probe failed: {detail}"),
                    (("detail", exc.message),),
                ),
            )
        if not probe.is_available:
            return Availability(
                False,
                Message(
                    deferred(
                        "SpeechTranscriber reports the on-device model "
                        "unavailable on this device"
                    )
                ),
            )
        return Availability(
            True,
            Message(deferred("Apple SpeechTranscriber (macOS 26+, on-device)")),
        )

    def prepare(self, model: str | None, model_dir: str | None) -> None:
        """Install/reserve the current locale's asset (the seam has no language).

        An explicit ``--language`` the prepare seam cannot see is provisioned by
        ``transcribe`` on first use.
        """
        self._helper.prepare(None)
        return None

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
        initial_prompt: str | None = None,
        process_runner=None,
        beam_size: int | None = None,
        best_of: int | None = None,
        temperature: float | None = None,
        entropy_thold: float | None = None,
        no_speech_thold: float | None = None,
        max_context: int | None = None,
        threads: int | None = None,
    ) -> TranscriptionResult:
        requested = {
            "beam_size": beam_size,
            "best_of": best_of,
            "temperature": temperature,
            "entropy_thold": entropy_thold,
            "no_speech_thold": no_speech_thold,
            "max_context": max_context,
            "threads": threads,
        }
        unsupported = sorted(
            name for name, value in requested.items() if value is not None
        )
        if unsupported:
            raise ValueError(
                f"{self.info.id} cannot honour decoder option(s): "
                f"{', '.join(unsupported)}."
            )
        lang = language if language and language not in ("", "auto") else None
        data = self._helper.transcribe(
            audio_path,
            language=lang,
            terms=_glossary_terms(initial_prompt),
            runner=process_runner,
        )
        detected = data.get("language")
        if not isinstance(detected, str) or not detected:
            detected = lang or ""
        duration = data.get("audioDuration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            duration = None
        segments = _segments_from_helper(
            data.get("segments", []), source=self.info.id, language=detected
        )
        return TranscriptionResult(
            source=self.info.id,
            segments=segments,
            language=detected,
            backend=self.info.id,
            model=model or self.info.default_model,
            audio_duration=duration,
        )


__all__ = [
    "AppleSpeechBackend",
    "AppleSpeechError",
    "AppleSpeechHelper",
    "AppleSpeechUnavailable",
    "HELPER_ENV",
    "HELPER_SOURCE",
    "MIN_MACOS_MAJOR",
    "SpeechProbe",
]
