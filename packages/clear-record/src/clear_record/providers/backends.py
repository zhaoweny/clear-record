"""Concrete backend adapters for the three supported compute families.

Each backend is a *capability*: a cheap probe in ``available()`` decides whether
the machine can run it, and any heavy stack is touched only inside
``transcribe()``. If a dependency or the runtime probe is missing, the backend
reports itself unavailable and the CLI proceeds without crashing.

Mapping to compute families (ADR-0005):

- ``apple``  — Apple/macOS via ``whisper.cpp`` (Metal), driving the *system*
  ``whisper-cli`` (Homebrew ``whisper-cpp`` + a ggml ``libggml-metal`` plugin).
- ``nvidia`` — NVIDIA via the *system* ``whisper-cli`` (CUDA / Vulkan),
  Linux-only probe.
- ``amd``    — AMD Radeon via the *system* ``whisper-cli`` (Vulkan / ROCm),
  Linux-only probe.

All three share one process-isolated code path: no PyPI wheel ships a
GPU-accelerated ggml backend, so each drives a system ``whisper-cli`` linked
against the system ``ggml``, which loads a GPU backend as a plugin (``ggml-cuda``
/ ``ggml-vulkan`` / ``ggml-hip`` on Linux; ``ggml-metal`` on macOS). The probe
checks for an accepted plugin and, on Linux, the vendor's GPU device — not just
an import. When the ggml model is absent locally, ``_resolve_ggml_model``
downloads it from Hugging Face on first use, verifies it against a pinned
SHA-256 digest when one is known
(:mod:`clear_record.providers.ggml_hashes`), and falls back to a clear pre-fetch
error when offline.
"""

from __future__ import annotations

import dataclasses
import glob
import itertools
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections.abc import Callable

from clear_record.core import (
    DECODER_KNOBS,
    DECODER_KNOB_FIELDS,
    Segment,
    TranscriptionResult,
)
from clear_record.core.i18n import deferred
from clear_record.core.message import Message
from clear_record.core.process import ProcessRunner, SubprocessRunner

from clear_record.providers.apple_speech import AppleSpeechBackend
from clear_record.providers.base import (
    APPLE_SPEECH_BACKEND_ID,
    Availability,
    Backend,
    BackendBase,
    BackendInfo,
)
from clear_record.providers.ggml_hashes import (
    ModelChecksumError,
    verify_model_sha256,
)
from clear_record.providers.paths import resolve_models_dir

#: The seam for this module's one-shot probes (``whisper-cli --version``).
_RUNNER = SubprocessRunner()


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


def _audio_duration(audio_path: str) -> float | None:
    try:
        import soundfile as sf

        data, sr = sf.read(audio_path, dtype="float32")
        return float(len(data) / sr)
    except Exception:  # non-fatal (e.g. ffmpeg-only format)
        return None


