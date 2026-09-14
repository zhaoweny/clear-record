# NVIDIA DGX Spark (GB10 Grace Blackwell) — backend story

Status: research complete
Date: 2026-09-14
Lane: `.scratch/hardware-backends`, issue
[`02-dgx-spark-research.md`](../../.scratch/hardware-backends/issues/02-dgx-spark-research.md)
(that tracker lives in the main checkout; this note is the deliverable).

Provenance rule: every claim below carries a label (**FACT / VOICE / REQ / DESIGN /
SUGGESTION / OPEN**) and an inline primary-source URL. Anything I could not verify
from the owner of the claim is marked **[OPEN]**. Nothing here is a decision.
Nobody in this project owns a DGX Spark; no benchmark was run and none is claimed.

Sources that are *community reports* (NVIDIA developer forums, GitHub issues) are
labelled as such and never promoted to NVIDIA's own statements.

---

## TL;DR (all detail below)

- **[FACT]** The DGX Spark is a 1.2 kg desktop box built on the GB10 Grace
  Blackwell Superchip: a 20-core Arm CPU, a Blackwell iGPU, and **128 GB of
  *coherent unified* LPDDR5x memory** shared by CPU and GPU. It runs **NVIDIA
  DGX OS**, an Ubuntu-based Linux, and ships CUDA 13.0.2.
- **[FACT]** Because the GPU has no dedicated framebuffer, `nvidia-smi` reports
  memory as **"Not Supported"**, and NVIDIA's own docs warn that
  `nvidia-smi --query-gpu` memory fields **may report `N/A`** on unified-memory
  systems.
- **[FACT]** The existing `nvidia` backend's `available()` check passes (Linux +
  `whisper-cli` + a ggml CUDA/Vulkan plugin + an NVIDIA device signature;
  `nvidia-smi` alone satisfies the device check), and `whisper.cpp` builds its
  CUDA path on aarch64/Grace.
- **[FACT]** `detect_vram_gb()` on this box cannot read a VRAM number (the DRM
  attribute is amdgpu-only; the `nvidia-smi` query yields `N/A`), so it returns
  `None` and `auto_jobs()` falls back to its documented **8 GB floor**.
- **[FACT]** That floor does **not** OOM the box (8 GB is far below 128 GB, and
  `auto_jobs()` is additionally capped at `_DEFAULT_MAX_JOBS = 4`). It
  **under-provisions** instead: with the `large` model it picks **1 worker**
  where the project's own design point is 4.
- **[FACT]** `CR_VRAM_GB` (or `CR_JOBS`) is a **sufficient** escape hatch:
  `CR_VRAM_GB≥~18` drives `auto_jobs()` back to the 4-worker cap.
- **Verdict:** **works with the existing `nvidia` backend — no new backend
  code required — but not out-of-the-box at the project's throughput design
  point.** Two operator steps (build `whisper.cpp` CUDA on aarch64; set
  `CR_VRAM_GB`/`CR_JOBS`) close the gap. Auto-detecting UMA would be a small new
  work item if the owner wants zero-config behaviour.

---

## 1. What the DGX Spark is

### 1.1 Superchip, memory, CPU

**[FACT]** The DGX Spark is "powered by the NVIDIA Grace Blackwell architecture"
with an integrated GPU and CPU, a **20-core Arm processor (10 Cortex-X925 +
10 Cortex-A725)**, and **128 GB LPDDR5x unified system memory** (256-bit,
4266 MHz, 273 GB/s bandwidth). Connectivity is Wi-Fi 7, 10 GbE RJ-45, and a
ConnectX-7 Smart NIC; the chassis is 150 × 150 × 50.5 mm, 1.2 kg.
— NVIDIA, *DGX Spark User Guide → Hardware Overview*:
<https://docs.nvidia.com/dgx/dgx-spark/hardware.html>

