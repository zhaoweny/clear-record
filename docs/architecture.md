# clear-record — Architecture & Provenance

Status: **v0.2 development trunk** (`0.2.0.dev0`; `releases/v0.1.x` is the 0.1
maintenance line — ADR-0011's 2026-09-14 Update) · Updated: 2026-09-14

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

The CLI (and the core layer's `PipelineSpec`) exposes five stages that group that
into a stable, user-facing surface:

| Stage | CLI | Job |
|---|---|---|
| ingest | `clear-record ingest` | pull in heterogeneous audio sources + their metadata |
| align | `clear-record align` | place every source onto a common clock/timebase |
| transcribe | `clear-record transcribe` | run a chosen local ASR backend (see §5) |
| reconcile | `clear-record reconcile` | merge segments; speaker attribution |
| export | `clear-record export` | write a searchable, archiveable artifact (links back to source) |

[DESIGN] The `clear_record.core` layer owns the *shape* of these stages (the
`PipelineSpec`); executing a stage against real audio and a real ASR backend is
the job of a provider (`clear_record.providers`) wired in by the CLI
(`clear_record.cli`). This keeps the domain model vendor-free (§5).

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

## 5. Backend strategy — native first, `whisper-cli` fallback

[VOICE] **Native, OS-provided transcription paths are first-class, and
`whisper-cli` is the fallback.** Apple's `SpeechAnalyzer`/`SpeechTranscriber`
(macOS 26+) and Windows' `Microsoft.Windows.AI.Speech` are the native direction,
and the platform default is **native first, `whisper-cli` fallback** where a
native path exists; the shipped `apple` / `nvidia` / `amd` adapters use the
`whisper-cli` + ggml substrate (owner position, ADR-0005's 2026-09-14 Update).
The native family is **built**: `apple-speech` drives Apple's
`SpeechAnalyzer`/`SpeechTranscriber` on macOS 26+ behind the same `Backend`
interface (ADR-0019), with `whisper-cli` as the fallback wherever it is absent.
Windows' `Microsoft.Windows.AI.Speech` is specified but **deferred** for its
MSIX/`systemAIModels` packaging requirement. Backend *selection* is its own
capability knob (`--backend auto`), not a profile choice (the
`transcription-profiles` lane).

[VOICE] Support all three desktop compute families behind **one interface**
instead of locking to a vendor. [FACT] The relevant ecosystem facts:

- Apple/macOS: the **implemented** `apple` backend uses `whisper.cpp`'s
  **Metal** backend (`ggml-metal`). `whisper.cpp` also has a **Core ML** path,
  and a
  June-2026 community experiment reported an **ANE**-native encoder roughly 2×
  that; Core ML and ANE are **not implemented** in clear-record — future
  ecosystem possibilities, not claimed capabilities.
- NVIDIA: `whisper.cpp`'s ggml supports a **CUDA** backend (and Vulkan).
- AMD Radeon: `whisper.cpp` supports **Vulkan** and **ROCm**, and targets the
  **`gfx1100`** (RX 7900 XTX) family.

[DESIGN] Model:

```text
     clear_record.core      (no vendor code)
            │  Backend interface: available() / prepare() / transcribe()
            ▼
   clear_record.providers
     ┌─────────────┬────────────┬────────────┬────────────┐
  apple-speech     apple        nvidia       amd
  SpeechAnalyzer   Metal        CUDA         ROCm/Vulkan
  on-device        ggml-metal   Vulkan       gfx1100…
  (macOS 26+)      whisper-cli  whisper-cli  whisper-cli
```

- A backend is a **capability**, not a hard dependency. It is usable only when
  its runtime probe succeeds (see `clear_record.providers.base.Backend.available()`).
  `clear-record backends` lists what is available on the current machine.
- The three `whisper-cli` backends (`apple` / `nvidia` / `amd`) **drive the
  system `whisper-cli`** (the fallback substrate), which
  links the system `ggml` and loads a backend plugin (`ggml-metal` on macOS via
  Homebrew `whisper-cpp`; `ggml-vulkan`/`ggml-hip` for AMD,
  `ggml-cuda`/`ggml-vulkan` for NVIDIA). There
  is no in-process wheel: PyPI ships no GPU-accelerated ggml backend, so `apple`,
  `nvidia` and `amd` all use the same process-isolated path, and all three extras
  are no-op markers. `available()` requires the platform (`Darwin`/`Linux`),
  `whisper-cli`, an accepted plugin, and — on Linux — the vendor's GPU device.
  Metal needs no device probe; the CLI plus plugin is the check. A missing
  `ggml-*.bin` is downloaded on first use into `model_dir` / `CR_MODELS_DIR` /
  `<data>/models` (ADR-0025); the download is a **provisioning** step, not an execution
  dependency (once the model is on disk the pipeline needs no network), the
  known sizes are checked against a pinned SHA-256 before being installed
  (`providers.ggml_hashes`; `CR_MODEL_CHECKSUM=off` opts out), and offline the
  actionable `hf download …` error is raised.
- The vendor stacks are **not imported by the core layer**; the CLI adapter is
  driven as a subprocess in the providers layer, so a plain dev/CI environment
  needs no GPU framework.
- [DESIGN] **Text-to-speech is a second provider seam**, not a vendor backend:
  `clear_record.providers.tts` detects the system engines (macOS `say`;
  Linux `espeak-ng` / `espeak` / `spd-say`) and synthesizes the setup
  walkthrough's locale hello-world clip to a WAV with no new runtime dependency
  (subprocess only). A missing voice for the requested language is a state, not
  an exception, and `clear_record.core` never imports it (ADR-0027).
- [DESIGN] `ingest` normalizes every source to **16 kHz mono WAV** once, so
  decode/resample (incl. phone m4a/mp3 via ffmpeg) happens a single time and
  every later stage + the ASR backend operate on canonical audio. This is also
  what makes whisper.cpp's 16 kHz requirement a non-issue. **Multi-channel files
  are split per channel by default** (when >2 channels) so a 4-channel DJI
  file becomes four sources and per-speaker isolation is preserved
  (`--split-channels` / `--mix-down` override).
- [DESIGN] The `whisper-cli` adapter reads millisecond `offsets` from its `-ojf`
  JSON (`clear_record.providers.backends._whispercli_segments`).
  Note: cross-device clock sync is still **out of scope** (§7).
- The reference/open hardware laboratory (owner's own, generic and personal):

  | Machine | Role |
  |---|---|
  | Apple Silicon Mac mini | always-on production-ish transcription node (Metal) |
  | AMD Radeon RX 7900 XTX Linux box | high-throughput ROCm/Vulkan worker |
  | Laptop / phone | client / control surface |
  | NAS | raw tapes + derived artifacts |

  Research pointers (dated, primary-source notes — **not** project decisions or
  measured results; see `docs/research/` and the tracker's `hardware-backends`
  lane):

  - **Intel:** reachable through the existing `whisper-cli` + ggml seam —
    `ggml-vulkan` (cross-vendor, already the AMD hot-test) is the primary path,
    `ggml-sycl` the Intel-native option, and `ggml-openvino` the only NPU route
    (plausible but unvalidated); **OpenVINO GenAI is not recommended**
    (in-process, a second model format). `docs/research/2026-09-14-intel-transcription.md`.
  - **NVIDIA DGX Spark (GB10):** works with the existing `nvidia` backend — no
    new backend code — but its **unified memory** makes `detect_vram_gb()` fall
    to the 8 GB floor and under-provisions `large` to one worker; `CR_VRAM_GB`
    closes it. Not hot-tested here. `docs/research/2026-09-14-nvidia-dgx-spark.md`.
  - **Mobile / edge:** no phone SoC is a node (client / control surface only),
    and Rockchip has **no upstream ggml backend**, so it stays outside the
    current seam. `docs/research/2026-09-14-mobile-edge-npus.md`.

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
packages/clear-record → clear-record  single published dist; import clear_record
  src/clear_record/core       domain model — NO vendor/ML code
  src/clear_record/engine     audio I/O (16 kHz normalize), cross-correlation align, reconcile (numpy + soundfile)
  src/clear_record/providers  ASR adapters (apple · nvidia · amd · apple-speech) and the system-TTS provider (tts)
  src/clear_record/cli        the CLI implementation and command; stages live in clear_record.cli.stages
  src/clear_record/service    headless app service: project registry (SQLite), meetings, tape sets, runs, archive
  src/clear_record/web        the local console: FastAPI + server-rendered htmx/Alpine (extra: web)
  frontend                    the console's front-end source (Vite + Tailwind v4); output committed into web/static
  src/clear_record/tray       PySide6 system-tray supervisor / desktop entry point (extra: tray)
  src/clear_record/mcp        the MCP server — the agent boundary (extra: agents)
docs/architecture.md          this document
docs/adr/                     decision records 0001–0026
docs/research/                dated primary-source research notes (Intel · DGX Spark · mobile/edge)
docs/vox/voice-of-owner.md    owner voice
```

**Current status: v0.2 development trunk.** The pipeline below is landed and
runnable, and the **project console** is built on top of it:

- `clear_record.service` — the headless service: an app-owned SQLite **project
  registry**, per-project **glossary** terms, **meetings** and their **tape
  sets**, background **pipeline runs** with structured progress events and
  recorded artifacts, and the **archive** (copy + sha256 manifest). It is
  importable with no web stack, which is what keeps the console optional
  (ADR-0013).
- `clear_record.web` — the local console: FastAPI serving a server-rendered
  **htmx + Alpine.js** UI on `/ui/*` and a JSON API on `/api/*`, bound to
  localhost, with no account (extra: `web`) (ADR-0013, ADR-0016). Its CSS/JS are
  built from `packages/clear-record/frontend/` (Vite + Tailwind v4) and the
  **compiled output is committed**, so the console works offline with no Node on
  the user's machine (ADR-0023).
- `clear_record.tray` — a **PySide6** system-tray supervisor / desktop entry
  point (open / status / quit) over a Qt-free `ServiceController` (extra:
  `tray`) (ADR-0016).
- `clear_record.mcp` — the **agent boundary**: the service exposed as stdio MCP
  tools (projects, glossary, meetings, runs, artifacts), BYOK, with no harness
  entering the core (extra: `agents`) (ADR-0016, ADR-0017).
- Packaging on top of the wheel: a **PyInstaller** desktop build
  (`clear-record-web` / `clear-record.app`, unsigned, no bundled weights) and a
  deferred Flatpak story (ADR-0014, ADR-0015).

The console's **service** owns projects, the glossary, meetings and tape sets,
background runs and the **archive** (copy + sha256 manifest); the **web UI**
renders projects, the glossary table, meetings and tape sets, the **live run
view**, **tape upload** into a managed workspace, and the **archive view**, and
the **MCP surface** now carries the **tuning loop** (ADR-0017, ADR-0018). The
**agent tasks**' runner seam, prompts, drafts and accept/reject states have
landed; their **console wiring** and **guided agent setup** are **specified but
unbuilt** — see ADR-0018 and the trackers below.

- `ingest` → normalize every source to 16 kHz mono WAV in the workspace
  (`<dir>/audio/`); **multi-channel splitting** (>2 ch by default) preserves
  per-speaker channels; re-ingest is idempotent.
- `align` → `clear_record.engine.align_sources`, windowed cross-correlation at 1 kHz
  (~1 ms; memory scales to multi-hour tapes), approximate offset with a
  simultaneous-start fallback.
- `transcribe` → real ASR via `clear_record.providers`; **Apple/macOS (system
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
  gain-normalized source energy** (`clear_record.engine.attribute`), with an optional
  mixed/room reference as a presence gate (never a speaker itself). Each source is
  normalized against its **own** level; `--window-s` switches from one static
  whole-recording level to a **causal rolling window** (≈15 s default) that tracks
  **drifting** gain and writes a calibrated per-segment confidence, while a
  constant imbalance is handled by the single static correction. **No pitch/F0
  cue** (a well-calibrated F0 cue still lost accuracy on synthetic truth). `run
  --attribute-energy` opts in without changing the default `diarize` path, and
  the corrected speaker is preserved by `reconcile`. See `docs/test-corpus.md`.
- `reconcile` → `clear_record.engine.reconcile`; shift by alignment, collapse overlaps,
  join only same-speaker runs, attribute speaker per source or diarizer.
- `export` → Markdown / SRT / VTT / JSON.
- `calibrate` → coverage, mean confidence, WER/similarity vs an optional
  reference transcript.

`just verify` is green; the CLI surface is derived from
`PipelineSpec`. **Meeting-tape readiness:** multi-channel input (per-channel
split), chunked/resumable transcription with progress, baseline diarization, a
glossary initial prompt, and Apple/macOS transcription are all landed. The
`apple` backend runs through the system `whisper-cli` + `ggml-metal` (a
subprocess, no Python wheel) and auto-downloads its ggml model on first use;
the separate **`apple-speech`** native backend needs neither (ADR-0019).

**Remaining gaps.** The **NVIDIA** path shares the hot-tested AMD code path but
is **not hot-tested** on real NVIDIA hardware (the DGX Spark note in §5 is a
candidate target); the **AMD** path's ROCm/HIP half is not exercised;
**diarization** is a transparent baseline, not a deep-embedding system, and will
struggle with same-pitch speakers and heavy overlap; and the exotic
spatial-invention scope (§7) remains out of scope. Beyond the pipeline, what is
still **specified but unbuilt** (each scoped in a lane of the local tracker,
which is not published):

- **Windows-native** (`Microsoft.Windows.AI.Speech`) — deferred for its
  MSIX/`systemAIModels` packaging requirement. Apple-native has left this list:
  it landed as the `apple-speech` backend (ADR-0019);
- the **agent tasks' console surface** — glossary collection, transcript check
  and minutes. Their runner seam, prompts, drafts and accept/reject states have
  landed (ADR-0018); launching a task and reviewing a draft from the console or
  MCP has not, so accepting a draft still only records its review state;
- **meeting review + minutes** and **guided agent setup** in the console, and
  deeper **diarization** behind the same seam.

No longer gaps, having landed since this list was written: **profiles and
`--auto`** (the profile table, the decoder knobs and the explainable recommended
default), the **MCP tuning-loop surface** (read transcript, notes, run options
on `start_run`), the **archive view**, **tape upload / storage** for a managed
workspace, and **scoped re-runs** (a glossary edit no longer re-decodes every
chunk of every source, so the tuning loop is affordable on multi-hour tapes).

**Ordered next slices (each a ticket-tracked slice; no branching in recipes):**

1. **Backend evidence** — hot-test the **nvidia** backend on real hardware (the
   DGX Spark research is a candidate target), decide whether to add an **Intel**
   adapter from the hardware research, and retire the AMD subprocess if a
   maintained Vulkan/HIP wheel appears.
2. **Agent tasks** — glossary collection, transcript check and minutes as
   reviewable drafts, on the runner seam that has landed (ADR-0018).
3. **Console UI** — meeting review + minutes, and guided agent setup, over the
   agent tasks. **Diarization** remains a baseline to deepen behind the same
   seam.

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

