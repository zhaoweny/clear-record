"""The console's use of the CLI's explainable ``--auto`` resolvers.

The resolvers live in ``clear_record.cli.auto`` (``--auto`` picks a profile +
model; ``--backend auto`` picks the best available backend). The **web** layer
sits above ``service`` in the layering DAG and may not import ``cli``, so this
module is the one place the console reaches them: ``service`` may import ``cli``
(it already drives the CLI's stage wiring), and the resolver is **re-exported**,
not reimplemented. The explanation the console shows is therefore byte-for-byte
the CLI's — the two surfaces cannot drift.

Nothing here runs on the default path: :func:`resolve_run` returns the plain
:func:`clear_record.core.resolve_options` result unless the caller explicitly
asks for ``--auto`` / ``--backend auto``.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from clear_record.cli import auto as _auto
from clear_record.core import PipelineOptions, resolve_options

#: The ``--backend`` sentinel ``--backend auto`` accepts (re-exported so ``web``
#: can name it without importing ``cli`` or ``providers``).
BACKEND_AUTO = _auto.BACKEND_AUTO

#: The re-exported resolver values and types. ``web`` names only what it needs;
#: ``available_backend_ids`` rides along from ``cli.auto``'s own namespace
#: (``cli.auto`` legitimately imports ``providers``), which is how ``service``
#: reaches the one availability probe without importing ``providers`` itself.
AutoChoice = _auto.AutoChoice
AutoProbe = _auto.AutoProbe
BackendChoice = _auto.BackendChoice
NoBackendAvailable = _auto.NoBackendAvailable
available_backend_ids = _auto.available_backend_ids
probe_auto = _auto.probe_auto
resolve_auto = _auto.resolve_auto
resolve_backend = _auto.resolve_backend


class ModelNotOnDisk(RuntimeError):
    """``--auto`` chose a model that is not on disk, and it never downloads one.

    The structured choice rides on the exception so a caller can render the
    CLI's no-download message with the facts it names.
    """

    def __init__(self, choice: AutoChoice, models_dir: str | None) -> None:
        self.choice = choice
        self.model = choice.model
        self.models_dir = models_dir
        super().__init__(
            f"the recommended model {choice.model!r} is not in the models "
            "directory, and --auto never downloads one"
        )


@dataclasses.dataclass(frozen=True)
class AutoRun:
    """A resolved run, the auto/backend-auto meta to record, and what to show.

    ``meta`` is merged into the run's recorded meta by
    :meth:`clear_record.service.RunManager.start`; ``explanations`` is what the
    console shows (the CLI's own words, in the CLI's own order).
    """

    options: PipelineOptions
    meta: dict
    explanations: tuple[str, ...] = ()


def resolve_run(
    options: PipelineOptions,
    *,
    profile: str | None = None,
    auto: bool = False,
    directory: str | Path | None = None,
    model_dir: str | None = None,
) -> AutoRun:
    """Resolve a console run's options the way the CLI's ``_apply_auto`` does.

    Mirrors the CLI precedence exactly, strongest first: an explicit ``--backend
    auto`` is replaced by the first available backend; ``--auto`` then fills a
    profile, model and per-speaker attribution only where the user left them
    unset; and the explicit > ``CR_*`` environment > profile > built-in-default
    layers apply last through the shared :func:`clear_record.core.resolve_options`.

    ``profile`` is ``None`` when the console's picker means "no preset" (the
    console has no separate unset state), so ``--auto`` may choose the profile —
    exactly as an unset ``--profile`` does in the CLI.

    ``--auto``'s worker recommendation is *not* written to the options: the CLI
    does not apply it either (it lives in the explanation), and the console must
    not resolve a run differently from the CLI.

    Raises :class:`NoBackendAvailable` when ``--backend auto`` finds nothing and
    :class:`ModelNotOnDisk` when ``--auto`` recommends a model the machine does
    not have.
    """
    explanations: list[str] = []
    backend_meta: dict | None = None

    if options.backend == BACKEND_AUTO:
        backend_choice = resolve_backend(available_backend_ids())
        explanations.append(backend_choice.explanation)
        backend_meta = {
            "backend": backend_choice.backend,
            "explanation": backend_choice.explanation,
        }
        options = dataclasses.replace(options, backend=backend_choice.backend)

    def _with_backend(meta: dict) -> dict:
        return {**meta, **({"backend_auto": backend_meta} if backend_meta else {})}

    if not auto:
        return AutoRun(
            resolve_options(options, profile=profile),
            _with_backend({}),
            tuple(explanations),
        )

    if directory is None:
        raise ValueError(
            "a run needs a workspace directory before --auto can probe the tape"
        )

    choice = resolve_auto(
        probe_auto(directory, model_dir=model_dir, language=options.language)
    )
    explanations.append(choice.explanation)

    chose: list[str] = []
    if options.model is None:
        if not choice.model_on_disk:
            raise ModelNotOnDisk(choice, model_dir)
        options = dataclasses.replace(options, model=choice.model)
        chose.append("model")
    if choice.diarize and options.do_diarize is None:
        options = dataclasses.replace(options, do_diarize=True)
        chose.append("diarize")
    if profile is None:
        chose.append("profile")

    resolved = resolve_options(
        options, profile=profile if profile is not None else choice.profile
    )
    auto_meta = {"auto": {"explanation": choice.explanation, "chose": sorted(chose)}}
    return AutoRun(resolved, _with_backend(auto_meta), tuple(explanations))


__all__ = [
    "AutoChoice",
    "AutoProbe",
    "AutoRun",
    "BACKEND_AUTO",
    "BackendChoice",
    "ModelNotOnDisk",
    "NoBackendAvailable",
    "available_backend_ids",
    "probe_auto",
    "resolve_auto",
    "resolve_backend",
    "resolve_run",
]
