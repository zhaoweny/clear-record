# clear-record — Architecture & Provenance

Status: **runnable v0.1** (~v0.1.0) · Updated: 2026-09-11

This document distills the original concept into an architecture + provenance
document using the project's provenance labels (**FACT / VOICE / REQ / DESIGN /
SUGGESTION / OPEN**). It is a **clean-room** reimplementation: it is scoped to a
generic public problem statement and deliberately excludes the company-side, and
the exotic/invention-grade, parts of the original concept. It does **not**
promote agent suggestions or open questions into requirements.

**Source of truth (provenance):** this document is the *generic, public*
rendering of an independently developed personal idea. The clean-room origin
history is recorded in §9; the original personal concept is kept outside this
repository.

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

1. This is a **local-first, open-source, work-unrelated** transcription and
   record-reconstruction project. It is a **clean-room** reimplementation: do
   not import work/company code, assets, credentials, recordings, datasets or
   internal docs into it (ADR-0001; §6).
2. The original concept is a **post-processing** system: several imperfect
   recordings of one event go in, **one reconstructed, attributable record
   comes out**. Taglines: *"reconstruct the record"* and *"from many recordings
   to one clear record."* The natural surface is a CLI, one subcommand per
   stage: `clear-record ingest | align | transcribe | reconcile | export`.
3. The concept was, at its heart, **"record several audio sources reliably and
   use local AI to turn them into useful transcripts."** It should not be
   limited to a single capture device; it ingests heterogeneous sources (e.g. a
   laptop/stereo mic, an H1n field recorder, DJI Mic transmitters).
4. It must be **offline and subscription-free**. Processing runs offline once
   models are provisioned: no cloud processing, no subscription. You own the
   hardware and the models, so there is no per-minute or cloud cost, and no
   dependence on a vendor's service continuing to exist.
5. **Backends — support all three major desktop compute families**, inspired by
   the observation (Marco Arment / Overcast) that Mac frameworks are excellent
   for on-device transcription: Apple/macOS (Metal), NVIDIA (CUDA), and AMD
   Radeon (ROCm / Vulkan). No single-vendor lock-in. (ADR-0005.)
6. **Reliability is a first-class property.** Workstation/OS instability and
   huge recordings should not destroy progress: ingestion and processing must be
   **timestamped and chunked/durable**, and the pipeline should be
   **resumable** rather than all-or-nothing.

---

## 2. What ClearRecord is (public problem statement)

> A local-first, open-source multitrack transcription and record-reconstruction
> tool for meetings, interviews, field recordings, podcasts and research. It is
> a post-processing pipeline that starts at `ingest`: it supports heterogeneous
> audio inputs, offline ASR, diarization, synchronization and export.

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
| ingest | `clear-record ingest` | pull in heterogeneous audio sources + their metadata |
| align | `clear-record align` | place every source onto a common clock/timebase |
| transcribe | `clear-record transcribe` | run a chosen local ASR backend (see §5) |
| reconcile | `clear-record reconcile` | merge segments; speaker attribution |
| export | `clear-record export` | write a searchable, archiveable artifact (links back to source) |

[DESIGN] The `cr-core` package owns the *shape* of these stages (the
`PipelineSpec`); executing a stage against real audio and a real ASR backend is
the job of a provider (`cr-providers`) wired in by the CLI (`cr-cli`). This
keeps the domain model vendor-free (§5).

---

## 4. Observation-first domain model

The central architectural principle, carried over deliberately from the
original concept:

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

This is the same **"ingest cheaply now, normalize later"** instinct that drives
the whole project: ingestion is lightweight and append-only; interpretation is a
heavier, offline, reconstructable step. It also makes the pipeline **resumable**
— if a run dies, the durable observations survive and the run is restarted, not
restarted-from-zero.

---

## 5. Backend strategy — Apple · NVIDIA · AMD

[VOICE] Support all three desktop compute families behind **one interface**
instead of locking to a vendor. [FACT] The relevant ecosystem facts:

- Apple/macOS: the **implemented** backend uses `whisper.cpp`'s **Metal**
  backend (`ggml-metal`). `whisper.cpp` also has a **Core ML** path, and a
  June-2026 community experiment reported an **ANE**-native encoder roughly 2×
  that; Core ML and ANE are **not implemented** in clear-record — future
  ecosystem possibilities, not claimed capabilities.
