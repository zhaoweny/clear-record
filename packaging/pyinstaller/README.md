# Desktop app builds (PyInstaller)

Freezes clear-record into a self-contained desktop app so the **average person
can double-click it** and get the local console — no Python, no `uv`, no
terminal. macOS and Windows are the targets; Linux is a stretch target (the
same spec builds, but there is no first-class bundle).

## Build

```sh
just app
```

Artifacts land in `dist/` (gitignored):

| Path | What |
|---|---|
| `dist/clear-record/clear-record-web` | the double-click target — starts the console and opens the browser |
| `dist/clear-record/clear-record` | the full console CLI (all subcommands) |
| `dist/clear-record.app` | macOS only — the above as an app bundle |

`just app` runs PyInstaller from the optional `app` dependency group and pulls
the console's stack in through the `web` extra (ADR-0013), so the verify
environment never carries the build toolchain.

**Build on each target OS.** PyInstaller does not cross-compile: a macOS app is
built on macOS, a Windows app on Windows. The `build-app` CI workflow does both.

## What the frozen app contains

- Python + `numpy` + `soundfile` (with its bundled `libsndfile`) — the audio
  stack the pipeline needs.
- `fastapi` + `uvicorn` and the console's UI (embedded as Python strings, so
  there are no frontend data files to collect).
- The `clear-record` dist metadata, so the `clear_record.commands` entry point
  still resolves inside the frozen app — that is what keeps `clear-record web`
  working there (`copy_metadata("clear-record")` in the spec).

## Signing and first-run friction

The builds are **unsigned**. That means:

- **macOS:** Gatekeeper refuses a downloaded `.app` ("Apple could not verify
  …"). On macOS 15 (Sequoia) and later the old right-click → **Open** shortcut
  **no longer works**; the user has to try opening it once, then go to
  **System Settings → Privacy & Security**, scroll to Security, and click
  **Open Anyway**. Clearing the download flag also works:
  `xattr -dr com.apple.quarantine /Applications/clear-record.app`.
  Removing this friction needs an **Apple Developer Program membership
  ($99/year)** — a Developer ID certificate plus notarization; a deliberate
  future step, not done here.
- **Windows:** SmartScreen may show "Windows protected your PC" → **More info** →
  **Run anyway**. Removing this needs a code-signing certificate.

No model weights are bundled: transcription still uses the machine's
`whisper-cli` + a ggml plugin and downloads a ggml model on first use (ADR-0005).
The app is a **console**, not an offline bundle of models.

## Quitting

The windowed app has no terminal, so the console's page carries a **Quit**
button that asks the local server to stop (`POST /api/shutdown`). Closing the
browser tab does not stop the server.

## Adding an icon later

Drop a `.icns` (macOS) / `.ico` (Windows) beside this README and set `icon=` in
the `BUNDLE`/`EXE` calls in `clear-record.spec`. Icons are deliberately omitted
so the build has no binary assets to license or maintain yet.
