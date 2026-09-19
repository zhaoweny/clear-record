# Intel transcription story — Arc GPUs, Core Ultra NPUs, ggml vs OpenVINO

Status: research note (desk research, no hardware run)
Date: 2026-09-14
Ticket: the local tracker's `hardware-backends` lane, ticket 01
Provenance: every factual claim is cited to a primary source (upstream repo,
vendor doc, or license text). Items that could not be verified are labelled
`[OPEN]`.

## 0. Method

This is desk research only — nothing below was executed on Intel hardware.
Claims were checked against the upstream repositories at the commit below and
against Intel/OpenVINO's own documentation.

- `ggml-org/whisper.cpp` at `f133970bbb8c034ad9055a70afb97d61c24038f9`
  (2026-09-14; reports `WHISPER_VERSION` 1.9.4) and its vendored `ggml`
  (0.23.0). Source: <https://github.com/ggml-org/whisper.cpp>.
- `ggml-org/ggml` at `456172ec733a135778adcd32d00e576a58232e45`
  (version 0.24.0, 2026-09-14). Source: <https://github.com/ggml-org/ggml>.
- `ggml-org/llama.cpp` `docs/backend/{SYCL,OPENVINO}.md` on `master` (the
  ggml backends live in llama.cpp and are vendored into whisper.cpp).
- OpenVINO / OpenVINO GenAI docs and license files, Intel's own technical
  article and GPU-driver docs, and `intel/llvm` license text.

## 1. Verdict (summary)

- **Intel joins the backend set, and it does not need a new runtime family in
  clear-record.** Every Intel capability below is reachable through the
  *existing* `whisper-cli` + ggml-plugin shape fixed by ADR-0005, because the
  Intel-specific work sits in the `ggml` backend that `whisper-cli` loads.
  `[FACT]` The ggml SYCL, Vulkan and OpenVINO backends are all ggml backends
  loaded by the CLI: <https://github.com/ggml-org/ggml/blob/master/CMakeLists.txt>
  (`GGML_SYCL`, `GGML_VULKAN`, `GGML_OPENVINO` are all `ggml` CMake options).
- **Primary path — Vulkan.** `ggml-vulkan` is cross-vendor and already the
  hot-tested AMD path in this repo (ADR-0005); the same plugin runs on Intel
  iGPUs and Arc GPUs. `[FACT]` `whisper.cpp` documents a Vulkan build
  (`-DGGML_VULKAN=1`) with no vendor restriction:
  <https://github.com/ggml-org/whisper.cpp/blob/master/README.md#vulkan-gpu-support>.
- **Intel-native path — SYCL.** `ggml-sycl` is Intel's GPU backend, actively
  maintained upstream, but it needs the Intel oneAPI toolchain/runtime and a
  purpose-built `whisper-cli`; `whisper.cpp`'s own SYCL doc is thinner than its
  Vulkan doc. `[FACT]` Verified Intel devices are listed in the shared
  llama.cpp SYCL doc: <https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md>.
- **NPU path — only via OpenVINO's ggml backend, and it is young.** The new
  `ggml-openvino` backend translates the GGML graph and can target Intel CPUs,
  GPUs and NPUs (`GGML_OPENVINO_DEVICE=NPU`), all *inside* `whisper-cli`.
  Upstream added "whisper.cpp support" to that backend on 2026-09-04, but no
  Whisper-on-NPU validation is published. Label `[OPEN]`.
- **OpenVINO GenAI's `ASRPipeline` would work but should not be adopted.**
  It is an in-process Python/C++ library (Apache-2.0) that breaks ADR-0005's
  "every backend drives `whisper-cli`" property and re-opens the discarded
  in-process-wheel alternative. It is not required to put Intel on the map.

## 2. Q1 — Intel Arc GPUs via ggml (SYCL and Vulkan)

### 2.1 Which GPUs

`[FACT]` The ggml SYCL backend's hardware table (shared by llama.cpp and
vendored into whisper.cpp) lists as **Verified**:

| Intel GPU | Verified model |
|---|---|
| Data Center Max Series | Max 1550, 1100 |
| Data Center Flex Series | Flex 170 |
| Arc A-Series | Arc A770, A730M, A750 |
| **Arc B-Series** | **Arc B580** |
| built-in Arc GPU | Meteor Lake, Arrow Lake, Lunar Lake |
| Intel iGPU | 13700k, 13400, i5-1250P, i7-1260P, i7-1165G7 |

Source: <https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md>
(section "Verified devices"). The same doc states SYCL supports the Intel GPU
family generally (Data Center Max, Flex/Arc, built-in Arc, and iGPU in 11th Gen
Core and newer).

`[FACT]` whisper.cpp ships its *own* (older) SYCL table, which lists Data
Center Max, Flex, Arc 770, built-in Arc in Meteor Lake, and iGPUs in i5-1250P /
i7-1165G7 — it does **not** list Arc B-Series / Battlemage:
<https://github.com/ggml-org/whisper.cpp/blob/master/README_sycl.md>. Because
whisper.cpp vendors the same `ggml-sycl` source (present at
`ggml/src/ggml-sycl/`), the *backend* supports B-Series per the ggml-level
table, but **whisper.cpp's own documentation predates Battlemage** — treat
"whisper.cpp on Arc B-Series" as `[OPEN]` until hot-tested.

`[FACT]` Intel's own developer article says "The SYCL backend supports all
Intel GPUs" and that Intel verified Data Center Max/Flex, Arc Discrete,
built-in Arc in Core Ultra, and 11th–13th Gen Core iGPUs — but it is an
llama.cpp article, not whisper.cpp:
<https://www.intel.com/content/www/us/en/developer/articles/technical/run-llms-on-gpus-using-llama-cpp.html>.

