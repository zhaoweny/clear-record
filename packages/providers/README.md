# cr-providers

Per-vendor ASR backend adapters for **clear-record**, exposed behind one
interface. The application core (`cr-core`) never imports a vendor stack; the
CLI picks a backend from this package at runtime.

The motivating constraint (owner voice, via the Marco Arment / Overcast
observation that Mac frameworks are great for transcription): support all three
major desktop compute families rather than only one.

| Backend id | Vendor / framework | Typical stack |
|---|---|---|
| `apple` | Apple Silicon | Metal / Core ML / ANE (system `whisper-cli` + `ggml-metal`) |
| `nvidia` | NVIDIA | CUDA / Vulkan (system `whisper-cli` + `ggml-cuda`/`ggml-vulkan`) |
| `amd` | AMD Radeon | ROCm / Vulkan (system `whisper-cli` + `ggml-vulkan`/`ggml-hip`, e.g. `gfx1100`) |

Each backend is a *capability*, not a hard dependency: it is available only when
its runtime probe succeeds. All three drive a system `whisper-cli`
(`whisper-cpp` + a ggml plugin: `ggml-metal` on macOS, `ggml-cuda`/`ggml-vulkan`
/`ggml-hip` on Linux), so all three extras — including `apple` — install no
Python package (they are no-op markers). A missing `ggml-*.bin` is
**downloaded on first use** from `huggingface.co/ggerganov/whisper.cpp` into
`model_dir` / `CR_MODELS_DIR` / `<cwd>/models`; offline, `transcribe()` raises
the actionable `hf download …` pre-fetch error. `BackendInfo.parallelizable`
marks adapters that are safe to run concurrently (process-isolated ones, which
is now all three); the transcribe stage uses it to size its worker pool. See
`docs/adr/0005-transcription-backend-strategy.md`.

## Setup

- macOS (Apple): `brew install whisper-cpp` — Homebrew pulls `ggml`, whose
  `libggml-metal.so` is the Metal plugin. The ggml model downloads on first use.
- Linux (AMD/NVIDIA): install the distro's `whisper-cpp` plus the matching ggml
  GPU plugin (`ggml-cuda` / `ggml-vulkan` / `ggml-hip`); point the probe at a
  from-source build with `CR_WHISPER_CLI` and `CR_GGML_BACKEND_DIRS`.

> **Probe caveat (residual risk).** For the `whisper-cli` paths, `available()`
> proves *presence*, not *loadability*: it checks that a `whisper-cli` binary, a
> matching `libggml-*` plugin file, and (on Linux) the vendor GPU device all
> exist, but not that this build can actually load the plugin. A ggml
> version/ABI mismatch can still report available and fall back to CPU
> (whisper-cli warns on stderr). A model-dependent load check would make
> `available()` expensive, so the failure is surfaced at `transcribe()` time,
> where a missing or invalid `-ojf` result now raises a clear `RuntimeError`
> rather than an opaque `FileNotFoundError`. On Homebrew the Metal plugin is a
> `.so` under the ggml `libexec` (not a `.dylib` in `lib`).
