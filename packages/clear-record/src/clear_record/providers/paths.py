"""The provider-facing name for the one models-directory resolver (ADR-0007/0025).

The precedence lives in :func:`clear_record.core.paths.resolve_models_dir`:
an explicit CLI flag (``models_dir``) wins, then ``CR_MODELS_DIR``, then the
config file's ``models_dir``, then ``<data>/models``. This module keeps the
``clear_record.providers.paths`` import path stable so the CLI's ``--models-dir``
default and the backend's first-use ggml download cannot disagree.

``providers`` may import ``core``; the platformdirs call itself stays one layer
up in :mod:`clear_record._native_paths`, because ``core`` may import no
third-party package.
"""

from __future__ import annotations

from clear_record.core.paths import ENV_MODELS_DIR
from clear_record.core.paths import MODELS_DIRNAME as DEFAULT_MODELS_DIRNAME
from clear_record.core.paths import resolve_models_dir as _resolve_models_dir


def resolve_models_dir(models_dir: str | None = None) -> str:
    """Resolve the models directory (see ``core.paths.resolve_models_dir``)."""
    return str(_resolve_models_dir(models_dir))


__all__ = ["DEFAULT_MODELS_DIRNAME", "ENV_MODELS_DIR", "resolve_models_dir"]
