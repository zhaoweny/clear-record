# clear-record

**Local-first multitrack transcription and record reconstruction:** ingest several
recordings of one event, align them onto a common timebase, transcribe them
locally, and reconcile **one clear, attributed record** — *from many recordings to
one clear record*.

Entirely offline after a one-time model download: no cloud, no telemetry, no
account.

```sh
# from PyPI
uvx clear-record --help           # run it without installing
uv tool install clear-record      # or: pipx install clear-record

# from a source checkout
uv sync --all-packages --extra apple
uv run --all-packages --extra apple clear-record --help
```

## The pipeline

One subcommand per stage; `run` chains them:

```sh
clear-record ingest       # discover/declare the audio sources
clear-record align        # place every source on a common clock (cross-correlation)
clear-record transcribe   # run a local ASR backend (apple / nvidia / amd / apple-speech)
clear-record reconcile    # merge into one attributed, aligned timeline
clear-record export       # Markdown / SRT / VTT / JSON
clear-record run          # ingest -> align -> transcribe -> reconcile -> export
clear-record calibrate    # run it and report transcript quality against a reference
```

## Backends

The `apple` / `nvidia` / `amd` backends drive the **system `whisper-cli`** plus
a ggml plugin; on macOS 26+ the native **`apple-speech`** backend
(`SpeechAnalyzer`/`SpeechTranscriber`, ADR-0019) needs neither and is preferred
by `--backend auto`. The default install depends only on `numpy` and `soundfile`
and never pulls a GPU framework; the `apple` / `nvidia` / `amd` / `apple-speech`
extras are **no-op markers** (ADR-0005). `clear-record backends` reports what the
current machine can actually run.

## How it is packaged

`clear-record` is a **single published distribution**: one import package,
`clear_record`, with four internal layers as subpackages —

| Layer | Contents |
|---|---|
| `clear_record.core` | backend-agnostic domain model (no third-party deps) |
| `clear_record.engine` | audio I/O, cross-correlation alignment, reconcile (numpy + soundfile) |
| `clear_record.providers` | per-vendor ASR backend adapters (apple / nvidia / amd / apple-speech) |
| `clear_record.cli` | the `clear-record` command implementation |

The layers are **not** separate distributions — the subpackages hide them behind
one install name while the import layering (and the vendor-free core) stays
enforced by a test (ADR-0012, ADR-0004). The console script targets
`clear_record.cli:main` directly, so `import clear_record` stays light.

**Code** is [MIT](https://github.com/zhaoweny/clear-record/blob/main/LICENSE); the
license boundary — including how copyleft is consumed over process/network
boundaries — is
[ADR-0003](https://github.com/zhaoweny/clear-record/blob/main/docs/adr/0003-license-boundary.md).
See the [repository README](https://github.com/zhaoweny/clear-record#readme) for
the full story.
