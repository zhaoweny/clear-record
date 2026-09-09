# clear-record

> **From many recordings to one clear record.**
>
> A local-first, open-source multitrack **recording and transcription**
> application for meetings, interviews, field recordings, podcasts and
> research: ingest several audio sources, align them onto a common clock,
> transcribe the result, reconcile it into an attributable record, and export a
> searchable/archiveable artifact. Runs entirely offline with your own models.

## Provenance note

This is a **clean-room** reimplementation of a personal concept. It was built
independently from a public problem statement, using personally owned hardware,
publicly available documentation, models, libraries and reference technology.
No work/company source code, generated artifacts, specifications, recordings,
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

- **ingest** — pull in heterogeneous audio sources plus their metadata.
- **align** — place every source onto a common clock/timebase.
- **transcribe** — run a chosen local ASR backend (see *Backends*).
- **reconcile** — merge segments into one transcript with speaker attribution
  and decision / action-item extraction.
- **export** — write a searchable, archiveable artifact (with links back to source).

The pipeline's core idea is **observation-first**: store timestamped,
source-attributed observations first, and reconstruct transcript, speakers and
meaning afterward. That keeps capture cheap and reliability a property of
*storage*, not of a fragile online model. See
[docs/architecture.md](docs/architecture.md) §4.

## Backends (Apple · NVIDIA · AMD)

Inspired by the observation (Marco Arment / Overcast) that Mac frameworks are
excellent for on-device transcription, the transcription step supports all
three major desktop compute families behind one interface rather than locking
to a single vendor:

| id | Vendor | Frameworks | Typical stack |
|---|---|---|---|
| `apple` | Apple Silicon | Metal, Core ML, ANE | `whisper.cpp` |
| `nvidia` | NVIDIA | CUDA, cuBLAS, cuDNN | `faster-whisper` / CTranslate2 |
| `amd` | AMD Radeon | ROCm, Vulkan | `whisper.cpp` (`gfx1100`…) |

Each backend is a *capability*, not a hard dependency — a backend is usable only
when its optional dependency extra is installed **and** its runtime probe
succeeds. `clearrecord backends` lists what is available on this machine. See
[ADR-0005](docs/adr/0005-transcription-backend-strategy.md).

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

uv run --all-packages clearrecord --help        # CLI surface
uv run --all-packages clearrecord backends --all
```

> `uv sync --all-packages` (create env, install all workspace members + dev
> deps) is available as the `just verify` step or directly. Vendor extras
> (`--extra apple` / `--extra nvidia` / `--extra amd`) are installed only when
> you actually want that backend.

## Repository layout

```text
packages/core      → cr-core      (backend-agnostic domain core; no vendor/ML code)
packages/providers → cr-providers (per-vendor ASR adapters behind one interface)
packages/cli       → cr-cli       (the `clearrecord` command)
docs/architecture.md               (spec + provenance, the primary doc)
docs/adr/                          (architecture decision records)
```

## License

[MIT](LICENSE) — covers this repo's own code only. The
[license boundary](docs/adr/0003-license-boundary.md) governs how copyleft /
vendor dependencies are consumed (permissive stacks are preferred and confined
behind provider interfaces; copyleft components are consumed only over external
process/network boundaries, never linked or vendored into the MIT core).

## Docs

- [docs/architecture.md](docs/architecture.md) — architecture + provenance
  document distilled from the record (FACT / VOICE / REQ / DESIGN / SUGGESTION
  / OPEN labels).
- [docs/adr/](docs/adr/) — architecture decision records.
- `AGENTS.md`, `docs/agents/` — agent workflow/geometry docs.
