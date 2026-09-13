# cr-cli

The **CLI implementation package** for clear-record: the `cr_cli` module tree
(`cli.py`, `stages.py`) that does the lifting. The public `clear-record`
command is provided by the
[`clear-record`](https://github.com/zhaoweny/clear-record/tree/main/packages/clear-record)
facade package, which re-exports `cr_cli.cli.main`.

`cr-cli` declares **no console script of its own**, so `uvx cr-cli` provides no
command (that is intended); the public install name is `clear-record`:

```sh
uvx clear-record --help
```

The command surface is one subcommand per pipeline stage, mirroring the
original concept's intended surface:

```sh
clear-record ingest       # pull in several audio sources + their metadata
clear-record align        # align sources onto a common clock/timebase
clear-record transcribe   # run a chosen ASR backend (apple / nvidia / amd)
clear-record reconcile    # merge segments into an attributable record
clear-record export       # write a searchable/archiveable artifact
clear-record backends     # list which ASR backends are currently available
```

The subcommands are declared from the pipeline spec and execute the real
pipeline stages against a workspace directory (see `docs/architecture.md` §8).
Consumers can also import `cr_cli` directly; the facade is only the published
entry point.