# --------------------------------------------------------------------------- #
# system ``whisper-cli`` adapter (Apple Metal / AMD / NVIDIA via ggml plugins)
# --------------------------------------------------------------------------- #
# No PyPI wheel ships a GPU-accelerated ggml backend, so Apple, AMD and NVIDIA
# shell out to the system's ``whisper-cli`` (Homebrew ``whisper-cpp`` on macOS;
# e.g. Arch ``whisper-cpp`` on Linux), which links the system ``ggml`` and loads
# a GPU backend plugin — ``ggml-metal`` for Apple, ``ggml-vulkan``/``ggml-hip``
# for AMD, ``ggml-cuda``/``ggml-vulkan`` for NVIDIA. A backend is only offered
# when an accepted plugin (and, on Linux, the vendor's GPU device) is present.
_WHISPER_CLI_CANDIDATES = ("whisper-cli", "whisper-cpp")
# Homebrew bin dirs as a fallback when Homebrew's prefix is absent from PATH
# (Apple Silicon first, then Intel). `CR_WHISPER_CLI` and PATH still win.
_WHISPER_CLI_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
# Arch/Fedora install plugin `.so`s directly under a `ggml` dir; Debian/Ubuntu
# put them under the multiarch tuple. macOS/Homebrew installs the ggml backend
# plugins under `libexec` (not `lib`), reachable via the stable `opt/` symlink
# or the versioned Cellar. Missing a layout here is a false negative (the probe
# rejects a machine that can actually run the backend), so list them all.
#
# The non-Arch layouts are pinned to the distro package contents rather than
# guessed; a miss here silently rejects a machine that can run the backend:
# - Arch `ggml-*`: /usr/lib/ggml/libggml-cuda.so
# - Ubuntu 25.10 `libggml-vulkan` (ggml ~0.0):
#     /usr/lib/x86_64-linux-gnu/ggml/libggml-vulkan.so
# - Ubuntu 26.04 `libggml0-backend-vulkan` (ggml >= 0.9) nests the plugin one
#   level deeper: /usr/lib/x86_64-linux-gnu/ggml/backends0/libggml-vulkan.so
# - Fedora `whisper-cpp` bundles ggml and drops it straight in lib64:
#     /usr/lib64/libggml-hip.so
# - Upstream `cmake --install` with the default prefix puts the plugins directly
#   in /usr/lib (ggml-org/whisper.cpp#3772).
# - Nixpkgs `whisper-cpp` sets GGML_BACKEND_DIR=$out/lib, so the plugins live in
#   the (hash-named) package output's lib/; some builds put them in bin/
#   (NixOS/nixpkgs#3420). The store hash varies, so those two entries glob.
#
# ggml >= 0.9 may nest the backends under a `backends<N>/` subdir of *any* of
# those dirs (the distro package uses `backends0`), so every base dir also gets
# a `backends*` glob variant.
_GGML_BACKEND_DIRS = (
    "/usr/lib/ggml",
    "/usr/lib/ggml/backends*",
    "/usr/lib64/ggml",
    "/usr/lib64/ggml/backends*",
    "/usr/local/lib/ggml",
    "/usr/local/lib/ggml/backends*",
    # Debian/Ubuntu multiarch: the plugin may sit directly in the tuple, under
    # `ggml/`, or (ggml >= 0.9) nested under a `backends*/` subdir.
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/usr/lib/x86_64-linux-gnu/ggml",
    "/usr/lib/aarch64-linux-gnu/ggml",
    "/usr/lib/x86_64-linux-gnu/ggml/backends*",
    "/usr/lib/aarch64-linux-gnu/ggml/backends*",
    "/usr/lib/x86_64-linux-gnu/backends*",
    "/usr/lib/aarch64-linux-gnu/backends*",
    # Direct lib/lib64 installs (upstream prefix=/usr; Fedora's bundled ggml).
    "/usr/lib",
    "/usr/lib/backends*",
    "/usr/lib64",
    "/usr/lib64/backends*",
    # Nix (hash-named store path, so glob it): package lib/, or bin/ on builds
    # that install the backends next to the CLI; either may nest `backends*/`.
    "/nix/store/*whisper-cpp*/lib",
    "/nix/store/*whisper-cpp*/lib/backends*",
    "/nix/store/*whisper-cpp*/bin",
    "/nix/store/*whisper-cpp*/bin/backends*",
    # macOS / Homebrew (Apple Silicon prefix, then Intel prefix).
    "/opt/homebrew/lib",
    "/opt/homebrew/libexec",
    "/opt/homebrew/lib/ggml",
    "/opt/homebrew/opt/ggml/lib",
    "/opt/homebrew/opt/ggml/libexec",
    "/opt/homebrew/Cellar/ggml/*/lib",
    "/opt/homebrew/Cellar/ggml/*/libexec",
    "/usr/local/lib",
    "/usr/local/libexec",
    "/usr/local/lib/ggml",
    "/usr/local/opt/ggml/lib",
    "/usr/local/opt/ggml/libexec",
    "/usr/local/Cellar/ggml/*/lib",
    "/usr/local/Cellar/ggml/*/libexec",
)
# One or more glob patterns per family. Metal is a dynamic backend plugin on
# macOS; Homebrew ships it as `libggml-metal.so` under `libexec`, while other
# builds may produce a `.dylib` or embed the `.metal` library.
_GGML_BACKEND_PATTERNS = {
    "vulkan": ("libggml-vulkan*.so*",),
    "hip": ("libggml-hip*.so*",),  # whisper.cpp's ROCm/HIP plugin
    "cuda": ("libggml-cuda*.so*",),
    "metal": (
        "libggml-metal*.so*",
        "libggml-metal*.dylib",
        "ggml-metal.metal",
    ),
}


def _find_whisper_cli() -> str | None:
    """Path to a system ``whisper-cli`` (``CR_WHISPER_CLI`` overrides)."""
    override = os.environ.get("CR_WHISPER_CLI")
    if override and os.path.isfile(override):
        return override
    for name in _WHISPER_CLI_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    for directory in _WHISPER_CLI_BIN_DIRS:
        for name in _WHISPER_CLI_CANDIDATES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def _ggml_backend_dirs() -> tuple[str, ...]:
    """Where to look for ggml backend plugins.

    ``CR_GGML_BACKEND_DIRS`` (``os.pathsep``-separated) is prepended, so a
    from-source ``whisper.cpp`` build — or a non-standard distro/brew layout —
    can point the probe at its own ``libggml-*.{so,dylib}`` directory.
    """
    extra = os.environ.get("CR_GGML_BACKEND_DIRS", "")
    return tuple(p for p in extra.split(os.pathsep) if p) + _GGML_BACKEND_DIRS


