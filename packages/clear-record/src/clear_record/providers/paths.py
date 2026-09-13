"""One resolver for app-owned deployment directories (ADR-0007).

The models-directory precedence lives here, in a single place, so the CLI's
``--models-dir`` default and the backend's first-use ggml download agree:
an explicit CLI flag (``models_dir``) wins, then the ``CR_MODELS_DIR``
environment variable, then ``<cwd>/models``. ADR-0007 extends this seam to the
config file and the XDG directories; new precedence belongs here, never
re-derived at a call site.
"""

from __future__ import annotations

import os

ENV_MODELS_DIR = "CR_MODELS_DIR"
DEFAULT_MODELS_DIRNAME = "models"


def resolve_models_dir(models_dir: str | None = None) -> str:
    """Resolve the models directory: flag -> ``CR_MODELS_DIR`` -> ``<cwd>/models``."""
    if models_dir:
        return models_dir
    env = os.environ.get(ENV_MODELS_DIR)
    if env:
        return env
    return os.path.join(os.getcwd(), DEFAULT_MODELS_DIRNAME)


__all__ = ["DEFAULT_MODELS_DIRNAME", "ENV_MODELS_DIR", "resolve_models_dir"]
