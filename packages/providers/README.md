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
| `amd` | AMD Radeon | ROCm / Vulkan (whisper.cpp, e.g. `gfx1100`) |

Each backend is a *capability*, not a hard dependency: it is only available when
its optional dependency extra is installed and its runtime probe succeeds. See
`docs/adr/0005-transcription-backend-strategy.md`.
