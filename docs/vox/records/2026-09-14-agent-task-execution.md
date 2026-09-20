# Agent task execution — owner-voice record (2026-09-14)

Status: **In force except the in-process rungs.** The owner decided on 2026-09-21 that the in-process BYOK path (endpoint and command runners) is dropped in v0.3, leaving MCP as the only agent integration; the superseding ADR is owed by that slice, so ADR-0018 stands until it lands. The 2026-09-21 correction below the 2026-09-14 text is part of the record.
Moved here verbatim from `docs/vox/voice-of-owner.md` on 2026-09-21: no wording changed — the entry
keeps the standing positions and the index, and each section below keeps its own date.

## Agent task execution: no bundled harness (2026-09-14)

- Owner answer to the last open question — *"should we build / bundle a genuine
  agent harness for jump start the agent experience?"* — is **no bundle**: task
  pipelines over a **BYOK endpoint**. See
  [ADR-0018](../../adr/0018-agent-task-execution.md).
- [DECISION] Three rungs, in setup order: **endpoint (default)** — a
  BYOK/local OpenAI-compatible server, which is what the web UI runs; **command
  (advanced)**; **MCP (power)** for a real agent.
- [FACT] The rationale turns on task **shape**, not preference: glossary
  collection, transcript check and minutes are structured generation, not agentic
  loops, so a harness would import a Node runtime (in the wheel *and* the desktop
  bundle) for capability the default path does not use. maa-whirlwind bundles
  pi-agent because *its* tasks really are agentic.
- [REQ] BYOK is enforced: the credential is read from the environment, never
  stored in config/registry, never bundled; a local endpoint needs no key.
- [VOICE: owner, 2026-09-14] Refinement on onboarding, verbatim: *"I mean, the
  user might want to iterate on it, so at that point we might bundle 1 simple
  harness or have a guided setup so we ease the onboard process"*.
- [DECISION] Split the concern: **iteration** is already covered (prompt/context
  hash + draft accept/reject lets a user re-run and compare); **onboarding** is
  the open half. Preferred order is **guide first** — a guided setup (ticket 20)
  adds no runtime and is reversible; bundling **one** simple harness remains the
  **escalation path**, with a trigger rather than a date. Recorded in
  [ADR-0018](../../adr/0018-agent-task-execution.md)'s 2026-09-14 Update.
- [VOICE: owner, 2026-09-14] Made the iteration half concrete, verbatim: *"the
  user might want to tell a story or iterate the glossary, or do back and forth of
  glossary <-> actual transcript, till it's tuned to their need. at that time we
  might become a simple mcp service and let the agent to do the heavy lifting"*.
- [DECISION] That loop is the **driving use case for the MCP rung**: clear-record
  stays a **simple MCP service** and the **agent drives the loop** — no bespoke
  tuning UI. The shipped surface is not yet sufficient (no transcript read, no
  project/meeting notes write, `start_run` cannot set options); ticket 21 closes
  those three. The glossary-keyed chunk cache already makes re-runs re-decode.
- [FACT, 2026-09-21] **All three of those are closed**, checked against the tree:
  the tool surface ships a transcript read, the project-notes write, and
  `start_run` resolving profile, backend, model, language, glossary and the
  `--auto` opt-in through the same explainable resolvers the console uses. The
  sentence above describes the state on 2026-09-14, not the state on 2026-09-21.
- [VOICE: owner, 2026-09-14, late] Final clarifications, verbatim: *"user bring
  their LLM for agentic useage - for anything LLM like, the agent based glossary
  management, the agent based transcription correction, the agent summary of
  transcript into minutes; onboarding of agent mode - we ask user to point or
  download a pi-agent as our default choice; the tuning of transcript: I think it
  would happen naturally since we'd expose the necessary tools"*.
- [DECISION] **pi-agent is the default agent to point at or download** — named as
  the default in onboarding, never bundled, and never a dependency (any
  MCP-capable harness works). **The user brings their LLM**, and all three
  LLM-shaped jobs (glossary management, correction, minutes) are **agent work**.
  **The tuning loop is emergent**: expose the tools and it happens in
  conversation.

