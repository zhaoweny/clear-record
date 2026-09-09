# cr-engine

Audio signal processing for **clear-record**. This package may use numpy (a
generic numerical library) but **no** vendor/ML-framework or ASR library — ASR
stays in `cr-providers`. It owns:

- **audio I/O** — decode a file to a mono float32 array + sample rate
  (`soundfile`, with an `ffmpeg` fallback for mp3/m4a/etc.), and resample.
- **alignment** — estimate a relative time offset between two recordings via
  windowed cross-correlation (numpy FFT), so multiple sources can be placed on a
  common timebase.
- **merge / reconcile** — apply alignment offsets, sort, and collapse
  overlapping segments into a single attributed timeline.

This is the "observations → aligned timeline" layer. See
`docs/architecture.md` §3–4.
