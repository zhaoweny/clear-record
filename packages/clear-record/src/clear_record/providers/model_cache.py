"""The recognizer for a legacy on-disk cache of downloaded model weights.

The core owns the app-owned directories and adopts a pre-move source checkout's
``<cwd>/models`` in place rather than orphaning its weights (ADR-0025). ``<cwd>``
is wherever the user happened to run the command, so a directory merely *named*
``models`` proves nothing: adoption needs a **positive** identification of the
contents. The names that identify them are the artifact format's own, and the
format belongs to a vendor — knowledge :mod:`clear_record.core` cannot carry,
because ``core`` is vendor-free (ADR-0012, ``tests/test_layering.py``).

So the recognition lives here, beside the format it recognizes, and this layer
installs it into the core's adoption rule when the package is imported
(:func:`clear_record.core.paths.install_models_recognizer`, called from
:mod:`clear_record.providers`). The rule consumes the recognizer and spells no
artifact name itself; ``providers`` may import ``core``, never the reverse.
"""

from __future__ import annotations

from pathlib import Path

#: Every artifact ADR-0005's auto-download writes into a models directory — the
#: naming :mod:`clear_record.providers.backends` downloads under and
#: :mod:`clear_record.providers.ggml_hashes` pins digests for.
MODEL_ARTIFACT_GLOB = "ggml-*.bin"


def looks_like_model_cache(directory: Path) -> bool:
    """True when *directory* holds at least one completed model download.

    ADR-0005's auto-download is the app's only writer of a models directory, and
    it names every weight ``ggml-<size>.bin``. Requiring one makes the legacy
    ``<cwd>/models`` adoption *positive*: a directory that merely shares the name
    (a source tree's, a user's notes) is left alone. A stray ``.part`` does not
    count, because only a completed download looks like a model to the resolver.
    An unreadable directory is not a cache either rather than an error — the
    candidate is a guess about a path the user never named.
    """
    try:
        return any(path.is_file() for path in directory.glob(MODEL_ARTIFACT_GLOB))
    except OSError:  # pragma: no cover - an unreadable directory is not a cache
        return False


__all__ = ["MODEL_ARTIFACT_GLOB", "looks_like_model_cache"]
