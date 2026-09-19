# ADR-0012 — One published distribution: `cr_*` layers become `clear_record` subpackages

Status: active
Date: 2026-09-13

## Context

- [VOICE: owner] 2026-09-13: *"I propose we hide all the `cr_*` layers behind
  the scene … I'm not sure I'd release all `cr_*` as different wheels"*. The
  owner proposes **one published dist** with the layers kept internal. (Recorded
  verbatim in [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).)
- [FACT] ADR-0004 fixed five workspace members (`cr-core`, `cr-engine`,
  `cr-providers`, `cr-cli`, `clear-record`) and ADR-0009 published all five in
  lockstep with exact `==` pins between them.
- [FACT] `uv_build` (ADR-0010) ships **exactly one import package per dist**
  — its subpackages included. `source-include` only affects the *sdist*, not the
  wheel. One wheel therefore cannot carry four top-level modules (`cr_core`,
  `cr_engine`, `cr_providers`, `cr_cli`); keeping them separate means four
  published wheels, which the owner does not want.
- [FACT] The vendor-free boundary was enforced **only by packaging**: `cr-core`
  declared **no third-party dependencies**, so vendor code could not enter its
  dependency graph. No test backed the rule. Collapsing to one dist removes that
  packaging fact, so the boundary needs an explicit test.
- [REQ] The hard rule stands (`AGENTS.md`, ADR-0003/ADR-0005): the core layer
  must never import CUDA, ROCm, Metal/CoreML, torch/tensorflow, a specific ASR
  library or any other vendor code; the audio/engine layer may use
  numpy/soundfile; vendor stacks stay behind the `Backend` interface.

## Decision

- [DECISION] **Publish one distribution**, `clear-record`, whose single import
  package is `clear_record`. The former members become **subpackages** that keep
  the layering:

  ```text
  clear_record.core       ← cr_core       backend-agnostic domain model (no third-party deps)
  clear_record.engine     ← cr_engine     audio I/O, alignment, reconcile (numpy, soundfile)
  clear_record.providers  ← cr_providers  per-vendor ASR backend adapters
  clear_record.cli        ← cr_cli        the `clear-record` command implementation
  ```

- [DECISION] The dist depends on **`numpy` and `soundfile` only** — no `cr-*`
  dependencies and no `[tool.uv.sources]` (nothing intra-workspace to pin). The
  `apple`/`nvidia`/`amd`/`all` backend extras remain no-op markers (ADR-0005).
- [DECISION] The dist owns the `clear-record` console script, targeting the CLI
  layer directly (`clear_record.cli.cli:main`). The top-level `clear_record`
  `__init__.py` is **light** (a docstring only) so `import clear_record` does
  not pull in the CLI or the audio stack (ADR-0010's facade module is gone along
  with the facade dist).
- [DECISION] The **import-boundary test**,
  `packages/clear-record/tests/test_layering.py`, now enforces what packaging
  used to: it parses the `clear_record` source with the stdlib `ast` module and
  asserts (a) `core` imports no third-party package and no sibling layer, (b) the
  allowed internal edges — at this date `engine → core`, `providers → core`,
  `cli → {core, engine, providers}`, the edges between the four `cr_*` layers the
  collapse kept (ADR-0013 added `service` and `web` the next day, ADR-0016 and
  ADR-0017 `tray` and `mcp`, so the guard's DAG is eight layers today) — and (c)
  that `core`, `engine` and `providers` do not import `clear_record.cli`. It is
  dependency-free, and it must genuinely fail when the rule is broken.
- [DESIGN] The workspace is **kept** (root is still a virtual project with
  `members = ["packages/*"]`), so a future GUI/MCP server is a *new* member, not
  a reparenting. The root's aggregate extras now reference
  `clear-record[apple|nvidia|amd|all]`, and `[tool.pytest.ini_options]`
  `testpaths` points at the one `packages/clear-record/tests/`.
- [DESIGN] The release machinery was simplified for one publisher on 2026-09-13
  (ADR-0011's Update "the bump is native `uv version`; the lockstep script is
  gone"): `scripts/bump-version.py` is deleted and the bump is native `uv version`
  behind the `just` recipes. The workflows keep running `just verify` /
  `just build` and smoke-installing the command.

## Rationale

- **One install, hidden internals.** `uvx clear-record` / `uv tool install
  clear-record` fetch one dist; the `cr_*` layer names never appear on PyPI as
  separate projects.
- **The layering is kept, not flattened.** The subpackages preserve the same
  import DAG, so the code stays as navigable as before; only the distribution
  surface changes.
- **A test replaces the packaging fact.** `uv_build`'s one-module-per-dist rule
  makes the old "empty dependency list" guard impossible, so the boundary moves
  from metadata to an explicit, code-level check that can fail loudly.

## Discarded alternatives

- **Publish all four `cr-*` wheels** — the owner would rather not release them
  separately, and it is more release machinery for a small project.
- **One flat top-level module** — would lose the layer boundaries that keep the
  core vendor-free and the domain model separable.
- **Keep the packaging boundary only** — no longer possible: one dist's
  dependency list cannot express "this subpackage uses nothing third-party".

## Consequences / review hook

- **Layer edges are now a test obligation.** A new cross-layer import outside
  the DAG fails `test_layering.py`; extending the DAG is a deliberate change
  here and in ADR-0004, not a quiet widening.
- **The facade is gone.** ADR-0009's facade/pin machinery is superseded for the
  published set. The **root/member version lockstep stays** (the two `version =`
  literals still move together, now via a second `uv version` call), and the
  cross-dist `==` pin machinery is gone (ADR-0011's Update "the bump is native
  `uv version`; the lockstep script is gone").
- Revisit if a future member (GUI, MCP server) is added — it becomes a second
  workspace member/dist, not a new subpackage of `clear_record`.

## Update (2026-09-19) — clause (c) stated in the guard's own terms

- [FACT] Clause (c) above was written as "no internal layer imports
  `clear_record.cli`". It held for the four layers this ADR named; ADR-0013
  added `service` and `web` to the guard the next day, and `service` imports the
  CLI package **deliberately** — ten of `service`'s modules do:
  `service.agent_flow`, `service.auto`, `service.benchmark`,
  `service.diagnostics`, `service.glossary`, `service.hello_tape`,
  `service.managed`, `service.runs`, `service.setup` and `service.transcript` —
  because the stage wiring the pipeline runs is still
  `clear_record.cli.stages`. The guard's `ALLOWED_INTERNAL` carries
  `service → cli` as an edge and its docstring says so, so what
  `test_no_layer_imports_the_cli` enforces is the narrower rule: **`core`,
  `engine` and `providers` never import `cli`**.
- [FACT] The edge list in (b) is likewise the edges between the four `cr_*`
  layers at this ADR's date, not the eight-layer DAG the guard enforces today
  (ADR-0004's Update carries that form); `test_layering.py`'s `ALLOWED_INTERNAL`
  is the source of truth for it.
- [DECISION: spec author, 2026-09-19] The **stronger** rule — no layer above
  `cli` imports it — is scheduled rather than held, reviewed by the owner when
  the spec was dispatched: the pipeline module (item `C3`, the next sprint's
  headline; [ADR-0030](0030-persistence-layer-and-the-restructure-order.md))
  moves the stage wiring out of the CLI package, which is what lets the
  `service → cli` edge go. Restate this clause and the guard together when it
  lands; until then the guard is the rule and the stronger clause is not.