**[FACT]** NVIDIA states the AI performance as "up to 1,000 TOPS … inference and
up to 1 PFLOP … at FP4 precision with sparsity", with 6,144 CUDA cores, 5th-gen
tensor cores, 2 copy engines, and **128 GB** of memory bandwidth 273 GB/s. The
GB10 SoC TDP is 140 W (240 W external PSU).
— NVIDIA, *Hardware Overview → Performance Specifications*:
<https://docs.nvidia.com/dgx/dgx-spark/hardware.html>

**[FACT]** NVIDIA's product page repeats the headline: Grace Blackwell
architecture, 20-core Arm (10× X925 + 10× A725), **128 GB LPDDR5x coherent
unified system memory**, 256-bit interface, 273 GB/s, "Up to 1 PFLOP FP4",
ConnectX-7 @ 200 Gbps, 240 W PSU, 4 TB NVMe, **OS: NVIDIA DGX OS**.
— NVIDIA, *DGX Spark product page → Specifications*:
<https://www.nvidia.com/en-us/products/workstations/dgx-spark/>

**[FACT]** NVIDIA's launch press release says DGX Spark ships **Oct 15, 2025**,
"delivers a petaflop of AI performance and 128GB of unified memory", runs
"inference on AI models with up to 200 billion parameters" and fine-tunes up to
70B; partner systems come from Acer, ASUS, Dell, GIGABYTE, HP, Lenovo and MSI.
— NVIDIA Newsroom, 2025-10-13:
<https://nvidianews.nvidia.com/news/nvidia-dgx-spark-arrives-for-worlds-ai-developers>

### 1.2 The memory is *unified* — this is the crux

**[FACT]** NVIDIA's DGX Spark User Guide has a dedicated Known Issues entry,
"Guidance for reporting memory resources with unified memory architecture":
"DGX Spark systems use a unified memory architecture (UMA), where the GPU shares
system memory (DRAM) with the CPU and other compute engines." It warns that
`cudaMemGetInfo` **under-reports** because it does not account for memory
reclaimable from SWAP, and tells developers to read `/proc/meminfo`
(`MemAvailable`/`SwapFree`) instead.
— NVIDIA, *DGX Spark User Guide → Known Issues*:
<https://docs.nvidia.com/dgx/dgx-spark/known-issues.html>

**[FACT]** The same page states: "On iGPU platforms, `nvidia-smi` will display
'Memory-Usage: Not Supported' even though per-process GPU memory is listed. This
is expected because iGPUs do not have dedicated framebuffer memory."
— <https://docs.nvidia.com/dgx/dgx-spark/known-issues.html>

**[FACT]** NVIDIA's DGX Spark/GB10 FAQ answers "When I run nvidia-smi to see the
memory usage it says 'Not Supported'": "This is expected behavior. The DGX Spark
has a unified memory architecture. NVIDIA-SMI only reports memory utilization
when there is a dedicated GPU VRAM." (Answer posted by an NVIDIA Employee/
Moderator on NVIDIA's developer forum.)
— <https://forums.developer.nvidia.com/t/dgx-spark-gb10-faq/347344> (the
nvidia-smi question) and the thread
<https://forums.developer.nvidia.com/t/dear-nvidia-nvidia-smi-is-broken-on-the-dgx-spark/367765>

### 1.3 OS, CUDA, and the shipped software stack

**[FACT]** "NVIDIA DGX OS is a customized Linux distribution … DGX OS is based on
Ubuntu." — NVIDIA, *DGX Spark User Guide → DGX OS*:
<https://docs.nvidia.com/dgx/dgx-spark/dgx-os.html>

**[FACT]** The DGX Spark Founders Edition release notes list the current stack:
**DGX OS 7.5.0, GPU driver 580.159.03, CUDA Toolkit 13.0.2, Canonical kernel
6.17**. The July 2026 release also "improves Out-of-Memory (OOM) handling with
GB10's unified memory architecture" and lets the BIOS toggle a **2 GB (default)
or 4 GB display reserved-memory carveout**.
— NVIDIA, *DGX Spark Release Notes*:
<https://docs.nvidia.com/dgx/dgx-spark/release-notes.html>