def _find_ggml_gpu_backend(families: tuple[str, ...]) -> str | None:
    """Path to a ggml GPU backend plugin for any accepted backend family."""
    for family in families:
        patterns = _GGML_BACKEND_PATTERNS.get(family)
        if not patterns:
            continue
        for pattern in patterns:
            for directory in _ggml_backend_dirs():
                matches = sorted(glob.glob(os.path.join(directory, pattern)))
                if matches:
                    return matches[0]
    return None


# Recognized GPU families for the opt-in plugin-load probe, mapped to the
# substrings the CLI / backend plugins print. The probe accepts the debug-build
# ``load_backend: loaded <name> backend`` line as well as a backend's own device
# banner (``ggml_vulkan:``, ``ggml_cuda``, ...), because release builds compile
# the load line out (``NDEBUG`` makes the loader silent). A line that names a
# family but only in a failure context (``error: failed to load vulkan backend``)
# is never read as loaded -- see ``_PLUGIN_LOAD_NEGATIVE``.
_GGML_FAMILY_MARKERS: dict[str, tuple[str, ...]] = {
    "vulkan": ("vulkan",),
    "cuda": ("cuda",),
    "hip": ("hip", "rocm"),
    "metal": ("metal",),
}

# Failure context for a CLI line that names a family. The release-banner fallback
# requires a *positive* line; this keeps a failed load from being reported as OK.
_PLUGIN_LOAD_NEGATIVE = (
    "failed",
    "fail to",
    "cannot",
    "can't",
    "could not",
    "unable to",
    "not loaded",
    "not supported",
    "error",
)


def _is_negative_load_line(line: str) -> bool:
    """True when ``line`` names a family only in a failure context."""
    if any(negative in line for negative in _PLUGIN_LOAD_NEGATIVE):
        return True
    # "no <family> backend" / "no <family> device" style.
    return "no " in line and "backend" in line


@dataclasses.dataclass(frozen=True)
class PluginLoadProbe:
    """Result of the opt-in plugin-load probe.

    ``loaded`` is ``True`` when the CLI reported a matching backend, ``False``
    when it reported a *different* backend, a matching one only in a failure
    context, or could not run at all, and ``None`` when the output carried no
    backend-load evidence at all -- a release build suppresses it, so the caller
    should treat ``None`` as inconclusive, never as a failure.
    """

    loaded: bool | None
    detail: str


# One-shot cache: a CLI invocation is one process, so caching per
# ``(cli, families)`` makes the probe once-per-run, never once-per-chunk.
_PLUGIN_LOAD_CACHE: dict[tuple[str, tuple[str, ...]], PluginLoadProbe] = {}
_PLUGIN_LOAD_CACHE_LOCK = threading.Lock()


def _clear_plugin_load_cache() -> None:
    """Drop cached probe results (tests only)."""
    with _PLUGIN_LOAD_CACHE_LOCK:
        _PLUGIN_LOAD_CACHE.clear()


def _classify_plugin_load(output: str, markers: tuple[str, ...]) -> PluginLoadProbe:
    """Interpret the CLI's combined output for the probe (see the class doc).

    A matching family is only accepted from a positive load/device line; a
    family that appears only in a failure context returns ``False``.
    """
    output = output.lower()
    if not output.strip():
        return PluginLoadProbe(None, "the CLI produced no output")
    load_lines = [
        line.strip() for line in output.splitlines() if "load_backend: loaded" in line
    ]
    if load_lines:
        for line in load_lines:
            if any(marker in line for marker in markers):
                return PluginLoadProbe(True, line)
        return PluginLoadProbe(False, f"a non-matching backend loaded: {load_lines[0]}")

    # Release builds suppress the ``load_backend`` line; a backend's own device
    # banner still names it. Require a positive line: a matching family that
    # appears only in a failure (e.g. "failed to load vulkan backend") is not
    # loaded. ``ggml_vulkan: No devices found.`` is still positive -- the plugin
    # loaded, and the vendor-device check lives in ``available()``.
    negative_hit: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or not any(marker in line for marker in markers):
            continue
        if _is_negative_load_line(line):
            negative_hit = negative_hit or line
            continue
        return PluginLoadProbe(True, f"backend banner: {line}")
    if negative_hit is not None:
        return PluginLoadProbe(
            False, f"matching family appeared only in a load failure: {negative_hit}"
        )
    return PluginLoadProbe(
        None,
        "the CLI ran but printed no backend-load line (release builds suppress it)",
    )


