# ADR-0019 — System-native speech backends: a second backend family

Status: active
Date: 2026-09-15

- Implements the seam half of the local tracker's `system-speech-backends`
  lane, ticket 01; the Apple adapter is ticket 02 and the Windows adapter
  ticket 03 (**deferred**).

## Context

- [DECISION] [ADR-0005](0005-transcription-backend-strategy.md)'s **2026-09-14
  Update** already makes native, OS-provided transcription paths **first-class**
  and `whisper-cli` + a ggml plugin the **portable fallback**, and already states
  that `parallelizable` is a **per-backend statement** rather than a property of
  the CLI adapter. This record does not restate that decision; it records the
  **family** that decision opens, and the seam a native adapter needs.
- [VOICE: owner, 2026-09-14] The request, verbatim: *"we'd like to system
  default transcription services, like apple SpeechTranscriber and Windows
  Microsoft.Windows.AI.Speech"* (recorded in the local tracker's
  `system-speech-backends` lane).
- [DECISION] **Apple-native is built first; Windows-native is deferred** (owner,
  round-3 grilling, 2026-09-14). Windows requires an **MSIX package with the
  `systemAIModels` capability**, which collides with the PyInstaller desktop app
  ([ADR-0014](0014-desktop-app-distribution.md)); that is a fork in the road,
  not a detail. Ticket 03 is deferred, not dropped.
- [FACT] Apple's path is `SpeechAnalyzer` + `SpeechTranscriber` in the `Speech`
  framework, **macOS 26.0+**. Models are Apple's own, run **on-device on the
  Neural Engine**, are **not in the app bundle**, and are installed/reserved
  through **`AssetInventory`**. Results are a timeline-aligned
  `AsyncSequence` with **volatile/final** ranges; `DictationTranscriber` is the
  fallback module for unsupported locales/devices.
- [FACT] Microsoft's path is **`Microsoft.Windows.AI.Speech`**, a **Windows App
  SDK** API — **not** core WinRT — with `SpeechRecognitionModel` /
  `RecognizeFromFile`. It requires Windows 11 24H2 (build 26100)+, Windows App
  SDK 1.7.1+, an MSIX package with the **`systemAIModels`** capability, and
  hardware that is an **NPU (Copilot+ PC, model preinstalled) or a CPU** (model
  downloaded via Windows Update); **GPU is not supported**.
- [FACT] The shipped `apple` / `nvidia` / `amd` backends are all the system
  `whisper-cli` + a ggml plugin, behind `BackendInfo` +
  `available()` / `prepare()` / `transcribe()`.

## Decision

### One family, two non-colliding ids

- [DECISION] The system-native family registers under **`apple-speech`** and
  **`windows-ai`**, distinct from the `whisper-cli` `apple` / `nvidia` / `amd`.
  The ids are defined **once**, in `clear_record.providers.base`
  (`APPLE_SPEECH_BACKEND_ID` / `WINDOWS_AI_BACKEND_ID`), and
  `clear_record.pipeline.auto.BACKEND_PREFERENCE` consumes those constants, so the
  documented id and the preference order cannot drift.
- [DESIGN] The native ids are **not registered** as backends until their
  adapters land (Apple ticket 02; Windows ticket 03). A registered-but-always-
  failing stub would be a lie in `clear-record backends`; the capability-driven
  resolver already lists the ids and simply cannot see them yet.

### The capabilities a non-`whisper-cli` backend declares

- [DECISION] `BackendInfo` gains `runtime` (`"whisper-cli"` | `"system"`),
  `chunked`, and the derived `uses_ggml_plugin`. Together with the existing
  `parallelizable` these are **per-backend statements**, not assumptions baked
  into the one CLI adapter:
  - `runtime` selects which probes apply. The opt-in `probe_ggml_plugin_load`
    reports **not applicable** for a `system` backend and never shells out;
    `--check-plugin` is a documented no-op there.
  - `parallelizable` says whether independent `transcribe()` calls may run
    concurrently — an OS service is not a per-chunk subprocess, so a native
    backend answers for itself either way.
  - `chunked` says whether the pipeline may split a source (below).
- [DECISION] **Availability carries a reason.** `Backend.availability()` returns
  `Availability(available, reason)`, and `available()` is derived from it, so a
  backend implements **one** of the pair. The shipped `whisper-cli` adapters now
  name the failing check (platform, CLI, plugin, device). `clear-record backends`
  prints the reason, so a missing OS version, capability or asset is not a
  mystery.
