# ADR-0028 — Repair malformed LLM output with json-repair

Status: superseded — see [ADR-0031](0031-harness-is-the-only-agent.md) (2026-09-23)
Date: 2026-09-16

- Superseded **in full** by [ADR-0031](0031-harness-is-the-only-agent.md)
  (2026-09-23): the decision below was about the app validating a **model's**
  reply against an output contract. clear-record calls no model, so the contract
  and `json-repair` with it are deleted from `src/` and the base distribution;
  a harness supplies JSON through a tool call, which the MCP boundary already
  parses. Kept as the record of the finding that motivated it.

## Context

- [FACT] The agent tasks (glossary collection, transcript check, minutes) ask a
  model for a JSON object and validate it against a contract; a response that
  cannot be parsed fails the task and writes no draft (ADR-0018).
- [FACT] Running the optional `just agent-drive` against a real endpoint
  (deepseek-chat) showed a strict `json.loads` rejecting a reply that carried the
  contract object followed by a sentence (`Extra data at line 9 column 1`), so
  the minutes task failed. Chat models wrap JSON in prose or a loose fence
  however firmly the prompt asks for JSON alone, and they also truncate, drop a
  bracket, or leave a trailing comma.
- [FACT] A hand-rolled recovery (parse the first well-formed object) handles the
  wrapped case only; the other malformations still fail the task.
- [FACT] `json-repair` (MIT, **zero dependencies**, ~51 KB wheel,
  `requires-python >=3.10`) repairs exactly these shapes and is a drop-in
  fallback for `json.loads`. The lockfile pins **0.63.4**.

## Decision

- [DECISION] Adopt `json-repair` as a **runtime dependency**, used only at the
  agent output contract (`service.agent_tasks._load_document`): a clean response
  is still parsed strictly first — so a genuinely malformed one keeps its exact
  error position — and repair is the fallback.
- [DECISION] Repair is **not leniency**: the contract's schema checks still run on
  the repaired object, so a repaired-but-wrong answer is rejected exactly as
  before. The "contract or nothing" rule is unchanged.
- [DECISION] The dependency is recorded in `THIRD_PARTY_NOTICES.md` (MIT).

## Rationale

- The output comes from a model whose only interface is text; refusing the common
  text wrappers failed a whole task on a cosmetic difference. Repair the envelope,
  keep the schema strict — that is the honest split.
- Zero dependencies means no transitive supply-chain growth, and MIT fits
  ADR-0003.

## Discarded alternatives

- **Keep the hand-rolled first-object recovery** — fixes the wrapped case only;
  truncation and bracket/quote errors still fail a task.
- **Loosen the schema instead** — would accept wrong answers, which ADR-0018
  forbids.
- **Ask the prompt harder for JSON** — the observed failure happened with the
  prompt already saying "answer only with the JSON object".
- **The package's `schema` extra** — pulls pydantic/jsonschema into the runtime
  for a check the contracts already do.

## Consequences

- One small dependency in the base dist; `uv.lock` and `THIRD_PARTY_NOTICES.md`
  move together.
- Revisit if a repaired answer ever passes the schema but is still wrong in a way
  the contracts cannot see — then the contract, not the repair, is the gap.
