# ADR-0017 — The MCP server is the agent boundary (a thin adapter over the service)

Status: active
Date: 2026-09-14

## Context

- [VOICE: owner, 2026-09-14] The owner fixed the application shape:
  *"so we would have: a python back-end, a python mcp service, a pi-agent powered
  local agent doing the useful stuff."* (Recorded in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).)
- [VOICE: owner, 2026-09-14] The owner's brief for the console names the agent
  seam directly: *"user brings their models and agents"* and *"user's ai agent
  does the glossary collection, check the transcript, and produce a meeting
  minutes for given meetings, which belongs to a project."*
- [VOICE: owner, 2026-09-14] On the harness, verbatim: *"For potential agent
  harness maa-whirlwind can become a priori art."*
- [FACT] [maa-whirlwind
  ADR-0005](https://github.com/zhaoweny/maa-whirlwind) is the owner's prior art:
  an MCP server of **semantic tools over shared services**, a reference external
  MCP consumer, **BYOK** (no bundled provider key), and the harness never
  entering the core.
- [FACT] ADR-0013 kept the single `clear-record` dist and reserved an **`agents`**
  extra for the MCP SDK once the server landed; ADR-0016 made **MCP the agent
  boundary** and pi-agent a replaceable client. Both left the server itself
  unbuilt.
- [FACT] The headless `clear_record.service` already owns projects, the
  per-project glossary, meetings and tape sets, background pipeline runs
  (`RunManager`) and run artifacts — the exact operations the owner's agent needs.
- [FACT] "MCP" is a request/response protocol; the SDK ships an **in-process
  client** for tests, so the adapter is testable without a subprocess or network.
- [FACT] The implementation targets the SDK's **v2** server API
  (`mcp.server.MCPServer`; v2 renamed v1's `FastMCP`) and its `ToolError`
  (`mcp.server.mcpserver.exceptions`). The `agents` extra must therefore floor at
  **`mcp>=2`**: a v1 install exposes a different class path and would fail at
  import, so `mcp>=1.0` would describe an API the code does not use.
- [REQ] `clear-record mcp` must start a server over the service; tools must be
  thin translations of service calls (no domain logic), return structured results
  and fail actionably; the base install stays audio-only.

## Decision

- [DECISION] The server is a subpackage of the **one** `clear-record` dist,
  `clear_record.mcp` — the same shape as `clear_record.web` and
  `clear_record.tray` (`uv_build` ships one import package per dist, ADR-0012).
  Its dependency, the official Python SDK, is the optional **`agents`** extra
  (`mcp>=2`, matching the v2 API the code uses), never a base dependency.
- [DECISION] `clear-record mcp` is registered **unconditionally** through the
  `clear_record.commands` entry point
  (`mcp = "clear_record.mcp:register"`), so the CLI never imports the SDK and a
  missing extra fails with an install hint naming `clear-record[agents]` (and
  `clear-record web` as the no-agent alternative) instead of a traceback
  (ADR-0013's seam).
- [DECISION] The transport is **stdio**: the user's agent launches
  `clear-record mcp` as a subprocess and speaks MCP over its stdin/stdout. A
  network transport is deliberately out of scope for v1.
- [DECISION] The server is a **thin adapter over `clear_record.service`**. Each
  tool is one service call plus argument marshalling; validation, status
  transitions and event recording stay in the service. The surface:
  - projects: `list_projects`, `get_project`;
  - glossary: `list_glossary_terms`, `add_glossary_term`,
    `update_glossary_term`;
  - meetings: `list_meetings`, `get_meeting`, `create_meeting`,
    `set_meeting_tapes`;
  - runs: `start_run`, `list_runs`, `run_status`, `run_events`;
  - artifacts: `list_artifacts`.
- [DECISION] Tools return **structured, JSON-serialisable** values (annotated
  `dict`/`list[dict]`), so the SDK publishes an output schema and an agent gets
  machine-readable data. Anticipated failures (unknown project/meeting, no tape
  set, unknown run, invalid status) are raised as the SDK's **`ToolError`** with
  a message naming what was wrong and what is available; an uncaught exception
  would reach the model only as `Error executing tool <name>`. A **backend
  failure** is not knowable at start time (runs execute in the background); it is
  recorded by the service and returned by `run_status`/`run_events`.
- [DECISION] **BYOK.** The server never reads, requires or bundles a model or
  provider credential; pi-agent (or any MCP-capable client) is external and
  replaceable.
- [DECISION] Dependency direction: `mcp → {core, service}`; the harness
  **never enters `clear_record.core`**, and `mcp` never imports `web`, `tray` or
  the CLI. The layering guard grows by one layer.

## Rationale

- **One boundary, one tested seam.** MCP, the JSON API and the GUI all sit over
  the same service, so an agent drives exactly what a human clicks and the
  pipeline cannot drift from the CLI's behaviour.
- **The agent stays replaceable.** Exposing the service over a protocol rather
  than embedding an agent keeps the owner's model and harness choice theirs, and
  keeps a Node runtime and a provider credential out of an MIT Python wheel.
- **The base install stays audio-only.** The SDK and its dependency tree land
  only in `clear-record[agents]`, matching ADR-0013's extra pattern.
- **Discoverable, not mysterious.** Registering `mcp` unconditionally and failing
  with the exact install command is friendlier than hiding the subcommand.
- **Offline and local.** stdio needs no port, no auth and no network, which is
  the project's whole stance.

## Discarded alternatives

- **Bundling pi-agent (Node) inside the wheel** — would put a Node runtime and a
  non-Python agent into an MIT Python distribution and couple the release to an
  agent version; the MCP boundary keeps the agent external (ADR-0016).
- **A dedicated `clear-record-mcp` dist** — the sharpest boundary, but it revives
  multi-dist publishing (version lockstep, rehearsal, pins) for a project that
  consolidated to one publisher (ADR-0012).
- **SSE / Streamable HTTP transport now** — would add a listening socket and an
  auth story to a local-first tool that already has a JSON API for remote needs;
  stdio is the smallest thing that works.
- **Putting domain logic in the tools** — every extra rule in the adapter is a
  rule the GUI and API do not share, and the drift the service was built to
  prevent.
- **Extending the tool surface with project creation/rename and run options in
  this slice** — the operations exist in the service and can be added later;
  keeping the first surface small keeps the adapter reviewable. (See the open
  questions below.)

## Consequences / review hook

- `test_layering.py` gains the `mcp` layer (`→ {core, service}`) and its
  isolation case; `test_packaging.py` asserts the `agents` extra holds exactly
  `mcp` and the `mcp` entry point is declared, with no leak into the base
  dependencies.
- `tests/mcp/` drives the server through the SDK's **in-process client**, so tool
  registration, structured results and actionable errors are asserted against the
  real protocol layer with no ASR backend and no GPU.
- The tool surface is reviewed as a whole (`TOOL_NAMES`): a rename is a visible
  diff, so an agent's tool names cannot change silently.
- [DECISION] `start_run` accepts the run's options — `profile`, `backend`
  (including the `--backend auto` sentinel), `model`, `language`, `glossary` and
  the opt-in `--auto` — and resolves them through the service's re-exported
  resolver, `clear_record.service.resolve_run` (the same code the console
  calls, so `mcp` still never imports the CLI). The resolved options are what
  run; the resolver's explanation (the CLI's own words) is returned to the
  caller and recorded in the run meta, so an agent sees what `--auto` decided.
  No-backend-available and `--auto`'s model-not-on-disk come back as
  `ToolError`s. Closes the earlier open question on run options.
- [OPEN] Run **stop** is not exposed: `RunManager` has no stop operation yet.
- [OPEN] Agent-task tools (create/list, fetch context, submit result) are not in
  this slice because the service's agent runner (ticket 07) and tasks (08–10)
  are not built yet; they join this surface when they land.
- Revisit if a client needs a network transport, or if the extra proves to be
  friction.
