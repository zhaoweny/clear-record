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

## Update (2026-09-14) — onboarding and iteration: guide first, bundle as escalation

Owner refinement, verbatim: *"I mean, the user might want to iterate on it, so at
that point we might bundle 1 simple harness or have a guided setup so we ease the
onboard process"*.

- [REQ] This separates **two different needs**, and only one of them is about the
  harness: **onboarding** (reach a working agent experience at all) and
  **iteration** (refine prompts and outputs over repeated runs).
- [DESIGN] **Iteration is largely already covered** by the task design: every run
  records the runner kind, model and **prompt/context hash**, and output lands as
  a draft with accept/reject — so a user can edit a prompt or the glossary,
  re-run, and compare drafts. The residual gap is *acting on feedback within a
  loop*, which is a harness capability.
- [OPEN] **Onboarding is the unresolved half.** Ease it either by (a) bundling
  **one** simple harness, or (b) a **guided setup**. Preferred order: **guide
  first** — a guided setup adds no runtime to the wheel or the desktop bundle and
  is reversible, whereas bundling stays the escalation path this ADR's seam
  already permits as a fourth implementation.
- [DESIGN] A guided setup is **wizard-shaped**: detect a local endpoint (an Ollama
  daemon, say), offer to pull a small model, verify with a test call, and — for
  the MCP rung — detect a client and write its config. The repo already has a
  `wizard` skill for the human-only provisioning steps.
- [OPEN] A **trigger, not a date**, for bundling: bundle one simple harness when a
  guided setup demonstrably fails to get users to a working experience, or when a
  task appears that needs the loop. Until then no runtime ships.

## Update (2026-09-14) — the MCP rung's driving use case: the tuning loop

Owner refinement, verbatim: *"let me be specific - the user might want to tell a
story or iterate the glossary, or do back and forth of glossary <-> actual
transcript, till it's tuned to their need. at that time we might become a simple
mcp service and let the agent to do the heavy lifting"*.

- [REQ] The iterative loop — **glossary ↔ transcript**, conversational, tuned by
  hand until it fits — is the concrete use case that justifies the MCP rung. It is
  not a power-user nicety; it is the natural shape of "tuning to their need".
- [DECISION] For this use case clear-record stays a **simple MCP service** and the
  **agent drives the loop**. We do **not** build a bespoke tuning UI: the work is
  a conversation, and a conversation is the agent's job (ADR-0016's boundary).
- [FACT] **Partial enabling mechanism, and an honest limit.** The chunk cache is
  keyed on the glossary (backend / model / language / glossary / chunk plan), so a
  glossary edit *does* invalidate the cache and force a re-decode — the loop is
  real. But the key holds the **whole glossary string**, so the invalidation is
  **global**: a single term change re-decodes **every chunk of every source**, not
  "only the affected chunks" (an earlier claim in this ADR that was wrong and is
  corrected here). On multi-hour tapes one tuning iteration is therefore minutes
  of GPU time, not seconds. That is exactly why scoped re-runs matter:
- [FACT] **Scoped re-runs were a real blocker on iteration, not polish.** To make
  iteration cheap on a multi-hour tape, a re-run has to be scoped — one source, or
  a time range — or the invalidation narrowed. Until the 2026-09-15 update below,
  the loop worked but cost a full re-decode per glossary edit.
- [FACT] The MCP surface as it stood on 2026-09-14 (ADR-0017, then 14 tools) was
  **not yet sufficient** for the loop; ticket 21 closed all three gaps below (the
  surface now carries 22 tools, including `read_transcript`, `update_project`
  and option-carrying `start_run`):
  - **no transcript-read tool** — `list_artifacts` returns paths and metadata, so
    an agent cannot read the transcript it is meant to reason about;
  - **no `update_project`**, and meetings have no notes — so the **story** the
    user tells has nowhere to persist;
  - `start_run` **cannot set options**, so a re-run cannot deliberately apply the
    glossary (or a model) — ADR-0017's recorded `[OPEN]`.
- [DESIGN] "A simple MCP service" here means **small but complete for the loop**:
  read the transcript, write the story and the terms, re-run with intent. Ticket
  21 closes exactly those three gaps.
- [FACT] A re-run is **scoped** (one source, or a time range) when the caller asks
  for it, and whole-meeting otherwise — see the 2026-09-15 update below.

## Update (2026-09-14, late) — pi-agent is the default agent to *point at or download*

