"""clear-record: a local-first multitrack transcription and record-reconstruction tool.

The published distribution is a single package with four internal layers:

- :mod:`clear_record.core` — backend-agnostic domain model (no third-party deps);
- :mod:`clear_record.engine` — audio I/O, alignment, reconcile (numpy/soundfile);
- :mod:`clear_record.providers` — per-vendor ASR backend adapters;
- :mod:`clear_record.cli` — the ``clear-record`` command implementation.

This module is deliberately light (a docstring only) so that importing
``clear_record`` does not pull in the heavy CLI or audio stack. The console
script targets :func:`clear_record.cli.main` directly.
"""

__all__: list[str] = []