- [DECISION] **`prepare()` stays the provisioning step.** Native provisioning is
  Apple `AssetInventory` install/reserve and Windows `EnsureReadyAsync` (plus an
  explicit consent step before a CPU model download). It runs once,
  single-threaded, before the pool; once provisioned, transcription is offline
  (VOICE §4).

### Streaming and the chunk/cache layer

- [DECISION] A whole-file/streaming backend declares **`chunked=False`**. The
  pipeline then hands it each source as a **single window**, and the existing
  per-source chunk cache and progress bar are **reused as the coarse progress
  and resume unit** — not bypassed. Chunking is a resumability/progress device;
  a native service needs neither the overlap nor the merge, but keeping one cache
  entry per source preserves the resume contract and the stage's shape.
  `parallelizable` still decides whether sources run concurrently.
- [OPEN] Whether the native service can itself emit coarse volatile progress
  (Apple's `volatileRange`) to replace the chunk-level bar is left to ticket 02.

### Bridging is per-platform, and stays in `providers`

- [DESIGN] **Apple:** `pyobjc` against the `Speech` framework, **or** a small
  **Swift helper invoked as a subprocess** (the project already runs
  subprocesses for `whisper-cli`, so a helper is a familiar execution shape).
  Ticket 02 picks one and hot-tests it.
- [DESIGN] **Windows (deferred):** `Microsoft.Windows.AI.Speech` is a Windows App
  SDK API, so a Python projection may not exist; a small **C#/C++ helper** is the
  likely bridge, with **`Windows.Media.SpeechRecognition`** as the
  lower-friction fallback. The MSIX + `systemAIModels` requirement above is what
  defers it.
- [DECISION] The bridge lives in `clear_record.providers`;
  `clear_record.core` stays **vendor-free**
  ([ADR-0003](0003-license-boundary.md),
  [ADR-0012](0012-single-distribution.md)). An extra stops being a no-op marker
  only when a real Python dependency appears. The layer DAG (`providers → core`,
  and no lower layer importing `cli`) is **unchanged**.

## Rationale

- The **interface** is the invariant, not the runtime behind it (ADR-0005's
  Update). Declaring a runtime and its capabilities on `BackendInfo` keeps the
  CLI generic: it never branches on a backend id to decide whether to probe ggml
  or how to chunk.
- A reason-bearing probe serves the user story directly — "say whether a system
  backend is available and why not" — at no extra probe cost: it is the same
  cheap check `available()` already performs.
- Reusing one cache entry per source rather than bypassing the cache keeps a
  single resume/merge path, so ticket 02 adds an adapter, not a second pipeline.

## Discarded alternatives

- **Carve native paths into the CLI** (special-case `apple-speech` selection or
  probing in `cli.py`) — rejected: it bends the one-interface/vendor-free
  boundary and hides the runtime behind id checks.
- **Extend `_WhisperCliBackend`** to carry a native backend — rejected: a native
  backend is not a `whisper-cli` subprocess and must not inherit a ggml plugin
  probe or an `-ojf` JSON parse.
- **Register stub `apple-speech` / `windows-ai` backends now** — rejected:
  always-unavailable catalog entries are noise, and the resolver already handles
  an absent id.
- **Bypass the chunk cache entirely** for a streaming backend — rejected: a
  second resume path for no benefit at this scale.
- **Adopt Intel's in-process OpenVINO GenAI as a second runtime family** — [FACT]
  not needed: the Intel research note finds every Intel path reachable through
  the existing ggml seam
  (`../research/2026-09-14-intel-transcription.md`), and ADR-0005's Update
  already records that the OpenVINO-GenAI question is answered "do not adopt".

## Consequences / review hook

- **Ticket 02 (Apple)** adds one adapter in `providers` (`runtime="system"`,
  `chunked=False`, its own `availability()` reason and `prepare()`), registers it
  as `apple-speech`, and adds an opt-in hot test. This seam already answers which
  probes apply, how availability is reported, and how chunking behaves.
- **Ticket 03 (Windows)** is deferred, not dropped; it is gated on the
  MSIX/`systemAIModels` packaging decision (ADR-0014), not on this seam.
- `clear-record backends --all` now reports **why** a `whisper-cli` backend is
  unavailable; the same line will report a system backend's
  OS-version/capability/asset reason.
- A new backend still costs "one adapter + one `available()` probe" (ADR-0005);
  this record only makes the second **kind** of adapter expressible.
- `[OPEN]`s carried forward from the tracker spec, to close in ticket 02/03:
  - **Glossary** — Apple `AnalysisContext` custom vocabulary; a Windows phrase
    list is unverified. If neither maps cleanly, the glossary must stop biasing
    that backend **visibly**, not silently.
  - **Confidence** — Apple reports it on attributed results; Windows'
    per-segment confidence is unverified. Where absent, `Segment.confidence`
    stays `None` rather than being invented.
  - **Language/locale coverage** differs from whisper's: a `--language` the
    backend cannot serve must fail with an actionable error, not silently
    auto-detect.

## Update (2026-09-15) — the Apple adapter lands; the bridge is a Swift helper

This closes ticket 02 (`apple-speech`) and the Apple half of the carried-forward
`[OPEN]`s. Ticket 03 (`windows-ai`) stays deferred.

### Bridging: a Swift helper, because the API is Swift-only

- [FACT] The macOS 26 `Speech` framework's Objective-C headers expose **only**
  the legacy `SFSpeechRecognizer` classes. `SpeechAnalyzer`,
  `SpeechTranscriber`, `AssetInventory` and `AnalysisContext` are declared in the
  framework's `.swiftinterface` with **no `@objc` bridging** (only the shared
  `@objc deinit`; `AssetInstallationRequest` is the lone `NSObject`). A `pyobjc`
  projection therefore cannot reach them.
