# Mobile and edge NPUs — Apple, Qualcomm, MediaTek, Rockchip

Status: research (not a decision) · Date: 2026-09-14 · Ticket:
`.scratch/hardware-backends/issues/03-mobile-npus-research.md`

This note answers one question per silicon class: **which of the two roles the
architecture actually names could this platform serve?** The roles are **client
/ control surface** (laptop, phone) and **node** (the always-on Mac mini; the NAS
holds tapes). It is *not* "can it run Whisper". Verdicts below are labelled
`[SUGGESTION]` because nothing here is a decision
(`.scratch/hardware-backends/spec.md`: "This is research and a recommendation
only").

## Provenance key

| Label | Meaning here |
| --- | --- |
| `[FACT]` | Verified against a primary source (vendor/SDK docs, the upstream repo, or an upstream issue); URL inline. |
| `[OPEN]` | Could **not** be verified from a primary source; stated as an open question, not a claim. |
| `[SUGGESTION]` | Analysis/verdict by this note, not owner voice and not a decision. |

No benchmark numbers are invented and none were measured here. The only numbers
quoted are **vendor-published** and attributed; where a number comes from a
community repo it is labelled as such.

## The seam this research is measured against

`[FACT]` clear-record's shipped backends (`apple` / `nvidia` / `amd`) all drive
the **system `whisper-cli` + a ggml plugin**, process-isolated, and the core is
vendor-free (ADR-0005, `docs/adr/0005-transcription-backend-strategy.md`).
Adding a backend that does **not** drive `whisper-cli` "changes the property this
ADR fixes" and needs its own ADR (ADR-0005, `[OPEN]` note; and the
`.scratch/system-speech-backends/` tracker exists for exactly that class).

`[FACT]` `whisper.cpp` (MIT) and `llama.cpp` (MIT) each **vendor their own copy of
`ggml`**. Upstream `whisper.cpp`'s acceleration list is ARM NEON, Accelerate,
**Metal**, **Core ML**, AVX/VSX, **Vulkan**, **CUDA**, **ROCm**, **AMD Ryzen AI
NPU via VitisAI**, **OpenVINO**, **Ascend NPU via CANN**, Moore Threads MUSA,
BLAS — retrieved 2026-09-14 from
<https://github.com/ggml-org/whisper.cpp> (README, "Core ML support", "OpenVINO
support", "AMD Ryzen AI NPU support", "NVIDIA GPU support", "Vulkan GPU support",
"AMD ROCm GPU support", "Ascend NPU support", "Moore Threads GPU support").

`[FACT]` **No Arm-vendor NPU (Apple excepted) appears in either upstream list.**
`llama.cpp`'s supported-backend table lists **Hexagon `[In Progress]`
(Snapdragon)**, **OpenCL (Adreno GPU)**, **OpenVINO `[In Progress]`**, and
`Metal`/`CANN`/`CUDA`/`HIP`/etc. — retrieved 2026-09-14 from
<https://github.com/ggml-org/llama.cpp> (README, "Supported backends"). There is
no Rockchip and no MediaTek entry in either project.

`[SUGGESTION]` Two consequences drive every verdict below: (1) a `ggml` backend
written for `llama.cpp` does **not** automatically exist in `whisper.cpp`'s
vendored `ggml`; and (2) any Arm-vendor NPU path that is not a ggml backend is a
*second runtime family* under ADR-0005, with the same weight as the
`SpeechTranscriber`/OpenVINO question.

---

## Class 1 — Apple (A-series phones, M-series Macs)

**Roles.** `[FACT]` The architecture's node is an Apple Silicon **Mac mini** (Metal)
and the **phone is a client / control surface** (`docs/architecture.md` §5).

**OS-level speech API.** `[FACT]` Apple replaced `SFSpeechRecognizer` with
`SpeechAnalyzer` + `SpeechTranscriber` in the `Speech` framework, announced as
new "in iOS 26 ... for all our platforms"; the model is **on-device**, good for
"long-form and distant audio", time-coded, installed/managed through the system
`AssetInventory`, and delivers volatile→final results
(<https://developer.apple.com/videos/play/wwdc2025/277/>, WWDC25 session 277,
transcript; canonical API refs
<https://developer.apple.com/documentation/speech/speechanalyzer>,
<https://developer.apple.com/documentation/speech/speechtranscriber> — the doc
pages are JS-rendered and could not be read as text here).
`[OPEN]` The exact minimum versions (iOS 26.0 / macOS 26.0) and the claim that the
model runs **on the Neural Engine** are recorded in the repo's
`.scratch/system-speech-backends/spec.md`, but the WWDC transcript I read says
only "on-device" and "outside of your application's memory space"; the ANE
attribution is not confirmed from a primary Apple doc here.

