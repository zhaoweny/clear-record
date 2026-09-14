# ADR-0018 — Agent tasks run as BYOK pipelines; no bundled agent harness

Status: active
Date: 2026-09-14

## Context

- [VOICE: owner, 2026-09-14] The default agent path is the **web UI**; a command
  template is the **advanced** path; *"if they want custom command, they can talk
  to the agent"*. Recorded in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).
- [VOICE: owner, 2026-09-14] Asked directly whether to build/bundle a genuine
  agent harness *"for jump start the agent experience"*; chose **no bundle** —
  task pipelines over a BYOK endpoint.
- [FACT] The three named tasks are **structured generation, not agentic**: glossary
  collection (transcript + glossary → candidate terms), transcript check (→ a
  corrected revision + change list), minutes (→ a Markdown document). None needs
  tools, iteration or a decision loop.
- [FACT] The owner's [maa-whirlwind
  ADR-0005](https://github.com/zhaoweny/maa-whirlwind) bundles **pi-agent**
  precisely because *its* tasks are agentic (observe → infer → decide → execute
  against a live game), and it accepts the cost deliberately: a bundled
  **Node ≥ 22** runtime, a non-Python agent inside an MIT Python dist, and release
  coupling to the agent.
- [FACT] ADR-0016 makes **MCP the agent boundary** and already lists bundling
  pi-agent as a discarded alternative; ADR-0017 ships the MCP server.
- [REQ] Local-first and offline once provisioned, BYOK, no subscription
  (VOICE §4); a provider key is never bundled.
- [REQ] The three tasks must run with **zero agent setup** from the web UI.

## Decision

- [DECISION] **No bundled agent harness.** clear-record ships no agent runtime
  and no Node.
- [DECISION] Agent tasks execute through a **provider-agnostic runner seam**:
  given a task (kind + inputs + packaged context), produce an artifact plus
  provenance. Three implementations, ordered by setup cost:
  1. **Endpoint — the default.** An OpenAI-compatible chat-completions endpoint
     the user brings: a *local* server (Ollama / LM Studio / llama.cpp) or a
     hosted BYOK endpoint. This is what "click in the web UI" runs.
  2. **Command — advanced.** A user-configured command template.
  3. **MCP — power.** The user's own agent connects to `clear-record mcp` and does
     richer, tool-using work.
- [DECISION] **BYOK is enforced.** The credential comes from the environment (or
  the endpoint's own auth), is never stored in the config or registry, and is
  never bundled. A local endpoint needs no key at all.
- [DECISION] **Task pipelines, not a general loop.** Where quality needs more than
  one pass, a task is a small **fixed pipeline** of calls (e.g. collect → dedupe →
  verify), written per task, reviewable, and testable against a fake endpoint.
- [DECISION] **The harness decision stays reversible.** Nothing in the seam
  assumes a runtime, so a bundled harness could be added later as a fourth
  implementation without reworking the tasks.
- [DESIGN] Prefer the stdlib (`urllib`) for the endpoint call so the base install
  gains no HTTP dependency; a richer client, if needed, lands in the `agents`
  extra.

## Rationale

- The tasks are not agentic, so a harness buys capability the default path does
  not use, at a real cost: Node in both the wheel and the desktop bundle, plus
  coupling our release cadence to an agent's.
- BYOK plus a **local** endpoint delivers "zero agent setup" **and** stays
  offline — something neither a bundled-key nor a hosted-only design can do.
- MCP already gives anyone who wants multi-step autonomy a way to bring it today,
  without us shipping it.
- A provider-agnostic seam means the decision can be revisited with evidence
  rather than reversed with a rewrite.

## Discarded alternatives

- **Bundle pi-agent (Node), mirroring maa-whirlwind** — the precedent is real but
  the task shape is not; it imports a Node runtime, a non-Python component in an
  MIT dist, and version coupling for capability the default path doesn't need.
- **MCP-only (no built-in execution)** — truest to "bring your own agent", but the
  owner's default path (click in the web UI) would be **empty** until the user has
  configured an MCP client.
- **A bundled provider key / cloud-only path** — violates BYOK and the offline
  stance (VOICE §4).
- **A general in-process agent loop/framework** — a framework to maintain with no
  task that needs it.

## Consequences / review hook

- Tickets 07–10 (agent-task runner and the three tasks) are **re-scoped to the
  ladder**; ticket 07 owns the seam plus the endpoint implementation.
- Provenance per artifact records the **runner kind, model and prompt hash**; the
  draft→accept/reject review states are unchanged.
- New config surface (plumbing, not a product surface): the endpoint URL/model and
  the command templates. **Never a key.**
- [OPEN] Which endpoint protocol(s) to support beyond OpenAI-compatible
  `/chat/completions`; the config schema; and whether the endpoint path needs
  streaming for long transcripts.
- **Revisit** if a task appears that genuinely needs multi-step tool use — then a
  bundled harness is an evidence-backed fourth implementation, not a rewrite.
