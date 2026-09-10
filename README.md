# clear-record

> **From many recordings to one clear record.**
>
> A local-first, open-source multitrack **recording and transcription**
> application: ingest several audio sources, align them onto a common clock,
> transcribe the result with a local model, reconcile it into an attributable
> record, and export a searchable/archiveable artifact. Runs entirely offline on
> your own hardware and models — no cloud, no subscription.

## Provenance note

This is a **clean-room** reimplementation of a personal concept. It was built
independently from a public problem statement, using personally owned hardware,
publicly available documentation, models, libraries and reference technology. No
work/company source code, generated artifacts, specifications, recordings,
datasets, partner names, internal documents or credentials are imported; see
[docs/architecture.md](docs/architecture.md) §6 for the exact clean-room
boundary. That note records *facts* (that the work was done independently) and
is not a legal opinion.

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
  per channel by default** (e.g. a 4-channel DJI capture → 4 sources), so
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
meaning afterward. That keeps capture cheap and reliability a property of
*storage*, not of a fragile online model (`docs/architecture.md` §4).

## Backends (Apple · NVIDIA · AMD)

Inspired by the observation (Marco Arment / Overcast) that Mac frameworks are
excellent for on-device transcription, the transcription step supports all
three major desktop compute families behind one interface:

| id | Vendor | Frameworks | Stack |
|---|---|---|---|
| `apple` | Apple Silicon | Metal, Core ML, ANE | system `whisper-cli` + `ggml-metal` (fallback: `pywhispercpp`) |
| `nvidia` | NVIDIA | CUDA, Vulkan | system `whisper-cli` + `ggml-cuda`/`ggml-vulkan` |
| `amd` | AMD Radeon | ROCm, Vulkan | system `whisper-cli` + `ggml-vulkan`/`ggml-hip` |

Each backend is a *capability*, not a hard dependency — it is usable only when
its runtime probe succeeds. All three prefer the system `whisper-cli`
(macOS/Homebrew: `whisper-cpp` + `ggml-metal`; Arch: `whisper-cpp` +
`ggml-cuda`/`ggml-vulkan`/`ggml-hip`) and the `nvidia`/`amd` extras install no
Python package. Apple keeps the `pywhispercpp` wheel (`--extra apple`) as a
fallback, so a pip-only Mac still works. `clearrecord backends` shows what is
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
- Models: `hf download ggerganov/whisper.cpp ggml-small.bin --local-dir models`;
  a size like `--model small` resolves to `models/ggml-small.bin` (a path works
  too).

## Calibrate your own model (recommended workflow)

Drop a private recording into a gitignored folder and run the pipeline; if you
have a known-good transcript, pass it to get WER:

```sh
# recordings/ is gitignored — nothing here is ever committed
mkdir -p recordings && cp ~/Downloads/my-take.wav recordings/

# install the backend you want once, then run
uv sync --all-packages --extra apple        # or --extra nvidia / --extra amd

# full pipeline:
uv run --all-packages --extra apple clearrecord calibrate recordings \
    --backend apple --model small --reference-transcript recordings/ref.txt

# or run and just get the report:
uv run --all-packages --extra apple clearrecord run recordings --backend apple --model medium
```

`calibrate` prints `coverage`, `mean_confidence`, `wer` and `similarity` and
writes `recordings/export/calibration.json`.

### Validate alignment (no real bad multi-track needed)

Real "4-channel pre-mixed badness with a correct answer" is scarce, so the
`align` set is **synthesized** (owner strategy, see
[docs/test-corpus.md](docs/test-corpus.md)):

```sh
uv run --all-packages clearrecord synth /tmp/align-test --devices 4 --duration 25 --seed 1
uv run --all-packages clearrecord align /tmp/align-test
# compare the printed offsets against /tmp/align-test/ground_truth.json
```

`just verify` already asserts `align` recovers the true offsets on a synthetic
4-device scene.

## Running a real meeting tape

```sh
# put the tape(s) somewhere gitignored, then run the whole pipeline
mkdir -p recordings && cp /path/to/meeting*.wav recordings/

uv sync --all-packages --extra apple
uv run --all-packages --extra apple clearrecord run recordings \
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
gh repo clone zhaoweny/clear-record      # private repo
cd clear-record
uv sync --all-packages --extra apple     # or --extra nvidia / --extra amd
uv run --all-packages --extra apple clearrecord run <tape-dir> --backend apple --model medium
```

