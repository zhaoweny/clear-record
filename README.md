# clear-record

> **From many recordings to one clear record.**
>
> A local-first, open-source multitrack **transcription and
> record-reconstruction** pipeline: ingest several audio sources, align them onto
> a common clock, transcribe the result with a local model, reconcile it into an
> attributable record, and export a searchable/archiveable artifact. It is a
> post-processing tool — the pipeline starts at `ingest` and does not capture —
> and processing runs offline once models are provisioned: no cloud processing,
> no subscription.

## Provenance note

This is the author's own idea, implemented independently from a public problem
statement, using personally owned hardware and publicly available documentation,
models and libraries. It is a personal open-source release; the history of how
the idea evolved lives in
[ADR-0001](docs/adr/0001-project-identity-and-provenance.md) and
[`docs/architecture.md`](docs/architecture.md) §9, and isn't the point of this
project. This note records *facts* (that the work was done independently) and is
not a legal opinion; see [`NOTICE.md`](NOTICE.md) for authorship and AI-agent
use.

## What it does

`clear-record` turns several imperfect recordings of the same event into one
reconstructed, attributable record:

```text
MacBook mic ─┐
H1n         ─┼─► ingest ─► align ─► transcribe ─► reconcile ─► export ─► one record
DJI Mic 3   ─┘
```

- **ingest** — decode/normalize every source to 16 kHz mono WAV (handles
  wav/flac/ogg and, via ffmpeg, mp3/m4a/etc.). **Multi-channel files are split
  per channel by default** (e.g. a 4-channel DJI file → 4 sources), so
  per-speaker isolation is preserved; `--mix-down` forces a downmix.
- **align** — place every source onto a common timebase (windowed
  cross-correlation; *approximate*, not precision clock-sync).
- **transcribe** — run a chosen local ASR backend (see *Backends*), with
  auto language detection and per-segment confidence.
- **reconcile** — merge segments into one attributed timeline
  (`speaker ≈ source`, since each channel is its own recording).
- **export** — write Markdown / SRT / VTT / JSON with `coverage`, `wer` and
  `similarity` metrics (`calibrate`) so you can measure model quality.

The pipeline's core idea is **observation-first**: store timestamped,
source-attributed observations first, and reconstruct transcript, speakers and
meaning afterward. That keeps ingestion cheap and reliability a property of
*storage*, not of a fragile online model (`docs/architecture.md` §4).

## Install

**Today the source checkout is the way** — there is no PyPI release yet:

```sh
git clone https://github.com/zhaoweny/clear-record.git
cd clear-record
uv sync --all-packages --extra apple     # or --extra nvidia / --extra amd
uv run --all-packages --extra apple clear-record --help
```

The release workflow ([`docs/releasing.md`](docs/releasing.md)) builds and
publishes wheels and sdists for all five workspace members. The public install
name is **`clear-record`**, a facade dist over the `cr-cli` implementation
package (which provides no command of its own); the facade forwards through a
small `clear_record` module — see ADR-0009. Once the first release is out:

```sh
uvx clear-record --help              # run without installing
uv tool install clear-record         # or: pipx install clear-record
clear-record --help
```

Until the first release is on PyPI these commands will not resolve, so use the
source checkout above in the meantime.

## Backends (Apple · NVIDIA · AMD)

Inspired by the observation (Marco Arment / Overcast) that Mac frameworks are
excellent for on-device transcription, the transcription step supports all
three major desktop compute families behind one interface:

| id | Vendor | Frameworks | Stack |
|---|---|---|---|
| `apple` | Apple/macOS | Metal | system `whisper-cli` + `ggml-metal` |
| `nvidia` | NVIDIA | CUDA, Vulkan | system `whisper-cli` + `ggml-cuda`/`ggml-vulkan` |
| `amd` | AMD Radeon | ROCm, Vulkan | system `whisper-cli` + `ggml-vulkan`/`ggml-hip` |

