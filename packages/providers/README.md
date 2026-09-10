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
| `nvidia` | NVIDIA | CUDA / Vulkan (system `whisper-cli` + `ggml-cuda`/`ggml-vulkan`) |
| `amd` | AMD Radeon | ROCm / Vulkan (system `whisper-cli` + `ggml-vulkan`/`ggml-hip`, e.g. `gfx1100`) |

Each backend is a *capability*, not a hard dependency: it is available only when
its runtime probe succeeds. `apple` is provided by its optional dependency extra;
`nvidia` and `amd` are system-provided (`whisper-cpp` + a ggml GPU plugin) and
install no Python package. `BackendInfo.parallelizable` marks adapters that are
safe to run concurrently (process-isolated ones); the transcribe stage uses it
to size its worker pool. See `docs/adr/0005-transcription-backend-strategy.md`.

> **Probe caveat (residual risk).** For `nvidia`/`amd`, `available()` proves
> *presence*, not *loadability*: it checks that a `whisper-cli` binary, a
> matching `libggml-*` plugin file, and the vendor GPU device all exist, but not
> that this build can actually load the plugin. A ggml version/ABI mismatch can
> still report available and fall back to CPU (whisper-cli warns on stderr). A
> model-dependent load check would make `available()` expensive, so the failure
> is surfaced at `transcribe()` time, where a missing or invalid `-ojf` result
> now raises a clear `RuntimeError` rather than an opaque `FileNotFoundError`.
