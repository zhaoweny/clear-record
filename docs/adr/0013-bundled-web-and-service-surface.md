# ADR-0013 — The web console ships in the single dist, its dependencies behind a `[web]` extra

Status: active
Date: 2026-09-14

- Superseded in part by [ADR-0016](0016-app-shell-htmx-tray-pi-agent.md)
  (2026-09-14): the console is now server-rendered htmx/Alpine with **package-data
  templates and vendored static assets**, not embedded Python string assets, and
  there is a PySide6 `tray` surface beside `web`.
- Superseded in part by [ADR-0022](0022-adopt-click.md) and
  [ADR-0025](0025-platformdirs.md): the base dist also carries `click` and
  `platformdirs`, not `numpy` + `soundfile` alone. (ADR-0031 removed the
  `json-repair` addition ADR-0028 made here.)

## Context

- [VOICE: owner, 2026-09-14] *"I expect we can bundle the web up with our wheel
  so end user do 'clear-record web' and it just works."* (Recorded in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).)
- [VOICE: owner, 2026-09-14] Immediately after, the owner opened the shape:
  *"we can build a '[web]' extra and hide web and UI behind it. You may decide
  which is better - ship it together or dedicate command and wheel."* The
  trade-off is therefore the agent's to resolve.
- [DECISION: owner, 2026-09-14] In the same session the owner chose a **local
  web UI**, a **FastAPI + no-build frontend**, a **SQLite registry**, and a
  **headless service first** build order.
- [FACT] ADR-0012 anticipated "a future GUI/MCP server is a new `packages/*`
  member", fixed the published dist's runtime dependencies to `numpy` +
  `soundfile`, and deliberately **collapsed five dists into one publisher**
  because multi-dist release machinery (version lockstep, rehearsal, pins) was
  not worth it for a small project.
- [FACT] `uv_build` ships exactly **one import package per dist**; extra
  top-level modules cannot ride one wheel. A separate web dist therefore means
  reviving multi-dist publishing.
- [FACT] [maa-whirlwind ADR-0005](https://github.com/zhaoweny/maa-whirlwind) is
  the owner's prior art for the agent harness: an MCP server exposing **semantic
  tools over shared services**, a **reference external MCP consumer**, **BYOK**
  (no bundled provider key), and **the harness never leaks into the core**.
- [REQ] `clear-record web` must work after one install step, offline.

## Decision

- [DECISION] The service and web surfaces ship as **subpackages of the single
  `clear-record` dist** — `clear_record.service` (headless registry, later runs,
  archive and agent drafts) and `clear_record.web` (the FastAPI app, the bundled
  no-build frontend, and the `web` subcommand). `uv_build`'s one-import-package
  rule makes this the only way to keep **one published wheel**.
- [DECISION] The base dist's runtime dependencies were **`numpy` + `soundfile`**
  at this record's date (ADR-0012 is *preserved*, not amended); see the
  supersession note above for the later additions. The console's stack moves to
  an optional extra:
  - `web = ["fastapi>=0.115", "uvicorn>=0.30"]`

  A CLI-only install therefore stays audio-only; a user who wants the console
  installs `clear-record[web]` (or `uvx --from 'clear-record[web]' clear-record
  web`). The MCP SDK has its own `agents` extra (it landed with ADR-0017's
  server) — no speculative dependency was declared before it did.
- [DECISION] The **`web` subcommand is registered unconditionally** through the
  `clear_record.commands` entry-point group, so it is visible in `--help` and,
  when the extra is missing, fails with an actionable install hint instead of an
  import traceback. Registration by entry point keeps
  `clear_record.cli` from importing `clear_record.web` at all, so the dependency
  arrow stays acyclic and a plain CLI run never pays the web import cost.
- [DECISION] Dependency direction: `web → service → cli → {core, engine,
  providers}`; `core`/`engine`/`providers` still never import `cli`, `service` or
  `web`. The layering guard grows by two layers.
- [DECISION] **BYOK** (bring your own key), inherited from maa-whirlwind
  ADR-0005: no provider key or agent runtime is bundled or defaulted. The user's
  agent connects to the `clear-record mcp` server and owns its own model
  (ADR-0031); the harness never enters `clear_record.core`.
- [DESIGN] The frontend is embedded as **Python string assets**, not files on
  disk, so the wheel needs no package-data configuration and cannot silently
  ship without its UI.

## Rationale

- **One wheel, one publisher.** Bundling the code as subpackages keeps the
  single-dist release machinery ADR-0012 deliberately bought; a dedicated wheel
  would undo it for a small project.
- **The extra answers the install-weight objection.** The owner's "just works"
  desire is met with one word (`clear-record[web]`), while the majority who only
  run the pipeline are not asked to download FastAPI they never import.
- **Discoverable, not mysterious.** Registering `web` unconditionally and
  failing with the exact install command beats hiding the subcommand until the
  extra is present.
- **The headless boundary survives.** `clear_record.service` is importable and
  testable with no web stack — which is what "headless service first" asked for —
  and an MCP client or script can drive it directly.

## Discarded alternatives

- **Web stack in the base `dependencies`** (the first cut of this ADR) — literal
  "no extra step", but it imposes FastAPI/uvicorn on every headless, CI and
  audio-only install; rejected as a tax on the majority for a minority feature.
- **A dedicated `clear-record-web` dist (and a dedicated command)** — matches
  ADR-0012's "new member" shape and gives the sharpest boundary, but revives
  multi-dist publishing (lockstep, rehearsal, pins) and a second command name,
  for a project that just consolidated to one publisher. Rejected.
- **stdlib `http.server`, zero new deps** — smallest and fully offline, but the
  owner explicitly selected FastAPI; retained as the fallback if the extra proves
  unwelcome.
- **A statically-mounted `static/` package directory** — works, but depends on
  build-backend package-data behaviour; string assets remove that risk.

## Consequences / review hook

- `test_packaging.py` gains a guard that the base dependencies stay
  `numpy`/`soundfile` **and** that the `web` extra holds exactly its stack, with
  no leak into the base.
- `test_layering.py` gains the `service` and `web` layers and their allowed
  edges.
- The dev dependency group carries the web stack so the verify gate exercises
  the console; end users get it only through the extra.
- Revisit if the extra proves to be friction ("just install it for me"), or if a
  remote-node deployment wants the service without the UI.