Each backend is a *capability*, not a hard dependency — it is usable only when
its runtime probe succeeds. All three drive the system `whisper-cli`
(macOS/Homebrew: `whisper-cpp` + `ggml-metal`; Arch: `whisper-cpp` +
`ggml-cuda`/`ggml-vulkan`/`ggml-hip`) and their extras install no
Python package (they are no-op markers). `clear-record backends` shows what is
available on this machine. See
[ADR-0005](docs/adr/0005-transcription-backend-strategy.md).

**Installing a `whisper-cli` GPU backend** (`apple` / `nvidia` / `amd`):

- macOS: `brew install whisper-cpp` (pulls `ggml`; the Metal plugin is a
  `libggml-metal.so` under the ggml `libexec`).
- Arch: `sudo pacman -S --needed whisper-cpp ggml-cuda` (or `ggml-vulkan`,
  `ggml-hip`).
- From source: `cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release &&
  cmake --build build -j --config Release`, then point the adapter at the build:
  `CR_WHISPER_CLI=$PWD/build/bin/whisper-cli` and
  `CR_GGML_BACKEND_DIRS=$PWD/build/bin`.
- WSL2: build with `-DGGML_CUDA=ON`; the device probe accepts the `/dev/dxg`
  passthrough (no `/dev/nvidia*` nodes there).
- Models: a size like `--model small` resolves to `models/ggml-small.bin` and is
  **downloaded automatically on first use** (from
  `huggingface.co/ggerganov/whisper.cpp`, into `--models-dir` / `CR_MODELS_DIR` /
  `./models`); a path works too, and `hf download ggerganov/whisper.cpp
  ggml-small.bin --local-dir models` is the offline/manual route. On a
  restricted network, set `HF_ENDPOINT=https://hf-mirror.com` (any Hugging
  Face-compatible endpoint works); the default is `https://huggingface.co`.
  The download is a **provisioning** step, not an execution dependency: once
  the model is present on disk, transcription needs no network.

**Verify the GPU plugin actually loads (opt-in).** The default runtime probe is
cheap: it checks that `whisper-cli`, a matching `libggml-*` plugin *file* and
the vendor device are present. An ABI/build mismatch can still pass that and
fall back to CPU silently, so `--check-plugin` runs a one-shot `whisper-cli`
load probe (no model, no network; cached for the invocation) and refuses to
transcribe when the plugin does not load:

```sh
clear-record transcribe <dir> --backend amd --check-plugin
```

On release builds the CLI may not print its backend-load line, in which case
the probe reports `inconclusive` rather than failing. `available()` never runs
the probe — it stays cheap.

## Calibrate your own model (recommended workflow)

Drop a private recording into a gitignored folder and run the pipeline; if you
have a known-good transcript, pass it to get WER:

```sh
# recordings/ is gitignored — nothing here is ever committed
mkdir -p recordings && cp ~/Downloads/my-take.wav recordings/

# install the system stack once (see "Installing a whisper-cli GPU backend"),
# then sync the (no-op) backend extra and run
uv sync --all-packages --extra apple        # or --extra nvidia / --extra amd

# full pipeline:
uv run --all-packages --extra apple clear-record calibrate recordings \
    --backend apple --model small --reference-transcript recordings/ref.txt

# or run and just get the report:
uv run --all-packages --extra apple clear-record run recordings --backend apple --model medium
```

`calibrate` prints `coverage`, `mean_confidence`, `wer` and `similarity` and
writes `recordings/export/calibration.json`.

### Validate alignment (no real bad multi-track needed)

Real "4-channel pre-mixed badness with a correct answer" is scarce, so the
`align` set is **synthesized** (owner strategy, see
[docs/test-corpus.md](docs/test-corpus.md)):

```sh
uv run --all-packages clear-record synth /tmp/align-test --devices 4 --duration 25 --seed 1
uv run --all-packages clear-record align /tmp/align-test
# compare the printed offsets against /tmp/align-test/ground_truth.json
```

`just verify` already asserts `align` recovers the true offsets on a synthetic
4-device scene.

