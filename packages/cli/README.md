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

Currently a skeletal scaffold: subcommands are declared and wired to the
pipeline spec, but the real pipeline execution is not implemented yet (see
`docs/architecture.md` §8 for what's next).
