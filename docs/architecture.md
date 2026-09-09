# clear-record — Architecture & Provenance

Status: **scaffold** (~v0.1.0) · Updated: 2026-09-09

This document distills the record into an architecture + provenance record using
the project's provenance labels (**FACT / VOICE / REQ / DESIGN / SUGGESTION /
OPEN**). It is a **clean-room** reimplementation: it is scoped to a generic
public problem statement and deliberately excludes the company-side, and the
exotic/invention-grade, parts of the original concept. It does **not** promote
agent suggestions or open questions into requirements.

**Source of truth (provenance):** the original personal concept was worked out
across several private chat conversations (see §9). Those conversations are the
record; this document is the *generic, public* rendering of them. It is kept in
this repository; the raw record is not.

---

## 0. Decision-provenance rule

The project is **personal-requirement-driven OSS**. Do not collapse facts,
owner intent, inferred requirements, architecture choices and agent ideas into
one undifferentiated specification. Every significant statement should be
classifiable as:

| Label | Meaning |
| --- | --- |
| **FACT** | Externally verifiable technical/ecosystem fact. |
| **VOICE** | Explicit owner requirement/preference. Highest product authority. |
| **REQ** | Requirement derived from a voice, with origin recorded. |
| **DESIGN** | Chosen implementation/architecture satisfying requirements (replaceable). |
| **SUGGESTION / AGENT-INVENTED** | Agent proposal, convenience, inferred requirement, speculation. MUST NOT silently become a requirement. |
| **OPEN** | Unresolved: needs experiment, upstream verification, or owner decision. |

**Owner voice** lives in [`docs/vox/voice-of-owner.md`](vox/voice-of-owner.md); a
`VOICE` entry here should be traceable back to it (or to a dated ADR) rather than
restating it. A `SUGGESTION` must never be presented as owner voice.

---

## 1. VOICE — project owner

1. This is a **local-first, open-source, work-unrelated** recording/
   transcription project. It is a **clean-room** reimplementation: do not import
   work/company code, assets, credentials, recordings, datasets or internal
   docs into it (ADR-0001; §6).
2. The original concept is a **post-processing** system: several imperfect
   recordings of one event go in, **one reconstructed, attributable record
   comes out**. Taglines: *"reconstruct the record"* and *"from many recordings
   to one clear record."* The natural surface is a CLI, one subcommand per
   stage: `clearrecord ingest | align | transcribe | reconcile | export`.
3. The concept was, at its heart, **"record several audio sources reliably and
   use local AI to turn them into useful transcripts."** It should not be
   limited to a single capture device; it ingests heterogeneous sources (e.g. a
   laptop/stereo mic, an H1n field recorder, DJI Mic transmitters).
4. It must be **offline and subscription-free**: you own the hardware and the
   models, so there is no per-minute or cloud cost, and no dependence on a
   vendor's service continuing to exist.
5. **Backends — support all three major desktop compute families**, inspired by
   the observation (Marco Arment / Overcast) that Mac frameworks are excellent
   for on-device transcription: Apple Silicon (Metal / Core ML / ANE), NVIDIA
   (CUDA), and AMD Radeon (ROCm / Vulkan). No single-vendor lock-in. (ADR-0005.)
6. **Reliability is a first-class property.** Workstation/OS instability and
   huge recordings should not destroy progress: capture must be
   **timestamped and chunked/durable**, and the pipeline should be
   **resumable** rather than all-or-nothing.

---

## 2. What ClearRecord is (public problem statement)

> A local-first, open-source multitrack recording and transcription application
> for meetings, interviews, field recordings, podcasts and research. It
> supports heterogeneous audio inputs, timestamped/chunked durable recording,
> offline ASR, diarization, synchronization and export.

It is deliberately scoped as a **generic tool**, not "describe a company
deployment." The company-side workflow, infrastructure, partner integration,
internal schemas and authentication are **out of scope** for this repository
(§7).

---

## 3. Pipeline

```text
audio ingest → normalization/VAD → ASR → diarization → alignment
            → speaker reconstruction → transcript cleanup → LLM reasoning
            → searchable/archive output
```

The CLI (and the `cr-core` `PipelineSpec`) exposes five stages that group that
into a stable, user-facing surface:

| Stage | CLI | Job |
|---|---|---|
| ingest | `clearrecord ingest` | pull in heterogeneous audio sources + their metadata |
| align | `clearrecord align` | place every source onto a common clock/timebase |
| transcribe | `clearrecord transcribe` | run a chosen local ASR backend (see §5) |
| reconcile | `clearrecord reconcile` | merge segments; speaker attribution; decision / action-item extraction |
| export | `clearrecord export` | write a searchable, archiveable artifact (links back to source) |

[DESIGN] The `cr-core` package owns the *shape* of these stages (the
`PipelineSpec`); executing a stage against real audio and a real ASR backend is
the job of a provider (`cr-providers`) wired in by the CLI (`cr-cli`). This
keeps the domain model vendor-free (§5).

---

## 4. Observation-first domain model

The central architectural principle, carried over deliberately from the record:

> **Store observations first; reconstruct state and meaning afterward.**

ClearRecord should not fundamentally store "a transcript." It should store
something closer to an observation:

```text
Observation
  timestamp
  clock_domain
  source
  entity_hint
  measurement
  confidence
  provenance
```

and then *derive*:

```text
speech segment
speaker identity
position estimate            (only for the generic "approximate seat" case, see §7)
conversation turn
decision
action item
topic transition
```

This is the same **"record cheaply now, normalize later"** instinct that drives
the whole project: capture is lightweight and append-only; interpretation is a
heavier, offline, reconstructable step. It also makes the pipeline **resumable**
— if a run dies, the durable observations survive and the run is restarted, not
restarted-from-zero.

---

## 5. Backend strategy — Apple · NVIDIA · AMD

[VOICE] Support all three desktop compute families behind **one interface**
instead of locking to a vendor. [FACT] The relevant ecosystem facts:

- Apple Silicon: `whisper.cpp` treats Apple Silicon as a first-class target with
  **Metal** and **Core ML** acceleration; a June-2026 community experiment
  reported an **ANE**-native encoder roughly 2× its Core ML path.
- NVIDIA: `faster-whisper` / **CTranslate2** use **CUDA / cuBLAS / cuDNN**.
- AMD Radeon: `whisper.cpp` supports **Vulkan** and **ROCm**, and targets the
  **`gfx1100`** (RX 7900 XTX) family.

[DESIGN] Model:

```text
          cr-core            (no vendor code)
            │  Backend interface: available() / transcribe()
            ▼
       cr-providers
     ┌───────────┬───────────┬───────────┐
   apple       nvidia       amd
 Metal/CoreML   CUDA       ROCm/Vulkan
 ANE           cuBLAS       gfx1100…
 whisper.cpp    cuDNN      whisper.cpp
             faster-whisper
```

- A backend is a **capability**, not a hard dependency. It is usable only when
  its optional dependency extra is installed **and** its runtime probe succeeds
  (see `cr_providers.base.Backend.available()`). `clearrecord backends` lists
  what is available on the current machine.
- The vendor stacks are **not imported by `cr-core`** and are imported lazily in
  `cr-providers`, so a plain dev/CI environment needs no GPU framework.