## Running a real meeting tape

```sh
# put the tape(s) somewhere gitignored, then run the whole pipeline
mkdir -p recordings && cp /path/to/meeting*.wav recordings/

uv sync --all-packages --extra apple
uv run --all-packages --extra apple clear-record run recordings \
    --backend apple --model medium --language zh   # zh/en; omit --language to auto-detect
```

Notes for a real meeting tape:

- **Multi-channel (DJI / multichannel interface):** each channel becomes its own
  source automatically when the file has **more than two** channels, so
  `speaker ≈ channel` (closest-mic-wins). Use `--split-channels` to force
  splitting of any multichannel file, `--mix-down` to collapse to mono.
- **Long recordings:** alignment runs at 1 kHz (~1 ms) so multi-hour tapes do
  not blow up memory; transcription is CPU/GPU-time-bound, not memory-bound.
- **Mixed Chinese/English:** use a multilingual model (`small`/`medium`, not
  `.en`) and optionally a language hint (`--language zh`).
- **Single mixed stream** (phone/room mic/podcast): you get one attributed
  speaker (the source), not diarization — per-speaker attribution needs
  per-speaker channels.

### Move it to a Mac (recommended node)

The repo is fully portable — no local state, model weights are downloaded per
machine. On the Mac:

```sh
git clone https://github.com/zhaoweny/clear-record.git
cd clear-record
uv sync --all-packages --extra apple     # or --extra nvidia / --extra amd
uv run --all-packages --extra apple clear-record run <tape-dir> --backend apple --model medium
```

Processing on an Apple Silicon Mac is the intended always-on-node setup from the
project concept; a 1–3 h tape is a batch job, not an interactive one. The
`apple` backend is **CLI-only**: `brew install whisper-cpp` (its `ggml` links the
Metal plugin), and the ggml model is downloaded on first use. The `--extra apple`
marker installs no Python package.

## Long tapes, diarization & glossary

**Chunked + resumable.** Long tapes are transcribed in overlapping windows and
each chunk is checkpointed under `<dir>/chunks/<source>/`, so a run can be
stopped and restarted (a second run reuses the cache). Progress is printed and
appended to `<dir>/transcribe.log`, so you can watch a background run.

```sh
clear-record transcribe <dir> --backend apple --model medium \
    --chunk-seconds 600 --overlap-seconds 5 --jobs 4   # --no-resume to force a redo
```

**Parallel + pipelined.** Pending chunks across *all* sources are fed through
one bounded worker pool, so the GPU stays fed (a single `whisper-cli` peaks well
below saturation). `--jobs 0` (default) picks a small adaptive fan-out for
process-isolated backends and serializes in-process ones. The default is capped
by the CPU count, a 4-way fan-out ceiling, and — so N large models cannot OOM a
small-VRAM GPU — the model's resident size
against the detected VRAM. When the GPU cannot be probed (`nvidia-smi`, or the
DRM `mem_info_vram_total`), an 8 GB minimum is assumed; `CR_VRAM_GB` overrides
either. `--jobs N` or `CR_JOBS=N` bypass the advisory cap entirely. Measured on
an RX 7900 XTX: four sources ×
300 s fell from 37.5 s to 8.8 s (~4.3×).

Pressing Ctrl-C stops the pool promptly: queued chunks are cancelled, in-flight
`whisper-cli` processes are terminated, and the partial chunk cache is left
consistent and resumable (each chunk result is published atomically).

**Multi-speaker diarization.** A single mixed stream (phone/room mic/podcast)
has no per-speaker channels, so segments can be clustered into speakers from the
audio itself (log-mel + F0 fingerprint, k-means; baseline, dependency-free). A
per-channel file already attributes per source, and a single voice must not be
split on weak evidence, so diarization is **opt-in**: enable it explicitly, or
pass a known count, and it otherwise reports one speaker per source.

```sh
clear-record run <dir> --backend apple --diarize         # enable auto diarization
clear-record run <dir> --backend apple --speakers 2      # known count
clear-record diarize <dir> --speakers 3                  # re-diarize existing segments
clear-record diarize <dir> --no-diarize                  # off
```