**Accelerator SDK.** `[FACT]` Core ML / Apple Neural Engine (and Metal for GPU)
is Apple's on-device inference path. `[FACT]` `whisper.cpp` has a **Core ML**
path that runs the **encoder on the ANE**: "more than x3 faster compared with
CPU-only execution", shipped via `-DWHISPER_COREML=1` and a generated
`.mlmodelc` (<https://github.com/ggml-org/whisper.cpp>, "Core ML support").
`[FACT]` Apple publishes an ANE-optimised transformer reference implementation
(<https://github.com/apple-aiml-research/ml-ane-transformers>; the repo is what
the `ane_transformers` conversion dependency comes from). `[OPEN]` That repo's
licence is reported by the GitHub API as "Other / NOASSERTION", i.e. **not a
confirmed permissive licence**; verify before depending on it.

**ggml / whisper.cpp backend.** `[FACT]` **Metal is the shipped, hot-tested
clear-record path** on Apple (ADR-0005 Update, 2026-09-11: verified end-to-end on
M4, 164 segments). `[FACT]` Core ML (ANE) exists upstream in `whisper.cpp` as an
**encoder-only** path, and is **not implemented** in clear-record
(`docs/architecture.md` §5; ADR-0005). `[OPEN]` The ADR also records a June-2026
"community experiment" with an **ANE-native encoder roughly 2× that Core ML
path"; the primary repo for that experiment was not located here.

**Licence.** `[FACT]` `whisper.cpp` is **MIT**
(<https://github.com/ggml-org/whisper.cpp>). Apple frameworks (Core ML, the
`Speech` framework) are OS-provided, not redistributable open source. The
conversion tooling licences (`coremltools`, `ane_transformers`) are `[OPEN]`.

**Verdict.** `[SUGGESTION]` **macOS Apple Silicon = already a plausible node
(shipped).** **iOS/iPadOS phone = client / control surface only.** A phone cannot
run the Python pipeline + subprocess `whisper-cli`; it reaches the node through
the local console (architecture §8 `service`/`web`). Its on-device
`SpeechTranscriber` is the `.scratch/system-speech-backends/` story, not a new
hardware backend, and is out of scope for *this* lane.

---

## Class 2 — Qualcomm Snapdragon 8 (phone SoC)

**Roles.** `[FACT]` Phones are the **client / control surface** (§5).

**OS-level speech API.** `[FACT]` Android exposes `SpeechRecognizer`; the class
doc warns "The implementation of this API is likely to stream audio to remote
servers", but an **on-device** recognizer has existed since **API level 31**
(`SpeechRecognizer.createOnDeviceSpeechRecognizer(Context)`,
`isOnDeviceRecognitionAvailable(Context)`) and `EXTRA_PREFER_OFFLINE` is API 23
(<https://developer.android.com/reference/android/speech/SpeechRecognizer>,
<https://developer.android.com/reference/android/speech/RecognizerIntent>).
`[OPEN]` Whether an OEM device actually ships an on-device recognizer is
device-dependent ("If this method returns `false`, ... will fail").

**Accelerator SDK.** `[FACT]` Qualcomm AI Engine Direct = **QNN / QAIRT**: a
unified API over Kryo CPU, Adreno GPU and Hexagon (DSP/HTP) with a compiled
backend library per core (<https://docs.qualcomm.com/doc/80-63442-10/topic/backend.html>).
`[FACT]` Supported Android Snapdragon targets include SD 8 Gen 3 (SM8650) and
SD 8 Elite / Elite Gen 5 (<https://docs.qualcomm.com/nav/home/QNN_general_overview.html?product=1601111740009302>).

**ggml / whisper.cpp backend.** `[FACT]` None in `whisper.cpp`. `[FACT]`
`llama.cpp` has a **Hexagon** backend labelled `[In Progress]`, and its own logs
call it "Hexagon backend (**experimental**)"; it is an **LLM/VLM** backend
(`MUL_MAT` etc.), not ASR
(<https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/snapdragon/README.md>,
<https://github.com/ggml-org/llama.cpp> backend table). `[FACT]` `llama.cpp` also
exposes an **OpenCL** backend targeting the Adreno GPU
(<https://github.com/ggml-org/llama.cpp> backend table;
<https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/OPENCL.md>).
`[FACT]` Qualcomm ships **GenieX** (BSD-3-Clause, "Developer Preview"), a
single C ABI that runs LLMs/VLMs over either `llama.cpp`/ggml (CPU, Adreno GPU,
Hexagon NPU) or QNN — again **LLM/VLM, not ASR**
(<https://github.com/qualcomm/GenieX>, <https://github.com/qualcomm/GenieX/blob/main/sdk/README.md>).

**Licence.** `[FACT]` The QNN/QAIRT SDK is **proprietary and license-gated**: the
product page carries a "Product license agreement" and the install flow requires
a Qualcomm ID and explicit license activation
(`qpm-cli --license-activate qualcomm_ai_engine_direct`)
(<https://www.qualcomm.com/developer/software/qualcomm-ai-engine-direct-sdk>,
<https://docs.qualcomm.com/doc/80-70014-15B/topic/qnn-download.html>). `[OPEN]`
The precise redistribution terms are behind the SDK's `LICENSE.pdf`.

**Verdict.** `[SUGGESTION]` **Client only.** No `whisper.cpp` path exists; the
only ggml story is an experimental LLM backend, and the ASR stack is a
proprietary non-ggml SDK. Nothing here changes the phone's role.

---

## Class 3 — Qualcomm Snapdragon X (PC-class ARM: Windows on Snapdragon, Copilot+)

This is deliberately kept separate: it is a **PC vendor** story, not a phone one,
and it overlaps the Windows AI APIs / `.scratch/system-speech-backends/` tracker.

**Roles.** `[FACT]` It competes with the existing node classes (the Mac mini /
AMD box) as a **node**, not as a phone client.

**OS-level speech API.** `[FACT]` `Microsoft.Windows.AI.Speech`
(`SpeechRecognitionModel`, `RecognizeFromFile`, `GetReadyState` /
`EnsureReadyAsync`). Requirements: **Windows 11 24H2 (build 26100)+**, **Windows
App SDK 1.7.1+**, and hardware that is either a **Copilot+ NPU** (model
preinstalled) or a **CPU** (model downloaded on demand; removable); **GPU is not
supported**, and NPU is chosen automatically on a Copilot+ PC. Apps must be
**MSIX-packaged with the `systemAIModels` capability** and should show consent
before a CPU model download
(<https://learn.microsoft.com/en-us/windows/ai/apis/speech-recognition>;
hardware matrix at <https://learn.microsoft.com/en-us/windows/ai/apis>).

**Accelerator SDK.** `[FACT]` Qualcomm AI Runtime on Windows on Snapdragon
(`aarch64-windows-msvc` / `arm64x-windows-msvc`, HTP backend)
(<https://docs.qualcomm.com/doc/80-62010-1/topic/qnn.html>,
<https://docs.qualcomm.com/doc/80-63442-10/topic/backend.html>).

**ggml / whisper.cpp backend.** `[FACT]` `llama.cpp`'s Snapdragon doc covers
**both Android and Windows on Snapdragon** (`docs/backend/snapdragon/windows.md`)
and the backend list places **Hexagon + OpenCL** there
(<https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/snapdragon/README.md>).
`whisper.cpp` has no Snapdragon backend.

**Licence.** `[FACT]` Same proprietary, license-gated QNN/QAIRT as above. Windows
AI APIs additionally impose the MSIX + `systemAIModels` packaging constraint.

**Verdict.** `[SUGGESTION]` **Out of scope for the mobile/edge lane.** If this
platform becomes a backend, it is a **system-speech backend** (Windows AI APIs,
already scoped in `.scratch/system-speech-backends/`) and/or a PC-vendor
(`Intel`/`NVIDIA`) question — not a phone/edge NPU. Treating it here would
double-count an existing tracker.

---

## Class 4 — MediaTek (Dimensity phone SoCs; Genio edge)

**Roles.** `[FACT]` Dimensity phones = **client / control surface**; Genio
modules are the edge/SBC class.

**OS-level speech API.** `[FACT]` Same Android `SpeechRecognizer` /
`createOnDeviceSpeechRecognizer` (API 31+) as above.

**Accelerator SDK.** `[FACT]` **NeuroPilot** is MediaTek's AI stack. Access is
tiered: **NeuroPilot Public** (limited, no NDA), **Basic** (full, NDA required),
**Premium** (OEM, full+advanced, NDA required)
(<https://neuropilot.mediatek.com/resources/public/latest/en/docs/readme>).
`[FACT]` Google's **LiteRT** supports MediaTek NeuroPilot through the
`CompiledModel` API, listing Dimensity 7300/8300/9000/9200/9300/9400/9500
(<https://ai.google.dev/edge/litert/next/mediatek>). `[FACT]` PyTorch
**ExecuTorch** has a MediaTek backend (Dimensity 9300/9400, NeuroPilot Express
SDK) (<https://docs.pytorch.org/executorch/main/backends-mediatek.html>).
`[FACT]` On the edge/Genio side, Radxa documents MediaTek MT8395 (Genio 1200)
**APU** inference on Ubuntu via `mediatek-libneuron` and the
`mtk-neuropilot` snap (<https://docs.radxa.com/en/nio/nio12l/ubuntu/npu-usage/env-setup>).

**ggml / whisper.cpp backend.** `[FACT]` **None** — MediaTek appears in neither
the `whisper.cpp` nor the `llama.cpp` backend lists.

**Licence.** `[FACT]` The NeuroPilot SDK is **proprietary and gated**; the full
SDK requires an NDA (above). The LiteRT/ExecuTorch paths are open *delegates*
but still require the vendor runtime.

**Verdict.** `[SUGGESTION]` **Dimensity phone = client only.** **Genio = an edge
node in principle, but out of scope today**: there is no `whisper.cpp`/ggml path
and the SDK is gated/proprietary, i.e. a second runtime family under ADR-0005.
`[OPEN]` Whether a Genio board could be a cheap always-on node is unassessed
beyond the existence of the NeuroPilot-on-Ubuntu stack.

---

## Class 5 — Rockchip RK3588-class (edge / SBC / NAS) — the hard case

This is the class "most likely to serve the architecture's **NAS / cheap
always-on node** role", so it is assessed hardest.

**Roles.** `[FACT]` In §5 the **NAS holds raw tapes + derived artifacts** — a
**storage** role, not a compute role. A Rockchip board as a *transcription node*
is therefore a **new** role, not the existing NAS role.

**Hardware.** `[FACT]` RK3588 has a **6 TOPS NPU, triple core**, with
int4/int8/int16/FP16/BF16/TF32, on an 8-core A76+A55 CPU
(<https://www.rock-chips.com/a/en/products/RK35_Series/2022/0926/1660.html> and
the Rockchip RK3588 datasheet, "triple NPU core ... computing power is up to
6TOPs"). `[FACT]` The RK3576 is also 6 TOPS @ INT8
(<https://www.rock-chips.com/uploads/pdf/2024.3.18/191/RK3576%20Brief%20Datasheet%20V1.2-20250828.pdf>).

**OS-level speech API.** `[FACT]` None specific to Rockchip (Linux, or Android
where the Android recognizer applies).

**Accelerator SDK.** `[FACT]` **RKNN-Toolkit2 + RKNPU2 runtime** (`librknnrt`),
covering RK3588, RK3576, RK3562/66/68, RV1126B and others
(<https://github.com/airockchip/rknn-toolkit2>). `[FACT]` Rockchip's own
**RKNN Model Zoo** ships a **Whisper** example (`whisper_encoder_base_20s` /
`whisper_decoder_base_20s`, FP16, 20 s window) plus **Zipformer**, and its
published benchmark table gives **whisper_base_20s RTF 0.215 on RK3588
(single core), 0.218 on RK3576 (single core), 0.420 on RK3562, 1.178 on
RK3566/RK3568** (vendor-published; the README notes the number is model inference
only, excluding pre/post-processing)
(<https://github.com/airockchip/rknn_model_zoo>,
<https://github.com/airockchip/rknn_model_zoo/tree/main/examples/whisper>).
`[FACT]` A newer `rknn3-model-zoo` also lists Whisper and Zipformer under ASR
(<https://github.com/airockchip/rknn3-model-zoo/blob/main/README.md>).

**ggml / whisper.cpp backend — the key finding.**

- `[FACT]` **There is no upstream `whisper.cpp` (or `llama.cpp`) RKNPU
  backend.** Rockchip is absent from both backend tables (READMEs above).
- `[FACT]` `whisper.cpp`'s own **"NPU support in whisper.cpp" issue (#1557) is
  open** (opened 2023-11-27, last updated 2026-04-21). The reporter converted the
  encoder to `.rknn` and found the runtime "quite slow, even lower than running
  on CPU", concluding "the NPU is not full support transformer, some operators
  are still running on the CPU"; the thread points at community efforts
  (`marty1885/llama.cpp` `rknpu2-backend`) and at useful-transformers
  (<https://github.com/ggml-org/whisper.cpp/issues/1557>).
- `[FACT]` A **community `llama.cpp` fork** adds an RKNPU2 **ggml backend**:
  `invisiofficial/rk-llama.cpp` (MIT, default branch `rknpu2`, ~276 stars, 50
  forks, last push 2026-08-30), description "Llama.cpp with the Rockchip NPU
  integration as a GGML backend", source
  `ggml/src/ggml-rknpu2/ggml-rknpu2.cpp`
  (<https://github.com/invisiofficial/rk-llama.cpp>,
  <https://github.com/invisiofficial/rk-llama.cpp/blob/rknpu2/ggml/src/ggml-rknpu2/ggml-rknpu2.cpp>).
  `[FACT]` A fork README documents the build flag `-DGGML_RKNPU2=ON`, RK3588/
  RK3588S/RK3576 support, and a requirement of **RKNN runtime ≥ 2.3.0**
  (<https://github.com/KHAEntertainment/rk-llama.cpp/blob/rknpu2/README.md>).
  **It is LLM-only** — a `llama.cpp` fork does not give `whisper.cpp` an RKNPU
  backend, because `whisper.cpp` vendors its own `ggml` copy.
- `[FACT]` **Non-ggml community Whisper on RK3588:**
  `useful-transformers` (now `moonshine-ai/useful-transformers`) runs Whisper
  `tiny.en`/`base.en` on the RK3588 NPU (FP16 matmul) and claims **30× real-time**
  on tiny.en, "2× faster than faster-whisper's int8"; its own TODO list still has
  larger models, int8/int4 matmuls, async launches and timestamp decoding
  (<https://github.com/moonshine-ai/useful-transformers>). `[FACT]` Its licence is
  **GPL-3.0** and its last code push was **2024-08-07** (GitHub API,
  <https://api.github.com/repos/moonshine-ai/useful-transformers>). `[OPEN]` A
  separate daemon `boundarybitlabs/rkwhisper` exists; maturity/licence not
  assessed (<https://github.com/boundarybitlabs/rkwhisper>). `[FACT]` A
  `jianglu/whisper_RK3588` fork (MIT) explicitly **self-describes as
  un-adapted** ("it's not already adapted to be used on RK3588")
  (<https://github.com/jianglu/whisper_RK3588>).
- `[FACT]` The RKNPU driver ships only in Rockchip's **BSP kernel**; a community
  out-of-tree module documents that mainline's newer "Rocket" driver "cannot run
  RKNN models or rkllama" (`antonioacg/rknpu-rk3588`, GPL-2.0 — community source)
  (<https://github.com/antonioacg/rknpu-rk3588>).

**Licence.** `[FACT]` The **RKNN SDK / Toolkit is proprietary**, under the
"RKNN SDK License": a royalty-free copyright licence limited to design/develop/
test of applications **compatible with Rockchip products**, with
no-reverse-engineering, no re-licensing, PRC governing law, and no support
obligation (<https://github.com/airockchip/rknn-toolkit2/blob/master/LICENSE>).
`[FACT]` The `rknn_model_zoo` **repo** is Apache-2.0, but that does not relicense
the runtime/toolkit it depends on
(<https://github.com/airockchip/rknn_model_zoo>, "License Apache License 2.0").

**Verdict (Rockchip).** `[SUGGESTION]` **Closest to a "plausible node" on
paper — but out of scope for the current `whisper-cli`/ggml seam, and therefore
`[OPEN]`.**
Rockchip has real, cheap, low-power NPU hardware and a *vendor* Whisper example
with a usable-looking RTF, which is exactly why it was nominated. But it cannot
ride the existing seam today: there is **no upstream ggml/`whisper-cli` RKNPU
backend**; the one ggml RKNPU backend is an **LLM-only `llama.cpp` fork**; the
official and strongest community ASR paths are **non-ggml RKNN** and the
community Whisper project is **GPL-3.0 / stale**; and the SDK is **proprietary**.
Adopting it would mean a **new non-`whisper-cli` runtime family**, with the same
"changes the property ADR-0005 fixes" cost as the `SpeechTranscriber`/OpenVINO
question — plus a licence review. It also converts the **NAS** (a storage role in
§5) into a compute node, which is a scope expansion, and §7 shows the project's
deliberate pattern of excluding appliance-fleet/pod directions.

---

## Verdict summary

| Platform | Class | OS speech API | Accelerator SDK | ggml / whisper.cpp backend | Licence | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Apple A/M | phone + Mac node | `SpeechAnalyzer`/`SpeechTranscriber` (iOS 26) | Core ML / ANE, Metal | Metal (shipped, mature); Core ML encoder (upstream, not used) | `whisper.cpp` MIT; Apple OS frameworks proprietary | macOS: **node (shipped)**; iPhone: **client only** |
| Qualcomm Snapdragon 8 | phone | Android `SpeechRecognizer` (on-device API 31+) | QNN / QAIRT (Hexagon HTP) | `llama.cpp` Hexagon (`[In Progress]`, "experimental"), OpenCL/Adreno — LLM only; **none in `whisper.cpp`** | QNN proprietary, license-gated | **client only** |
| Qualcomm Snapdragon X | PC-class ARM | `Microsoft.Windows.AI.Speech` (NPU or CPU; no GPU) | QNN on Windows-on-Snapdragon | `llama.cpp` Hexagon + OpenCL (WoS); **none in `whisper.cpp`** | QNN proprietary; MSIX + `systemAIModels` | **out of scope** (system-speech / PC-vendor lane) |
| MediaTek Dimensity / Genio | phone + edge | Android `SpeechRecognizer` | NeuroPilot (Public/Basic/Premium; NDA for full) | **none upstream** | NeuroPilot proprietary/gated | phone: **client only**; Genio: **out of scope / `[OPEN]`** |
| Rockchip RK3588-class | edge / SBC / NAS | none (Linux) | RKNN-Toolkit2 / RKNPU2 | **no upstream backend**; LLM-only community `llama.cpp` RKNPU2 fork; community RKNN Whisper (GPL-3.0, stale) | RKNN SDK proprietary; model zoo Apache-2.0 | **out of scope for the current seam / `[OPEN]`** |

## Open questions / claims that could not be sourced

- `[OPEN]` Apple `SpeechTranscriber` minimum OS versions (iOS 26.0 / macOS 26.0)
  and whether it uses the **Neural Engine** specifically — the WWDC transcript
  says "on-device" only; the Apple API pages are JS-rendered.
- `[OPEN]` The primary repo/benchmark for the ADR-0005-noted June-2026
  **ANE-native encoder ~2× Core ML** experiment.
- `[OPEN]` Licences of `coremltools` and Apple's `ane_transformers`
  (`ml-ane-transformers` GitHub API reports "NOASSERTION").
- `[OPEN]` Whether any Qualcomm QNN backend exists for **`whisper.cpp`** outside
  the upstream tree (none found); the same for MediaTek and Rockchip.
- `[OPEN]` `boundarybitlabs/rkwhisper` maturity, licence, and whether it is
  packaged/maintained.
- `[OPEN]` Exact QNN/QAIRT redistribution terms (behind the SDK's `LICENSE.pdf`).
- `[OPEN]` Whether a Rockchip board could serve as a *cheap always-on node*
  **without** leaving the `whisper-cli` seam — e.g. does upstream ever merge an
  RKNPU backend, and would a `llama.cpp` ggml backend be portable to
  `whisper.cpp`'s vendored `ggml`?
- `[OPEN]` Whether a phone is ever a compute node here (the architecture
  currently says client); this research finds no phone-SoC path that changes it.

## Sources

Primary sources retrieved 2026-09-14 (URLs inline above). Principal ones:
upstream `whisper.cpp` / `llama.cpp` / `ggml` repos and the `whisper.cpp` issue
#1557; `airockchip/rknn-toolkit2`, `airockchip/rknn_model_zoo`,
`airockchip/rknn3-model-zoo`, `invisiofficial/rk-llama.cpp` (and its
KHAEntertainment fork README), `moonshine-ai/useful-transformers`,
`antonioacg/rknpu-rk3588`; Rockchip RK3588 product page and datasheet; Qualcomm
QAIRT/QNN docs and product page; MediaTek NeuroPilot docs; Google LiteRT
MediaTek page; PyTorch ExecuTorch MediaTek/QNN pages; Radxa Genio docs;
Microsoft Learn Windows AI APIs; Android `SpeechRecognizer`/`RecognizerIntent`
reference; Apple WWDC25 session 277 and the `Speech` framework API references.
