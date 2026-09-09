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
  wav/flac/ogg and, via ffmpeg, mp3/m4a/etc.).
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
| `apple` | Apple Silicon | Metal, Core ML, ANE | `whisper.cpp` |
| `nvidia` | NVIDIA | CUDA, cuBLAS, cuDNN | `faster-whisper` / CTranslate2 |
| `amd` | AMD Radeon | ROCm, Vulkan | `whisper.cpp` (`gfx1100`…) |

Each backend is a *capability*, not a hard dependency — it is usable only when
its optional extra is installed **and** its runtime probe succeeds. `clearrecord
backends` shows what is available on this machine. See
[ADR-0005](docs/adr/0005-transcription-backend-strategy.md).

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
- [docs/adr/](docs/adr/) — architecture decision records.
- `AGENTS.md`, `docs/agents/` — agent workflow/geometry docs.