- NVIDIA: `whisper.cpp`'s ggml supports a **CUDA** backend (and Vulkan).
- AMD Radeon: `whisper.cpp` supports **Vulkan** and **ROCm**, and targets the
  **`gfx1100`** (RX 7900 XTX) family.

[DESIGN] Model:

```text
          cr-core            (no vendor code)
            │  Backend interface: available() / prepare() / transcribe()
            ▼
       cr-providers
     ┌───────────┬───────────┬───────────┐
   apple       nvidia       amd
 Metal         CUDA         ROCm/Vulkan
 ggml-metal    Vulkan       gfx1100…
 whisper-cli   whisper-cli  whisper-cli
```

- A backend is a **capability**, not a hard dependency. It is usable only when
  its runtime probe succeeds (see `cr_providers.base.Backend.available()`).
  `clear-record backends` lists what is available on the current machine.
- All three **drive the system `whisper-cli`**, which links the system `ggml`
  and loads a backend plugin (`ggml-metal` on macOS via Homebrew `whisper-cpp`;
  `ggml-vulkan`/`ggml-hip` for AMD, `ggml-cuda`/`ggml-vulkan` for NVIDIA). There
  is no in-process wheel: PyPI ships no GPU-accelerated ggml backend, so `apple`,
  `nvidia` and `amd` all use the same process-isolated path, and all three extras
  are no-op markers. `available()` requires the platform (`Darwin`/`Linux`),
  `whisper-cli`, an accepted plugin, and — on Linux — the vendor's GPU device.
  Metal needs no device probe; the CLI plus plugin is the check. A missing
  `ggml-*.bin` is downloaded on first use into `model_dir` / `CR_MODELS_DIR` /
  `<cwd>/models`; the download is a **provisioning** step, not an execution
  dependency (once the model is on disk the pipeline needs no network), and
  offline, the actionable `hf download …` error is raised.
- The vendor stacks are **not imported by `cr-core`**; the CLI adapter is driven
  as a subprocess in `cr-providers`, so a plain dev/CI environment needs no GPU
  framework.
- [DESIGN] `ingest` normalizes every source to **16 kHz mono WAV** once, so
  decode/resample (incl. phone m4a/mp3 via ffmpeg) happens a single time and
  every later stage + the ASR backend operate on canonical audio. This is also
  what makes whisper.cpp's 16 kHz requirement a non-issue. **Multi-channel files
  are split per channel by default** (when >2 channels) so a 4-channel DJI
  file becomes four sources and per-speaker isolation is preserved
  (`--split-channels` / `--mix-down` override).
- [DESIGN] The `whisper-cli` adapter reads millisecond `offsets` from its `-ojf`
  JSON (`cr_providers.backends._whispercli_segments`).
  Note: cross-device clock sync is still **out of scope** (§7).