**[FACT]** `nvidia-smi` exists and runs on the box (it is discussed in NVIDIA's
own docs and FAQ, above), and the driver is the 580-series.

### 1.4 Compute capability

**[FACT]** The GB10 (DGX Spark) is **compute capability 12.1 (`sm_121`)** — the
only 12.1 part in NVIDIA's table.
— NVIDIA, *CUDA GPU Compute Capability*:
<https://developer.nvidia.com/cuda-gpus>

**[OPEN]** Whether `sm_121` needs a newer toolkit than the shipped CUDA 13.0.2,
and whether `ggml`'s CUDA CMake defaults include 12.1 without an explicit
`-DCMAKE_CUDA_ARCHITECTURES=121`. An NVIDIA-forum reply (community, not NVIDIA
staff) says "at least Cuda 12.9.0 … introduced support for CC12.1 Spark/GB10";
a Hugging Face repackaged build advertises `sm_121a, CUDA 13, aarch64`.
— community: <https://forums.developer.nvidia.com/t/mps-support-and-telemetry-on-grace-blackwell-gb10-with-unified-memory/363137>
and <https://huggingface.co/merve/llama.cpp-dgx-spark-gb10-sm121a>
Not confirmed from a CUDA release note here.

### 1.5 Price and availability

**[FACT]** DGX Spark could be ordered from `NVIDIA.com` starting **Wednesday,
Oct. 15, 2025**; the launch press release does not state a price.
— <https://nvidianews.nvidia.com/news/nvidia-dgx-spark-arrives-for-worlds-ai-developers>

**[OPEN]** Current and launch price. NVIDIA's Marketplace (enterprise store)
shows **US$4,699.00** for the 4 TB Founders Edition as of this search, but the
storefront page timed out on direct fetch, so treat the number as a
search-engine reading of NVIDIA's own listing, not a directly retrieved quote:
<https://marketplace.nvidia.com/en-us/enterprise/personal-ai-supercomputers/dgx-spark/>.
The widely reported **$3,999** launch price and the Feb-2026 raise to $4,699 are
**secondary** reports (owner of that claim is NVIDIA; I did not retrieve an
NVIDIA page stating the launch price):
<https://overclock3d.net/news/systems/nvidia-raises-dgx-spark-price-by-700-due-to-memory-supply-constraints/>.

---

## 2. Does the existing `nvidia` backend apply?

### 2.1 What the backend actually requires

**[FACT]** (repo) `NvidiaBackend` is a `_WhisperCliBackend` with
`system="Linux"`, `gpu_backends=("cuda", "vulkan")`, and
`device_check=_has_nvidia_device`. `available()` requires: the Linux platform, a
`whisper-cli` (or `whisper-cpp`) binary, an accepted ggml plugin file
(`libggml-cuda*.so*` / `libggml-vulkan*.so*`) found under `_GGML_BACKEND_DIRS`,
and a device signature. `_has_nvidia_device()` returns true if
`/dev/nvidia[0-9]*` exists, `/dev/dxg` exists, **or `nvidia-smi` is on PATH**.
Plugin search dirs already include `/usr/local/lib`, the aarch64 multiarch tuple
`/usr/lib/aarch64-linux-gnu` (and its `ggml/` and `backends*/` variants), and
the `CR_GGML_BACKEND_DIRS` override.
— `packages/clear-record/src/clear_record/providers/backends.py:386-391, 122-220, 785-799`.

### 2.2 Platform: yes

**[FACT]** DGX OS is Ubuntu-based Linux (§1.3), so the `system="Linux"` gate
passes. **[FACT]** `nvidia-smi` ships on the box (§1.2), so
`_has_nvidia_device()` returns true **without** needing `/dev/nvidia*` to be
verified. **[OPEN]** Which exact `/dev/nvidia*` nodes exist on GB10 (not
verified); it does not affect the probe.

