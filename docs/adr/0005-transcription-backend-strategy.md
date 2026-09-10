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
- [FACT] NVIDIA: `faster-whisper` / **CTranslate2** use **CUDA / cuBLAS /
  cuDNN**.
- [FACT] AMD Radeon: `whisper.cpp` supports **Vulkan** and **ROCm**, and targets
  the **`gfx1100`** (RX 7900 XTX) family.
- [FACT] The ASR stacks in scope are permissive (see ADR-0003).

## Decision

- [DECISION] Define a single vendor-neutral backend interface in `cr-providers`
  (`Backend`, with `info`, `available()`, `transcribe()`).
- [DECISION] Provide three backend adapters, each a **capability**:
  - `apple`   — Apple Silicon (Metal / Core ML / ANE), via `whisper.cpp`
    (`pywhispercpp`, in process);
  - `nvidia`  — NVIDIA CUDA (cuBLAS / cuDNN), via `faster-whisper` / CTranslate2;
  - `amd`     — AMD Radeon (Vulkan / ROCm), via the **system `whisper-cli`**
    (subprocess), because no PyPI wheel ships a Vulkan/HIP ggml backend.
- [DECISION] A backend is usable only when its runtime probe succeeds
  (`available()`). For the wheel-backed families the optional extra provides the
  stack; for `amd` the stack is a **system** one (`whisper-cpp` +
  `ggml-vulkan`/`ggml-hip`), so the probe requires Linux, `whisper-cli` on PATH,
  an installed ggml GPU backend plugin, and a DRM render node. `clearrecord
  backends` lists the current availability. The default dev/CI env installs
  **no** vendor framework.
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
- A single mixed "whisper.cpp for everything" path — considered; the CUDA path
  through faster-whisper/CTranslate2 is the natural high-throughput NVIDIA
  option, while whisper.cpp covers Apple + AMD. The interface leaves the
  per-family choice to the adapter.

## Consequences / review hook

- Adding a backend = one adapter + one `available()` probe (plus, for
  wheel-backed families, one optional dependency extra). Each new backend must
  stay behind the interface and keep `cr-core` vendor-free (ADR-0003).
- **Apple is proven** (Apple M4, whisper.cpp/Metal, 16 kHz normalize + 10 ms
  time-scale calibration). **AMD is proven** on an RX 7900 XTX (RADV NAVI31,
  Mesa 26.2.2): `whisper-cli` loads `libggml-vulkan.so`, and on 300 s of a real
  tape `ggml-small` ran 6.5 s wall / 6.4 s compute versus 46.0 s on CPU (~7×
  wall, encoder ~80×). The `nvidia` adapter is declared and capability-gated but
  not yet hot-tested on real hardware.
- The AMD mechanism deliberately deviates from "the extra installs the stack":
  the extra is a no-op marker and the capability is system-provided. Revisit if a
  maintained Vulkan/HIP `pywhispercpp` wheel appears (the subprocess could then
  be retired), or if a hardware family's stack changes materially.
