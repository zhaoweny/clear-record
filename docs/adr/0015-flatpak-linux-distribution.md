# ADR-0015 — Flatpak on Linux: document the story and scaffold it, defer the build

Status: active
Date: 2026-09-14

## Context

- [VOICE: owner, 2026-09-14] *"let's don't stuck on getting it signed before me
  trying it. build a flatpak package story for linux, and we use github action
  to do some heavy lifting once we do have a good reason to do it."*
- [FACT] ADR-0005 makes the transcription path drive the **system `whisper-cli`**
  plus a ggml GPU plugin; the provider is a subprocess adapter, not a Python
  library. A Flatpak sandbox contains the runtime's filesystem, so the host's
  `whisper-cli` and `ggml-*.so` plugin are **not visible**.
- [FACT] ADR-0014 ships a PyInstaller desktop app for macOS and Windows and
  leaves Linux a stretch target with no first-class bundle. ADR-0009/0012/0013
  publish one `clear-record` dist; the console is the double-click surface.
- [FACT] Linux is a first-class compute target (the AMD RX 7900 XTX worker,
  ADR-0005), and Flatpak is the one cross-distro sandboxed bundle an average
  Linux user can install. The owner's prior-art repo reserved the Flatpak slot
  (`maa-whirlwind/packaging/flatpak/`).
- [FACT] The remaining Flatpak work is review-and-maintenance heavy: Flathub
  wants hash-pinned sources, an icon/screenshots, a reproducible build and a
  passing `flatpak-builder-lint`; the app id is effectively permanent once
  submitted; signing and submission are inter-human actions.
- [FACT] The ggml model is auto-downloaded on first use (ADR-0005) and, inside a
  Flatpak, lands in the app's own persistent `$XDG_DATA_HOME`, so it survives
  upgrades.
- [REQ] The vendor-free core rule stands (`AGENTS.md`): no vendor/ASR code enters
  `clear_record.core`; the Flatpak bundles the vendor stack **beside** the
  MIT code, never into it (ADR-0003).

## Decision

- [DECISION] Write the Linux **story** now
  (`packaging/flatpak/README.md`) and commit a **manifest scaffold**
  (`packaging/flatpak/io.github.zhaoweny.clear-record.yml`) plus **AppStream
  metadata** (`io.github.zhaoweny.clear-record.metainfo.xml`).
- [DECISION] Add a **CI lane** (`.github/workflows/build-flatpak.yml`) that
  builds a `.flatpak` on `workflow_dispatch` and on `v*` tags with
  `flatpak/flatpak-github-actions/flatpak-builder@v6`, and keeps it as a
  **workflow artifact**. It signs nothing and publishes nowhere.
- [DECISION] The build is **deferred**: the scaffold is not wired into `just
  verify`, not released and not submitted to Flathub until there is a reason
  (a Linux user asking, a console worth double-clicking, or a submission
  decision).
- [DECISION] The app id is **`io.github.zhaoweny.clear-record`** — the GitHub
  basis (no domain hosting) and the hyphen is legal because it is in the last
  component (Flathub rule).
- [DECISION] The Flatpak **bundles whisper.cpp built with `-DGGML_VULKAN=ON`**.
  Vulkan is the one plausible Linux GPU backend inside the sandbox; **CUDA and
  ROCm are explicitly out of scope** for the data size and packaging cost.
- [DECISION] The app is installed from the **built wheel** into the Freedesktop
  SDK's Python (deps via `flatpak-pip-generator`), **not** from the PyInstaller
  bundle: a frozen bundle carries its own interpreter/manylinux wheels and layers
  badly on the runtime, while a pip install reuses the runtime ABI and is the
  supported pattern.
- [DECISION] The scaffold grants **`--device=dri`**, **`--socket=wayland`** /
  **`--socket=fallback-x11`** and **`--share=network`**. `--share=network` is
  kept deliberately: the only network use is the **first-use model download**
  (provisioning), so the offline-first execution path is unaffected; dropping it
  would require out-of-band model provisioning.
- [DECISION] The scaffold grants **`--filesystem=home`** as a stated compromise.
  A fully sandboxed "point it at any directory" flow needs the document portal
  plus console UI work; the CLI takes a path today.

## Rationale

- The owner's instruction is explicit: don't block on signing, write the story,
  let CI carry the heavy lifting. Scaffolding now is cheap; a Flathub submission
  is not, and doing it prematurely would freeze an app id and a maintenance
  promise for a surface that is still moving.
- Writing the whisper-cli/Vulkan/portal analysis down now surfaces the real
  blockers (sandboxed `whisper-cli`, CUDA/ROCm cost, ffmpeg) before anyone
  promises Linux users a bundle.
- Bundling the vendor stack beside the MIT code keeps ADR-0003 intact and matches
  how the PyInstaller app already drives a machine stack (ADR-0014) — here the
  stack simply travels with the app because the host's is invisible.
- One wheel, one install path: the Flatpak consumes the same `clear-record`
  wheel as every other channel (ADR-0012), so the packaging layer adds no second
  source of truth.

## Discarded alternatives

- **Submit to Flathub now / sign now** — the owner explicitly does not want to
  be stuck on signing before trying it, and a submission freezes the app id and
  the maintenance commitment. Recorded as a later, deliberate step.
- **Spawn the host `whisper-cli`** (`flatpak-spawn --host`, or
  `--talk-name=org.freedesktop.Flatpak`) — punches through the sandbox, depends
  on host libs resolving, and defeats the purpose. Rejected.
- **CUDA/ROCm in the sandbox** — large proprietary/toolkit payloads, not in the
  Freedesktop runtime. Vulkan covers AMD/Intel/NVIDIA with one binary. Rejected.
- **Wrap the PyInstaller onedir bundle** — a second interpreter and manylinux
  wheels inside the runtime; fragile loader/glibc layering, and `flatpak-builder`
  cannot validate it. Rejected in favour of the wheel.
- **`--filesystem=host`** — maximal convenience, maximal grant; `home` is
  narrower and covers the realistic workspace. Portal-based sandboxing is the
  eventual correct answer, deferred.
- **No network at all (fully offline edition)** — preserves the sandbox ideal but
  breaks the documented first-run model download; **closed by the owner
  2026-09-14** (round-4 grilling): the sandbox keeps `--share=network` for
  provisioning, so this variant is not taken.
- **A dedicated `clear-record-flatpak` dist** — no: the Flatpak packages the one
  published wheel; a second dist would revive multi-publisher machinery for no
  gain.

## Consequences / review hook

- The manifest is a **scaffold**: `python3-modules.json` and the wheel are build
  inputs generated in CI, and the `whisper-cpp` source tag / Vulkan build deps /
  icon / ffmpeg codecs are `[OPEN]` items tracked in
  `packaging/flatpak/README.md`.
- The scaffold is **not** part of `just verify` and never touches PyPI; the
  `build-flatpak` lane is a separate workflow that only uploads a bundle.
- Signing and Flathub submission are **inter-human actions** and need the owner's
  explicit grant before they happen (standing rules).
- Revisit when one of the stated reasons appears; the review must confirm the
  bundled Vulkan plugin actually loads under `--device=dri`, resolve the ffmpeg
  gap, and settle the metadata licence and icon before submission.