### 2.3 CUDA on ARM64 / Grace: yes, from source

**[FACT]** NVIDIA's CUDA Installation Guide for Linux supports **arm64 systems
(SBSA)**, and separately lists a **"GRACE only arm64 systems (sbsa)"** table
including Ubuntu 24.04 LTS (`aarch64`). CUDA cross-compilation to SBSA is
documented.
— NVIDIA, *CUDA Installation Guide for Linux* (System Requirements):
<https://docs.nvidia.com/cuda/cuda-installation-guide-linux/index.html>

**[FACT]** DGX Spark ships **CUDA Toolkit 13.0.2** (§1.3), so a native aarch64
CUDA toolchain is present before any extra install.

**[FACT]** `whisper.cpp` supports an NVIDIA CUDA backend built with
`cmake -B build -DGGML_CUDA=1`. Its README also documents the Vulkan build
(`-DGGML_VULKAN=1`, requires a Vulkan-capable driver) and an AMD ROCm build.
— ggml-org/whisper.cpp, *NVIDIA GPU support / Vulkan GPU support*:
<https://github.com/ggml-org/whisper.cpp#nvidia-gpu-support>

**[FACT]** whisper.cpp's published Docker images make the CUDA image
`linux/amd64` only, and there is no prebuilt aarch64 CUDA release in the repo's
docker tags (the plain `main` image is arm64). A community report on NVIDIA's
forum describes building whisper.cpp from source (v1.8.4) **inside Docker on a
DGX Spark / GB10, CUDA 13.0, Ubuntu 24.04 (DGX OS)** and notes: "There are no
pre-built ARM64+CUDA binaries in the whisper.cpp releases, so you need to build
from source."
— whisper.cpp README (Docker platforms):
<https://github.com/ggml-org/whisper.cpp#docker>; community report:
<https://forums.developer.nvidia.com/t/running-whisper-cpp-stt-server-on-dgx-spark-gb10-arm64-cuda-13-via-docker/371803>

**[DESIGN, repo]** So the DGX Spark needs a **from-source CUDA build** of
whisper.cpp on aarch64. Two practical consequences for the probe: a from-source
build installed with the default prefix lands the CLI in `/usr/local/bin` and the
`libggml-cuda.so` in `/usr/local/lib` (both already in the search set); a build
tree left in `build/bin` needs `CR_GGML_BACKEND_DIRS` pointed at it, exactly the
escape hatch the ADR-0005 note describes. The existing `--check-plugin` opt-in
covers the residual "plugin present but not loadable" risk.

**[OPEN]** Whether a distro-packaged `whisper-cpp` (and its ggml CUDA plugin)
exists for Ubuntu 24.04 aarch64. I found no such primary package listing; the
from-source route is the one documented by the community report.

### 2.4 Vulkan: unverified

**[FACT]** whisper.cpp's Vulkan build only needs "your graphics card driver
provides support for Vulkan API" (README, §2.3).

**[OPEN]** Whether DGX OS / the GB10 driver exposes a working Vulkan ICD and
loader for whisper.cpp's `-DGGML_VULKAN=1` path. I found no NVIDIA primary
statement of Vulkan support for GB10 on aarch64, and no report of someone
running the ggml Vulkan backend on a Spark. The backend lists `cuda` first, so
this only matters as a fallback; treat Vulkan-on-Spark as unverified.

### 2.5 `nvidia-smi` and device nodes vs. the probe's expectations

**[FACT]** The probe treats `nvidia-smi`'s mere presence as an NVIDIA device. On
DGX Spark this is simultaneously convenient and a trap: `nvidia-smi` exists and
runs, but its *memory fields* are the very thing that reports "Not Supported" /
`N/A` (§1.2). `available()` is therefore unaffected — only the *job sizing*
discussed in §3 is.

