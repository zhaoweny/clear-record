# cr-cli

The `clearrecord` command. One subcommand per pipeline stage, mirroring the
original concept's intended surface:

```sh
clearrecord ingest       # pull in several audio sources + their metadata
clearrecord align        # align sources onto a common clock/timebase
clearrecord transcribe   # run a chosen ASR backend (apple / nvidia / amd)
clearrecord reconcile    # merge segments into an attributable record
clearrecord export       # write a searchable/archiveable artifact
clearrecord backends     # list which ASR backends are currently available
```

The subcommands are declared from the pipeline spec and execute the real
pipeline stages against a workspace directory (see `docs/architecture.md` §8).
