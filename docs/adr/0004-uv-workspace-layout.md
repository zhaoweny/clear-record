# ADR-0004 — uv workspace layout (core / providers / cli)

Status: active
Date: 2026-09-09

## Context

- [VOICE] The pipeline domain model must stay **vendor-free**: no CUDA, ROCm,
  Metal/CoreML, torch/tensorflow or a specific ASR library in the core.
- [VOICE] The `clearrecord` CLI surface should derive from the pipeline spec
  (`ingest → align → transcribe → reconcile → export`) so the CLI and domain
  cannot drift.
- [OWNER] Toolchain decision: use **uv** as the workspace/project tool,
  Python `>= 3.12` (3.14 in use), a single committed `uv.lock`, lint/test via
  ruff + pytest in a root dev group (mirrors the sibling `maa-whirlwind` repo).

## Decision

- [DECISION] Organize the repository as a **uv workspace** with four members
  under `packages/`, each in a `src/` layout:

  ```text
  packages/core      → dist cr-core,      import cr_core      (backend-agnostic domain model)
  packages/engine    → dist cr-engine,    import cr_engine    (audio I/O, alignment, reconcile)
  packages/providers → dist cr-providers, import cr_providers (per-vendor ASR adapters)
  packages/cli       → dist cr-cli,       import cr_cli       (the `clearrecord` command)
  ```

- Root `pyproject.toml` is the uv workspace root (`[tool.uv.workspace]
  members = ["packages/*"]`, `requires-python = ">=3.12"`, root dev group with
  `ruff` + `pytest`). The root is a **virtual project** (`package = false`),
  never built/published.
- Dependency edges: `cr-engine → cr-core`, `cr-providers → cr-core`, `cr-cli →
  cr-core`, `cr-cli → cr-engine`, `cr-cli → cr-providers`. No other member-to-
  member edges.
- `cr-core` has **no third-party dependencies**. `cr-engine` adds `numpy` +
  `soundfile` (generic numerics + I/O, no vendor/ASR code). Vendor stacks are
  **optional extras** on `cr-providers` (`apple` / `nvidia` / `amd`), mirrored as
  aggregate extras at the workspace root.
- Each member declares `license = "MIT"`. `cr-cli` exposes the
  `clearrecord` console script.

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
- setuptools backend — fine, but hatchling's `src/` defaults are simpler;
  not a binding choice.

## Consequences / review hook

- Any future member (e.g. a GUI, an MCP server, a `cr-ingest` recorder) is a
  *new* member, not a reparenting.
- Revisit on the first real packaging/distribution push.
