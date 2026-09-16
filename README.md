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

**Install from PyPI** — the package is
[`clear-record`](https://pypi.org/project/clear-record/) (Python 3.12+):

```sh
uvx clear-record --help              # run without installing
uv tool install clear-record         # or: pipx install clear-record
```

Its internal layers (`core`, `engine`, `providers`, `cli`, then the console's
`service`, `web`, `tray` and `mcp`) are `clear_record` subpackages, not separate
distributions — see ADR-0012. The console layers are optional extras: `web`
(FastAPI + htmx/Alpine), `tray` (PySide6) and `agents` (the MCP SDK), so a
CLI-only install stays audio-only (ADR-0013/0016/0017). Transcription uses a
native OS path where one exists and the system `whisper-cli` + a ggml plugin as
the fallback (ADR-0005); `clear-record backends` reports what the machine can run.

For development, work from a source checkout:

```sh
git clone https://github.com/zhaoweny/clear-record.git
cd clear-record
uv sync --all-packages --extra apple     # or --extra nvidia / --extra amd
uv run --all-packages --extra apple clear-record --help
```

The release flow — version bump, TestPyPI rehearsal, tag, and the OIDC publish —
is in [`docs/releasing.md`](docs/releasing.md).

## Backends (native Apple Speech · Apple / NVIDIA / AMD via `whisper-cli`)

Inspired by the observation (Marco Arment / Overcast) that Mac frameworks are
excellent for on-device transcription, the transcription step supports the three
major desktop compute families behind one interface — and, on macOS 26+, Apple's
own on-device transcriber:

| id | Vendor | Frameworks | Stack |
|---|---|---|---|
| `apple-speech` | Apple/macOS 26+ | `SpeechAnalyzer`/`SpeechTranscriber` | on-device; no `whisper-cli`, no model download |
| `apple` | Apple/macOS | Metal | system `whisper-cli` + `ggml-metal` |
| `nvidia` | NVIDIA | CUDA, Vulkan | system `whisper-cli` + `ggml-cuda`/`ggml-vulkan` |
| `amd` | AMD Radeon | ROCm, Vulkan | system `whisper-cli` + `ggml-vulkan`/`ggml-hip` |

Each backend is a *capability*, not a hard dependency — it is usable only when
its runtime probe succeeds. A native path is preferred where one exists, so the
default is **native first, `whisper-cli` fallback** (ADR-0005), and
`--backend auto` applies the same order. `apple-speech` installs nothing: no
`whisper-cli`, no ggml plugin, no downloaded model — it needs macOS 26+ and
reserves the locale's asset on first use. The three ggml backends drive the
system `whisper-cli`
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
- Models: a size like `--model small` resolves to `ggml-small.bin` in the models
  directory and is **downloaded automatically on first use** (from
  `huggingface.co/ggerganov/whisper.cpp`, into `--models-dir` / `CR_MODELS_DIR` /
  `models/` under the platform data directory); a path works too, and `hf
  download ggerganov/whisper.cpp ggml-small.bin --local-dir models` is the
  offline/manual route. On a
  restricted network, set `HF_ENDPOINT=https://hf-mirror.com` (any Hugging
  Face-compatible endpoint works); the default is `https://huggingface.co`.
  A model size that `clear-record` knows is also checked against a pinned
  SHA-256 before it is installed: a mismatch is discarded and reported rather than fed
  to `whisper-cli`. A mirror or self-hosted endpoint that serves different bytes
  under a pinned name can be accepted with `CR_MODEL_CHECKSUM=off`; a name with
  no pinned digest is downloaded unchecked.
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

`bench` shows one run on the four axes: accuracy (WER, or coverage and mean
confidence), speed as x-realtime, the peak memory of the transcribe stage's
decoder workers, and what `--auto` chose. Point it at a recorded run in the
console's registry, or at a workspace (which has no speed/fit record):

```sh
uv run --all-packages clear-record bench --run-id 12     # or --meeting-id 3
uv run --all-packages clear-record bench --directory recordings
```

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

**Decoding effort (profiles).** At a fixed model, accuracy trades against speed
through the decoder settings. A **profile** is a preset for them:
`--profile fast` is greedy and quick, `balanced` and `accurate` widen the beam
search (each pass is slower but usually cleaner), and `custom` (the default)
sets nothing. Any explicit flag overrides the profile, and a `CR_*` environment
variable overrides the profile too, so `--profile accurate --beam-size 3`
narrows just that one knob. With no profile and no knob set, the command is
exactly as it was before profiles existed.

| Flag | Default | What it costs/does |
|---|---|---|
| `--beam-size N` | whisper-cli default | Beam-search width: larger = slower, more accurate. |
| `--best-of N` | whisper-cli default | Candidates tried in greedy decode: larger = slower. |
| `--temperature T` | whisper-cli default | Decoding temperature; `0.0` is deterministic. |
| `--entropy-thold T` | whisper-cli default | Stop a decode when its entropy falls below `T`. |
| `--no-speech-thold T` | whisper-cli default | Probability below which a window is treated as silence. |
| `--max-context N` | whisper-cli default | Tokens of earlier text used as decoder context (`-1` = default). |
| `--threads N` | whisper-cli default | CPU threads; matters on the CPU-only paths. |

Each also has a `CR_*` override (`CR_BEAM_SIZE`, `CR_BEST_OF`,
`CR_TEMPERATURE`, `CR_ENTROPY_THOLD`, `CR_NO_SPEECH_THOLD`, `CR_MAX_CONTEXT`,
`CR_THREADS`) and a matching `PipelineOptions` field. The chunk cache keys on
the set knobs, so switching profile re-decodes rather than reusing the other
profile's chunks. A backend that cannot honour a knob fails loudly instead of
silently ignoring it.

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
clear-record glossary <dir> --add "李工" --add "Project Falcon" --add "ZX-2000"
clear-record transcribe <dir> --backend apple --model medium   # picks up glossary.txt
```

> **Privacy:** recordings and derived artifacts are environment-local data.
> They are gitignored and never enter the repository ([ADR-0006](docs/adr/0006-private-data-boundary.md)).

## Web console (optional)

A local desktop-style console for the daily work, with the control plane kept
separate from it: four surfaces, four jobs
([ADR-0027](docs/adr/0027-console-information-architecture.md)).

- **Projects** — the daily workspace: the project list, then a project's own
  Overview, Meetings, Glossary and Media.
- **Settings** — the control plane: models, backends, agent, MCP, storage and
  status — configured occasionally, read often.
- **Setup** — system readiness: the first-run and upgrade path (welcome,
  transcription, agent, try it), skippable at every step.
- **Agent setup** — integration readiness: one reusable flow, reached from the
  Setup path and from Settings → Agent.

The console ships in this wheel, but its web stack is an optional extra so a
CLI-only install stays audio-only:

```sh
uv tool install 'clear-record[web]'    # or:  pip install 'clear-record[web]'
clear-record web                       # serves http://127.0.0.1:8765 and opens it
clear-record web --tailscale           # sets up Tailscale Serve for remote access
```

Running `clear-record web` without the extra prints the exact install command.
`--no-browser`, `--port`, `--host` and `--data-dir` control the launch; the
server binds `127.0.0.1` by default and needs no account — leave `--host` on
loopback and let your proxy be the ingress
([ADR-0021](docs/adr/0021-localhost-only-deployment.md)). The UI language comes from
`--lang`, `CR_LANG` or `LANG` (English is the source, `zh_CN` ships) — see
[docs/i18n.md](docs/i18n.md). With `--tailscale` the
console also resolves this machine's tailnet name, runs a **foreground**
`tailscale serve --https=<port> http://127.0.0.1:<port>` (the tailnet port
defaults to `--port`; choose another with `--tailscale-port`), trusts that name,
and prints the URL. Serve is a child of the console, so the mapping stops with
it — Ctrl-C included — and a mapping that already existed on that port is left
untouched. If Serve cannot start, the console still starts and says why. The
`tailscale` binary is found on `PATH` with symlinks resolved before it runs —
the macOS App Store install symlinks `~/.local/bin/tailscale` into
`Tailscale.app`, whose bundle aborts when the CLI is invoked through that
symlink; `CR_TAILSCALE` points at a non-standard install. **The
tailnet is then the authentication — anyone on your tailnet can reach the
console**
([ADR-0021](docs/adr/0021-localhost-only-deployment.md),
[deployment guide](docs/service-deployment.md#tailscale)). The app-owned
**project registry** (SQLite) lives in the platform-native data directory —
`~/Library/Application Support/clear-record` on macOS, the XDG data dir
(`~/.local/share/clear-record`) on Linux — overridable with `CR_DATA_DIR` or a
`[paths] data_dir` entry in the config file
(`<config>/clear-record/config.toml`); recordings and archives stay in your own
directories ([ADR-0006](docs/adr/0006-private-data-boundary.md),
[ADR-0007](docs/adr/0007-deployment-directories-xdg.md),
[ADR-0025](docs/adr/0025-platformdirs.md),
[ADR-0013](docs/adr/0013-bundled-web-and-service-surface.md)).

A **managed workspace** ([ADR-0024](docs/adr/0024-managed-workspace-tape-upload.md))
is an opt-in, app-owned alternative: create a meeting in managed mode and
**upload** its tapes to the node, and clear-record stores and transcribes them on
your behalf — the route to self-hosting a node and managing it remotely. The
managed root defaults to `workspaces/` under the platform data directory and is
overridable with `CR_WORKSPACE_ROOT` (point it at a NAS or a big disk). Uploads
are guarded by construction: a bare filename, an audio-extension allow-list, a
`CR_MAX_UPLOAD_BYTES` cap, a disk-space precheck and no symlink following; each
upload streams to a `.part` file, is `fsync`-ed and atomically renamed, and is
recorded with its sha256 and size. Upload is a single streaming POST — a dropped
multi-GB transfer restarts, and resumable upload is deliberately out of scope. A
`--dir` workspace and a meeting's user-chosen `workspace_path` are unchanged
([deployment guide §4](docs/service-deployment.md)).

> **Status:** the console is the page information architecture of ADR-0027 —
> Projects, a project's Overview/Meetings/Glossary/Media, Settings, Setup and
> the one agent flow. It covers the multi-project glossary table, meetings and
> tape sets, running tapes with progress and a live run view, tape upload into a
> managed workspace, and the archive view. The **agent tasks** (glossary
> collection, transcript check, minutes) run on the runner seam and land as
> reviewable drafts (ADR-0018), and the setup path's hello-world acceptance test
> proves tape → transcription → transcript, localizing a failure to a leg (`tts`,
> `backend`, `model`, `transcribe`). The real agent round-trip over MCP is proven
> by the optional `just agent-drive` — see [docs/architecture.md](docs/architecture.md).

### Desktop app (macOS · Windows)

For a non-developer machine there is no Python install: `just app` freezes the
console into a double-clickable app with **PyInstaller** (see
[packaging/pyinstaller/README.md](packaging/pyinstaller/README.md) and
[ADR-0014](docs/adr/0014-desktop-app-distribution.md)).

| Artifact | What it is |
|---|---|
| `clear-record-tray` / `clear-record.app` | double-click opens the menu-bar tray, which supervises the console |
| `clear-record` | the full console CLI |

The `build-app` CI workflow produces macOS and Windows artifacts. The builds are
**unsigned**, so on macOS 15+ the first launch goes through System Settings →
Privacy & Security → Open Anyway (on Windows, More info → Run anyway); signing
is a documented future step. No model weights or
keys are bundled — the app drives the machine's own `whisper-cli` and downloads a
ggml model on first use, exactly like the CLI.

For a menu-bar app instead of a browser launch, `clear-record tray` runs the
console in the background under a **system-tray icon** (open / status / quit).
It is an optional extra — `pip install 'clear-record[tray]'` — so the base
install stays small.

## Diagnostics (not telemetry)

Structured logs rotate in the platform-native log directory
(`~/Library/Logs/clear-record` on macOS, `$XDG_STATE_HOME/clear-record/log` on
Linux) (`CR_LOG_LEVEL`, or `-v`, raises detail). `clear-record diagnose` — or the
console's **Diagnostics** link — writes a **redacted** bundle you can attach to a
bug report. **This is not telemetry:** nothing is ever transmitted; you create the
file, read it, and choose whether to send it.

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

# console assets (needs bun; the compiled output is committed — see below):
just web-assets         # build the console's CSS/JS
just web-assets-check   # rebuild and fail if the committed output is stale

# message catalogs (Babel, build-time only; the compiled .mo is committed):
just i18n-extract       # merge new source strings into the .po catalogs
just i18n-compile       # compile each .po into the committed .mo
just i18n-check         # freshness guard (its own CI job)

# backend stacks are optional no-op extra markers (system `whisper-cli`):
uv sync --all-packages --extra apple       # Apple: system `whisper-cli` + `ggml-metal`
uv run --all-packages --extra apple clear-record --help
```

> **Front-end changes.** The console's CSS and JS are built from a real source
> tree in `packages/clear-record/frontend/` (Vite + Tailwind v4, bun) and the
> **compiled output is committed** under `clear_record/web/static/`. A plain
> `pip install` ships those bytes, so end users never need Node, and neither
> does `just verify` — the freshness guard runs as its own CI job. Edit the UI,
> then `just web-assets` and commit both source and output. See
> [docs/frontend-assets.md](docs/frontend-assets.md) and
> [ADR-0023](docs/adr/0023-frontend-toolchain.md).

> **Translations.** User-facing UI strings are marked with `tr("…")` (and
> `trn(...)` for plurals); the **English string is the message ID**, so the
> English default is unchanged and adoption is incremental. The runtime is
> stdlib `gettext` (no runtime dependency) and the compiled catalogs are
> committed, so a `pip install` needs no build step. Babel is a build-time tool
> (`just i18n-*`, with its own CI freshness job). `--lang` / `CR_LANG` / `LANG`
> pick the language. See [docs/i18n.md](docs/i18n.md).

> `uv sync --all-packages` creates the env and installs all workspace members +
> dev deps (this is the `just verify` step). The `--extra <backend>` markers are
> no-ops: they install no Python package, because the `apple` / `nvidia` / `amd`
> backends drive the system `whisper-cli` + a ggml plugin, and the native
> `apple-speech` path needs neither (see *Backends* above).

## Repository layout

```text
packages/clear-record → dist clear-record, import clear_record
  src/clear_record/core       backend-agnostic domain model (no vendor/ML code)
  src/clear_record/engine     audio I/O, cross-correlation alignment, reconcile (numpy + soundfile)
  src/clear_record/providers  per-vendor ASR adapters (apple · nvidia · amd · apple-speech)
  src/clear_record/cli        the CLI implementation and the `clear-record` command
  src/clear_record/service    headless app service: SQLite registry, meetings, tape sets, runs, archive
  src/clear_record/web        the local console: FastAPI + server-rendered htmx/Alpine
  src/clear_record/tray       PySide6 system-tray supervisor / desktop entry point (extra: tray)
  src/clear_record/mcp        the MCP server — the agent boundary (extra: agents)
  frontend                    the console's front-end source (Vite + Tailwind; output committed into web/static)
docs/architecture.md          (spec + provenance, the primary doc)
docs/adr/                     (decision records 0001–0028)
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
- [docs/frontend-assets.md](docs/frontend-assets.md) — how the console's
  compiled CSS/JS are built and kept fresh.
- [docs/i18n.md](docs/i18n.md) — how user-facing strings are translated
  (`tr()` + stdlib `gettext`, Babel at build time).
- [docs/adr/](docs/adr/) — architecture decision records.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to build, test, and contribute.
- [`SECURITY.md`](SECURITY.md) · [`PRIVACY.md`](PRIVACY.md) ·
  [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) — reporting and community policies.
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) — dependency licenses and
  the license boundary.
- `AGENTS.md`, `docs/agents/` — agent workflow/geometry docs.
