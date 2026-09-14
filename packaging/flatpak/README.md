# Flatpak packaging — the story

Status: **scaffold — nothing is built, signed or submitted yet.** The manifest,
the AppStream metadata and the CI lane exist so the Linux story is written down
and the heavy lifting can be switched on later. [ADR-0015](../../docs/adr/0015-flatpak-linux-distribution.md)
records the decision to do it this way.

Owner framing (2026-09-14), verbatim:

> "let's don't stuck on getting it signed before me trying it. build a flatpak
> package story for linux, and we use github action to do some heavy lifting
> once we do have a good reason to do it."

## Why Flatpak at all

- [VOICE] The owner's always-on transcription node is an Apple Silicon Mac, but
  the high-throughput worker is an **AMD Radeon RX 7900 XTX Linux box**
  (ADR-0005, `docs/architecture.md` §5). On Linux, "install from PyPI and put the
  system `whisper-cli` on PATH" is a developer path.
- [FACT] Flatpak is the one cross-distro, sandboxed bundle an average Linux user
  can install from a software centre. Flathub is the natural route, and it is the
  route the owner already reserved in the prior-art repo (`maa-whirlwind`
  `packaging/flatpak/`).
- [FACT] macOS and Windows already have a PyInstaller story
  ([`packaging/pyinstaller/`](../pyinstaller/README.md), ADR-0014). Flatpak is the
  Linux counterpart, not a replacement.

## What is in this directory

| File | What it is |
|---|---|
| `io.github.zhaoweny.clear-record.yml` | the `flatpak-builder` manifest (scaffold) |
| `io.github.zhaoweny.clear-record.metainfo.xml` | AppStream metadata |
| `README.md` | this story |

`python3-modules.json` and the `clear-record` wheel are **build inputs**, not
source: CI generates the first and `just build` produces the second. See §5.

`.github/workflows/build-flatpak.yml` builds a `.flatpak` on manual dispatch and
on `v*` tags. It **signs nothing and publishes nowhere** — it only uploads the
bundle as a workflow artifact.

## 1. The `whisper-cli` problem (the real blocker)

The whole transcription path drives the **system `whisper-cli`** plus a ggml
plugin (ADR-0005); there is no in-process ASR wheel. A Flatpak sandbox contains
the runtime's filesystem, not the host's `/usr`: the host's `whisper-cli` and its
`ggml-*.so` plugin are simply not there. So the manifest has to choose:

| Option | What it costs | Verdict |
|---|---|---|
| **Bundle whisper.cpp + a ggml backend** inside the Flatpak | A CMake module in the manifest, a Vulkan toolchain at build time, and a few MB of binary. The model (hundreds of MB) is still downloaded at runtime, so the binary is not the size problem. | **Chosen.** |
| **Spawn the host `whisper-cli`** | Blocked by the sandbox by default. `flatpak-spawn --host` / `--talk-name=org.freedesktop.Flatpak` would punch through the sandbox, the host binary's libs may not resolve, and it defeats the point of the sandbox. | Rejected. |
| **Skip GPU, CPU-only** | Simplest, but abandons the owner's "own the hardware" property and makes a multi-hour tape unusable. | Fallback only. |

**Which backend:** **Vulkan**, and only Vulkan. Metal is macOS-only; CUDA and
ROCm inside a Flatpak are effectively out of scope (§2). Vulkan is
cross-vendor (AMD/Intel/NVIDIA), needs no proprietary runtime, and is exactly the
path already hot-tested on the RX 7900 XTX (ADR-0005). The bundled binary is
built with `-DGGML_VULKAN=ON`.

Two consequences worth naming:

- The provider's `available()` probe expects `whisper-cli` on PATH and an
  accepted plugin in a searched directory. `/app/bin` is on the sandbox PATH, so
  a bundled `whisper-cli` satisfies the first half; the plugin lives in
  `/app/lib`. Two wires make that work: the probe reads
  `--env=CR_GGML_BACKEND_DIRS=/app/lib` (the built-in search list does **not**
  include `/app`), and the binary is compiled with
  `-DGGML_BACKEND_DL=ON -DGGML_BACKEND_DIR=/app/lib` so ggml itself loads the
  plugin from there. This is the one place the packaging touches the runtime
  contract. **[OPEN]** ggml ≥ 0.9 occasionally nests the plugin under
  `/app/lib/backends*/`, which the probe's env list does not glob — verify the
  install layout on the first real build.
- VRAM probing reads the DRM `mem_info_vram_total`; that may not be readable in
  the sandbox, in which case the documented `CR_VRAM_GB` override is the
  answer (the adaptive `--jobs` cap already assumes 8 GB when it cannot probe).

## 2. GPU access

- [FACT] `--device=dri` is what exposes the DRM render nodes to the sandbox; the
  GL/Vulkan drivers themselves arrive through the runtime's graphics extension.
  A Vulkan-enabled ggml plugin is the plausible GPU path. **[OPEN]** — verify on
  real hardware that `whisper-cli` inside the Flatpak actually loads
  `libggml-vulkan.so` and picks RADV; presence is not loadability (the same
  caveat as `--check-plugin`).
- [FACT] **CUDA inside Flatpak is out of scope.** It would mean shipping the
  proprietary CUDA runtime in the sandbox (large, and not in the Freedesktop
  runtime). **ROCm likewise** — huge and not packaged for this. The Flatpak is
  therefore **Vulkan-only**, and the `nvidia`/`amd` Python extras are irrelevant
  inside it (they are already no-op markers).

## 3. Filesystem access

The tool's whole job is reading tapes and writing archives **wherever the user
points**. That is the opposite of a sandbox-friendly shape:

- The CLI takes a **directory path**; it does not (yet) speak the document
  portal. A portal-based flow needs a file-chooser UI in the console that returns
  a sandbox-visible handle — real work, not a flag.
- So the scaffold grants **`--filesystem=home`**: the narrowest static grant that
  keeps `clear-record run ~/Recordings/meeting` working. It is coarse — the app
  can read the whole home directory — and that should be stated plainly, not
  hidden behind "sandboxed".
- Removable media (`/run/media`, `/media`) needs its own grant if the owner
  wants to point at an SD card or an external drive.
- The console's own registry lives under the app's private
  `~/.var/app/io.github.zhaoweny.clear-record/data` area, which is writable
  without a grant.

**[OPEN]** the fully sandboxed "pick any directory" experience needs portals plus
UI work; it is out of scope for the scaffold.

## 4. Models

- [FACT] A missing `ggml-*.bin` is downloaded on first use into `--models-dir` /
  `CR_MODELS_DIR` / `<data>/models`, atomically; offline it raises the actionable
  `hf download …` error (ADR-0005).
- [FACT] Inside a Flatpak, `$XDG_DATA_HOME` is the app's own persistent
  `~/.var/app/io.github.zhaoweny.clear-record/data`. A model downloaded there
  **survives app upgrades** (it is not part of the app ref).
- The platformdirs-based resolver (ADR-0025) now defaults models to
  `<data>/models`, which inside a Flatpak is exactly
  `$XDG_DATA_HOME/clear-record/models`. The scaffold's `clear-record-web`
  launcher still pins `CR_MODELS_DIR=$XDG_DATA_HOME/clear-record/models` — now
  merely explicit; the export can go away.

## 5. Build strategy — wheel, not the PyInstaller bundle

The manifest builds the app from the **wheel produced by `just build`**
(`uv build --package clear-record`), installed into the Freedesktop SDK's Python
with `pip3 install --no-deps`.

Why not freeze the PyInstaller bundle into the Flatpak?

- The PyInstaller onedir bundle carries its **own interpreter and manylinux
  wheels**. Layering that on the Freedesktop runtime duplicates glibc/loader
  assumptions and can break in ways `flatpak-builder` cannot see.
- A pip install into the runtime's Python reuses the runtime's ABI and is the
  supported Flatpak pattern; it also gets `clear_record.commands` entry points
  and dist metadata for free.
