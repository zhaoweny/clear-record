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
  one interface, three families (ADR-0005).
- **Clean-room:** independently implemented from a generic public problem
  statement; no work/company artifacts are imported (ADR-0001, architecture §6).

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

**Scaffold only.** The repo is established: uv workspace
(`cr-core` / `cr-providers` / `cr-cli`), MIT license, provenance labeling, a
`clearrecord` CLI whose surface derives from the pipeline spec, and a
passing `just verify` gate. **Pipeline execution is not implemented yet** —
see `docs/architecture.md` §8 for the ordered next slices.
