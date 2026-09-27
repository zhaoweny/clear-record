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
| `dist/clear-record/clear-record-tray` | the double-click target — runs the console behind a menu-bar tray icon |
| `dist/clear-record/clear-record-web` | starts the console and opens the browser |
| `dist/clear-record/clear-record` | the full console CLI (every bundled subcommand) |
| `dist/clear-record.app` | macOS only — the above as an app bundle (`CFBundleExecutable = clear-record-tray`) |

`just app` runs PyInstaller from the optional `app` dependency group and pulls
the console's stack in through the `web` extra and PySide6 through the `tray`
extra (ADR-0013/0016) — the tray supervises the console, so it needs both — so
the verify environment never carries the build toolchain.

**Build on each target OS.** PyInstaller does not cross-compile: a macOS app is
built on macOS, a Windows app on Windows. The `build-app` CI workflow does both.

The bundle deliberately does **not** pull the `agents` extra, so
`clear-record mcp` is **not included** in the desktop app — the MCP server needs
the MCP SDK, which the frozen build leaves out (`clear-record.spec` carries no
`clear_record.mcp` hidden import). Install `clear-record[agents]` from PyPI to
run the MCP server; the desktop app carries the console and the CLI.

## What the frozen app contains

- Python + `numpy` + `soundfile` (with its bundled `libsndfile`) — the audio
  stack the pipeline needs.
- `fastapi` + `uvicorn` and the console's UI (templates + vendored htmx/Alpine,
  collected as package data).
- `PySide6` — the menu-bar tray that supervises the console and is the app's
  entry point. Its Qt plugins and libraries come from PyInstaller's PySide6
  hooks; the spec only has to keep `clear_record.tray.app` on the module graph
  (its `PySide6` imports are deferred into the entry function).
- The `clear-record` dist metadata, so the `clear_record.commands` entry point
  still resolves inside the frozen app — that is what keeps `clear-record web`
  and `clear-record tray` working there (`copy_metadata("clear-record")` in the
  spec).

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
button that asks the local server to stop (`POST /web/ui/shutdown`), and the tray's
menu has a **Quit** item that exits, stopping the node this tray started — a
node it only joined is left running. Closing the browser tab does not stop the
server.

## Adding an icon later

Drop a `.icns` (macOS) / `.ico` (Windows) beside this README and set `icon=` in
the `BUNDLE`/`EXE` calls in `clear-record.spec`. Icons are deliberately omitted
so the build has no binary assets to license or maintain yet.