def probe_ggml_plugin_load(backend) -> PluginLoadProbe:
    """Opt-in: confirm ``backend``'s ggml plugin actually *loads*.

    ``available()`` only proves the plugin file is present -- an ABI/build
    mismatch still passes and the CLI silently falls back to CPU. This runs the
    system CLI once with no model and no network and inspects its output for the
    backend's load banner. The result is cached per ``(CLI, families)`` for the
    process, so it is a one-shot cost per CLI invocation, never per chunk. It
    never runs on the default ``available()`` path.
    """
    info = getattr(backend, "info", None)
    if info is not None and not getattr(info, "uses_ggml_plugin", True):
        # A system-native (or otherwise non-whisper-cli) backend has no ggml
        # plugin to load; the probe is not applicable rather than inconclusive.
        return PluginLoadProbe(
            None,
            f"{info.id!r} is a {info.runtime} backend; the ggml plugin probe "
            "does not apply",
        )
    families = tuple(getattr(backend, "gpu_backends", ()) or ())
    if not families:
        return PluginLoadProbe(None, "backend does not use ggml plugins")
    cli = _find_whisper_cli()
    if cli is None:
        return PluginLoadProbe(False, "whisper-cli not found on PATH")
    key = (cli, families)
    with _PLUGIN_LOAD_CACHE_LOCK:
        cached = _PLUGIN_LOAD_CACHE.get(key)
    if cached is not None:
        return cached
    markers = tuple(
        marker for family in families for marker in _GGML_FAMILY_MARKERS.get(family, ())
    )
    try:
        # ``--version`` still runs ``ggml_backend_load_all()`` (the first thing
        # ``main`` does) and then exits, so no model is touched.
        proc = _RUNNER.run(
            [cli, "--version"], capture_output=True, text=True, timeout=30
        )
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    except (OSError, subprocess.SubprocessError) as exc:
        result = PluginLoadProbe(False, f"could not run {cli!r}: {exc}")
    else:
        result = _classify_plugin_load(output, markers)
    with _PLUGIN_LOAD_CACHE_LOCK:
        _PLUGIN_LOAD_CACHE[key] = result
    return result


_AMD_VENDOR_ID = "0x1002"


def _has_amd_gpu() -> bool:
    """An AMD GPU is present (vendor id 0x1002 on a DRM card/render node)."""
    nodes = glob.glob("/sys/class/drm/renderD*/device/vendor")
    nodes += glob.glob("/sys/class/drm/card[0-9]*/device/vendor")
    for node in nodes:
        try:
            with open(node, encoding="ascii") as fh:
                if fh.read().strip().lower().startswith(_AMD_VENDOR_ID):
                    return True
        except OSError:
            continue
    return False


def _has_nvidia_device() -> bool:
    return (
        bool(glob.glob("/dev/nvidia[0-9]*"))  # native Linux proprietary driver
        or os.path.exists("/dev/dxg")  # WSL2 CUDA passthrough
        or shutil.which("nvidia-smi") is not None
    )


def _is_special_token(text: str) -> bool:
    return text.startswith("[") and text.endswith("]")


def _whispercli_segments(entries, source: str, language: str) -> tuple[Segment, ...]:
    """Normalize ``whisper-cli -ojf`` entries into core ``Segment`` objects.

    ``offsets`` are milliseconds; per-segment confidence is the mean token
    probability (control tokens such as ``[_BEG_]`` are excluded).

    The document structure is validated here so a malformed ``-ojf`` payload
    raises a clear ``RuntimeError`` naming the backend instead of leaking an
    ``AttributeError`` from an unexpected ``entry``/``token``/``text`` shape
    (``text`` must be a ``str`` or ``None``). A single segment with non-numeric
    timestamps is skipped (a per-segment glitch), but structurally wrong
    containers and field types are a hard error.
    """
    if not isinstance(entries, list):
        raise RuntimeError(
            f"whisper-cli ({source}) returned unexpected JSON: 'transcription' "
            f"must be a list, got {type(entries).__name__}."
        )
    out: list[Segment] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"whisper-cli ({source}) returned a non-object transcription "
                f"entry: {entry!r}."
            )
        offsets = entry.get("offsets") or {}
        if not isinstance(offsets, dict):
            raise RuntimeError(
                f"whisper-cli ({source}) returned non-object 'offsets': {offsets!r}."
            )
        try:
            start = float(offsets.get("from", 0.0)) / 1000.0
            end = float(offsets.get("to", 0.0)) / 1000.0
        except (TypeError, ValueError):
            continue
        tokens = entry.get("tokens") or []
        if not isinstance(tokens, list):
            raise RuntimeError(
                f"whisper-cli ({source}) returned non-list 'tokens': {tokens!r}."
            )
        probs: list[float] = []
        for token in tokens:
            if not isinstance(token, dict):
                raise RuntimeError(
                    f"whisper-cli ({source}) returned a non-object token: {token!r}."
                )
            if isinstance(token.get("p"), (int, float)) and not _is_special_token(
                str(token.get("text", ""))
            ):
                probs.append(float(token["p"]))
        confidence = sum(probs) / len(probs) if probs else None
        text = entry.get("text")
        if text is not None and not isinstance(text, str):
            raise RuntimeError(
                f"whisper-cli ({source}) returned non-string 'text': {text!r}."
            )
        segment = _make_segment(
            start,
            end,
            text,
            source=source,
            confidence=confidence,
            language=language,
        )
        if segment is not None:
            out.append(segment)
    return tuple(out)