`[FACT]` Vulkan is cross-vendor by construction; whisper.cpp's Vulkan section
imposes no vendor list and says only "make sure your graphics card driver
provides support for Vulkan API":
<https://github.com/ggml-org/whisper.cpp/blob/master/README.md#vulkan-gpu-support>.
On Linux, Intel GPUs are serviced by Mesa's ANV driver; on Windows by Intel's
Arc graphics driver
(<https://www.intel.com/content/www/us/en/products/docs/discrete-gpus/arc/software/drivers.html>).
`[OPEN]` I found no primary source that enumerates *which* Intel GPU
generations `ggml-vulkan` verifies in whisper.cpp; the upstream llama.cpp
discussion thread reports Arc B580 working with a new Mesa/ANV:
<https://github.com/ggml-org/llama.cpp/discussions/12570>.

### 2.2 Build flags

`[FACT]` SYCL (from whisper.cpp's `README_sycl.md`, its `examples/sycl/build.sh`
and CI `build-sycl.yml`):

```
cmake .. -DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx          # FP32
cmake .. -DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx -DGGML_SYCL_F16=ON   # FP16
```

`WHISPER_SYCL` / `WHISPER_SYCL_F16` are deprecated aliases that warn and map to
`GGML_SYCL` (`CMakeLists.txt` lines 146–147). Runtime device choice:
`GGML_SYCL_DEVICE=<id>` (default 0), or `ONEAPI_DEVICE_SELECTOR` per the
llama.cpp doc. Sources:
<https://github.com/ggml-org/whisper.cpp/blob/master/README_sycl.md>,
<https://github.com/ggml-org/whisper.cpp/blob/master/examples/sycl/build.sh>.

`[FACT]` Vulkan, from whisper.cpp's README:

```
cmake -B build -DGGML_VULKAN=1
cmake --build build -j --config Release
```

Source: <https://github.com/ggml-org/whisper.cpp/blob/master/README.md#vulkan-gpu-support>.

`[FACT]` `ggml` defines both options itself: `option(GGML_SYCL ...)` and
`option(GGML_VULKAN ...)`:
<https://github.com/ggml-org/ggml/blob/master/CMakeLists.txt>.

### 2.3 Maturity / maintenance

- `[FACT]` **SYCL is actively maintained upstream.** In the `ggml` checkout
  (2026-09-14), recent `src/ggml-sycl/` commits include "sycl: rfc: Use radix
  select for top_k (llama/28670)", "sycl : fix oneDNN scratchpad breaking the
  pool free order (llama/28704)", and "sycl: add a batched L2_NORM kernel
  (llama/28222)" — i.e. daily traffic mirrored from llama.cpp.
- `[FACT]` The `ggml-sycl` source is Intel-authored under MIT: the header reads
  "MIT license / Copyright (C) 2024 Intel Corporation / SPDX-License-Identifier:
  MIT":
  <https://github.com/ggml-org/ggml/blob/master/src/ggml-sycl/ggml-sycl.cpp>.
- `[FACT]` **whisper.cpp's SYCL CI only *builds*, and is allowed to fail.**
  `.github/workflows/build-sycl.yml` sets `continue-on-error: true` and runs
  CMake builds across several container arches; it does not execute inference
  on an Intel device:
  <https://github.com/ggml-org/whisper.cpp/blob/master/.github/workflows/build-sycl.yml>.
- `[FACT]` whisper.cpp's own SYCL doc still lists two open TODOs ("Support to
  build in Windows"; "Support multiple cards") and a known startup hang
  workaround (`--no-mmap`):
  <https://github.com/ggml-org/whisper.cpp/blob/master/README_sycl.md>.
- `[FACT]` `ggml-vulkan` is also actively maintained (in the same checkout:
  "vulkan: workaround NV queuesubmit driver bug (llama/28830)", "vulkan: fix
  data race and OOB access in argsort(large) (llama/28705)").
- `[FACT]` whisper.cpp publishes a Vulkan Docker image
  (`ghcr.io/ggml-org/whisper.cpp:main-vulkan`):
  <https://github.com/ggml-org/whisper.cpp/blob/master/README.md>.
- `[OPEN]` There is no published **whisper.cpp-on-Intel-GPU** performance or
  hot-test result in the upstream repos; the only Intel-facing benchmarks I
  found were Intel's llama.cpp article and third-party llama.cpp discussions.

### 2.4 What is *not* a GPU path

`[FACT]` SYCL is a **GPU** backend; it does not drive the Intel NPU (there is
no SYCL/layer-zero path to the NPU in ggml). The NPU story is OpenVINO
(§3). `[FACT]` whisper.cpp *does* have a non-Intel NPU path via AMD VitisAI,
which reinforces the asymmetry — there is an "AMD Ryzen AI NPU" section in the
README but **no Intel NPU section**:
<https://github.com/ggml-org/whisper.cpp/blob/master/README.md#amd-ryzen-ai-npu-support>.

## 3. Q2 — Intel NPUs (Core Ultra) and OpenVINO's NPU plugin

### 3.1 Is there a ggml/NPU path?

`[FACT]` Yes — but only via `ggml-openvino`, the OpenVINO-backed ggml backend.
It translates a GGML compute graph into an OpenVINO graph and exposes a device
selector. Documented supported hardware: "Intel CPUs, Intel GPUs (integrated
and discrete), Intel NPUs":
<https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/OPENVINO.md>.
Device is chosen at runtime with `GGML_OPENVINO_DEVICE=CPU|GPU|NPU`; on NPU the
backend enables static compilation and NPUW:
<https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/OPENVINO.md>.

`[FACT]` `ggml-openvino` is vendored into whisper.cpp
(`ggml/src/ggml-openvino/`), and the backend registers as a **GPU-type** device
(`GGML_BACKEND_DEVICE_TYPE_GPU` in `ggml-openvino/ggml-openvino.cpp`), so
whisper.cpp's GPU selection can pick it. Upstream commit `7f78e1b` (2026-09-04,
"OpenVINO: Update OV to 2026.3.1, whisper.cpp support, …", authored by Intel)
added "OpenVINO backend: Support Whisper.cpp". `[OPEN]` Upstream publishes **no
validated Whisper model on the OpenVINO backend**, and the OPENVINO.md
validated-model table contains only LLMs. So: whisper-on-Intel-NPU via
`whisper-cli` is *plausible and in-tree*, but **unverified**.

