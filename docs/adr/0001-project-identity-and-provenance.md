# ADR-0001 — ClearRecord: clean-room reimplementation, MIT, provenance

Status: active
Date: 2026-09-09

## Context

- [VOICE] The owner had a **personal** concept for a multi-source recording /
  transcription tool, worked out over a series of private conversations. It
  later transferred into a company boundary such that the company-side
  implementation is not the owner's to release.
- [VOICE] The owner wants to rebuild the **original personal concept** as a
  **clean-room** implementation: derive a generic scope from a public problem
  statement rather than recreating the company work, and publish it under MIT.
- [VOICE] Keep Git history from day one and record provenance facts (independent
  development, personal hardware, public docs/models/libraries). This records
  facts; it is not a legal opinion.

## Decision

- [DECISION] Establish a **new** open-source repository named **clear-record**
  (product/repo name, per the naming resolution in the original concept; CLI
  binary `clearrecord`, renamed to `clear-record` on 2026-09-13 — see ADR-0009).
- [DECISION] The implementation is a **clean-room reimplementation** of the
  *generic* concept ("record several audio sources reliably and use local AI to
  turn them into useful transcripts"), scoped from the public problem statement
  in `docs/architecture.md` §2.
- [DECISION] This is **work-unrelated personal OSS**. Do not import work/company
  code, prompts/specs, recordings, datasets, partner names, internal docs or
  credentials. The boundary is documented in `docs/architecture.md` §6.
- [DECISION] License the repo's own code under **MIT** (ADR-0002).
- [DECISION] Record and preserve provenance in the repo docs using the
  FACT / VOICE / REQ / DESIGN / SUGGESTION / OPEN label set.

## Rationale

- The original concept's *generic* core is far too general for a company to own.
  The potentially contentious material is the **particular implementation,
  confidential spec/know-how, and inventions developed in connection with the
  duties** — all excluded by the boundary and the scope in §7 of
  `docs/architecture.md`.
- A clean-room reimplementation from a generic scope + a provenance record is
  the defensible path; importing company artifacts, or regenning from a
  company-spec prompt, would manufacture bad provenance.

## Discarded alternatives

- Reimplementing by reproducing the company implementation — rejected: imports
  company work product and defeats the clean-room boundary.
- Keeping it private indefinitely — not the owner's stated intent (they want it
  under MIT / open).
- Importing the company-side deployment as a feature — out of scope; that is a
  separate, proprietary concern (the "company deployment" split in the original
  concept).

## Consequences / review hook

- The repo must never grow company artifacts; scope changes that would import
  them are rejected and escalated (see `AGENTS.md` hard rules).
- Revisit if the owner later wants a narrow, evidence-backed OSS scope for one of
  §7's excluded ideas — that is a new ADR, not a retroactive import.