# where first-use model downloads come from (Hugging Face's whisper.cpp repo).
# The endpoint honours ``HF_ENDPOINT`` -- the huggingface_hub/hf convention -- so
# restricted networks can use a reachable mirror (e.g. ``https://hf-mirror.com``)
# or a self-hosted, HF-compatible endpoint.
_DEFAULT_HF_ENDPOINT = "https://huggingface.co"
_GGML_MODEL_REPO = "ggerganov/whisper.cpp/resolve/main"
# Socket timeout per read/write: a stalled connection must not block a pool
# worker (or the single-threaded prefetch) indefinitely.
_GGML_DOWNLOAD_TIMEOUT_S = 60
# Serialize first-use downloads. The model can be requested by many pool
# workers at once (``AppleBackend`` is ``parallelizable``), and without this
# every worker would stream into the same temp and race ``os.replace``. One
# lock makes the check-and-download single-flight.
_DOWNLOAD_LOCK = threading.Lock()
# Per-call unique temp suffix; combined with the (cross-process) pid it makes
# concurrent callers and separate CLI invocations unable to share a temp file.
_PART_COUNTER = itertools.count()


def _ggml_model_url(name: str) -> str:
    """Return the download URL for the ggml model ``name``.

    The base is ``HF_ENDPOINT`` when set (trailing slashes stripped), else the
    public ``https://huggingface.co``; a mirror or self-hosted HF-compatible
    endpoint therefore works unchanged.
    """
    endpoint = os.environ.get("HF_ENDPOINT", _DEFAULT_HF_ENDPOINT).rstrip("/")
    return f"{endpoint}/{_GGML_MODEL_REPO}/{name}"


def _resolve_ggml_model(model: str, model_dir: str | None) -> str:
    """Resolve a model name/size (e.g. ``small``) to a local ``ggml-*.bin`` path.

    Accepts an explicit existing path, or a name resolved against the single
    models-directory resolver (``model_dir`` -> ``CR_MODELS_DIR`` ->
    ``<data>/models``; see :func:`clear_record.providers.paths.resolve_models_dir`). When
    the model is absent it is downloaded from Hugging Face on first use,
    streamed to a unique ``<name>.<pid>.<n>.part`` and atomically renamed on
    success so an interrupted download is never mistaken for a model. On a
    network failure the temp file is removed and the original clear pre-fetch
    error (with the ``hf download`` hint) is raised.

    A model already on disk is returned as-is: the check applies to what this
    process downloads, so the manual ``hf download`` / offline pre-fetch route
    keeps working and a multi-GB file is not re-hashed on every run.
    """
    expanded = os.path.expanduser(model)
    if os.path.isfile(expanded):
        return expanded
    base = resolve_models_dir(model_dir)
    name = os.path.basename(model)
    if not name.startswith("ggml-"):
        name = f"ggml-{name}"
    if not name.endswith(".bin"):
        name = f"{name}.bin"
    candidate = os.path.join(base, name)
    if os.path.isfile(candidate):
        return candidate
    return _download_ggml_model(model, name, base, candidate)


def download_ggml_model(model: str, model_dir: str | None = None) -> str:
    """Resolve a ggml checkpoint by name, downloading and verifying it on first use.

    A **backend-independent** entry point: ``model`` is always a ggml name/size
    (e.g. ``"medium"``), never a backend-specific language or asset request. The
    console's explicit model picker uses it to fetch exactly the checkpoint the
    user chose even when the resolved backend has no ggml checkpoint of its own
    (``apple-speech`` provisions a language asset, not a ``.bin``). The pinned,
    checksum-verified download is the same one the whisper-cli backends perform.
    """
    return _resolve_ggml_model(model, model_dir)


