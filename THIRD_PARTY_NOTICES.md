# Third-Party Notices

`clear-record`'s **code** is distributed under the [MIT License](LICENSE), and
its **documentation and authored content/assets** under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
([ADR-0008](docs/adr/0008-content-and-asset-licensing.md)). Those licences cover
**this repository's own material only**; they do not relicense any third-party
component listed below. The licensing boundary — including how copyleft is
consumed — is [ADR-0003](docs/adr/0003-license-boundary.md).

This file is informational. It is not legal advice, and it is not a substitute
for the license text shipped by each component.

## This repository's own material

| Component | License |
| --- | --- |
| Code — `cr-core`, `cr-engine`, `cr-providers`, `cr-cli`, `clear-record` (this repo) | MIT |
| Documentation and authored content/assets (none currently committed) | CC BY 4.0 |

## Python dependencies

Versions below are those pinned in [`uv.lock`](uv.lock); license identifiers are
taken from each package's metadata (`License` / `License-Expression`) in the
resolved environment. They are installed from PyPI via `uv`, not vendored into
this repository.

> Build tooling: the workspace members declare `uv_build` in their
> `[build-system]` tables. It is used only to build/install the distributions and
> is not part of the locked runtime/dev set below.

### Runtime dependencies (installed by default)

| Package | Version | License |
| --- | --- | --- |
| [`numpy`](https://pypi.org/project/numpy/) | 2.5.3 | BSD-3-Clause (the wheel also bundles code under 0BSD, MIT, Zlib and CC0-1.0) |
| [`soundfile`](https://pypi.org/project/soundfile/) | 0.14.0 | BSD-3-Clause; the wheel bundles **libsndfile** (LGPL-2.1) including **libmp3lame** (LGPL-2+) and **libmpg123** (LGPL-2.1) |
| [`cffi`](https://pypi.org/project/cffi/) *(transitive, via soundfile)* | 2.1.1 | MIT-0 |
| [`pycparser`](https://pypi.org/project/pycparser/) *(transitive, via cffi)* | 3.0 | BSD-3-Clause |
| [`typing-extensions`](https://pypi.org/project/typing-extensions/) *(transitive, via soundfile)* | 4.16.0 | PSF-2.0 |

### Development-only dependencies

Not installed in a runtime environment; used by the `just verify` gate.

| Package | Version | License |
| --- | --- | --- |
| [`pytest`](https://pypi.org/project/pytest/) | 9.1.1 | MIT |
| [`ruff`](https://pypi.org/project/ruff/) | 0.16.6 | MIT |
| [`trove-classifiers`](https://pypi.org/project/trove-classifiers/) | 2026.6.1.19 | Apache-2.0 |
| [`iniconfig`](https://pypi.org/project/iniconfig/) *(transitive, via pytest)* | 2.3.0 | MIT |
| [`packaging`](https://pypi.org/project/packaging/) *(transitive, via pytest)* | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [`pluggy`](https://pypi.org/project/pluggy/) *(transitive, via pytest)* | 1.6.0 | MIT |
| [`pygments`](https://pypi.org/project/pygments/) *(transitive, via pytest)* | 2.21.0 | BSD-2-Clause |
| [`colorama`](https://pypi.org/project/colorama/) *(transitive, via pytest; Windows only)* | 0.4.6 | BSD-3-Clause |

## External runtimes (not bundled, not linked)

These are separate programs that `clear-record` invokes as local
**subprocesses**. They are not copied into, linked into, or vendored with this
repository; you install them yourself, and their own licenses apply.

| Component | Role | License |
| --- | --- | --- |
| [`whisper.cpp` / `ggml`](https://github.com/ggml-org/whisper.cpp) (the `whisper-cli` binary and its GPU plugins) | transcription engine | MIT |
| [`ffmpeg`](https://ffmpeg.org/) | optional audio-decode fallback for formats the built-in decoder does not cover (e.g. m4a/aac) | LGPL-2.1-or-later in a default build; a build configured with `--enable-gpl` (or with non-free components) is under GPL or unredistributable. The exact license depends on how your copy was built — check the build's `--enable-*` flags. |

The **model weights** (`ggml-*.bin`) are downloaded on first use from
[`ggerganov/whisper.cpp`](https://huggingface.co/ggerganov/whisper.cpp) on
Hugging Face and are not distributed with this repository. Their upstream terms
govern them; check the model publisher.

## Copyleft boundary

Per [ADR-0003](docs/adr/0003-license-boundary.md):

- clear-record is MIT licensed.
- LGPL dependencies may be used through their supported library interfaces,
  subject to their LGPL obligations. The one such component bundled in a runtime
  dependency is `soundfile`'s `libsndfile` (LGPL-2.1), which `soundfile` links
  in-process through its supported interface.
- GPL/AGPL software is **never** copied into, statically or dynamically linked
  into, or otherwise incorporated into the MIT-licensed core. Integration with
  GPL/AGPL applications happens only through clearly defined external process or
  network interfaces (for example `ffmpeg` invoked as a subprocess).
- Modifications to third-party LGPL/GPL/AGPL components retain the license
  required by their upstream projects.

No third-party source or artifact is vendored into this repository.
