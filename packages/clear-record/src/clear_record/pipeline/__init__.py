"""The stage wiring and the machinery that runs it.

:mod:`clear_record.core.pipeline` owns the pipeline's **declaration** — ``Step``,
``PipelineStage`` and ``PipelineSpec``, the one source of stage truth the CLI's
subcommands and the ``run`` dispatch are built from. This package owns what
*executing* those stages does. The command surface sits above it and drives it;
nothing here imports back up into the command-surface package.

- :mod:`~clear_record.pipeline.stages` — the stage implementations and the
  ``run`` they compose (the module's own header is the list);
- :mod:`~clear_record.pipeline.transcription` — the resumable chunked
  transcriber: chunk planning, the chunk cache key and its invalidation, the
  bounded worker pool, and the overlap-aware merge;
- :mod:`~clear_record.pipeline.workspace` — the workspace layout and the
  read/writes over it;
- :mod:`~clear_record.pipeline.auto` — the capability resolvers behind
  ``--auto`` and ``--backend auto``;
- :mod:`~clear_record.pipeline.eval` — the decoder-knob evaluation ``calibrate``
  reports;
- :mod:`~clear_record.pipeline.tts` — the text-to-speech provider seam: the
  engines, the clip writer and the two error classes, re-exported because
  ``service`` may not import ``providers``. ``service.hello_tape`` writes the
  hello-world tape with ``Synthesis`` and ``synthesize_clip``; the acceptance
  check in ``service.agent_flow`` catches the errors.

Declaration and execution live apart because they belong to different layers:
the declaration is ``core`` — dependency-free, and the layer both the CLI's
subcommand surface and ``run``'s dispatch derive from — while executing a stage
needs a workspace, a provider and an event sink. Its consumers reach it as a
layer, not through the command surface: ``clear_record.service`` drives
``run``/``transcribe`` here directly, and the CLI's own commands are the thin
per-command wrappers over these stages — all but ``run``, which a command-line
run hands to the **node** instead (ADR-0032): ``cli/runs.py`` submits the run and
follows it, and the node is what executes these stages.
"""