Processing on an Apple Silicon Mac is the intended always-on-node setup from the
project concept; a 1–3 h tape is a batch job, not an interactive one. The
`apple` backend **prefers the system `whisper-cli` + `ggml-metal`** path
(`brew install whisper-cpp`) and falls back to the `pywhispercpp` wheel that
`--extra apple` installs, so both a Homebrew Mac and a pip-only Mac work.

## Long tapes, diarization & glossary

**Chunked + resumable.** Long tapes are transcribed in overlapping windows and
each chunk is checkpointed under `<dir>/chunks/<source>/`, so a run can be
stopped and restarted (a second run reuses the cache). Progress is printed and
appended to `<dir>/transcribe.log`, so you can watch a background run.

```sh
clearrecord transcribe <dir> --backend apple --model medium \
    --chunk-seconds 600 --overlap-seconds 5 --jobs 4   # --no-resume to force a redo
```

**Parallel + pipelined.** Pending chunks across *all* sources are fed through
one bounded worker pool, so the GPU stays fed (a single `whisper-cli` peaks well
below saturation). `--jobs 0` (default) picks a small adaptive fan-out for
process-isolated backends and serializes in-process ones (e.g. the Apple wheel
fallback). The default is capped by the CPU count, a 4-way fan-out ceiling, and
— so N large models cannot OOM a small-VRAM GPU — the model's resident size
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
per-channel capture already attributes per source, and a single voice must not be
split on weak evidence, so diarization is **opt-in**: enable it explicitly, or
pass a known count, and it otherwise reports one speaker per source.

```sh
clearrecord run <dir> --backend apple --diarize         # enable auto diarization
clearrecord run <dir> --backend apple --speakers 2      # known count
clearrecord diarize <dir> --speakers 3                  # re-diarize existing segments
clearrecord diarize <dir> --no-diarize                  # off
```

**Glossary (initial prompt).** Put names/terms one per line in
`<dir>/glossary.txt` (or pass `--glossary FILE`); they become the decoder's
initial prompt. The chunk cache is keyed on the glossary, so the intended
workflow is: **start a first pass in the background, build the glossary while it
runs, then re-run** — the chunks are re-decoded with the finished terms.

```sh
clearrecord glossary <dir> --add "李工" "Wenyuan" "ATE-2000"
clearrecord transcribe <dir> --backend apple --model medium   # picks up glossary.txt
```

> **Privacy:** recordings and derived artifacts are environment-local data.
> They are gitignored and never enter the repository. The clean-room boundary
> in `docs/architecture.md` §6 and ADR-0006 make this explicit.

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

# backend stacks are optional extras (mirrored at the workspace root):
uv sync --all-packages --extra apple       # install Apple/whipser.cpp stack
uv run --all-packages --extra apple clearrecord --help
```

> `uv sync --all-packages` creates the env and installs all workspace members +
> dev deps (this is the `just verify` step). Add `--extra <backend>` to bring in
> that backend's ASR stack.

## Repository layout

```text
packages/core       → cr-core       backend-agnostic domain model (no vendor/ML code)
packages/engine     → cr-engine     audio I/O, cross-correlation alignment, reconcile (numpy)
packages/providers  → cr-providers  per-vendor ASR adapters (apple / nvidia / amd)
packages/cli        → cr-cli        the `clearrecord` command
docs/architecture.md                (spec + provenance, the primary doc)
docs/adr/                           (decision records 0001–0006)
```

## License

[MIT](LICENSE) — covers this repo's own code only. The
[license boundary](docs/adr/0003-license-boundary.md) governs how copyleft /
vendor dependencies are consumed (permissive stacks preferred and isolated
behind provider interfaces; copyleft only over external process/network
boundaries, never linked or vendored into the MIT core).

## Docs

- [docs/architecture.md](docs/architecture.md) — architecture + provenance
  document distilled from the record (FACT / VOICE / REQ / DESIGN / SUGGESTION
  / OPEN labels).
- [docs/test-corpus.md](docs/test-corpus.md) — the owner's public reference
  anchors & the synthesize-the-badness strategy.
- [docs/adr/](docs/adr/) — architecture decision records.
- `AGENTS.md`, `docs/agents/` — agent workflow/geometry docs.
