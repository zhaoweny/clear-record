# ADR-0014 — Desktop app distribution via PyInstaller

Status: active
Date: 2026-09-14

## Context

- [VOICE: owner, 2026-09-14] *"In the end build a pyinstaller spec, which means
  we could offer Mac and windows build with minimal friction to average
  person"* (recorded in [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md)).
- [FACT] ADR-0013 ships the console inside the wheel with a `[web]` extra, but
  installing it still requires Python plus `uv`/`pip` — not "minimal friction"
  for an average person.
- [FACT] PyInstaller freezes a Python application into a standalone executable;
  it **does not cross-compile**, so each target OS builds its own artifact.
- [FACT] ADR-0005/ADR-0006 keep model weights, recordings and keys
  environment-local and never bundled.
- [FACT] Unsigned macOS and Windows builds carry first-run warnings (Gatekeeper
  / SmartScreen); removing them needs paid signing identities and notarization.

## Decision

- [DECISION] Ship a **PyInstaller spec** (`packaging/pyinstaller/clear-record.spec`)
  and a `just app` recipe. The artifacts are **CI workflow artifacts**, never
  committed and never published to PyPI — `just build` still publishes only the
  `clear-record` wheel (ADR-0012).
- [DECISION] One analysis produces **two executables**:
  - `clear-record-web` — windowed, the **double-click target**; runs
    `clear-record web` and opens the browser.
  - `clear-record` — the full console CLI.

  On macOS a `clear-record.app` bundle is added, with
  `CFBundleExecutable = clear-record-web` so double-clicking opens the console.
- [DECISION] PyInstaller lives in its own **`app` dependency group**, and the
  console's stack is pulled through the `web` extra (ADR-0013) — the verify
  environment never carries the build toolchain.
- [DECISION] The frontend stays embedded as Python strings (no data files), and
  the spec calls `copy_metadata("clear-record")` so the
  `clear_record.commands` entry point still resolves inside the frozen app — the
  thing that makes `clear-record web` work when frozen. The dynamically-imported
  `clear_record.service` / `clear_record.web` modules are declared as hidden
  imports.
- [DECISION] Builds are **unsigned**; the first-run friction and its workarounds
  are documented in `packaging/pyinstaller/README.md`. Signing/notarization is an
  explicit **future** step (it needs paid identities) — not claimed here.
- [DECISION] **No model weights and no keys are bundled** (BYOK): the app drives
  the machine's `whisper-cli` + ggml plugin and downloads a ggml model on first
  use, exactly as the CLI does (ADR-0005).
- [DECISION] The console gains `POST /api/shutdown` and a **Quit** button,
  because a windowed app has no terminal to interrupt; closing the browser tab
  does not stop the local server.

## Rationale

- A double-clickable app is the only path to "minimal friction" for the owner's
  target user; the wheel and the CLI remain for developers and servers.
- Keeping the app build out of the wheel and out of `dev` keeps the published
  artifact and the verify gate unchanged and lean.
- One analysis with two executables keeps the artifact small; the two launch
  scripts differ only in their default subcommand.
- Freezing preserves the existing offline/data boundaries rather than inventing
  a second distribution model.

## Discarded alternatives

- **Briefcase / cx_Freeze / PyOxidizer / Nuitka** — PyInstaller is the most
  conventional, well-documented choice and needs no extra packaging DSL; the
  others trade familiarity for size/speed gains not worth the learning curve
  here.
- **Signing and notarizing now** — requires a paid **Apple Developer Program
  membership (US$99/year)** for a Developer ID certificate (notarization itself
  is free but member-only) and a Windows code-signing certificate; the owner has
  neither in this repo. Apple's fee waivers cover nonprofits, accredited
  educational institutions and government entities, **not individuals**, so a
  personal OSS project would not qualify. Recorded as a future step with the
  exact user-facing workaround documented instead.
- **Bundling the ggml model(s)** — would grow the artifact by hundreds of MB,
  duplicate model management, and violate the environment-local rule
  (ADR-0006).
- **`--onefile`** — one neat download, but slower cold start (self-extraction)
  and more macOS quarantine/signing trouble; the app bundle and onedir folder are
  the friendlier shape.
- **A separate GUI toolkit build** — the console is a local web app; freezing it
  reuses every existing surface and test.

## Consequences / review hook

- The `build-app` workflow builds and uploads macOS and Windows artifacts on
  manual dispatch and on `v*` tags. It is a distinct workflow from the PyPI
  publish lanes; the app is never published to an index.
- The spec is coupled to the optional stacks: adding a new optional dependency
  that is imported dynamically needs a matching `hiddenimports` (or a
  `collect_submodules`) entry, or the frozen app will miss it silently.
- Unsigned artifacts keep the Gatekeeper/SmartScreen caveat; revisit when
  signing identities exist.
- The frozen app inherits the console's localhost-only, no-auth posture; it must
  never be exposed beyond localhost.