- [DECISION] **The Apple bridge is a small Swift helper invoked as a subprocess**
  — the `pyobjc` option in the seam above is **infeasible**, not merely
  dispreferred. `providers/apple_speech_helper.swift` ships in the wheel and is
  compiled once (`xcrun --find swiftc`, `-parse-as-library`) into the platform
  cache directory; `CR_APPLE_SPEECH_HELPER` points at a prebuilt helper instead
  (e.g. a frozen app bundle). A subprocess is the project's familiar shape
  (`whisper-cli`), and it keeps the vendor code in `providers`.
- [DECISION] **No Python dependency is added.** The `apple-speech` extra stays a
  **no-op marker** (like `apple`/`nvidia`/`amd`): no `pyobjc`-family wheel — which
  does not build on Linux/Windows — enters the graph, so those installs are
  untouched. The toolchain, not a package, is the optional prerequisite.

### Capabilities, provisioning and the carried-forward opens

- [DECISION] The adapter declares `runtime="system"`, `chunked=False`,
  `parallelizable=False` (one in-process analyzer session) and
  `decoder_knobs=()`; the whisper-cli plugin probe reports not-applicable.
- [DECISION] `available()` is a **cheap** probe: platform, macOS major version,
  then the helper's `SpeechTranscriber.isAvailable` (cached per process). It never
  downloads an asset. The helper is compiled once and cached the first time a
  probe needs it — a one-time local cost, not an asset download; the "cheap"
  rule exists to keep `available()` off the network and off a multi-GB model.
  A missing OS version or a missing Swift toolchain reports
  `unavailable` **with the fix** (`xcode-select --install`) — never an
  `ImportError` traceback.
- [DECISION] `prepare()` performs the `AssetInventory`
  install/reserve. The seam's `prepare(model, model_dir)` has **no language
  argument**, so it provisions the **current system locale**; an explicit
  `--language` outside it is provisioned on first `transcribe()`. The backend is
  serialized, so there is no install race, and once provisioned transcription is
  offline.
- [DECISION] **Glossary: supported**, biasing via
  `AnalysisContext.contextualStrings[.general]` (Apple documents it as a bias, not
  a constraint) — closing that `[OPEN]` for Apple.
- [DECISION] **Confidence: populated where Apple provides it**, read from the
  `transcriptionConfidence` attributed-string attribute and averaged over a
  segment's runs; `None` otherwise. It is never invented.
- [DECISION] **Language: gated**, resolved through
  `SpeechTranscriber.supportedLocale(equivalentTo:)`; a hint it cannot serve
  fails with an actionable error rather than silently auto-detecting.
- [DESIGN] Committed timings use the finalized `Result.range` (falling back to
  the per-run `audioTimeRange` attributes when the overall range is unusable);
  volatile results drive progress only. Whether the volatile range **replaces**
  the chunk-level bar is still open, but the per-source cache chunk already
  serves as the coarse progress/resume unit.
- [FACT] Hot-verified on macOS 26.6 / Apple Silicon: the real adapter transcribes
  a synthesized clip into timed, source-attributed segments with per-segment
  confidence (the `test_hot_real_transcription` test, opt-in via
  `CR_APPLE_SPEECH_HOT=1`).
