# clear-record

The **public install name** for the `clear-record` command:

```sh
uvx clear-record --help          # run it without installing
uv tool install clear-record     # or: pipx install clear-record
```

These commands need the distribution from PyPI; until it is published, run from
a source checkout (see the
[repo README](https://github.com/zhaoweny/clear-record#readme)).

The implementation lives in
[`cr-cli`](https://github.com/zhaoweny/clear-record/tree/main/packages/cli),
which holds the
`cr_cli` module tree and **declares no console script of its own**. This
distribution is a **facade**: it ships a small `clear_record` module whose
`clear_record.main` re-exports `cr_cli.cli.main`, and it owns the
`clear-record` console script. Installing it pulls in `cr-cli==X.Y.Z`, so
`uvx clear-record` resolves and runs cleanly, without the
dependency-provided-command warning (ADR-0009). `clear-record` is the sole owner
of the `clear-record` command.
