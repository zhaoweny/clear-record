# ADR-0005 — Transcription backend strategy (Apple / NVIDIA / AMD)

Status: active
Date: 2026-09-09

## Context

- [VOICE] Support all three major desktop compute families behind **one**
  interface instead of locking to a single vendor. The owner was inspired by the
  observation (Marco Arment / Overcast) that **Mac frameworks are excellent for
  on-device transcription**, and wants the same "own the hardware, no
  subscription" property on every family.
- [FACT] Apple/macOS: `whisper.cpp` supports **Metal**, and also has a
  **Core ML** path; a June-2026 community experiment reported an **ANE**-native
  encoder roughly 2× that Core ML path. clear-record **implements only the Metal
  path** — Core ML and ANE are future ecosystem possibilities, not claimed
  capabilities.
- [FACT] NVIDIA: `whisper.cpp`'s ggml provides a **CUDA** backend (and Vulkan).
- [FACT] AMD Radeon: `whisper.cpp` supports **Vulkan** and **ROCm**, and targets
  the **`gfx1100`** (RX 7900 XTX) family.
- [FACT] The ASR stacks in scope are permissive (see ADR-0003).

## Decision

- [DECISION] Define a single vendor-neutral backend interface in `cr-providers`
  (`Backend`, with `info`, `available()`, `prepare()`, `transcribe()`).
- [DECISION] Provide three backend adapters, each a **capability**:
  - `apple`   — Apple/macOS (Metal), via the **system `whisper-cli`**
    (`brew install whisper-cpp`) linked against a `ggml` Metal plugin; the ggml
    model is auto-downloaded on first use;
  - `nvidia`  — NVIDIA CUDA / Vulkan, via the **system `whisper-cli`** (the
    same process-isolated path as `amd`);
  - `amd`     — AMD Radeon (Vulkan / ROCm), via the **system `whisper-cli`**
    (subprocess), because no PyPI wheel ships a Vulkan/HIP ggml backend.
- [DECISION] A backend is usable only when its runtime probe succeeds
  (`available()`). All three families use a **system** stack (`whisper-cpp` + a
  ggml GPU plugin — `ggml-metal` on macOS, `ggml-cuda`/`ggml-vulkan` for NVIDIA,
  `ggml-vulkan`/`ggml-hip` for AMD), so the probe requires the platform
  (`Darwin` for Apple, `Linux` for the others),
  `whisper-cli` on PATH (Homebrew bin dirs are a fallback), an accepted plugin
  (search dirs are overridable via `CR_GGML_BACKEND_DIRS`), and — on Linux — the
  vendor's GPU device. Metal needs no separate device probe: the plugin plus the
  CLI *is* the check. `clearrecord backends` lists the current availability (the
  command was renamed to `clear-record` on 2026-09-13 — see ADR-0009). The
  default dev/CI env installs **no** vendor framework, and the `apple` extra
  (like `nvidia`/`amd`) is a no-op marker.
- [DECISION] Backends declare `parallelizable` in `BackendInfo` — true for the
  process-isolated `whisper-cli` adapters (`apple`, `nvidia`, `amd`). The
  transcribe stage feeds pending chunks through a bounded worker pool for those
  and serializes in-process ones. The auto fan-out is bounded by the model size
  against the detected VRAM (8 GB assumed when unprobed; `--jobs` / `CR_JOBS`
  override), and an interrupted pool cancels queued chunks, terminates in-flight
  `whisper-cli` children and leaves the chunk cache resumable. Cancellation is
  **scoped**: the pool injects an explicit `CancellableProcessRunner` into the
  backend (`cr_providers.process`), which launches every child, rather than
  monkey-patching `subprocess.Popen` globally — so two pools in one process, or
  `cr_cli` embedded as a library, cannot interfere.
- [DECISION] `cr-core` never imports a vendor stack; it only depends on the
  interface. Vendor stacks are driven only from `cr-providers` (as a
  subprocess).

## Rationale

- One interface + capability-gated backends maximizes coverage across hardware
  while keeping the repo and CI dependency-light.
- The "own the hardware, no subscription" property holds on every family: the
  user selects whichever backend their machine can run (Apple node, AMD ROCm
  box, NVIDIA workstation).

## Discarded alternatives

- Single-vendor (e.g. CUDA-only) — rejected: leaves the Apple node and the AMD
  ROCm box unserved, and contradicts the owner's explicit requirement.