def _download_ggml_model(model: str, name: str, base: str, candidate: str) -> str:
    """Download ``ggml-<name>.bin`` to ``candidate`` (single-flight).

    A process-wide lock serializes the check-and-download, and each call streams
    to its own temp file, so concurrent workers can neither interleave writes nor
    lose the ``os.replace`` race. A model with a pinned digest is verified
    **before** the rename (see :mod:`clear_record.providers.ggml_hashes`), so a
    corrupt or substituted body is never installed as a model.

    Any failure -- including a failed replace -- removes the temp and raises the
    actionable ``FileNotFoundError``. A checksum mismatch is deliberately *not*
    folded into that error: nothing failed to download, the bytes were wrong, so
    its own clear error propagates (after the temp is removed). An interrupt
    still cleans the temp before propagating.
    """
    os.makedirs(base, exist_ok=True)
    url = _ggml_model_url(name)
    with _DOWNLOAD_LOCK:
        # Another caller may have finished the download while we waited.
        if os.path.isfile(candidate):
            return candidate
        part = f"{candidate}.{os.getpid()}.{next(_PART_COUNTER)}.part"
        verified = False
        try:
            with (
                urllib.request.urlopen(
                    url, timeout=_GGML_DOWNLOAD_TIMEOUT_S
                ) as response,
                open(part, "wb") as out,
            ):
                shutil.copyfileobj(response, out)
            # Inside the try, so a mismatch discards the staged bytes before the
            # rename -- fail closed, and never leave a `.part` behind.
            verified = verify_model_sha256(name, part)
            # Inside the try too: a failed rename surfaces as the clear error.
            os.replace(part, candidate)
        except ModelChecksumError:
            _remove_partial(part)  # wrong bytes: keep neither part nor model
            raise
        except (KeyboardInterrupt, SystemExit):
            _remove_partial(part)  # interrupted: leave no stray temp
            raise
        except Exception as exc:  # download failure -> actionable pre-fetch error
            _remove_partial(part)
            raise FileNotFoundError(
                f"ggml model not found for {model!r}; looked for {candidate} and "
                f"could not download {url} ({exc}). Download one manually, e.g. "
                f"`hf download ggerganov/whisper.cpp {name} --local-dir {base}`."
            ) from exc
        size = os.path.getsize(candidate)
    checked = ", sha256 verified" if verified else ""
    print(
        f"[clear_record.providers] downloaded {name} ({size} bytes{checked}) to "
        f"{candidate}",
        file=sys.stderr,
    )
    return candidate


def _remove_partial(part: str) -> None:
    try:
        os.remove(part)
    except OSError:
        pass