- The reference/open hardware laboratory (owner's own, generic and personal):

  | Machine | Role |
  |---|---|
  | Apple Silicon Mac mini | always-on production-ish transcription node (Metal) |
  | AMD Radeon RX 7900 XTX Linux box | high-throughput ROCm/Vulkan worker |
  | Laptop / phone | client / control surface |
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

The original concept also explored the following ideas, which sit outside the
generic "recording → transcript" concept and are **not** in scope for this
repository:

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
packages/core      → cr-core      backend-agnostic domain model — NO vendor/ML code
packages/engine    → cr-engine    audio I/O (16 kHz normalize), cross-correlation align, reconcile (numpy + soundfile)
packages/providers → cr-providers per-vendor ASR adapters (apple / nvidia / amd) behind the Backend interface
packages/cli       → cr-cli       the `clear-record` command; stages live in cr_cli.stages
docs/architecture.md               this document
docs/adr/                          decision records 0001–0009
docs/vox/voice-of-owner.md         owner voice
```

**Current status: runnable v0.1 pipeline.**

- `ingest` → normalize every source to 16 kHz mono WAV in the workspace
  (`<dir>/audio/`); **multi-channel splitting** (>2 ch by default) preserves
  per-speaker channels; re-ingest is idempotent.
- `align` → `cr_engine.align_sources`, windowed cross-correlation at 1 kHz
  (~1 ms; memory scales to multi-hour tapes), approximate offset with a
  simultaneous-start fallback.
- `transcribe` → real ASR via `cr_providers`; **Apple/macOS (system
  `whisper-cli` + `ggml-metal`) is hot-tested end-to-end on an Apple M4** (164
  segments, no wheel installed), with auto language detection and per-segment
  confidence; validated against a known-good 11 s reference (coverage 1.0,
  WER 0.0). Long tapes run **chunked and resumable** (`<dir>/chunks/<source>/`,
  progress in `<dir>/transcribe.log`), and a **glossary** (`<dir>/glossary.txt`)
  is applied as the decoder's initial prompt (cache-keyed, so a background first
  pass can be corrected by a finished glossary). Pending chunks across **all
  sources** run through one bounded worker pool (`--jobs` / `CR_JOBS`; adaptive
  default sized against the model and detected VRAM so a large model cannot OOM
  the documented minimum GPU) so the GPU stays fed; `BackendInfo.parallelizable`
  serializes any backend that is not process-isolated. Ctrl-C cancels queued
  chunks and terminates the in-flight `whisper-cli` processes, leaving a
  consistent, resumable cache.
- `diarize` → baseline multi-speaker attribution for a **single mixed stream**
  (log-mel + F0 fingerprint, k-means; dependency-free), preserving per-channel
  attribution when channels are already split.
- `attribute` → cross-talk-aware per-segment attribution by **relative,
  gain-normalized source energy** (`cr_engine.attribute`), with an optional
  mixed/room reference as a presence gate (never a speaker itself). Each source is
  normalized against its **own** level; `--window-s` switches from one static
  whole-recording level to a **causal rolling window** (≈15 s default) that tracks
  **drifting** gain and writes a calibrated per-segment confidence, while a
  constant imbalance is handled by the single static correction. **No pitch/F0
  cue** (a well-calibrated F0 cue still lost accuracy on synthetic truth). `run
  --attribute-energy` opts in without changing the default `diarize` path, and
  the corrected speaker is preserved by `reconcile`. See `docs/test-corpus.md`.
- `reconcile` → `cr_engine.reconcile`; shift by alignment, collapse overlaps,
  join only same-speaker runs, attribute speaker per source or diarizer.
- `export` → Markdown / SRT / VTT / JSON.
- `calibrate` → coverage, mean confidence, WER/similarity vs an optional
  reference transcript.

`just verify` is green; the CLI surface is derived from
`PipelineSpec`. **Meeting-tape readiness:** multi-channel input (per-channel
split), chunked/resumable transcription with progress, baseline diarization, a
glossary initial prompt, and Apple/macOS transcription are all landed. Apple
is CLI-only (system `whisper-cli` + `ggml-metal`) and auto-downloads its ggml
model on first use.
**Remaining gaps:** the NVIDIA path (system `whisper-cli` + ggml CUDA/Vulkan)
shares the hot-tested AMD code path but is not yet hot-tested on real NVIDIA
hardware; the AMD path is hot-tested on an RX
7900 XTX (its ROCm/HIP half is not exercised); diarization is a transparent
baseline (not a deep-embedding system) and will struggle with same-pitch
speakers and heavy overlap; and the exotic spatial-invention scope (§7) remains
out of scope.

**Ordered next slices (each a ticket-tracked slice; no branching in recipes):**

1. `ingest` — richer manifest metadata (sample rate, channels, original path,
   a checksum); *(normalize + channel split landed)*.
2. `align` — per-source alignment confidence and a manual-offset override;
   *(synthesized-badness harness landed)*.
3. `transcribe` — hot-test the **nvidia** backend on real hardware
   (**amd** hot-tested via system `whisper-cli` + `ggml-vulkan`; **apple**
   hot-tested end-to-end on an M4 via `whisper-cli` + `ggml-metal`); retire the
   AMD subprocess if a maintained Vulkan/HIP wheel appears; *(chunked resume +
   glossary landed)*.
4. `diarize` — replace/augment the spectral+F0 baseline with a deep embedding
   provider behind the same seam; handle same-pitch speakers and overlap.
5. `reconcile` — `--prefer` tie-breaking; better same-speaker joining across
   sources.
6. `export` — per-format options (word timestamps, speaker labels in SRT).

---

## 9. Record provenance (origin history)

[VOICE] The concept began as a **personal idea** and was developed
independently. The public specification in §§1–5 is scoped from a **generic
public problem statement**: reconstruct one clear, attributable record from
several imperfect recordings of the same event.

[DESIGN] The origin history is:

```text
personal idea
  → work-context implementation
  → clean-room personal-context implementation   (this repository)
```

The first two entries are context, not sources: the private conversation
**content** is not reproduced or imported, and no path, identifier, artifact or
work material is referenced. The generic public problem statement in §2 is the
re-derived, clean-room scope.