- The reference/open hardware laboratory (owner's own, generic and personal):

  | Machine | Role |
  |---|---|
  | Apple Silicon Mac mini | always-on production-ish transcription node (Metal/Core ML/ANE) |
  | AMD Radeon RX 7900 XTX Linux box | high-throughput ROCm/Vulkan worker |
  | Laptop / phone | capture / client / control surface |
  | NAS | raw tapes + derived artifacts |

---

## 6. Clean-room boundary

[VOICE] This is an independent personal OSS project; do not import company
material. The boundary is deliberately aggressive. **Excluded from this
repository, entirely:**

- work repository code or commits;
- work/agent-generated code;
- work prompts or specifications copied verbatim;
- company meeting recordings and partner recordings;
- internal requirement documents and internal architecture documents;
- partner names;
- private APIs / protocols;
- company datasets and internal test fixtures;
- credentials;
- company-specific deployment requirements.

[DESIGN] The repo defines scope from a **generic public problem statement**
(§2), not from "recreate what was built at work." Git history is kept from day
one, and the README carries a simple provenance note (facts, not legal claims):
"developed independently using personally owned hardware and publicly available
documentation, models, libraries and test recordings." We do **not** assert
unsupported conclusions like "no employer IP involved."

This is a *provenance-and-scope* rule, not a legal opinion. The conceptual
knowledge and general engineering skill carried into the project cannot be
unlearned; what this rule prevents is importing concrete company **artifacts**.

---

## 7. Out of scope (deliberately excluded from the open repo)

The original concept explored a set of more exotic, **invention-grade** ideas
that sit on the wrong side of an OSS/company boundary and were **not** part of
the generic "recording → transcript" concept. These are **not** in scope for
this repository:

- turning several distributed microphone transmitters into a **spatial array /
  beamforming** rig;
- multi-**camera** / camera-fusion reconstruction;
- **relative 3-D speaker localization** (acoustic triangulation, DOA/TDOA
  fusion, bearing-vector intersection);
- cross-device **clock synchronization** as a product feature;
- **network-attached cameras** / active world-reconstruction pods.

[OPEN] The only *generic* spatial affordance retained in scope is the much
weaker, purely descriptive question "which approximate seat/direction was the
speaker at" — and even that is explicitly **not** part of the v1 scaffold. It is
recorded here only so a future reader knows *why* it is absent.

Should any of the above later be desired in this repository, that is a fresh,
evidence-backed scope decision (new ADR), not a retroactive import.

---

## 8. Repository layout, current status, and next slices

Layout:

```text
packages/core      → cr-core      backend-agnostic domain core (PipelineSpec, observation model) — NO vendor/ML code
packages/providers → cr-providers per-vendor ASR adapters (apple / nvidia / amd) behind the Backend interface
packages/cli       → cr-cli       the `clearrecord` command; surface derives from PipelineSpec
docs/architecture.md               this document
docs/adr/                          decision records 0001–0005
docs/vox/voice-of-owner.md         owner voice
```

**Current status:** scaffold established — uv workspace, MIT license,
provenance labeling, a `clearrecord` CLI whose subcommand surface derives from
the pipeline spec, and a passing `just verify` gate. **Pipeline execution is not
yet implemented**; the CLI stages and the `transcribe()` methods are declared
placeholders.

**Ordered next slices (each a ticket-tracked slice; no branching in recipes):**

1. `ingest` — read a source container/manifest, validate audio files, emit
   `Observation` records with timestamps + provenance.
2. `align` — normalize per-source clock domains onto a common timebase.
3. `transcribe` — wire a real `Backend.transcribe()` for at least one backend
   (Apple first, since it is the author's always-on node); keep the interface
   vendor-neutral.
4. `reconcile` — merge segments into an attributable transcript (VAD/ASR output
   → segments → speaker turns).
5. `export` — searchable/archiveable artifact with links back to source audio.
6. resilience — chunked/durable capture + resumable runs.

---

## 9. Record provenance (where the concept came from)

The source record is the owner's private chat archive (ChatGPT export) under the
personal logbook, `data/chatgpt-export/`. The conversations that define the
concept (kept outside this repo; referenced for traceability only):

| Conversation | Date (UTC) | Topic |
|---|---|---|
| `6a97ea09-…` (命名建議) | 2026-09-02 09:20 | Naming: `clear-transcript` → `ClearRecord`; "reconstruct the record"; CLI surface |
| `6a982644-…` (Local Development Plan) | 2026-09-02 13:38 | Local-first dev loop; generic-vs-work split; IP caution |
| `6a98f33a-…` (Dedicated AI Hardware) | 2026-09-03 04:11 | Dedicated compute; Apple / AMD / NVIDIA backend matrix; offline + subscription-free |
| `6a996848-…` (Open Source IP Risks) | 2026-09-03 12:36 | The clean-room boundary adopted in §6; the three-projects split (generic / company / exotic) |
| `6a9e0282-…` (Compare Recording Setups) | 2026-09-07 00:18 | Capture topologies; iPhone + DJI Mic; field/podcast recorders |

These are the *facts and decisions* that seeded the public spec in §§1–5; the
**generic** public problem statement in §2 is the re-derived, clean-room scope.
