"""The pipeline's **execution**: the stages and the machinery that runs them.

:mod:`clear_record.core.pipeline` owns the pipeline's **declaration** — ``Step``,
``PipelineStage`` and ``PipelineSpec``, the one source of stage truth the CLI's
subcommands and the ``run`` dispatch are built from. This package owns what
*executing* those stages does. The command surface sits above it and drives it;
nothing here imports back up into the command-surface package.

- :mod:`~clear_record.pipeline.stages` — the stage implementations (ingest,
  align, transcribe, reconcile, export and the ``run`` they compose);
- :mod:`~clear_record.pipeline.transcription` — the resumable chunked
  transcriber: chunk planning, the chunk cache key and its invalidation, the
  bounded worker pool, and the overlap-aware merge;
- :mod:`~clear_record.pipeline.workspace` — the workspace layout and the
  read/writes over it;
- :mod:`~clear_record.pipeline.auto` — the capability resolvers behind
  ``--auto`` and ``--backend auto``;
- :mod:`~clear_record.pipeline.eval` — the decoder-knob evaluation ``calibrate``
  reports.

Declaration and execution live apart because they belong to different layers:
the declaration is ``core`` — dependency-free, and the layer both the CLI's
subcommand surface and ``run``'s dispatch derive from — while executing a stage
needs a workspace, a provider and an event sink. Its consumers reach it as a
layer, not through the command surface: ``clear_record.service`` drives
``run``/``transcribe`` here directly, and the CLI's own commands are the thin
per-command wrappers over these stages.
"""
