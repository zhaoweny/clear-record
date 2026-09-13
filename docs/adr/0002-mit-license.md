# ADR-0002 — License clear-record's own code under MIT

Status: active
Date: 2026-09-09

- Superseded in part by [ADR-0012](0012-single-distribution.md) (2026-09-13): the `cr-*` dists are now internal `clear_record` subpackages.

## Context

- [VOICE] Personal, work-unrelated OSS project; the owner wants it under MIT.
- [VOICE] The software itself (audio ingestion, alignment, ASR orchestration,
  reconciliation, export) should be broadly adoptable — MIT is the most
  permissive, lowest-friction choice and carries no copyleft deterrent for a
  future company that might want to adopt or contribute.
- [FACT] The repo is greenfield: no third-party code has been copied in (clean
  room, ADR-0001).

## Decision

- [DECISION] License this repo's own code under **MIT** (SPDX `MIT`). The full
  text lives in `LICENSE` at the repo root.
- Every package declares `license = "MIT"` in its `pyproject.toml` (root
  `clear-record-workspace` virtual project + members `cr-core`, `cr-engine`,
  `cr-providers`, `cr-cli`, `clear-record`).
- No per-file SPDX headers are needed while every file in the tree is
  project-default MIT. If upstream content is ever vendored, that content keeps
  its own license and per-file SPDX markers.

## Rationale

- MIT is the default permissive license for personal Python OSS: short, widely
  understood, no friction with PyPI.
- Apache-2.0 (patent grant + change notices) was considered and set aside: no
  contributor base exists to justify the heavier text.

## Discarded alternatives

- Apache-2.0 — heavier machinery, no present need.
- AGPL-3.0 / GPL for our own code — a dependency's license does not force our
  independent code's license; see ADR-0003 for how copyleft deps are handled.
- Deciding later — the owner chose to record the decision now.

## Consequences / review hook

- MIT here does **not** resolve obligations for distributing any
  vendor/upstream ASR stacks — see ADR-0003/0005 for the boundary.
- Revisit only if our own code must become a derivative of copyleft code (a
  one-way door; not a concern with zero third-party contributors).
