# ADR-0020 — Outbound webhooks: notifications, never content by default

Status: active
Date: 2026-09-15

## Context

- [VOICE: owner, 2026-09-15] The request, verbatim: *"also: do a web-hook as some
  user might want a notification system to their, umm, knowledge and project
  management system"*. Recorded in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).
- [FACT] This is the **first feature that pushes data outward**, against a
  local-first, offline-once-provisioned stance (VOICE §4) and ADR-0006's privacy
  boundary. Everything else in the project either stays on the machine or reads
  from it.
- [FACT] The service already owns the lifecycle events (`RunManager` runs a
  pipeline; `archive_meeting` writes an archive) and already does work off-thread,
  which is the shape delivery needs.
- [FACT] The receiver is typically a **third party** — a hosted PM or knowledge
  system. It cannot fetch a local artifact path.
- [REQ] A notification problem must never fail, block or slow a run.
- [REQ] A configuration mistake must be **visible**, not silent — this project's
  recurring failure class (a `mcp>=1.0` constraint that did not describe the API,
  a claim that a glossary edit re-decodes "only affected chunks").

## Decision

- [DECISION] **Opt-in outbound webhooks** for `run.started`, `run.finished`,
  `run.failed`, `transcript.ready` and `archive.created`. `glossary.updated`,
  `agent_task.draft` and `minutes.accepted` are **reserved** until the agent-task
  work lands (ADR-0018).
- [DECISION] **Metadata-only by default.** A payload carries an event id, its
  type, `occurred_at`, and the project / meeting / run ids — **no transcript and
  no content**. `include_content` is a **per-endpoint opt-in**.
- [DECISION] **Delivery can never affect a run.** It runs on its own worker with
  bounded exponential backoff, and outcomes are recorded; a test asserts a
  failing endpoint still leaves the run `done` and the meeting `recorded`.
- [DECISION] **HMAC-signed**, with the secret named by `secret_env` and read from
  the **environment** — never stored in the registry (the BYOK rule). When the
  named variable is unset the endpoint **fails closed**: it refuses to send
  unsigned rather than letting the receiver reject it silently.
- [DECISION] **Config problems are loud but not fatal.** A malformed config, an
  unknown event name or an unset secret is reported to stderr as an actionable
  one-liner and exposed on `WebhookConfig.problems` / `WebhookEmitter.problems`,
  while the app keeps running. Delivery failures stay separately visible in
  `deliveries()`, so "endpoint unhealthy" and "config broken" are distinguishable.
- [DECISION] Config follows ADR-0007's precedence: **explicit argument >
  `CR_WEBHOOKS` > config file > disabled**.
- [DECISION] **No new base dependency** — stdlib `urllib`.

## Rationale

- **Local-first survives as the default.** Nothing leaves the machine unless the
  user configures an endpoint; the feature is opt-in end to end.
- **Metadata-only by default makes the common case safe.** "Your record is ready"
  does not require shipping a private transcript to a third party, and the opt-in
  keeps the power for those who genuinely want it.
- **Fail-closed matches the endpoint's expectation** — it asked for a signature,
  so unsigned delivery would be rejected anyway while hiding the misconfiguration.
- **Loud config errors are the only way a user can answer "why is nothing
  arriving?"** Silently disabling looks exactly like "no events happened".

## Discarded alternatives

- **Content by default** — would leak private transcripts to a third party by
  default; contradicts ADR-0006's spirit.
- **Delivering inline from the run** — makes an endpoint's health part of a run's
  success. Rejected.
- **Best-effort unsigned delivery when the secret is missing** — the receiver
  expects a signature; unsigned would fail anyway and the misconfiguration would
  stay hidden.
- **Silently disabling on a bad config** — the shipped-first behaviour. Rejected
  in review: a typo must not look like "no events happened".
- **A general integration framework / plugin system** — out of scope; this is
  *notifications*, not a workflow engine.

## Consequences / review hook

- [OPEN] **No content producer yet.** `include_content` is implemented and tested,
  but nothing generates a summary, and sending the raw transcript would contradict
  the metadata-only decision. The likely answer is an **agent-produced summary**,
  which needs the agent runtime (ADR-0018) — so content stays off until then.
- [OPEN] The console does not yet surface delivery status or config problems (both
  are on stderr and on the emitter); a small status view is the natural follow-up.
- [OPEN] Per-project endpoints, per-event filtering in the UI, and whether the
  process-lifetime config cache should be re-read on demand.
- **Revisit** if delivery grows past notifications — e.g. a user wanting to *write
  back* to their PM system is a different feature with its own privacy story.
