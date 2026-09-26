# ADR-0031 — The harness is the only agent; the app holds no model and drafts are a version chain

Status: active
Date: 2026-09-23

## Context

- [VOICE: owner, 2026-09-21] The release decision this ADR records: *drop the
  in-process BYOK path*. clear-record stops being an LLM client and becomes a
  **backend for AI harness agents** — the user's harness does the model work, and
  **MCP is the only agent integration**. It is v0.3.0's one breaking change.
- [VOICE: owner, 2026-09-21] Both runners go — `EndpointRunner` *and*
  `CommandRunner` — "along with the prompt renderer and the task plumbing". The
  removal is **complete**: "the code is deleted, not hidden".
- [VOICE: owner, 2026-09-21] The **draft store survives and is extended** to a
  *version chain with author provenance*: each version records which harness/agent
  identity produced it, and which human accepted it. Written through MCP like
  every other artifact; an ADR is owed for that shape.
- [VOICE: owner, 2026-09-21] An existing install's `[agent]` config keys and
  `CR_AGENT_*` variables are **ignored with a message** — never fatal, never
  migrated (the least destructive option).
- [FACT] ADR-0018 shipped the runner seam: an OpenAI-compatible
  `/chat/completions` endpoint as the default, a user command template as the
  advanced path, MCP as the power path, and a `[agent]` config table with
  `CR_AGENT_*` precedence. The three jobs (glossary collection, transcript check,
  minutes) were packaged in-process, rendered prompts and validated answers
  against output contracts.
- [FACT] The three jobs are **not agentic** (ADR-0018's own finding): each is one
  structured generation over a transcript. What ADR-0018 called "structured
  generation" is exactly what a harness does in one or two tool calls — and the
  tuning loop the owner named (glossary ↔ transcript, iterated by hand) is a
  conversation, which is the harness's job, not the app's.
- [FACT] The MCP surface (ADR-0017) already exposes `read_transcript`, the
  registry, the glossary, meetings and runs. What it lacked for the three jobs
  was the **write** side: nothing let a harness hand clear-record what it
  produced.
- [REQ] Local-first and offline once provisioned, BYOK, no bundled key (VOICE
  §4). Holding a model client in-process made clear-record a second, worse
  producer of the same content and put a credential in the app's config
  vocabulary.
- [FACT] `docs/architecture.md` §8 and the console's Agent page carried the
  runner seam into the product: a Settings/Setup flow whose first stage asked for
  an endpoint, and an in-process "Try it" path that could not prove the thing the
  user actually runs (a harness).

## Decision

- [DECISION] **clear-record calls no model.** The endpoint runner, the command
  runner, the prompt renderer, the task plumbing (kinds, packaged context, output
  contracts, fixed pipelines, hashes), the `[agent]` endpoint/model table and the
  `CR_AGENT_*` precedence are **deleted from `src/`**, with their tests. No
  shipped code path speaks an LLM protocol, and there is no code left to reach
  for.
- [DECISION] **MCP is the only agent integration.** The stdio server needs no
  endpoint, no key and no model to be configured; every agent-shaped operation is
  a tool. The console's Agent page is **harness setup**: where to point a
  harness, the MCP command line, the readiness the path can check, and no
  credential field at all — a page that would prompt for a key is a bug now.
- [DECISION] **The three jobs are the harness's, expressed as its tool calls.**
  For each meeting: `read_transcript` for the material; then
  `write_agent_draft` with `kind` = `glossary_collection` (candidate terms),
  `transcript_check` (a corrected revision plus its change list) or `minutes`
  (the meeting/project artifact). The app validates the *kind* and the JSON
  object shape, not the prose: what a model wrote is the harness's claim, and the
  human reviews the claim.
- [DECISION] **The draft store is a version chain with author provenance.** A
  draft is one chain, written under `<workspace>/agent/<draft_id>/draft.json`;
  each version records the **author identity the writer declared**, when it was
  written, its value, and the human's decision (`accepted`/`rejected`, with who
  and when) plus the promotion's outcome. The reviews are the chain:
  - a **new version** written by a harness **re-opens** the draft — a decided
    draft is not accepted on an earlier version's strength, which is what makes
    tuning a conversation rather than a one-shot run;
  - deciding a version **happens once**: a version that already carries a
    decision is returned unchanged, so a repeated accept can never run a
    promotion twice.
- [DECISION] **A 0.2 install's own agent runs are not part of the chain.** The
  0.2 in-process path wrote one directory per run
  (`<workspace>/agent/<kind>-<run_id>/`, with `run.json`); the store reads only
  `<draft_id>/draft.json` and does **not** migrate those runs, so they are named
  on the meeting's page (`MeetingAgent.legacy_drafts`) rather than left silently
  invisible.
