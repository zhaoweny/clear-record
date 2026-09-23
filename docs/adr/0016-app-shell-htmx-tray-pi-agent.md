# ADR-0016 — App shell: htmx/Alpine console, PySide6 tray, pi-agent as the local agent

Status: active
Date: 2026-09-14

- Superseded **in part** by [ADR-0023](0023-frontend-toolchain.md) (2026-09-15):
  the front-end now has a real toolchain (Tailwind v4 + a bundler) and **ships
  compiled assets**, so this ADR's *"no build step"* property is superseded. Its
  reasoning survives in a different form — offline, with no runtime toolchain —
  and its **htmx/Alpine over server-rendered Jinja** choice, the tray, and the
  pi-agent boundary all stand.
- Superseded **in part** by [ADR-0031](0031-harness-is-the-only-agent.md)
  (2026-09-23): **MCP is the only agent integration** — the harness owns the
  model and pi-agent is a replaceable client. What does not stand is
  this ADR's claim that clear-record owns the **task context/contract**: with no
  in-process runner there is no packaged context and no prompt renderer, so a
  harness reads the transcript and writes its result as a draft (ADR-0031).

## Context

- [VOICE: owner, 2026-09-14] The owner settled the whole application shape in
  one run of decisions (recorded in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md)):
  - *"so we would have: a python back-end, a python mcp service, a pi-agent
    powered local agent doing the useful stuff."*
  - *"let's make a pi-agent compatible vue / react app and produce a bundle that
    our fastapi could serve it; we need fastapi or something for the mcp anyway."*
  - *"beside the obvious web ui route, let's make a pyside2 native tray icon app,
    for supervising the service and give the whole app an entry point"*
  - then a correction: *"nah, I'd like to change it again. let's try htmx and
    alpine.js for the web-ui."*
- [FACT] **PySide2 is unusable here**: its wheels stop at Python 3.10 and the
  project requires `>=3.12`. The tray therefore uses **PySide6**, the current Qt
  binding. (This was flagged to the owner when the tray was requested.)
- [FACT] ADR-0013 chose FastAPI + a **no-build** frontend and shipped the UI as
  embedded Python strings; ADR-0013 also kept the web stack in a `web` extra and
  registered `clear-record web` through the `clear_record.commands` entry point.
- [FACT] `uv_build` **does** ship non-Python files inside the import package
  (verified: a probe file under `clear_record/web/static/` appeared in the
  wheel), so templates and vendored libraries can be package data rather than
  Python string literals.
- [FACT] The owner's own [maa-whirlwind
  ADR-0005](https://github.com/zhaoweny/maa-whirlwind) is the prior art for the
  agent: an MCP server of semantic tools over shared services, a reference
  external MCP consumer, **BYOK**, and the harness never entering the core.
- [FACT] MCP is a request/response protocol; the web app needs an HTTP surface
  regardless, so FastAPI is not extra weight for the console alone.

## Decision

- [DECISION] **The console is htmx + Alpine.js, server-rendered by FastAPI.**
  htmx drives partial updates (`/ui/*` returns HTML fragments); Alpine.js owns
  local UI state. Both libraries are **vendored** under
  `clear_record/web/static/` (htmx 2.0.4, Alpine 3.14.9), so there is **no build
  step** and no npm toolchain in CI, and the console works offline. This
  **supersedes** the brief Vue/React direction and ADR-0013's embedded-string
  assets.
- [DECISION] Two surfaces over one service adapter: **`/api/*` JSON** (scripts,
  the MCP server, and any future client) and **`/ui/*` HTML fragments** (the
  browser). Neither contains domain logic.
- [DECISION] Templates (`web/templates/*.html`) and static assets
  (`web/static/*`) are **package data**: `uv_build` ships them in the wheel and
  the PyInstaller spec collects them explicitly (`collect_data_files`), or the
  frozen app would serve a blank page. `jinja2` and `python-multipart` join the
  `web` extra.
- [DECISION] **The native entry point is `clear-record tray`**, an optional
  **`tray` extra** with **PySide6**. It supervises the console from a system-tray
  icon (open / status / quit) and is registered through the
  `clear_record.commands` entry point like `web`, so the base dist stays
  audio-only. The supervision logic is a **Qt-free `ServiceController`**, which
  is what the tests exercise; the Qt shell is thin and untested in CI.
- [DECISION] **The local agent is pi-agent**, reached over the Python **MCP
  server** (per maa-whirlwind ADR-0005). MCP is the only agent integration;
  pi-agent is the default, replaceable client we point at it, so any
  MCP-capable harness works.
  Credentials are **BYOK** — never bundled, and an agent runtime never enters
  `clear_record.core`. (ADR-0031: clear-record owns no task context or prompt
  contract and calls no model of its own.)
- [DECISION] Layer DAG grows by `tray` (may import `core`, `service`, `web`).
  The base dist's runtime dependencies are unchanged (`numpy`, `soundfile`).

## Rationale

- htmx + Alpine gives the interactive, table-heavy console the owner wants
  without a JS build step, a lockfile, or npm in the release pipeline — the
  smallest thing that is pleasant to use, and it matches the repo's minimalism.
- Vendoring two small, permissively licensed libraries keeps the console
  offline-first, which is the project's whole stance.
- A tray app is the honest "entry point" for a non-developer: the web UI is the
  product, the tray is how it is started and stopped without a terminal.
- Keeping MCP as the agent boundary (rather than embedding an agent) preserves
  replaceability and BYOK, and matches the owner's own prior art.

## Discarded alternatives

- **Vue/React SPA** (the owner's previous direction) — richer components, but
  adds a Node toolchain, a frontend lockfile and a CI build step to a console
  that is mostly tables and forms; htmx covers the same interactions.
- **Embedded Python string assets** (ADR-0013) — no longer necessary once
  `uv_build` was shown to ship package data, and it made the UI unmaintainable
  (a Jinja template inside a Python string).
- **PySide2** — impossible on Python >= 3.12; PySide6 is the current binding.
- **Bundling pi-agent (Node) inside the wheel** — would put a Node runtime and a
  non-Python agent into an MIT Python distribution, and couples the release to an
  agent version. The MCP boundary keeps the agent external and replaceable.

## Consequences / review hook

- Template/static data is now part of the wheel contract: the packaging test
  guards that the files exist, and the PyInstaller spec must keep collecting
  them. A new optional surface follows the same pattern.
- The tray's Qt shell is deliberately outside the verify gate (no display in
  CI); its supervision logic is on the gate. Revisit if a headless Qt platform
  proves reliable enough to test the shell.
- The base install stays audio-only; `web`, `tray` and `agents` are extras (the
  MCP SDK landed behind its own extra, ADR-0017).
- Layers may import the CLI's stage wiring but never the other way around; the
  layering guard grows with the DAG (ADR-0012).