- The console's frontend is embedded as Python strings (ADR-0013), so there are
  no data files to collect — a wheel install is enough.

Python dependencies are resolved by **`flatpak-pip-generator`** from
`packages/clear-record/pyproject.toml` (including the `web` optional group), with
`numpy`/`soundfile` taken from platform wheels rather than rebuilt. **This file is
generated in CI, not committed** — it is a build input with URLs and hashes that
belong to a build, not to source.

## 6. Names, licensing and the app id

- The app id is `io.github.zhaoweny.clear-record`. The hyphen is legal because it
  is in the **last** component (Flathub: "a dash is only allowed in the last
  component"); a hyphen anywhere earlier would fail lint (`cid-rdns-contains-hyphen`).
- The GitHub basis (`io.github.*`) is the owner-controlled basis noted in the
  prior-art repo; no domain hosting is needed.
- whisper.cpp and ggml are **MIT** (ADR-0003), so bundling them is clean.
- **[OPEN]** the metainfo file is licensed `CC-BY-4.0` to match the repo's content
  licence (ADR-0008); Flathub normally prefers a permissive metadata licence
  (FSFAP/CC0-1.0). Revisit at submission time.

## 7. The `ffmpeg` gap (found while writing this)

- [FACT] `ingest` shells out to **ffmpeg** for mp3/m4a-style inputs
  (`clear_record.engine.audio`), while wav/flac/ogg go through `soundfile`.
- [FACT] A Flatpak does not inherit the host's `ffmpeg`. The Freedesktop runtime
  historically exposed it through the `org.freedesktop.Platform.ffmpeg-full`
  extension; 25.08 replaced that with `codecs-extra`.
- So, as scaffolded, the Flatpak would ingest wav/flac/ogg fine and **fail on
  mp3/m4a**. **[OPEN]** add the codecs extension or bundle an ffmpeg module
  before claiming full input support.

## 8. Why the build is deferred

The scaffold and CI exist; the real build waits for a reason. That reason is one
of: a Linux user actually asking for an installable bundle, the console reaching a
state worth double-clicking, or a decision to submit to Flathub.

Deferring buys time because the remaining work is **review-and-maintenance
heavy**, not just code:

- Flathub requires hash-pinned sources (a **git commit + a wheel URL**, not a
  `dir` source), a reproducible build, an icon and screenshots, and passing
  `flatpak-builder-lint`.
- The app id is effectively **permanent** once on Flathub.
- Signing and submission are inter-human actions on the project's behalf; per the
  standing rules they need the owner's explicit grant first.

## 9. Running the CI lane (and what it does today)

`.github/workflows/build-flatpak.yml` runs on `workflow_dispatch` and on `v*`
tags. It:

1. builds the wheel (`just build`);
2. generates `python3-modules.json` with `flatpak-pip-generator` — the "heavy
   lifting" the owner asked CI to do;
3. builds the bundle with
   [`flatpak/flatpak-github-actions/flatpak-builder@v6`](https://github.com/flatpak/flatpak-github-actions)
   on the `freedesktop-25.08` container image;
4. uploads the `.flatpak` with `actions/upload-artifact@v4`.

It does **not** sign, publish to Flathub/OSTree, or touch PyPI.

## Open items

- **[OPEN]** Verify a bundled Vulkan `whisper-cli` actually loads the plugin and
  sees the host GPU through `--device=dri` (the §1/§2 unknown).
- **[OPEN]** Add `vulkan-headers` + `shaderc`/`glslc` to the manifest if the SDK
  does not provide them.
- **[OPEN]** Add ffmpeg (codecs extension or module) for mp3/m4a ingest (§7).
- **[OPEN]** Ship an icon; Flathub needs one (`Icon=` is a forward reference).
- **[OPEN]** Replace the `dir` source with a pinned git + wheel source for a real
  submission, and decide the metadata licence.
- **[OPEN]** Decide the filesystem strategy: `--filesystem=home` now, portals
  later (§3).
