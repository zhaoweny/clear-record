# ADR-0004 — uv workspace layout (core / engine / providers / cli / clear-record)

Status: active
Date: 2026-09-09

## Context

- [VOICE] The pipeline domain model must stay **vendor-free**: no CUDA, ROCm,
  Metal/CoreML, torch/tensorflow or a specific ASR library in the core.
- [VOICE] The `clearrecord` CLI surface should derive from the pipeline spec
  (`ingest → align → transcribe → reconcile → export`; the command was renamed to
  `clear-record` on 2026-09-13 — see ADR-0009) so the CLI and domain cannot
  drift.
- [OWNER] Toolchain decision: use **uv** as the workspace/project tool,
  Python `>= 3.12` (3.14 in use), a single committed `uv.lock`, lint/test via
  ruff + pytest in a root dev group.

## Decision

- [DECISION] Organize the repository as a **uv workspace** with five members
  under `packages/`, each in a `src/` layout:

  ```text
  packages/core         → dist cr-core,       import cr_core      (backend-agnostic domain model)
  packages/engine       → dist cr-engine,     import cr_engine    (audio I/O, alignment, reconcile)
  packages/providers    → dist cr-providers,  import cr_providers (per-vendor ASR adapters)
  packages/cli          → dist cr-cli,        import cr_cli       (the `clear-record` CLI implementation)
  packages/clear-record → dist clear-record,  import clear_record (facade / public command; forwards to cr_cli)
  ```

- Root `pyproject.toml` is the uv workspace root (`[tool.uv.workspace]
  members = ["packages/*"]`, `requires-python = ">=3.12"`, root dev group with
  `ruff` + `pytest`). The root is a **virtual project** (`package = false`),
  never built/published. It is named `clear-record-workspace` because uv
  requires every workspace member to have a distinct name and the published
  `clear-record` name belongs to the facade member.
- Dependency edges: `cr-engine → cr-core`, `cr-providers → cr-core`, `cr-cli →
  cr-core`, `cr-cli → cr-engine`, `cr-cli → cr-providers`, `clear-record →
  cr-cli`. No other member-to-member edges.
- `cr-core` has **no third-party dependencies**. `cr-engine` adds `numpy` +
  `soundfile` (generic numerics + I/O, no vendor/ASR code). Vendor stacks are
  **optional extras** on `cr-providers` (`apple` / `nvidia` / `amd`), mirrored as
  aggregate extras at the workspace root.
- Each member declares `license = "MIT"`. `cr-cli` is the CLI implementation
  (module `cr_cli`) and exposes **no** console script; the `clear-record` facade
  owns the `clear-record` command and forwards to `cr_cli` (ADR-0009).

## Rationale

- **Vendor-free core by construction:** separating `cr-core` makes "core must
  not import vendor code" mechanically enforceable (imports/packaging fail
  loudly if violated).
- **Backend-agnostic providers + CLI:** the CLI selects a backend at runtime; a
  plain dev/CI env needs no GPU framework because vendor stacks are optional
  extras.
- **Single lockfile + one environment:** uv resolves every member in one
  `uv.lock` with one sync, so members can't silently diverge.

## Discarded alternatives

- Single flat package — would put vendor code in the same dependency graph as
  the domain model, making the vendor-free boundary a discipline rule instead of
  a packaging fact.
- Core as a subpackage of the CLI — same contamination problem in reverse.
- Per-package virtualenvs without a workspace — more moving parts and version
  drift for a small personal repo.
- setuptools backend — fine, but its `src/` configuration is more verbose than
  a build backend whose defaults already match the layout; not a binding choice.
  (The build backend is settled separately in
  [ADR-0010](0010-build-backend-uv-build.md): all members use `uv_build`.)

## Consequences / review hook

- Any future member (e.g. a GUI, an MCP server, a `cr-ingest` recorder) is a
  *new* member, not a reparenting.
- Resolved by [ADR-0009](0009-packaging-and-distribution.md) (2026-09-13).