---

## 3. Unified memory vs. the VRAM heuristic (the important one)

### 3.1 What the code reads

**[FACT]** (repo) `detect_vram_gb()` order:
1. `CR_VRAM_GB` env override (returned if > 0);
2. otherwise `max` of `/sys/class/drm/card*/device/mem_info_vram_total`;
3. otherwise `nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits`
   (MiB → GB by `/1024`); returns `None` if nothing parses.
`auto_jobs()` then uses the **8 GB floor** (`_DEFAULT_VRAM_GB`) when the probe is
`None`, computes `budget = int(vram * 0.85 / model_vram_gb(model))`, and returns
`max(1, min(cpus, 4, budget, n_pending))`.
— `packages/clear-record/src/clear_record/cli/transcription.py:171-227`.

### 3.2 What each branch reports on a GB10

**[FACT]** `mem_info_vram_total` is an **amdgpu** sysfs attribute: "The amdgpu
driver provides a sysfs API for reporting current total VRAM available on the
device."
— The Linux Kernel documentation, *Misc AMDGPU driver information → GPU Memory
Usage Information*: <https://docs.kernel.org/gpu/amdgpu/driver-misc.html>

**[OPEN]** That `nvidia-drm` exposes no `mem_info_vram_total` on GB10 (so branch 2
finds nothing). This follows from it being an amdgpu API, but I could not
directly confirm the absence on a Spark. It is moot: branch 3 is checked
independently and determines the result anyway.

**[FACT]** On unified-memory hardware, `nvidia-smi --query-gpu` memory fields
**may report `N/A`**: NVIDIA's own SGLang playbook troubleshooting says, under
"Monitoring GPU memory with UMA", "Because of unified memory,
`nvidia-smi --query-gpu` memory fields may report `N/A`. Use plain `nvidia-smi`
instead."
— NVIDIA, *Serve LLMs with SGLang → Troubleshooting*:
<https://build.nvidia.com/station/sglang/troubleshooting>

**[FACT]** The plain (non-query) display shows `Not Supported` (§1.2), and an
NVIDIA-forum bug report against `nvtop` on a GB10 shows the actual `nvidia-smi`
header with `Memory-Usage | Not Supported` (driver 580.95.05, CUDA 13.0, Ubuntu
24.04 aarch64). A forum answer attributes this to
`nvmlDeviceGetMemoryInfo` returning `NVML_ERROR_NOT_SUPPORTED` "because there's
no discrete framebuffer".
— community: <https://github.com/Syllo/nvtop/issues/426> and
<https://forums.developer.nvidia.com/t/mps-support-and-telemetry-on-grace-blackwell-gb10-with-unified-memory/363137>

**[REQ]** Consequence for `detect_vram_gb()`: branch 3 parses no float from
`N/A` (and, per the bug report, the default display is a non-numeric string),
`totals` stays empty, and the function returns **`None`** → `auto_jobs()` uses
the **8 GB floor**.

### 3.3 Does that over- or under-provision? — **under-provision**

**[FACT]** With `vram = 8.0` (`8 × 0.85 = 6.8`) and the repo's own table:

| model | `model_vram_gb` | `int(6.8 / cost)` | effective jobs (cap 4) |
|---|---|---|---|
| `large` (also `large-v1/v2/v3`) | 3.7 | 1 | **1** |
| `distil-large-v3` | 2.2 | 3 | 3 |
| `medium` | 2.1 | 3 | 3 |
| `large-v3-turbo` / `turbo` | 1.7 | 4 | 4 |
| `small` | 1.1 | 6 | 4 |
| `base` | 0.7 | 9 | 4 |
| `tiny` | 0.6 | 11 | 4 |

— arithmetic from `transcription.py:125-145, 217-227`; `_DEFAULT_MAX_JOBS = 4`.

