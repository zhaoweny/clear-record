# cr-providers

Per-vendor ASR backend adapters for **clear-record**, exposed behind one
interface. The application core (`cr-core`) never imports a vendor stack; the
CLI picks a backend from this package at runtime.

The motivating constraint (owner voice, via the Marco Arment / Overcast
observation that Mac frameworks are great for transcription): support all three
major desktop compute families rather than only one.

| Backend id | Vendor / framework | Typical stack |
|---|---|---|
| `apple` | Apple Silicon | Metal / Core ML / ANE (whisper.cpp) |
| `nvidia` | NVIDIA | CUDA / cuBLAS / cuDNN (faster-whisper / CTranslate2) |
| `amd` | AMD Radeon | ROCm / Vulkan (system `whisper-cli` + `ggml-vulkan`/`ggml-hip`, e.g. `gfx1100`) |

Each backend is a *capability*, not a hard dependency: it is available only when
its runtime probe succeeds. `apple`/`nvidia` are provided by their optional
dependency extras; `amd` is system-provided (`whisper-cpp` + `ggml-vulkan`) and
its extra installs no Python package. See
`docs/adr/0005-transcription-backend-strategy.md`.