def _load_whispercli_json(path: str, backend_id: str, stdout: str) -> dict:
    """Read the ``-ojf`` output, turning every failure into a clear error.

    whisper.cpp's argument parser calls ``exit(0)`` on an unknown flag, so a
    ``whisper-cli`` without ``-ojf``/``--prompt`` "succeeds" while writing no
    file. Read defensively so a bare ``FileNotFoundError`` / ``JSONDecodeError``
    / ``UnicodeDecodeError`` never escapes; every failure names the backend and
    the cause.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        tail = (stdout or "").strip()[-800:]
        raise RuntimeError(
            f"whisper-cli ({backend_id}) produced no readable -ojf JSON at "
            f"{path!r}: {exc}. The installed whisper-cli may not support "
            f"-ojf/--prompt (usage: {tail!r})."
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"whisper-cli ({backend_id}) produced unparseable JSON at {path!r}: {exc}."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"whisper-cli ({backend_id}) returned unexpected JSON (expected an "
            f"object, got {type(data).__name__})."
        )
    return data


def _decoder_flags() -> dict[str, str]:
    """whisper-cli's long flag per decoder knob, from the run-knob declaration
    (``core.options.RUN_KNOBS``, which is where a flagless row would show up).

    Every decoder knob must name one: a knob this adapter cannot express would be
    silently dropped from the built command, which is the drift the declaration
    exists to prevent, so it is a programming error here rather than a user's
    quietly untuned decode.
    """
    flags: dict[str, str] = {}
    for knob in DECODER_KNOBS:
        if not knob.provider_flag:
            raise RuntimeError(
                f"decoder knob {knob.name!r} declares no whisper-cli flag"
            )
        flags[knob.name] = knob.provider_flag
    return flags


#: The flag per decoder knob, keyed by the option field name (the declaration's
#: decoder rows). A flag is added only when its knob is set, so an unset knob
#: leaves the built command exactly as it was before these knobs existed.
_DECODER_FLAGS: dict[str, str] = _decoder_flags()


class _WhisperCliBackend(BackendBase):
    """System ``whisper-cli`` backend, accelerated by a ggml backend plugin.

    Subclasses declare which ggml backend families they accept, how to probe the
    vendor's GPU device, and which OS they run on, so Apple, AMD and NVIDIA share
    one process-isolated code path (and the same parallel transcription).

    Residual risk in ``available()``: the probe proves that a ``whisper-cli``
    binary and a matching ``libggml-*`` *file* are present plus (on Linux) a
    vendor device, but not that this build can *load* the plugin (a ggml
    ABI/build mismatch still passes). Such a build warns on stderr and may
    silently fall back to CPU; the probe cannot tell without a model-dependent
    run, which would make ``available()`` expensive. The failure surfaces at
    ``transcribe()`` time, where a missing/invalid ``-ojf`` result now raises a
    clear ``RuntimeError`` instead of an unhelpful ``FileNotFoundError``.
    :func:`probe_ggml_plugin_load` is the documented, opt-in way to close that
    gap: it runs the CLI once and confirms the plugin actually loads.
    """

    def __init__(
        self,
        info: BackendInfo,
        *,
        gpu_backends: tuple[str, ...],
        device_check: Callable[[], bool],
        system: str = "Linux",
        runner: ProcessRunner | None = None,
    ) -> None:
        # The shared CLI adapter implements every decoder knob, so all three
        # families advertise the full set without repeating it per subclass.
        self.info = dataclasses.replace(info, decoder_knobs=DECODER_KNOB_FIELDS)
        self._gpu_backends = gpu_backends
        self._device_check = device_check
        self._system = system
        # Children launch through this runner; a pool injects a cancellable one
        # per ``transcribe`` call so cancellation stays scoped to that pool.
        self._runner: ProcessRunner = runner or SubprocessRunner()

    @property
    def gpu_backends(self) -> tuple[str, ...]:
        """The ggml backend families this adapter accepts (for the load probe)."""
        return self._gpu_backends

    def availability(self) -> Availability:
        """Cheap probe naming the first failing check, for ``clear-record backends``.

        Same verdict as the old boolean, with the reason attached: the platform,
        the CLI, the ggml plugin, or the vendor GPU device. ``available()`` is
        inherited from :class:`BackendBase` and derives from this.
        """
        system = platform.system()
        if system != self._system:
            return Availability(
                False,
                Message(
                    deferred("requires {target} (this is {host})"),
                    (("target", self._system), ("host", system)),
                ),
            )
        if _find_whisper_cli() is None:
            return Availability(
                False,
                Message(deferred("whisper-cli not found on PATH (set CR_WHISPER_CLI)")),
            )
        plugin = _find_ggml_gpu_backend(self._gpu_backends)
        if plugin is None:
            families = "/".join(self._gpu_backends)
            return Availability(
                False,
                Message(
                    deferred(
                        "no ggml {families} plugin found (see CR_GGML_BACKEND_DIRS)"
                    ),
                    (("families", families),),
                ),
            )
        if not self._device_check():
            return Availability(
                False, Message(deferred("no matching GPU device found"))
            )
        families = "/".join(self._gpu_backends)
        return Availability(
            True,
            Message(
                deferred("whisper-cli + ggml {families} plugin"),
                (("families", families),),
            ),
        )

    def prepare(self, model: str | None, model_dir: str | None) -> str:
        """Resolve (downloading on first use) this backend's ggml model path.

        The CLI calls this once, **single-threaded before the chunk pool**, so
        parallel workers only ever read a model that is already present;
        ``transcribe`` still resolves lazily as a defensive fallback.
        """
        name = model or self.info.default_model
        return _resolve_ggml_model(name, model_dir)

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        model: str | None = None,
        model_dir: str | None = None,
        initial_prompt: str | None = None,
        process_runner: ProcessRunner | None = None,
        **decoder_knobs: object,
    ) -> TranscriptionResult:
        """Transcribe one file with ``whisper-cli``.

        The decoder knobs arrive as keywords named by the run-knob declaration
        (``core.RUN_KNOBS``); taking them as a mapping rather than one parameter
        each is what keeps this adapter from restating the table, so a new knob
        is a row there and nothing here. A keyword that is not a declared decoder
        knob is a caller's error and is rejected, never ignored.
        """
        unknown = sorted(set(decoder_knobs) - set(DECODER_KNOB_FIELDS))
        if unknown:
            raise TypeError(
                "transcribe() got an unexpected keyword argument(s): "
                + ", ".join(repr(name) for name in unknown)
            )
        cli = _find_whisper_cli()
        if cli is None:  # defensive: available() already checked this
            raise RuntimeError("whisper-cli not found on PATH (set CR_WHISPER_CLI).")
        name = model or self.info.default_model
        model_path = self.prepare(name, model_dir)
        lang = language if language and language not in ("", "auto") else "auto"
        runner = process_runner or self._runner

        with tempfile.TemporaryDirectory(prefix="cr-whisper-") as tmp:
            out_prefix = os.path.join(tmp, "out")
            cmd = [
                cli,
                "-m",
                model_path,
                "-f",
                audio_path,
                "-l",
                lang,
                "-ojf",
                "-of",
                out_prefix,
            ]
            # One flag per set knob, straight from the declaration: an unset knob
            # (absent, or None) adds nothing, so the built command is unchanged
            # for a caller that asks for no tuning.
            for knob_name, flag in _DECODER_FLAGS.items():
                value = decoder_knobs.get(knob_name)
                if value is not None:
                    cmd += [flag, str(value)]
            if initial_prompt:
                cmd += ["--prompt", initial_prompt]
            proc = runner.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip()[-800:]
                raise RuntimeError(
                    f"whisper-cli ({self.info.id}) failed (exit "
                    f"{proc.returncode}): {tail}"
                )
            data = _load_whispercli_json(
                out_prefix + ".json", self.info.id, proc.stdout or ""
            )

        result = data.get("result")
        if result is not None and not isinstance(result, dict):
            raise RuntimeError(
                f"whisper-cli ({self.info.id}) returned non-object 'result': "
                f"{result!r}."
            )
        detected = (result or {}).get("language") or ("" if lang == "auto" else lang)
        duration = _audio_duration(audio_path)
        segments = _whispercli_segments(
            data.get("transcription", []), source=self.info.id, language=detected
        )
        return TranscriptionResult(
            source=self.info.id,
            segments=segments,
            language=detected,
            backend=self.info.id,
            model=name,
            audio_duration=duration,
        )


def _has_metal_device() -> bool:
    """Metal is present on every macOS host (Apple Silicon and recent Intel).

    There is no DRM/``nvidia-smi`` equivalent to probe: the Metal plugin file
    plus the ``whisper-cli`` binary is the capability check, so this is a
    constant-true device predicate (see :class:`_WhisperCliBackend`).
    """
    return True


class AppleBackend(_WhisperCliBackend):
    """Apple/macOS: Metal via the system ``whisper-cli``.

    Drives the same process-isolated path as AMD and NVIDIA — a Homebrew
    ``whisper-cpp`` links the system ``ggml`` and loads the ``libggml-metal``
    plugin (ADR-0005). It does **not** select Core ML or ANE (those remain
    future ecosystem possibilities, not implemented capabilities). There is no
    in-process wheel fallback; the ggml model is downloaded on first use by
    :func:`_resolve_ggml_model`.
    """

    def __init__(self) -> None:
        super().__init__(
            BackendInfo(
                id="apple",
                vendor="Apple",
                frameworks=("Metal",),
                description="macOS ASR via the system whisper-cli + ggml-metal.",
                parallelizable=True,
            ),
            gpu_backends=("metal",),
            device_check=_has_metal_device,
            system="Darwin",
        )


class AmdBackend(_WhisperCliBackend):
    """AMD Radeon on Linux: Vulkan / ROCm via the system ``whisper-cli``."""

    def __init__(self) -> None:
        super().__init__(
            BackendInfo(
                id="amd",
                vendor="AMD",
                frameworks=("ROCm", "Vulkan"),
                description="AMD Radeon ASR via the system whisper-cli "
                "(ggml Vulkan/HIP backend, e.g. gfx1100).",
                parallelizable=True,
            ),
            gpu_backends=("vulkan", "hip"),
            device_check=_has_amd_gpu,
        )


class NvidiaBackend(_WhisperCliBackend):
    """NVIDIA on Linux: CUDA / Vulkan via the system ``whisper-cli``."""

    def __init__(self) -> None:
        super().__init__(
            BackendInfo(
                id="nvidia",
                vendor="NVIDIA",
                frameworks=("CUDA", "Vulkan"),
                description="NVIDIA ASR via the system whisper-cli "
                "(ggml CUDA/Vulkan backend).",
                parallelizable=True,
            ),
            gpu_backends=("cuda", "vulkan"),
            device_check=_has_nvidia_device,
        )


# The advertised catalog in stable order (backends may be unavailable at runtime).
# ``apple-speech`` is appended after the shipped trio so ``next(iter(BACKENDS))``
# (the CLI's default ``--backend``) stays the portable ``apple`` adapter: on a
# non-Mac the native backend is unavailable, and a positional default must not be
# one that cannot run. Native-first applies to ``--backend auto`` via
# ``cli.auto.BACKEND_PREFERENCE``, which is the separate capability knob (ADR-0005
# 2026-09-14 Update, ADR-0019).
BACKENDS: dict[str, Backend] = {
    "apple": AppleBackend(),
    "nvidia": NvidiaBackend(),
    "amd": AmdBackend(),
    APPLE_SPEECH_BACKEND_ID: AppleSpeechBackend(),
}


def available_backend_ids() -> tuple[str, ...]:
    """Backend ids whose runtime probe currently succeeds, in catalog order."""
    return tuple(bid for bid, backend in BACKENDS.items() if backend.available())


def backend_availability() -> dict[str, Availability]:
    """Each catalog backend's probe verdict with its reason, in catalog order.

    The ``clear-record backends`` command reads this so an unavailable backend
    reports *why* (OS version, capability, asset), not only that it is absent.
    """
    return {bid: backend.availability() for bid, backend in BACKENDS.items()}


def get_backend(backend_id: str) -> Backend:
    try:
        return BACKENDS[backend_id]
    except KeyError as exc:  # pragma: no cover - trivial guard
        raise KeyError(f"unknown ASR backend {backend_id!r}") from exc


__all__ = [
    "BACKENDS",
    "PluginLoadProbe",
    "available_backend_ids",
    "backend_availability",
    "download_ggml_model",
    "get_backend",
    "probe_ggml_plugin_load",
]