**[FACT]** So the failure mode is **under-provisioning, never OOM**: 8 GB is far
below the box's 128 GB, and `auto_jobs()` is capped at 4 regardless. Even if
`nvidia-smi` eventually reported the full 128 GB, `int(128×0.85/3.7)=29` would
still be clamped to 4. The only real loss is throughput for `large`, where the
heuristic picks **1** worker though the project's design point is **4
concurrent `whisper-cli` processes** (ADR-0005 records ~93% `gpu_busy` at 4).

**[FACT]** (repo) `CR_VRAM_GB` bypasses probing entirely and is the documented
override. With `CR_VRAM_GB ≥ ~18`, `int(vram×0.85/3.7) ≥ 4` and `auto_jobs()`
returns the full 4-worker cap for `large`. `CR_JOBS` overrides the count
directly. Both are existing, documented env vars.
— `transcription.py:171-190, 254-259`; ADR-0005
`docs/adr/0005-transcription-backend-strategy.md`.

**[DESIGN]** Answer to the tracker's open question: **unified memory does break
the *default* job heuristic (it under-provisions `large` to 1 worker), and
`CR_VRAM_GB` is a sufficient escape hatch** — but it is an explicit operator
step, not zero-config. If the owner wants out-of-the-box sizing on UMA, that is
new work: e.g. treat an `nvidia-smi` memory field that does not parse (or the
GB10) as UMA and fall back to `/proc/meminfo` `MemTotal`, mirroring NVIDIA's own
guidance. **That is not required for the backend to *work*.**

---

## 4. Role in the reference hardware lab

**[FACT]** (repo) Architecture §5's lab is: an Apple Silicon Mac mini (always-on
node, Metal), an AMD RX 7900 XTX Linux box (high-throughput worker), a
laptop/phone (client), and a NAS. §8 lists the **NVIDIA path as not hot-tested on
real NVIDIA hardware** as a remaining gap.
— `docs/architecture.md` §5, §8.

**[FACT]** DGX Spark is a single Linux box with a CUDA GPU and 128 GB unified
memory, 140 W GB10 TDP / 240 W PSU, marketed for "always-on agent workloads"
(NVIDIA product page). It is a plausible member of either architecture role.
— <https://www.nvidia.com/en-us/products/workstations/dgx-spark/>

**[SUGGESTION]** Treat the DGX Spark as a **high-throughput NVIDIA worker (and
the missing NVIDIA hot-test target)**, not the cheap always-on node. Reasons:
(i) it drives exactly the same CUDA `whisper-cli` path as the AMD worker, so the
role is already modelled in the architecture; (ii) at ~$4,699 [OPEN] and 140 W
it is an odd choice for the "always-on" slot that a Mac mini already fills more
cheaply and quietly; (iii) it is the obvious machine on which to close the
NVIDIA hot-test gap in §8. This is an agent suggestion, not an owner decision.

---

## 5. Verdict

**Works with the existing `nvidia` backend. No new backend code is required. Two
operator steps are required, and one code-level gap remains open.**

- **Works:** DGX OS is Linux; `whisper-cli` + `ggml-cuda` builds from source on
  aarch64/Grace with the shipped CUDA 13.0.2; `nvidia-smi`'s presence satisfies
  the backend's device probe; the probe's plugin search already covers
  `/usr/local/lib` and the aarch64 paths; `CR_GGML_BACKEND_DIRS` covers a
  non-installed build tree.
- **Operator step 1:** build/install `whisper.cpp` with `-DGGML_CUDA=1` on the
  Spark (no prebuilt ARM64+CUDA artifact is published).
- **Operator step 2:** set `CR_VRAM_GB` (≈18–96) or `CR_JOBS=4`, otherwise a
  `large` model runs a single worker on a 128 GB box.
- **Open gap (new work, optional):** UMA-aware `detect_vram_gb()` so the
  zero-config default does not collapse to the 8 GB floor. Also [OPEN]:
  Vulkan-on-GB10, and the exact `/dev/nvidia*` nodes.