- Hard-requiring all three stacks — rejected: burdens dev/CI and anyone without
  a GPU; backends should be optional capabilities.
- Serving NVIDIA through `faster-whisper`/CTranslate2 — the original choice (the
  natural high-throughput CUDA option), **superseded**: it runs in-process and
  holds shared model state, so it cannot share the process-isolated parallel
  path. NVIDIA now uses the same system `whisper-cli` (ggml CUDA/Vulkan) path as
  AMD. `apple` follows the same reasoning: Homebrew's `whisper-cpp` links a
  Metal ggml plugin, and the in-process `pywhispercpp` wheel was retired once the
  CLI path was hot-tested (see Update below).

## Consequences / review hook

- Adding a backend = one adapter + one `available()` probe (plus, if the stack
  ever ships as a wheel, an optional dependency extra). Each new backend must
  stay behind the interface and keep `cr-core` vendor-free (ADR-0003).
- **Apple is proven** (Apple M4). It first landed on the in-process
  `pywhispercpp` wheel (16 kHz normalize + 10 ms time-scale calibration); it now
  runs **only** the system `whisper-cli` + ggml Metal path (the wheel and its
  calibration were retired — see Update). On Homebrew's `ggml` 0.23.0 the Metal
  plugin is a **`.so`** under `<prefix>/Cellar/ggml/<version>/libexec/`
  (`libggml-metal.so`), not a
  `.dylib` in `lib` — the probe globs both `libexec` and `lib`, `opt/` and
  Cellar. **AMD is proven** on an RX 7900 XTX (RADV NAVI31,
  Mesa 26.2.2): `whisper-cli` loads `libggml-vulkan.so`, and on 300 s of a real
  tape `ggml-small` ran 6.5 s wall / 6.4 s compute versus 46.0 s on CPU (~7×
  wall, encoder ~80×); end-to-end, 4 sources × ~80 min ran in 2:55. The
  `nvidia` adapter shares that path; its hot-test target is an RTX 4090
  (sm_89 / `ggml-cuda`).
- The mechanism deliberately deviates from "the extra installs the stack": for
  `apple`/`nvidia`/`amd` the extras are no-op markers and the capability is
  system-provided (`brew install whisper-cpp` on macOS; e.g. Arch `whisper-cpp`
  + a ggml plugin on Linux). The residual risk is the same for all three: the
  probe proves presence, not plugin loadability. An **opt-in** `--check-plugin`
  closes that gap with a one-shot `whisper-cli` load probe (cached per CLI
  invocation, never on the default `available()` path); on a release build whose
  CLI prints no load banner the probe reports `inconclusive` rather than failing.
- The ggml model is **auto-downloaded on first use** from
  `https://huggingface.co/ggerganov/whisper.cpp/` if it is not already in
  `model_dir` / `CR_MODELS_DIR` / `<cwd>/models`; offline, the actionable
  `hf download …` pre-fetch error is raised instead.

## Update (2026-09-11) — one CLI implementation; the wheel is retired

- [VOICE: owner] Retire `pywhispercpp`. The Apple CLI/Metal path was verified
  end-to-end on real M4 hardware (164 segments, wheel not installed).
- [DECISION] `apple` is CLI-only: the same `_WhisperCliBackend` as AMD/NVIDIA,
  with `system="Darwin"` and the `metal` family, id `apple`, and
  `parallelizable=True`. There is literally **one** CLI adapter for all three
  families, and the `apple` extra (like `nvidia`/`amd`) is a no-op marker.
- [DECISION] `_resolve_ggml_model` downloads `ggml-<name>.bin` on first use
  (owner option A, 2026-09-11): streamed to `<name>.part` in the resolved models
  dir and atomically renamed on success; a network failure rolls the part file
  back and raises the existing clear `hf download …` error. The wheel's
  auto-download behaviour is therefore preserved on the CLI path.

## Update (2026-09-13) — the interface states `prepare()`

- [DESIGN] `Backend` gains `prepare(model, model_dir)` with a no-op default
  (`BackendBase`), so the whole contract is declared rather than reached by a
  duck-typed `getattr(backend, "resolve_model")`. The `whisper-cli` adapters
  implement it (the ggml resolve/download formerly named `resolve_model`); the
  transcribe stage calls it once, single-threaded, before the chunk pool. This
  mildly extends the interface enumerated above; `docs/architecture.md` and
  `packages/providers/README.md` describe the same seam.