**Glossary (initial prompt).** Put names/terms one per line in
`<dir>/glossary.txt` (or pass `--glossary FILE`); they become the decoder's
initial prompt. The chunk cache is keyed on the glossary, so the intended
workflow is: **start a first pass in the background, build the glossary while it
runs, then re-run** — the chunks are re-decoded with the finished terms.

```sh
clear-record glossary <dir> --add "李工" "Project Falcon" "ZX-2000"
clear-record transcribe <dir> --backend apple --model medium   # picks up glossary.txt
```

> **Privacy:** recordings and derived artifacts are environment-local data.
> They are gitignored and never enter the repository ([ADR-0006](docs/adr/0006-private-data-boundary.md)).

## Development

Requires [uv](https://docs.astral.sh/uv/) (workspace tooling) and
[just](https://github.com/casey/just) (recipe runner). Python 3.14 is used
locally (`.python-version`); `requires-python >= 3.12`.

```sh
just                # list recipes (this is the default)
just verify         # full gate: sync --locked + lint + format-check + tests
just lint           # ruff check
just format-check   # ruff format --check
just format         # ruff format (in place)
just test           # pytest
./scripts/verify    # thin shim -> `just verify`

# backend stacks are optional no-op extra markers (system `whisper-cli`):
uv sync --all-packages --extra apple       # Apple: system `whisper-cli` + `ggml-metal`
uv run --all-packages --extra apple clear-record --help
```

> `uv sync --all-packages` creates the env and installs all workspace members +
> dev deps (this is the `just verify` step). The `--extra <backend>` markers are
> no-ops: they install no Python package, because every backend drives the
> system `whisper-cli` + a ggml plugin (see *Backends* above).

## Repository layout

```text
packages/core         → cr-core       backend-agnostic domain model (no vendor/ML code)
packages/engine       → cr-engine     audio I/O, cross-correlation alignment, reconcile (numpy)
packages/providers    → cr-providers  per-vendor ASR adapters (apple / nvidia / amd)
packages/cli          → cr-cli        the CLI implementation package (`cr_cli`), which provides no command
packages/clear-record → clear-record  the public install name; a facade that forwards to cr-cli
docs/architecture.md                  (spec + provenance, the primary doc)
docs/adr/                             (decision records 0001–0010)
```

## License

**Code** is [MIT](LICENSE) — covers this repo's own code only. The
[license boundary](docs/adr/0003-license-boundary.md) governs third-party
dependencies: permissive stacks are preferred; **GPL/AGPL** is consumed only over
external process or network boundaries, never linked or vendored into the MIT
core; **LGPL** is used through its supported library interfaces, under its own
obligations.

**Content and assets** — the documentation, and any non-synthesized audio clips
or other assets authored for this repository — are licensed
[**CC BY 4.0**](https://creativecommons.org/licenses/by/4.0/): use them freely,
with attribution ([ADR-0008](docs/adr/0008-content-and-asset-licensing.md)).
Synthesized test audio is generated by the tool at run time and is not a shipped
asset; third-party material keeps its own licence. Your own recordings, and the
records the tool produces from them, are **yours** — not licensed by this
project.

## Docs

- [docs/architecture.md](docs/architecture.md) — architecture + provenance
  document distilled from the original concept (FACT / VOICE / REQ / DESIGN / SUGGESTION
  / OPEN labels).
- [docs/test-corpus.md](docs/test-corpus.md) — the owner's public reference
  anchors & the synthesize-the-badness strategy.
- [docs/adr/](docs/adr/) — architecture decision records.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to build, test, and contribute.
- [`SECURITY.md`](SECURITY.md) · [`PRIVACY.md`](PRIVACY.md) ·
  [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — reporting and community policies.
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) — dependency licenses and
  the license boundary.
- `AGENTS.md`, `docs/agents/` — agent workflow/geometry docs.
