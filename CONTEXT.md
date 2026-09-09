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
- **Backends:** Apple (Metal/Core ML/ANE), NVIDIA (CUDA), AMD (ROCm/Vulkan) —
  one interface, three families (ADR-0005). Apple is proven; install a backend's
  stack with `uv sync --extra <backend>`.
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
  provenance labeling, and a passing `just verify` gate (17 tests).
- `ingest` normalizes each source to 16 kHz mono WAV; `align` estimates source
  offsets via windowed cross-correlation; `transcribe` runs a real local ASR
  backend (Apple Silicon via whisper.cpp/Metal is proven on Apple M4, incl.
  language detection + per-segment confidence); `reconcile` produces a
  source-attributed timeline; `export` writes Markdown/SRT/VTT/JSON; `calibrate`
  reports coverage / WER / similarity against an optional reference.
- **Not yet:** NVIDIA (faster-whisper) and AMD (whisper.cpp) backends are
  declared and capability-gated but not hot-tested here (no such hardware on
  this machine); the exotic spatial-invention scope (§7) remains out of scope.

See `docs/architecture.md` §3–§8 for the pipeline and the ordered remaining
slices.
