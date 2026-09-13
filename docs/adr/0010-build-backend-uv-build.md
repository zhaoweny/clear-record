# ADR-0010 — Build backend: `uv_build` for every dist

Status: active
Date: 2026-09-13

## Context

- [VOICE: owner] 2026-09-13: *"use `uv_build` to replace `hatchling`, so we are
  uv front-to-back, end to end."* (Recorded in
  [`docs/vox/voice-of-owner.md`](../vox/voice-of-owner.md).)
- [FACT] Before this decision every workspace member and the virtual root
  declared `hatchling`:

  ```toml
  [build-system]
  requires = ["hatchling"]
  build-backend = "hatchling.build"
  ```

  and each built member carried a `[tool.hatch.build.targets.wheel]` block.
  `uv` was already the resolver, lockfile owner and command runner (ADR-0004),
  so `hatchling` was the one remaining non-uv build tool in the chain.
- [FACT] `uv` ships its own PEP 517 backend, `uv_build`, whose default module
  discovery already matches the members' layouts: `cr-core` → `src/cr_core`,
  `cr-engine` → `src/cr_engine`, `cr-providers` → `src/cr_providers`,
  `cr-cli` → `src/cr_cli`, `clear-record` → `src/clear_record`. No per-package
  wheel configuration is needed.
- [FACT] `uv_build` supports editable installs (PEP 660), so `uv sync
  --all-packages` keeps installing the members editable from a checkout.
- [FACT] `uv_build` exposes a `[tool.uv.build-backend] module-name` override
  for any dist whose import name does not match its distribution name; none is
  needed today.
- [FACT] `uv_build` requires every distribution to ship a Python module and
  fails otherwise (`error: Expected a Python module at:
  src/<name>/__init__.py`). The `clear-record` facade had been metadata-only
  (`bypass-selection = true`); it now ships a real module instead (ADR-0009).
- [FACT] uv was `0.12.7` at decision time and the `uv_build` backend was
  `0.12.13`, so the requirement is pinned to the `0.12` line.

## Decision

- [DECISION] Every workspace member (`core`, `engine`, `providers`, `cli`,
  `clear-record`) **and** the virtual root declare:

  ```toml
  [build-system]
  requires = ["uv_build>=0.12.0,<0.13.0"]
  build-backend = "uv_build"
  ```

- [DECISION] Remove every `[tool.hatch.build.targets.wheel]` block; rely on
  `uv_build`'s default `src/<module>` discovery. Use
  `[tool.uv.build-backend] module-name` only if a future dist's import name
  stops matching its distribution name.
- [DECISION] The `clear-record` facade ships a real module
  (`clear_record/__init__.py`, `from cr_cli.cli import main`) so it satisfies
  the "every dist has a module" rule; it is no longer metadata-only (ADR-0009).
- [DESIGN] Pin the backend to the `0.12` line (`>=0.12.0,<0.13.0`); a
  build-backend bump is a deliberate change, not something a resolver floats.
- [DESIGN] The root stays `package = false` and is never built; its
  `[build-system]` names the same backend as the members, for consistency.

## Rationale

- **One toolchain end to end.** `uv` resolves, locks, runs and now builds; there
  is no second packaging tool to install, version or explain.
- **Zero wheel config.** `uv_build`'s defaults already match every member's
  `src/` layout, so the hatch wheel blocks disappear (less to drift).
- **Editable installs still work.** The dev loop (`uv sync --all-packages`,
  `just verify`) installs the members editable, as before.

## Discarded alternatives

- **Keep `hatchling`** — works, but leaves a second build tool in a project the
  owner wants uv front-to-back.
- **Use `setuptools`** — more per-package configuration than the defaults
  require, and the same "second toolchain" objection.
- **Mixed backends per member** — needless divergence; one backend keeps the
  build reproducible and the docs simple.
- **Configure `module-name` everywhere** — unnecessary while the defaults match;
  it would add config the defaults already cover.

## Consequences / review hook

- **`uv_build` requires a module per dist.** A future metadata-only (or
  otherwise module-less) distribution is not buildable this way; give it a
  facade module or revisit this decision.
- **The facade now has an import package.** `clear_record` is importable and
  re-exports `cr_cli.cli.main`; the facade is asserted by
  `packages/cli/tests/test_packaging.py`.
- Revisit if a member's layout stops matching `uv_build`'s defaults, or if a
  build-backend bump is wanted.
