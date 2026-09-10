# ADR-0005 — Transcription backend strategy (Apple / NVIDIA / AMD)

Status: active
Date: 2026-09-09

## Context

- [VOICE] Support all three major desktop compute families behind **one**
  interface instead of locking to a single vendor. The owner was inspired by the
  observation (Marco Arment / Overcast) that **Mac frameworks are excellent for
  on-device transcription**, and wants the same "own the hardware, no
  subscription" property on every family.
- [FACT] Apple Silicon: `whisper.cpp` supports **Metal** and **Core ML**; a
  June-2026 community experiment reported an **ANE**-native encoder ~2× its
  Core ML path.
- [FACT] NVIDIA: `whisper.cpp`'s ggml provides a **CUDA** backend (and Vulkan).
- [FACT] AMD Radeon: `whisper.cpp` supports **Vulkan** and **ROCm**, and targets
  the **`gfx1100`** (RX 7900 XTX) family.
- [FACT] The ASR stacks in scope are permissive (see ADR-0003).

## Decision

- [DECISION] Define a single vendor-neutral backend interface in `cr-providers`
  (`Backend`, with `info`, `available()`, `transcribe()`).
- [DECISION] Provide three backend adapters, each a **capability**:
  - `apple`   — Apple Silicon (Metal / Core ML / ANE), via `whisper.cpp`
    (`pywhispercpp`, in process);
  - `nvidia`  — NVIDIA CUDA / Vulkan, via the **system `whisper-cli`** (the
    same process-isolated path as `amd`);
  - `amd`     — AMD Radeon (Vulkan / ROCm), via the **system `whisper-cli`**
    (subprocess), because no PyPI wheel ships a Vulkan/HIP ggml backend.
- [DECISION] A backend is usable only when its runtime probe succeeds
  (`available()`). The `apple` extra provides its stack; for `nvidia`/`amd` the
  stack is a **system** one (`whisper-cpp` + a ggml GPU plugin — `ggml-cuda`/
  `ggml-vulkan` for NVIDIA, `ggml-vulkan`/`ggml-hip` for AMD), so the probe
  requires Linux, `whisper-cli` on PATH, an accepted plugin (search dirs are
  overridable via `CR_GGML_BACKEND_DIRS`), and the vendor's GPU device.
  `clearrecord backends` lists the current availability. The default dev/CI env
  installs **no** vendor framework.
- [DECISION] Backends declare `parallelizable` in `BackendInfo` — true for the
  process-isolated `whisper-cli` adapters (`nvidia`, `amd`). The transcribe stage
  feeds pending chunks through a bounded worker pool for those and serializes
  in-process ones (Apple), which share model state.
- [DECISION] `cr-core` never imports a vendor stack; it only depends on the
  interface. Vendor stacks are lazy-imported inside `cr-providers`.

## Rationale

- One interface + capability-gated backends maximizes coverage across hardware
  while keeping the repo and CI dependency-light.
- The "own the hardware, no subscription" property holds on every family: the
  user selects whichever backend their machine can run (Apple node, AMD ROCm
  box, NVIDIA workstation).

## Discarded alternatives

- Single-vendor (e.g. CUDA-only) — rejected: leaves the Apple Silicon node and
  the AMD ROCm box unserved, and contradicts the owner's explicit requirement.
- Hard-requiring all three stacks — rejected: burdens dev/CI and anyone without
  a GPU; backends should be optional capabilities.
- Serving NVIDIA through `faster-whisper`/CTranslate2 — the original choice (the
  natural high-throughput CUDA option), **superseded**: it runs in-process and
  holds shared model state, so it cannot share the process-isolated parallel
  path, and PyPI ships no GPU-accelerated `pywhispercpp`. NVIDIA now uses the
  same system `whisper-cli` (ggml CUDA/Vulkan) path as AMD.

## Consequences / review hook

- Adding a backend = one adapter + one `available()` probe (plus, for the Apple
  wheel, one optional dependency extra). Each new backend must stay behind the
  interface and keep `cr-core` vendor-free (ADR-0003).
- **Apple is proven** (Apple M4, whisper.cpp/Metal, 16 kHz normalize + 10 ms
  time-scale calibration). **AMD is proven** on an RX 7900 XTX (RADV NAVI31,
  Mesa 26.2.2): `whisper-cli` loads `libggml-vulkan.so`, and on 300 s of a real
  tape `ggml-small` ran 6.5 s wall / 6.4 s compute versus 46.0 s on CPU (~7×
  wall, encoder ~80×); end-to-end, 4 sources × ~80 min ran in 2:55. The
  `nvidia` adapter shares that path; its hot-test target is an RTX 4090
  (sm_89 / `ggml-cuda`).
- The NVIDIA/AMD mechanism deliberately deviates from "the extra installs the
  stack": their extras are no-op markers and the capability is system-provided.
  Revisit if a maintained GPU `pywhispercpp` wheel appears (the subprocess could
  then be retired), or if a hardware family's stack changes materially.
