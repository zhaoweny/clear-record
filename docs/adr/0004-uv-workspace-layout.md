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
  `ruff`, `pytest` + `trove-classifiers`). The root is a **virtual project** (`package = false`),
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

## Update (2026-09-13) — the four `cr_*` members collapse into one dist

Owner (2026-09-13, verbatim): *"I propose we hide all the `cr_*` layers behind
the scene … I'm not sure I'd release all `cr_*` as different wheels"* — recorded
in [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md). The five-member
Decision and Rationale above are kept for the record; this Update supersedes
them for the workspace layout.

- [DECISION] The four library/CLI members become **subpackages of one published
  dist**, `clear-record` → `clear_record.{core,engine,providers,cli}`. The
  workspace is kept (`members = ["packages/*"]` now resolves to one member), so
  a future GUI/MCP member is still a new member, not a reparenting. See
  [ADR-0012](0012-single-distribution.md).
- [DECISION] The dependency edges are unchanged in substance for the four
  `cr_*` layers — `engine → core`, `providers → core`,
  `cli → {core, engine, providers}` — and are now subpackage imports enforced by
  `packages/clear-record/tests/test_layering.py` rather than by
  member-to-member packaging. That list is not the whole enforced DAG: ADR-0013
  extended the guard to `service` and `web` the next day, ADR-0016 and
  ADR-0017 to `tray` and `mcp`, so the eight layers of the single dist are what
  `test_layering.py`'s `ALLOWED_INTERNAL` is the source of truth for —
  `service → cli` among the allowed edges, deliberately, because the pipeline's
  stage wiring still lives in `clear_record.cli.stages` (ADR-0012's 2026-09-19
  Update states the clause that supersedes the stronger reading; the pipeline
  module — `C3`, [ADR-0030](0030-persistence-layer-and-the-restructure-order.md)
  — is what will let that edge go). `core` still declares/uses no third-party
  dependency.
- [DECISION] The `clear-record` **facade member is gone**: the single dist owns
  the `clear-record` command (`clear_record.cli:main`) and the top-level
  `clear_record` module stays light.
- [DESIGN] `[tool.pytest.ini_options] testpaths` now points at the one
  `packages/clear-record/tests/`; the root's aggregate extras reference
  `clear-record[apple|nvidia|amd|all]`.
