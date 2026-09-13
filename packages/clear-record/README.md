# clear-record

The **single published distribution** for clear-record:
[`clear-record`](https://pypi.org/project/clear-record/) ships one import package,
`clear_record`, that contains four internal layers as subpackages:

| Layer | Contents |
|---|---|
| `clear_record.core` | backend-agnostic domain model (no third-party deps) |
| `clear_record.engine` | audio I/O, cross-correlation alignment, reconcile (numpy + soundfile) |
| `clear_record.providers` | per-vendor ASR backend adapters (apple / nvidia / amd) |
| `clear_record.cli` | the `clear-record` command implementation |

The layers are **not** separate distributions; `clear_record` subpackages hide
them behind one install name while keeping the import layering (ADR-0012,
ADR-0004). The console script targets
`clear_record.cli:main` directly, so `import clear_record` stays light.

```sh
uvx clear-record --help          # run it without installing
uv tool install clear-record     # or: pipx install clear-record
```

These commands need the distribution from PyPI; until it is published, run from
a source checkout (see the
[repo README](https://github.com/zhaoweny/clear-record#readme)):

```sh
uv sync --all-packages --extra apple
uv run --all-packages --extra apple clear-record --help
```

The default install depends only on `numpy` and `soundfile`. The
`apple`/`nvidia`/`amd` extras are **no-op markers**: every backend drives the
system `whisper-cli` + a ggml plugin (ADR-0005).

**Code** is [MIT](LICENSE); the license boundary (including how copyleft is
consumed over process/network boundaries) is
[ADR-0003](https://github.com/zhaoweny/clear-record/blob/main/docs/adr/0003-license-boundary.md).