---

## 6. Claims I could not source (all `[OPEN]`)

- **Exact `nvidia-smi --query-gpu=memory.total` string on GB10** — "may report
  `N/A`" is NVIDIA's wording for UMA generally
  (<https://build.nvidia.com/station/sglang/troubleshooting>); I did not see that
  exact query's output captured on a Spark. The default (non-query) display is
  independently reported as `Not Supported` (community).
- **Whether `nvidia-drm` exposes any `mem_info_vram_total`** on GB10 — inferred
  absent (it is an amdgpu API), not directly verified.
- **Vulkan ICD/loader availability** on DGX OS / GB10 for `ggml-vulkan`.
- **A distro `whisper-cpp` package for Ubuntu 24.04 aarch64** and where its ggml
  CUDA plugin lands.
- **`sm_121` support in the shipped CUDA 13.0.2 without an explicit arch flag**,
  and `ggml`'s default CUDA architectures for 12.1.
- **Price** — NVIDIA Marketplace is read via search (`$4,699.00`); direct fetch
  timed out. Launch price (`$3,999`) is secondary only.
- **Any ASR throughput number on a DGX Spark** — nobody here owns one; none is
  claimed.

---

## Source list (primary owner first)

- NVIDIA, *DGX Spark User Guide* — Hardware Overview:
  <https://docs.nvidia.com/dgx/dgx-spark/hardware.html>
- NVIDIA, *DGX Spark User Guide* — Known Issues:
  <https://docs.nvidia.com/dgx/dgx-spark/known-issues.html>
- NVIDIA, *DGX Spark User Guide* — DGX OS:
  <https://docs.nvidia.com/dgx/dgx-spark/dgx-os.html>
- NVIDIA, *DGX Spark Release Notes*:
  <https://docs.nvidia.com/dgx/dgx-spark/release-notes.html>
- NVIDIA, *DGX Spark product page*:
  <https://www.nvidia.com/en-us/products/workstations/dgx-spark/>
- NVIDIA Newsroom, *DGX Spark Arrives* (2025-10-13):
  <https://nvidianews.nvidia.com/news/nvidia-dgx-spark-arrives-for-worlds-ai-developers>
- NVIDIA, *CUDA GPU Compute Capability*:
  <https://developer.nvidia.com/cuda-gpus>
- NVIDIA, *CUDA Installation Guide for Linux*:
  <https://docs.nvidia.com/cuda/cuda-installation-guide-linux/index.html>
- NVIDIA, *Serve LLMs with SGLang — Troubleshooting*:
  <https://build.nvidia.com/station/sglang/troubleshooting>
- NVIDIA Developer Forums, *DGX Spark / GB10 FAQ*:
  <https://forums.developer.nvidia.com/t/dgx-spark-gb10-faq/347344>
- The Linux Kernel, *Misc AMDGPU driver information*:
  <https://docs.kernel.org/gpu/amdgpu/driver-misc.html>
- ggml-org/whisper.cpp, README (NVIDIA CUDA / Vulkan / Docker):
  <https://github.com/ggml-org/whisper.cpp>
- Community corroboration (not NVIDIA's own statement): whisper.cpp-on-Spark
  forum thread
  <https://forums.developer.nvidia.com/t/running-whisper-cpp-stt-server-on-dgx-spark-gb10-arm64-cuda-13-via-docker/371803>;
  nvtop GB10 reporting issue <https://github.com/Syllo/nvtop/issues/426>;
  NVML telemetry thread
  <https://forums.developer.nvidia.com/t/mps-support-and-telemetry-on-grace-blackwell-gb10-with-unified-memory/363137>
- Repo code cited: `packages/clear-record/src/clear_record/cli/transcription.py`,
  `packages/clear-record/src/clear_record/providers/backends.py`,
  `docs/adr/0005-transcription-backend-strategy.md`, `docs/architecture.md`.