`[FACT]` `ggml-openvino` is part of `ggml` (MIT), but links the Apache-2.0
OpenVINO runtime:
<https://github.com/ggml-org/ggml/blob/master/src/ggml-openvino/CMakeLists.txt>
(`find_package(OpenVINO REQUIRED ...)`).

### 3.2 What OpenVINO's NPU plugin actually supports

`[FACT]` Supported platforms (integrated NPUs), per the plugin's own README:
Meteor Lake (NPU 3720), Arrow Lake (NPU 3720), Lunar Lake (NPU 4000), Panther
Lake (NPU 5010), Wildcat Lake (NPU 5020), Nova Lake (NPU 6010); OSes Ubuntu 22,
Ubuntu 24, Windows 11:
<https://github.com/openvinotoolkit/openvino/blob/master/src/plugins/intel_npu/README.md>.

`[FACT]` Stated limitations from the same README:

- "**Dynamic shapes are not supported by the NPU plugin yet.**"
- Only **one** device is enumerated ("NPU plugin does not currently support
  multiple devices").
- Inference **precision** internal to primitives is FP16.
- The compiler/plugin path is new (Compiler-In-Plugin preview in 2026.0,
  preferred from 2026.1) and the README notes a Meteor Lake fallback for older
  drivers.

`[FACT]` The user-facing NPU device doc adds that NPU support "is still under
active development and may offer a limited set of supported OpenVINO features"
and that offline compilation/blobs are for development only:
<https://docs.openvino.ai/2025/openvino-workflow/running-inference/inference-devices-and-modes/npu-device.html>.

`[FACT]` In the `ggml-openvino` backend, the NPU path is **stateless only** and
specializes around `Q4_0`-class quantization; `GGML_OPENVINO_CACHE_DIR` is "not
supported on NPU devices":
<https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/OPENVINO.md>.

`[OPEN]` Whether whisper.cpp's encoder/decoder graph meets the NPU plugin's
static-shape constraints (whisper uses fixed 30 s windows but variable token
counts) is unverified. The general `ggml-openvino` note that "the NPU operates
in stateless mode only" and the plugin's static-shape requirement make this the
main technical risk for a whisper-on-NPU claim.

## 4. Q3 — OpenVINO GenAI's Whisper pipeline

### 4.1 Capability and devices

`[FACT]` OpenVINO GenAI's `ASRPipeline` (formerly `WhisperPipeline`) performs
Whisper ASR with: automatic or forced language, translation, segment
timestamps, word-level timestamps, long-form audio via automatic 30 s sliding
windows, `initial_prompt` and `hotwords`, plus perf metrics. It expects
normalized 16 kHz WAV input:
<https://openvinotoolkit.github.io/openvino.genai/docs/use-cases/speech-recognition/>.

`[FACT]` It runs on **CPU, GPU and NPU** (the GenAI README states all scenarios
run on OpenVINO Runtime which supports CPU/GPU/NPU):
<https://github.com/openvinotoolkit/openvino.genai>. The dedicated NPU guide
says "There are no NPU-specific requirements when running the Whisper GenAI
pipeline on NPU, so a standard OpenVINO GenAI sample works without any
limitations", and notes Whisper GenAI support arrived in OpenVINO 2024.5
(stateless `--disable-stateful` export no longer required since 2025.1):
<https://docs.openvino.ai/2025/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html>.

`[FACT]` Models are **OpenVINO IR** exported from Hugging Face with
`optimum-cli export openvino …` — *not* GGUF/ggml `.bin`:
<https://openvinotoolkit.github.io/openvino.genai/docs/use-cases/speech-recognition/>.
This is a second model-provisioning format, distinct from clear-record's
existing `ggml-*.bin` download.

### 4.2 License (verified, not assumed)

`[FACT]` The `openvino.genai` repository is **Apache-2.0** (README "License"
section + the repo's `LICENSE`):
<https://github.com/openvinotoolkit/openvino.genai>. `[FACT]` The OpenVINO
Runtime it depends on is also **Apache-2.0**:
<https://github.com/openvinotoolkit/openvino/blob/master/LICENSE>. Both are
permissive.

### 4.3 Integration shape — and why it breaks ADR-0005

`[FACT]` OpenVINO GenAI is an **in-process library**: `pip install
openvino-genai` gives `import openvino_genai as ov_genai`, then
`ov_genai.ASRPipeline(model_path, "NPU")`:
<https://github.com/openvinotoolkit/openvino.genai>.

`[FACT]` **This breaks ADR-0005's property.** ADR-0005 fixes that "every backend
drives the system `whisper-cli` + a ggml plugin", and explicitly records that
the in-process `faster-whisper`/CTranslate2 path was *discarded* precisely
because it "runs in-process and holds shared model state, so it cannot share
the process-isolated parallel path"
(`docs/adr/0005-transcription-backend-strategy.md`, "Discarded alternatives").
An `openvino_genai.ASRPipeline` backend is exactly that shape: in-process,
shares model/decoder state, cannot be parallelized by the existing
`_WhisperCliBackend` worker pool, and would need `parallelizable=False`-style
serialization. It would be a **new runtime family** and a new ADR, not an edit.

**Contrast that matters:** you do *not* need GenAI to reach Intel CPU/GPU/NPU.
The same OpenVINO engine is already reachable *inside* `whisper-cli` through the
`ggml-openvino` backend (§3.1), which preserves the subprocess shape. So GenAI
is a capability that is *already covered by the existing seam* — with the same
GGUF model artifacts — at the cost of new upstream validation, whereas GenAI
adds a second model format and breaks the ADR property.

## 5. Q4 — Where each path sits

| Path | Artifact | Reuses the `Backend` seam unchanged? | Runtime family |
|---|---|---|---|
| `ggml-vulkan` | system `whisper-cli` + `libggml-vulkan` plugin | **Yes** — same `_WhisperCliBackend`; add an `intel` family + Intel device probe | none new (plugin) |
| `ggml-sycl` | system `whisper-cli` + `libggml-sycl` plugin | **Yes** — same shape; needs a `sycl` family + oneAPI runtime | none new (plugin) |
| `ggml-openvino` (CPU/GPU/**NPU**) | system `whisper-cli` + `libggml-openvino` plugin | **Yes, essentially** — same subprocess; needs an `openvino` family, OpenVINO runtime presence, and a device hint passed to the child | none new (plugin) |
| OpenVINO GenAI `ASRPipeline` | `openvino-genai` Python package | **No** — in-process library | **new in-process family** |

`[FACT]` The existing adapter already parameterizes exactly these knobs:
`_WhisperCliBackend(gpu_backends=(...), device_check=..., system=...)` plus a
plugin-glob table `_GGML_BACKEND_PATTERNS = {"vulkan": ("libggml-vulkan*.so*",),
"hip": ..., "cuda": ..., "metal": ...}` — visible in
`packages/clear-record/src/clear_record/providers/backends.py`. So an Intel
backend is ADR-0005's own "one adapter + one `available()` probe" consequence.

Two concrete gaps an Intel adapter must close (from that source, `[DESIGN]`):

1. **New family + probe.** `sycl` → `libggml-sycl*.so*`; `openvino` →
   `libggml-openvino*.so*`. The Linux device check currently has
   `_has_amd_gpu()` (DRM vendor `0x1002`) and `_has_nvidia_device()`; an Intel
   check would read DRM vendor `0x8086`.
2. **OpenVINO device selection for the child.** The adapter's
   `ProcessRunner.run(cmd, …)` has **no `env` parameter**, and `whisper-cli`'s
   OpenVINO *backend* chooses the device from `GGML_OPENVINO_DEVICE`, so
   selecting NPU vs GPU vs CPU needs either an `env=` extension on the runner or
   an env-aware launch. (The CLI's own `-oved/--ov-e-device` flag belongs to the
   older *encoder-only* `WHISPER_OPENVINO` path, not the ggml backend:
   `whisper_ctx_init_openvino_encoder(... params.openvino_encode_device ...)` in
   `examples/cli/cli.cpp`.)

`[FACT]` Note also the *encoder-only* OpenVINO path that whisper.cpp ships and
documents (`-DWHISPER_OPENVINO=1`, `-oved` device default `"CPU"`, device string
passed to `core.compile_model`): it, too, runs inside `whisper-cli`, so it also
preserves the subprocess property — but it accelerates only the encoder and its
README names only x86 CPUs and Intel GPUs (not NPU):
<https://github.com/ggml-org/whisper.cpp/blob/master/README.md#openvino-support>,
<https://github.com/ggml-org/whisper.cpp/blob/master/src/openvino/whisper-openvino-encoder.cpp>.

## 6. Q5 — License per option against ADR-0003

ADR-0003 prefers permissive and permits copyleft only across process/network
boundaries (`docs/adr/0003-license-boundary.md`).

| Component | License | Source |
|---|---|---|
| `whisper.cpp` (incl. `ggml-sycl`, `ggml-vulkan`, `ggml-openvino`) | MIT | <https://github.com/ggml-org/whisper.cpp/blob/master/LICENSE>, <https://github.com/ggml-org/ggml/blob/master/LICENSE> |
| `ggml-sycl` headers | MIT (© Intel) | <https://github.com/ggml-org/ggml/blob/master/src/ggml-sycl/ggml-sycl.cpp> |
| OpenVINO Runtime | Apache-2.0 | <https://github.com/openvinotoolkit/openvino/blob/master/LICENSE> |
| OpenVINO GenAI | Apache-2.0 | <https://github.com/openvinotoolkit/openvino.genai> |
| Intel oneAPI DPC++/SYCL compiler+runtime (build/runtime for SYCL) | Apache-2.0 WITH LLVM-exception | <https://github.com/intel/llvm/blob/sycl/LICENSE.TXT> |

`[FACT]` **No option introduces copyleft into the MIT core.** All of the
above are permissive, and in any case each is reached from
`clear_record.providers` behind the `Backend` interface / process boundary —
never imported by `clear_record.core`. The Linux Intel GPU *kernel* driver
(`i915`/`xe`) is GPL, but that is the same OS-driver situation the existing AMD
(`amdgpu`) and NVIDIA paths already live with, and it is not linked into the
app; `[OPEN]` I did not separately verify the license text of the Intel userspace
driver packages (they are not required for Vulkan-on-Mesa).

## 7. Recommendation

1. **Admit Intel as a fourth capability family, via the existing `whisper-cli`
   seam.** Start with the `ggml-vulkan` plugin (cross-vendor, already
   hot-tested for AMD, works on Intel Arc/iGPU with the system Mesa/ANV or
   Intel driver), then offer `ggml-sycl` as the Intel-native option where oneAPI
   is present.
2. **Treat the NPU as an experimental, opt-in sub-capability of an
   `ggml-openvino` family** — same `whisper-cli` subprocess, device selected by
   `GGML_OPENVINO_DEVICE`. Do not claim it until a Whisper hot-test exists;
   keep it `[OPEN]` per §3.
3. **Do not adopt OpenVINO GenAI.** It is permissively licensed but in-process,
   needs a second (IR) model format, and would overturn the ADR-0005 property
   for a capability the ggml backend already reaches.
4. ADR/architecture impact: this is an **additive** backend (one adapter + one
   probe), consistent with ADR-0005's "Consequences" — *not* a new runtime
   family, so it does not by itself force a new ADR. A new ADR is only needed
   if the project also decides to admit GenAI (or any in-process runtime).

## 8. Least-certain claims / `[OPEN]`

1. **Whisper end-to-end on the `ggml-openvino` backend — especially the NPU.**
   The backend and the "whisper.cpp support" commit exist, and it registers as a
   GPU device, but upstream publishes no validated Whisper result and the NPU is
   static-shape/stateless-only. This is the single biggest uncertainty in the
   Intel/NPU story.
2. **Arc B-Series (Battlemage/Xe2) *in whisper.cpp specifically*.** The shared
   `ggml-sycl` doc verifies Arc B580, but whisper.cpp's own SYCL table does not
   list B-Series; the inference that whisper.cpp inherits it is reasonable but
   not documented.
3. **The exact probe mechanics for an Intel adapter.** The plugin globs
   (`libggml-sycl*`, `libggml-openvino*`), the DRM vendor id (`0x8086`), and the
   need for an `env=` runner extension to pass `GGML_OPENVINO_DEVICE` are read
   from source, not stated in documentation.

## 9. Could not source

- Any **published whisper.cpp-on-Intel** benchmark or hot-test (upstream or
  Intel). `[OPEN]`
- An official statement that `-DGGML_OPENVINO=ON` is a *supported whisper.cpp*
  build configuration (it is documented on the llama.cpp side only; whisper.cpp
  documents the older encoder-only `WHISPER_OPENVINO` path). `[OPEN]`
- The Intel marketing name for the NPU (avoided; described by platform
  generation per OpenVINO's own tables).
- License text of the Intel userspace GPU/NPU driver packages (not required for
  the Mesa/Vulkan path).

## 10. Sources (primary)

Upstream repositories / source
- whisper.cpp README, `README_sycl.md`, `CMakeLists.txt`, `examples/sycl/`,
  `src/whisper.cpp`, `src/openvino/`, `ggml/src/ggml-openvino/`,
  `.github/workflows/build-sycl.yml` — <https://github.com/ggml-org/whisper.cpp>
- ggml README / `CMakeLists.txt` / `src/ggml-sycl/` /
  `src/ggml-openvino/CMakeLists.txt` — <https://github.com/ggml-org/ggml>
- llama.cpp `docs/backend/SYCL.md` and `docs/backend/OPENVINO.md` —
  <https://github.com/ggml-org/llama.cpp/tree/master/docs/backend>
- whisper.cpp OpenVINO encoder PR #1037:
  <https://github.com/ggml-org/whisper.cpp/pull/1037>

Vendor docs
- Intel, "Run LLMs on Intel GPUs Using llama.cpp":
  <https://www.intel.com/content/www/us/en/developer/articles/technical/run-llms-on-gpus-using-llama-cpp.html>
- Intel dGPU driver installation: <https://dgpu-docs.intel.com/driver/installation.html>
- Intel client GPU drivers: <https://dgpu-docs.intel.com/driver/client/overview.html>
- Intel oneAPI Base Toolkit: <https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit.html>
- OpenVINO NPU plugin README:
  <https://github.com/openvinotoolkit/openvino/blob/master/src/plugins/intel_npu/README.md>
- OpenVINO NPU device docs:
  <https://docs.openvino.ai/2025/openvino-workflow/running-inference/inference-devices-and-modes/npu-device.html>
- OpenVINO GenAI README/ASR docs/NPU guide:
  <https://github.com/openvinotoolkit/openvino.genai>,
  <https://openvinotoolkit.github.io/openvino.genai/docs/use-cases/speech-recognition/>,
  <https://docs.openvino.ai/2025/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html>

Licenses
- whisper.cpp / ggml MIT; OpenVINO + OpenVINO GenAI Apache-2.0; intel/llvm
  Apache-2.0 WITH LLVM-exception (links in §6).

Repo context (read-only): `AGENTS.md`, `docs/adr/0003-license-boundary.md`,
`docs/adr/0005-transcription-backend-strategy.md`, `docs/architecture.md` §5/§8,
`packages/clear-record/src/clear_record/providers/{base,backends,process}.py`.
