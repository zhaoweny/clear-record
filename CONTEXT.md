# Context — clear-record

Single-context project. This file is the agent's entry point to the domain; the
authoritative, living record is `docs/architecture.md` + the ADRs under
`docs/adr/`.

## What this is

**clear-record** is a **local-first, open-source multitrack recording and
transcription** application. It takes several recordings of the same event
(e.g. a MacBook mic, an H1n field recorder, a DJI Mic 3 transmitter) and
reconstructs **one clear, attributable record**: ingest → align → transcribe →
reconcile → export. It runs fully offline with your own models.

- **License:** MIT (own code). Vendor ASR stacks are consumed behind a
  provider interface; permissive stacks are preferred, copyleft components are
  never linked or vendored into the MIT core (ADR-0003).
- **Backends:** Apple (Metal/Core ML/ANE), NVIDIA (CUDA/Vulkan), AMD
  (ROCm/Vulkan) — one interface, three families (ADR-0005). All three drive the
  system `whisper-cli` + a ggml plugin (`ggml-metal` on macOS, `ggml-cuda`/
  `ggml-vulkan`/`ggml-hip` on Linux); every backend extra (including `apple`) is
  a no-op Python marker. A missing ggml model auto-downloads on first use
  (offline → the actionable `hf download` pre-fetch error).
- **Clean-room:** independently implemented from a generic public problem
  statement; no work/company artifacts are imported (ADR-0001, architecture §6).
- **Privacy:** recordings and model weights are environment-local data — always
  gitignored, never committed (ADR-0006).

## Where the detail lives

| What | Where |
|---|---|
| Architecture + provenance (the primary doc) | `docs/architecture.md` |
| Owner voice (what the owner actually wants) | `docs/vox/voice-of-owner.md` |
| Decision records (0001–0005) | `docs/adr/` |
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

**Runnable v0.1 pipeline.** The repo now does real work end-to-end:

- uv workspace (`cr-core` domain · `cr-engine` audio/align/reconcile ·
  `cr-providers` ASR adapters · `cr-cli` the `clearrecord` command), MIT license,
  provenance labeling, and a passing `just verify` gate (109 tests).
- `ingest` normalizes each source to 16 kHz mono WAV and **splits multi-channel
  captures per channel**; `align` estimates source offsets via windowed
  cross-correlation; `transcribe` runs a real local ASR backend (Apple Silicon
  via the system whisper.cpp/Metal `whisper-cli`, hot-tested end-to-end on Apple
  M4; CLI-only, ggml model auto-downloaded) with **chunked, resumable**
  processing and a **glossary** initial prompt; `diarize` does baseline
  multi-speaker attribution for a single mixed stream; `reconcile` produces a
  source/speaker-attributed timeline; `export` writes Markdown/SRT/VTT/JSON;
  `calibrate` reports coverage / WER / similarity against an optional reference.
- **AMD is hot-tested** (RX 7900 XTX, RADV) via the system `whisper-cli` +
  `ggml-vulkan`; the `nvidia` backend shares that path but is not hot-tested
  here. The Apple `whisper-cli`/Metal path is hot-tested end-to-end on an M4
  (164 segments, no wheel installed). Diarization is a baseline, not a
  deep-embedding system; the exotic spatial-invention scope (§7) is out of scope.

See `docs/architecture.md` §3–§8 for the pipeline and the ordered remaining
slices.
