# The console, the desktop app, and the app shell — owner-voice record (2026-09-14)

Status: In force, except the *no build step* property, superseded by ADR-0023 (annotated in place), and the *command-template runner* half of the agent seam, superseded by ADR-0031 (annotated in place). Live owners: ADR-0016, ADR-0023, ADR-0027, ADR-0014.
Moved here verbatim from `docs/vox/voice-of-owner.md` on 2026-09-21: no wording changed — the entry
keeps the standing positions and the index, and each section below keeps its own date.

## Project Console / GUI (2026-09-14)

- Owner brief, verbatim (line breaks are the owner's):

  > I want a GUI with simple-ish feature set. It would be a sub command for clear
  > record, and it would be pyside2 or web based. Main features:
  > - select a local set of audio tape
  > - run through the clear record pipeline
  > - show a progress bar and estimate
  > - maintain a multi-project glossary table
  > - archive the incoming tape set and its transcript result
  > - user brings their models and agents
  > - user's ai agent does the glossary collection, check the transcript, and
  >   produce a meeting minutes for given meetings, which belongs to a project.
- The selections below are **owner decisions**; the option wording was
  agent-authored. They are not yet implementation — the scoped design lives in
  the local tracker (`project-console` lane, gitignored) and lands as a spec
  plus tickets.
  1. [DECISION] The UI is a **local web UI**, not a Qt desktop app. PySide2 is
     unusable on this project's Python (its wheels stop at 3.10; the project
     requires ≥3.12), so "pyside2 or web" resolves to web.
  2. [DECISION] The agent seam is **both**: an MCP server the owner's agent can
     connect to, and a user-configured command template the console can invoke.
  3. [DECISION] **App-owned registry + user-chosen archive root**: the
     project/glossary/meeting registry lives under the XDG data dir, while
     recordings and archives stay in user-chosen directories (consistent with
     ADR-0006/ADR-0007).
  4. [DECISION] Build order is **headless service first, thin GUI second**.
  5. [DECISION] The registry is **SQLite + file artifacts** (no audio blobs in
     the database).
  6. [DECISION] The web surface is **FastAPI + a no-build frontend**.
  7. [DECISION] The work is **two new workspace members** — a headless service
     and a GUI — matching ADR-0012's anticipated "GUI/MCP member". The published
     `clear-record` dist keeps `numpy`/`soundfile` only.

### Packaging revision, and the harness prior art (2026-09-14)

- Owner directive, verbatim: *"I expect we can bundle the web up with our wheel
  so end user do 'clear-record web' and it just works."* Then, opening the shape:
  *"we can build a '[web]' extra and hide web and UI behind it. You may decide
  which is better - ship it together or dedicate command and wheel."*
- Owner direction on the agent harness, verbatim: *"For potential agent harness
  maa-whirlwind can become a priori art."*
- **Agent resolution of item 7 above** (the owner delegated the packaging
  choice): the web and service **code** ships in the **one `clear-record` dist**
  (as `clear_record.service` + `clear_record.web` subpackages, since `uv_build`
  ships one import package per dist), while the web stack is gated behind a
  **`[web]` extra** (`fastapi`/`uvicorn`) and the MCP SDK behind `[agents]`. So
  the base dist *still* depends only on `numpy`/`soundfile` — item 7's "two new
  workspace members" is superseded, item 7's dependency line is preserved. The
  command is `clear-record web`. See
  [ADR-0013](../../adr/0013-bundled-web-and-service-surface.md).
- **Harness prior art** is the owner's own [maa-whirlwind
  ADR-0005](https://github.com/zhaoweny/maa-whirlwind): an MCP server exposing
  semantic tools over shared services, a reference external MCP consumer, **BYOK**
  (no bundled provider key), and the harness never entering the core. The
  clear-record agent seam follows that shape (MCP server + a command-template
  runner), also recorded in ADR-0013. *(Superseded 2026-09-23 by
  [ADR-0031](../../adr/0031-harness-is-the-only-agent.md): the command-template
  runner is deleted and MCP is the only agent integration — the MCP-server half
  of the shape stands.)*

### Desktop app build (2026-09-14)

- Owner directive, verbatim: *"In the end build a pyinstaller spec, which means
  we could offer Mac and windows build with minimal friction to average
  person"*.
- [DECISION] A **PyInstaller spec** plus a `just app` recipe freeze the console
  into a double-clickable desktop app (`clear-record-web` / `clear-record.app`)
  and the full CLI (`clear-record`), built per-OS and kept as **CI workflow
  artifacts** — never published to an index. Builds are unsigned; the Gatekeeper
  / SmartScreen first-run workarounds are documented, and signing/notarization
  is an explicit future step. No model weights or keys are bundled. See
  [ADR-0014](../../adr/0014-desktop-app-distribution.md).

## App shell: htmx/Alpine, PySide6 tray, pi-agent (2026-09-14)

- Owner directives, verbatim:
  - *"so we would have: a python back-end, a python mcp service, a pi-agent
    powered local agent doing the useful stuff."*
  - *"I'd like to change my decision: let's make a pi-agent compatible vue /
    react app and produce a bundle that our fastapi could serve it; we need
    fastapi or something for the mcp anyway."*
  - *"and beside the obvious web ui route, let's make a pyside2 native tray icon
    app, for supervising the service and give the whole app an entry point"*
  - correction: *"nah, I'd like to change it again. let's try htmx and alpine.js
    for the web-ui."*
- [DECISION] The application shape is **Python backend + Python MCP service +
  pi-agent-powered local agent**. The local agent talks to the MCP server; it is
  a replaceable client, never imported by `clear_record.core` (BYOK — no key or
  agent runtime is bundled). Prior art: maa-whirlwind ADR-0005.
- [DECISION] The web UI is **htmx + Alpine.js**, server-rendered by FastAPI, with
  the two libraries vendored under `clear_record/web/static/` — **no build
  step**. This **supersedes** the briefly-stated Vue/React direction (and
  ADR-0013's embedded Python-string assets). See
  [ADR-0016](../../adr/0016-app-shell-htmx-tray-pi-agent.md).
- [DECISION] A **native tray supervisor** gives the app an entry point:
  `clear-record tray` runs the console in the background and offers open /
  status / quit from the system tray. The owner asked for "pyside2"; **PySide2
  cannot work** on this project's Python (its wheels stop at 3.10, the project
  requires ≥3.12), so this is **PySide6** — noted to the owner at the time.
  It is an optional `tray` extra so the base install stays audio-only.

