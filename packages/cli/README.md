# cr-cli

The `clear-record` command. One subcommand per pipeline stage, mirroring the
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
