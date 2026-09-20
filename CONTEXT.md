# Context — clear-record

Single-context project. This file is the agent's entry point to the domain; the
authoritative, living record is `docs/architecture.md` + the ADRs under
`docs/adr/`.

## What this is

**clear-record** is a **local-first, open-source multitrack transcription and
record-reconstruction** pipeline. It is a post-processing tool: it starts at
`ingest`, taking several recordings of the same event (e.g. a MacBook mic, an
H1n field recorder, a DJI Mic 3 transmitter), and reconstructs **one clear,
attributable record**: ingest → align → transcribe → reconcile → export.
Processing runs offline once models are provisioned: no cloud processing, no
subscription.

- **License:** MIT (own code). Vendor ASR stacks are consumed behind a
  provider interface; permissive stacks are preferred, copyleft components are
  never linked or vendored into the MIT core (ADR-0003).
- **Backends:** Apple/macOS (Metal), NVIDIA (CUDA/Vulkan), AMD (ROCm/Vulkan) —
  one interface, three families (ADR-0005) — plus Apple's native `apple-speech`
  (`SpeechAnalyzer`/`SpeechTranscriber`, macOS 26+; ADR-0019) as the preferred
  native path where it exists. The three families drive the system
  `whisper-cli` + a ggml plugin (`ggml-metal` on macOS, `ggml-cuda`/
  `ggml-vulkan`/`ggml-hip` on Linux); the native path drives neither. Every
  backend extra (including `apple` and `apple-speech`) is
  a no-op Python marker. A missing ggml model auto-downloads on first use — a
  **provisioning** step, not an execution dependency: once present, transcription
  needs no network (offline → the actionable `hf download` pre-fetch error).
- **Clean-room:** independently implemented from a generic public problem
  statement; no work/company artifacts are imported (ADR-0001, architecture §6).
- **Privacy:** recordings and model weights are environment-local data — always
  gitignored, never committed (ADR-0006).

## Where the detail lives

| What | Where |
|---|---|
| Architecture + provenance (the primary doc) | `docs/architecture.md` |
| Owner voice (what the owner actually wants) | `docs/vox/voice-of-owner.md` |
| Decision records (0001–0029) | `docs/adr/` |
| Agent workflow / landing geometry | `AGENTS.md`, `docs/agents/git-worktree.toml` |
| Issue tracker + triage | `docs/agents/` |

## Provenance convention

Every significant statement carries a label: `FACT` (externally verifiable),
`VOICE` (owner requirement/preference), `REQ` (requirement derived from a
voice), `DESIGN` (chosen implementation), `SUGGESTION` (agent proposal),
`OPEN` (unresolved). Never promote a `SUGGESTION`/`OPEN` to a `DECISION`
without evidence. The owner's authoritative words live in
[`docs/vox/voice-of-owner.md`](docs/vox/voice-of-owner.md).

## Current status

**v0.3 development trunk** (`0.3.0.dev0`; `releases/v0.2.x` is the 0.2
maintenance line and `releases/v0.1.x` the 0.1 one — ADR-0011's 2026-09-14
Update). The repo does real work end-to-end:

- uv workspace publishing a single **`clear-record`** dist whose internal layers
  are `clear_record.core` (domain) · `clear_record.engine` (audio/align/reconcile) ·
  `clear_record.providers` (ASR adapters) · `clear_record.cli` (the CLI and the
  `clear-record` command), plus the console's `service` (registry, meetings,
  runs, archive), `web` (FastAPI + htmx/Alpine), `tray` (PySide6 supervisor) and
  `mcp` (the agent boundary) — one install name, layers hidden behind it
  (ADR-0012), MIT license, provenance labeling, and a green `just verify` gate.
- `ingest` normalizes each source to 16 kHz mono WAV and **splits multi-channel
  files per channel**; `align` estimates source offsets via windowed
  cross-correlation; `transcribe` runs a real local ASR backend (Apple/macOS
  via the system `whisper-cli` + `ggml-metal`, or the preferred native
  `apple-speech` on macOS 26+; CLI-only, ggml model auto-downloaded) with
  **chunked, resumable** processing and a **glossary** initial prompt;
  `diarize` does baseline
  multi-speaker attribution for a single mixed stream; `reconcile` produces a
  source/speaker-attributed timeline; `export` writes Markdown/SRT/VTT/JSON;
  `calibrate` reports coverage / WER / similarity against an optional reference.
- **AMD is hot-tested** (RX 7900 XTX, RADV) via the system `whisper-cli` +
  `ggml-vulkan`; the `nvidia` backend shares that path but is not hot-tested
  here. The Apple `whisper-cli`/Metal path is hot-tested end-to-end on an M4
  (164 segments, no wheel installed), and the preferred native `apple-speech`
  path is hot-verified on macOS 26.6 Apple Silicon (opt-in test). Diarization is
  a baseline, not a deep-embedding system; the exotic spatial-invention scope
  (§7) is out of scope.

See `docs/architecture.md` §3–§8 for the pipeline and the ordered remaining
slices.
