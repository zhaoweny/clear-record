# Voice of Owner — clear-record

The authoritative recorded owner intent. A `VOICE` entry in
`docs/architecture.md` or an ADR should trace back to a line here (or to a dated
ADR). These are the owner's own words/positions, distilled from the original
concept (see `docs/architecture.md` §9).

## How to read this file

| Document | Kind | Owns | Supersedes |
|---|---|---|---|
| `docs/architecture.md` | the synthesis | what the system is, now | — |
| `docs/adr/*` | **law** | each decision, its rationale and its status line | other ADRs, explicitly |
| this file and `records/` | **testimony** | the owner's words, verbatim, with their provenance | nothing: an old utterance is annotated, never rewritten |

- **Nothing becomes a decision by being written here.** A tentative word ("I propose…", "I guess", "I think
  I'd accept") is recorded as tentative and waits for an ADR, or for a second, firmer word.
- **Only the owner's lines are `[VOICE]`.** What the assistant proposed is `[AGENT-SUGGESTION]`, or is marked
  inline as agent-authored framing, and is never promoted to a requirement without owner evidence
  (`docs/architecture.md` §0).
- **A date is when the words were *captured*, not necessarily when they were said.** Several entries rest on
  an exported conversation or a working session, and an export is written after the fact — a section dated
  2026-09-21 may record a position first spoken earlier. Where the difference matters, the entry says which
  source it rests on.
- **A superseded position is annotated in place** ("*(Superseded 2026-09-15 by ADR-0025…)*"), so the record
  stays readable in order. The dated sections live under `records/`; this file keeps the standing positions,
  the index below, and the pointers.

## What is in force

One row per topic: the position that holds today, the document that owns it, and the testimony it rests on.
Rows name the owning document and never restate its decision text.

| Topic | In force | Owned by | Testimony |
|---|---|---|---|
| Scope and the clean-room boundary | local-first, open-source, work-unrelated; the exotic spatial scope stays out | `AGENTS.md`; `docs/architecture.md` §1, §6, §7 | standing, below |
| Offline and subscription stance | own hardware and models; no cloud cost; no dependence on a service surviving | `docs/architecture.md` §1 | standing, below |
| Distribution | one published dist (`clear-record`), layers internal, web/tray/agents behind extras | ADR-0012, ADR-0013 | `records/2026-09-13-release-and-packaging.md` |
| Build toolchain | `uv_build`, front to back | ADR-0010 | same record |
| CLI | the command is `clear-record`; Click; one declaration per knob | ADR-0022 | same record |
| Versions, releases, and lines | dev builds are CI artifacts; rc and stable publish; 0.1 and 0.2 maintenance, 0.3 development on `main` | ADR-0011 and its Updates | same record, and `records/2026-09-21-v03-scope.md` |
| Backends | native first (Apple `apple-speech`); system `whisper-cli` + a ggml plugin as the portable fallback; profiles tune knobs, `--auto` is opt-in | ADR-0005 and its 2026-09-14 Update; ADR-0019 | `records/2026-09-14-backends.md` |
| Console and app shell | server-rendered htmx/Alpine with committed compiled assets; a PySide6 tray; the page IA and the view/lookup seam | ADR-0016, ADR-0023, ADR-0027, ADR-0030 | `records/2026-09-14-console-and-app-shell.md` |
| Agent boundary | clear-record is a **backend for harnesses**: MCP is the only agent integration, and the in-process BYOK path is dropped in 0.3 | ADR-0017; ADR-0018 stands until the removal's own ADR | `records/2026-09-14-agent-task-execution.md`, `records/2026-09-21-v03-scope.md` |
| Paths and deployment | platform-native directories; localhost only, remote access is the operator's reverse proxy | ADR-0025, ADR-0021 | `records/2026-09-15-service-paths-and-i18n.md` |
| Managed workspace | tapes upload into an app-owned workspace; the data-not-state split holds | ADR-0024, ADR-0025 | same record |
| i18n | `tr()` with English message IDs, the English default unchanged; logs, JSON, exports and the diagnostics bundle are never translated | `docs/i18n.md` | same record |
| Outbound notifications | webhooks, with endpoint health and the last delivery outcome surfaced | ADR-0020 | `records/2026-09-15-webhooks.md` |
| The stated next position | dictation as a first-class input, with near-real-time and re-transcription modes — **deferred to 0.4**, no release date yet | no ADR yet; the tracker's 0.4 headline | `records/2026-09-21-dictation-input.md` |

## Standing positions

### Project framing

- The original concept was **"from many recordings to one clear record"** — a
  post-processing system that ingests several imperfect recordings of one event
  and produces one reconstructed, attributable record. (Also: *"reconstruct the
  record."*)
- The natural surface is a CLI with one subcommand per stage:
  `clearrecord ingest | align | transcribe | reconcile | export`.

### Scope / boundaries

- **Local-first, open-source, work-unrelated.** Do not import work/company
  code, assets, credentials, recordings, datasets, partner names or internal
  docs into the repo. (Provenance note; not a legal opinion.)
- **Define it from a generic public problem statement**, not from
  "recreate what was built at work." Keep Git history from day one.
- **Offline and subscription-free**: own the hardware and models; no cloud
  cost; no dependence on a third-party service continuing to exist.

### Backends

- **Support all three major desktop compute families** behind one interface,
  inspired by the observation (Marco Arment / Overcast) that Mac frameworks are
  excellent for on-device transcription: **Apple** (Metal / Core ML / ANE),
  **NVIDIA** (CUDA), **AMD Radeon** (ROCm / Vulkan). No single-vendor lock-in.

### Reliability

- Ingestion and processing must be **timestamped and chunked/durable**; the
  pipeline must be **resumable**, so workstation/OS instability and multi-GB
  recordings do not destroy progress.

### What is deliberately NOT part of the open repo

The exotic, invention-grade ideas — distributed-transmitter spatial
arrays/beamforming, camera fusion, relative 3-D speaker localization
(acoustic triangulation, DOA/TDOA fusion), cross-device clock sync as a product
feature, network-attached world-reconstruction pods — are **not** in scope for
this repository. See `docs/architecture.md` §7.

## Records

Dated testimony, one file per topic, moved verbatim from this file (2026-09-21):

- [`records/2026-09-13-release-and-packaging.md`](records/2026-09-13-release-and-packaging.md) — the command and
  distribution names, the build toolchain, versioning and the release train, and the 0.2.x cut.
- [`records/2026-09-14-console-and-app-shell.md`](records/2026-09-14-console-and-app-shell.md) — the console
  brief and its decisions, the packaging revision, the desktop app, and the app shell.
- [`records/2026-09-14-backends.md`](records/2026-09-14-backends.md) — native transcription paths, profiles and
  `auto`, the hardware research, and `whisper-cli` as a fallback.
- [`records/2026-09-14-grilling-rounds-1-4.md`](records/2026-09-14-grilling-rounds-1-4.md) — the consolidated
  open-question answers.
- [`records/2026-09-14-agent-task-execution.md`](records/2026-09-14-agent-task-execution.md) — no bundled
  harness, the three rungs, the tuning loop, and the 2026-09-21 correction.
- [`records/2026-09-15-webhooks.md`](records/2026-09-15-webhooks.md) — outbound notifications.
- [`records/2026-09-15-service-paths-and-i18n.md`](records/2026-09-15-service-paths-and-i18n.md) — logging and
  feedback, the deployment investigation, i18n, the managed workspace, `platformdirs`, Tailscale, and Click.
- [`records/2026-09-21-dictation-input.md`](records/2026-09-21-dictation-input.md)
  — the dictation position; its verbatim testimony is personal-context material and is kept in the private
  tracker (`vox-private-records`).
- [`records/2026-09-21-v03-scope.md`](records/2026-09-21-v03-scope.md) — the v0.3 scope and milestone decisions.