- [DECISION] **One chain, one writer at a time.** The console and the MCP server
  are two processes over one workspace, and a chain is one file written whole, so
  a writer **holds the chain** while it reads it and writes it back: a
  `draft.lock` in the chain's own directory (`fcntl.flock` on POSIX,
  `msvcrt.locking` on Windows — advisory and local, which is what this store is),
  and the file is replaced by rename from a temporary name, so a reader sees the
  chain before the write or after it and never half of either. Nothing this app
  writes can leave a chain unreadable; one a hand-edit or a crash leaves unusable
  is **refused rather than written over**, on both reads that write a chain back —
  an append (a harness's re-run) and a decision.
- [DECISION] **A decision names the version it decides.** `accepted`/`rejected`
  carry the version number the human read — required at the store and on every
  surface that offers one (the console's form, the JSON API's query, the MCP tool
  schema and `just agent-drive`'s own calls). There is no default to the newest
  version, so a decision can never be applied to text nobody named; a version that
  is no longer the newest when the decision arrives is refused rather than
  recorded against it.
- [DECISION] **The human's accept/reject survives, and it means something.** An
  acceptance promotes per kind: `glossary_collection` → **candidate** registry
  terms (never confirmed — confirming is the owner's per-term act);
  `transcript_check` → a new `transcript_revision` artifact beside the chain
  (never an in-place overwrite of the reconciled record); `minutes` → the
  meeting's `minutes` artifact. Rejection keeps the whole chain as history.
- [DECISION] **The config story for an existing install is "ignored, with a
  message".** An `[agent]` table and `CR_AGENT_*` variables are reported on the
  Agent page and in the wizard and otherwise left exactly where they are: no
  migration, no fatal error, and an existing config file keeps working. The setup
  state's `endpoint`/`model` keys leave the allow-list (nothing writes them).
- [DECISION] **`just agent-drive` becomes the harness stand-in.** It reaches
  clear-record **only** through MCP tools: it lists the surface, writes the three
  jobs as scripted drafts (no key needed), accepts one and rejects another, and
  only then, if a key is present, lets a real model drive the same tools. Without
  a key it reports the model leg as **skipped** and still exits 0 — the MCP
  surface is proven either way. Its own BYOK variable is `CR_DRIVE_API_KEY`,
  deliberately not `CR_AGENT_*`, which now names what the app ignores.
- [DESIGN] **No new runtime dependency, and one fewer.** `json-repair` existed
  only for the deleted output contract, so it leaves the base distribution
  (ADR-0028 is superseded with it). The MCP entry is a command and args, so it
  has nowhere to put a credential.

## Rationale

- **The default path was empty, and ADR-0018 knew it.** Its own discarded
  alternatives list read "MCP-only — truest to 'bring your own agent', but the
  owner's default path would be empty until the user has configured an MCP
  client". Once the harness is the answer to onboarding (ADR-0016's "point at or
  download a pi-agent"), the in-process path is not a fallback: it is a second
  way to do the same work, with a worse model, a credential in the app, and no
  tools.
- **One producer per artifact.** With the app as a model client, the same
  minutes could be produced in-process or by a harness, with different provenance
  vocabularies. With the harness as the only agent, every agent artifact has one
  producer and one honest provenance record: *who claimed it*.
- **The tuning loop needs no app-side machinery.** A glossary edit invalidates
  the chunk cache and a scoped re-run re-decodes what the user asks
  (ADR-0018's 2026-09-15 update, which stands). The loop is
  `read_transcript` → write terms as a draft → accept → **confirm** the terms
  (only confirmed terms bias the decoder) → `start_run`; every step is already
  a tool.
- **A key cannot leak from a process that never had one.** The removal deletes
  the app's credential vocabulary rather than hardening it.

## Discarded alternatives

- **Keep the command runner as an advanced path.** The owner's decision removes
  both runners: the command template was a second way to run a model in-process,
  and a harness that cannot be reached over MCP is a harness this app cannot
  integrate with anyway.
- **Keep the runners but hide the UI.** "Not hidden": a dormant endpoint client
  is still an LLM client with a credential to configure, and every future
  question about it ("does it stream? which protocol?") would still be the app's.
- **Migrate `[agent]` config into a harness config.** The app cannot know which
  client the user has, where its config lives, or whether the endpoint was even
  running; a silent migration would invent a client. Ignoring with a message is
  the same information with no false claim.
- **A per-version review state machine in the registry.** The chain is a file a
  harness writes and a human reads; keeping the decision on the version (rather
  than in a second store) is what lets "a new version re-opens the draft" fall
  out of the data instead of being enforced.
- **Delete the draft store with the runner.** The store is the human's half of
  the loop — review, accept, reject — and it is exactly what a harness cannot do
  for the user.

## Consequences / review hook

- **The console asks for nothing.** Settings → Agent and `/setup/agent` are the
  harness and MCP-client-config rungs plus the hello-world acceptance check;
  `service.setup` keeps `find_harness`/`resolve_harness`/`write_mcp_config` and
  drops the endpoint probe, the verify call and the managed-block writer.
- **The MCP surface is 22 tools**, the same count, with `run_agent_task` replaced
  by `write_agent_draft`; the draft reads and the two decisions are unchanged in
  name.
- **Provenance is the author, not the model.** `describe_draft` publishes the
  chain (per-version author, written_at, decision, who reviewed it) and the
  newest value; the runner/model/prompt-hash vocabulary is gone with the runner.
- **What a harness must do is documented on the tool**, not in a prompt: each
  kind's expected `value` keys are in `write_agent_draft`'s docstring, and
  `INSTRUCTIONS` tells a connecting agent to read the transcript first.
- **The release notes owe one paragraph** to an existing install: `[agent]` and
  `CR_AGENT_*` are ignored with a message, and a harness is the way to an agent
  experience now.
- [OPEN] Whether a harness-authored draft should also carry a declared **model**
  label (free text, unverified by the app) for a reviewer's benefit; the store
  records only what the writer declares, and this version records the author.
- **Revisit** if a genuinely in-app, model-free job appears (a deterministic
  check, say): that is a feature, not a runner, and it would not bring a
  credential back.

## Update (2026-09-26) — the declared author becomes the transport's actor

- [DECISION] [ADR-0033](0033-the-auth-position.md) supersedes the **declared**
  part of this ADR's author provenance: a draft version's author becomes the actor
  its transport supplies — `mcp` for the stdio adapter, `console` for the console
  — rather than a string a caller chooses. The chain shape, the accept/reject
  decisions, and everything else here stand.