Owner clarification, verbatim: *"user bring their LLM for agentic useage - for
anything LLM like, the agent based glossary management, the agent based
transcription correction, the agent summary of transcript into minutes;
onboarding of agent mode - we ask user to point or download a pi-agent as our
default choice; the tuning of transcript: I think it would happen naturally since
we'd expose the necessary tools"*.

- [DECISION] **pi-agent is the default named agent**, **not bundled** but *asked
  for*: onboarding offers to **point at an existing pi-agent** or **download
  one**. This preserves the no-bundled-runtime decision while giving the guided
  setup (ticket 20) an opinionated default instead of a blank "configure an
  endpoint".
- [DECISION] **The user brings their LLM.** Every LLM-shaped job is **agent-driven**
  — glossary management, transcription correction, and transcript → minutes — not
  a built-in feature we implement ourselves.
- [DECISION] **The tuning loop is emergent, not built.** We expose the tools
  (ticket 21) and the iteration happens in conversation; no bespoke tuning UI and
  no orchestration in the adapter.
- [FACT] **Reversibility holds:** pi-agent is a *choice the setup offers*, not a
  dependency — any MCP-capable harness works equally.

## Update (2026-09-15) — scoped re-runs land; the glossary is tracked per chunk

Scoped in the local tracker's `project-console` lane. The two `[OPEN]`s above
are answered here.

- [FACT] **The cache key is split.** `cli.workspace.chunk_cache_key` still
  returns the same run-meta, but the cache reads it as two parts: the **plan**
  (backend, model, language, the chunk plan, decoder knobs) and the **glossary**.
  A plan change still makes a source's chunk bodies meaningless in place and
  invalidates **every chunk of that source**, exactly as before. What no longer
  invalidates at source level is the glossary: each cached body records the
  glossary **digest** it was actually decoded under.
- [DECISION] **A re-run may be explicitly scoped** (`core.ChunkScope`): one or
  more sources and/or a time range (`--rerun-source`, `--rerun-range
  12:30-18:00`). The chunks the scope selects are re-decoded; every other chunk
  is reused from the cache, whatever glossary it was decoded under.
- [FACT] **An unscoped glossary edit still re-decodes every chunk of every
  source.** That has not changed and is not claimed to have changed: the glossary
  is the decoder's initial prompt, so any chunk's output *can* move. The change is
  that the caller can now state which part to re-decode instead of paying for all
  of it.
- [DECISION] **Carried chunks stay honest.** A reused chunk that was decoded under
  an earlier glossary keeps its old digest, is reported as carried over, and is
  re-decoded by the next unscoped run. A scoped run never claims the current
  glossary was applied to a chunk it did not decode.
- [DECISION] **The conservative near-match guard only ever widens.** A changed
  term — added *or removed* — that plausibly matches a cached transcript pulls
  that chunk back into the re-decode set even when the scope excluded it. It can
  add decodes and cannot remove one, so its failure mode is cost, never a skipped
  chunk. Its limit is stated in `engine.text.term_could_affect`: a term the
  decoder never wrote at all will not be found by it, which is why scope, not the
  guard, decides reuse.
- [DECISION] **Scoping refuses to guess.** An unreadable or empty range, a source
  not in the manifest, a scope that selects no chunk of any selected source, or a
  scope combined with `--no-resume` is an actionable error. The range is validated
  when the command is parsed and the selection before the cache is touched, so a
  typo cannot silently become a full re-decode, a no-op, or a cache wipe.
- [FACT] **The cost is reported, not assumed.** A run logs
  `N re-decoded, M reused` (plus how many reused chunks still carry an earlier
  glossary), records a `chunk_report` in `segments.json`, and returns it on the
  transcription result. The motivating case is pinned by a test: with two sources
  of four chunks each, an unscoped glossary edit re-decodes all 8 chunks and a
  scoped edit (one source, one time range) re-decodes 1.
- [FACT] **What is *not* narrowed: the term.** A scope selects by source and time,
  not by term. A term-subset scope was considered and dropped: mapping a term to
  chunks needs the transcript-matching heuristic above, which is unsound as the
  sole mechanism — the edit that motivates the loop is usually a term the decoder
  never wrote, so the matching chunk is exactly the one a literal match misses.
- [FACT] Exposing the scope through the MCP surface and the console is separate,
  later work; the CLI and the run-options value (`PipelineOptions.rerun_sources` /
  `rerun_range`) carry it today.
