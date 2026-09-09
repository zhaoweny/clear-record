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
  - `apple`   — Apple Silicon (Metal / Core ML / ANE), via `whisper.cpp`;
  - `nvidia`  — NVIDIA CUDA (cuBLAS / cuDNN), via `faster-whisper` / CTranslate2;
  - `amd`     — AMD Radeon (ROCm / Vulkan), via `whisper.cpp`.
- [DECISION] A backend is usable only when its optional dependency extra is
  installed **and** its runtime probe succeeds (`available()`). `clearrecord
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

- Adding a backend = one extra -> one adapter + one optional dependency group +
  one `available()` probe. Each new backend must stay behind the interface and
  keep `cr-core` vendor-free (ADR-0003).
- `transcribe()` is currently a declared placeholder in the scaffold; wiring a
  real backend (Apple first, per the owner's always-on Apple Silicon node) is
  slice 3 in `docs/architecture.md` §8.
- Revisit if a hardware family's recommended stack changes materially (e.g. a
  new Apple ASR runtime or an AMD CUDA-compat path).
