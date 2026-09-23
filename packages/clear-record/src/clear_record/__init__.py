"""clear-record: a local-first multitrack transcription and record-reconstruction tool.

The published distribution is a single package with nine internal layers:

- :mod:`clear_record.core` — backend-agnostic domain model (no third-party deps);
- :mod:`clear_record.engine` — audio I/O, alignment, reconcile (numpy/soundfile);
- :mod:`clear_record.providers` — per-vendor ASR backend adapters;
- :mod:`clear_record.pipeline` — the stage wiring and the machinery that runs it;
- :mod:`clear_record.cli` — the ``clear-record`` command implementation;
- :mod:`clear_record.service` — the console's registry, meetings, runs, archive;
- :mod:`clear_record.web` — the console's browser surface (FastAPI + htmx/Alpine);
- :mod:`clear_record.tray` — the native desktop supervisor;
- :mod:`clear_record.mcp` — the agent boundary.

It stays light (no CLI, no audio stack): importing ``clear_record`` only
installs the platform-native directories (ADR-0025). ``clear_record.core`` may
import no third-party package, so :mod:`clear_record._native_paths` — which
imports ``platformdirs`` — resolves the defaults and hands them to
:mod:`clear_record.core.paths`. The console script targets
:func:`clear_record.cli.cli.main` directly.
"""

from clear_record._native_paths import install as _install_native_paths

_install_native_paths()

__all__: list[str] = []
