# ADR-0022 — Adopt Click for the CLI layer

Status: active
Date: 2026-09-15

## Context

- [VOICE: owner, 2026-09-15] The suggestion, verbatim: *"perhaps we may adopt
  click for cli interfaces"*; on being shown the trade-off the owner chose
  **"Adopt Click now, as a prefactor port"**.
- [FACT] The CLI is **argparse**, `cli.py` is **629 lines**, **8** subcommands are
  registered there and **4** more arrive via the `clear_record.commands`
  entry-point group (`web`, `tray`, `mcp`, `diagnose`).
- [FACT] The subcommand surface is **derived from `PipelineSpec`** — one source of
  stage truth for the CLI and the `run` dispatch — and must stay derived.
- [FACT] The entry-point seam passes an **`argparse._SubParsersAction`** to each
  provider's `register(subparsers)`; `click-plugins` provides this pattern in a
  supported form.
- [FACT] **21 `CR_*` variables** exist, with the ADR-0007 precedence (flag > env >
  config > default) re-implemented across `cli`, `providers`, `service` and `web`.
  This is the defect the framework change actually addresses: Click declares an
  option's env var where the option is declared.
- [FACT] `tests/cli/test_cli_surface.py` pins argparse internals
  (`argparse._SubParsersAction`, `parser._actions`, `choices`).
- [FACT] The base dist depends on `numpy` + `soundfile` only (ADR-0012). Click is
  pure Python, zero transitive deps, BSD-3 — within the license boundary
  (ADR-0003) — but it is a third base dependency.

## Decision

- [DECISION] **Adopt Click for the CLI layer** (`clear_record.cli`). The port is a
  deliberate **prefactor**, not interleaved with feature work.
- [DECISION] The **spec-derived surface is preserved**: subcommands are added in a
  loop over `pipeline_spec().stages`, so the CLI and the `run` dispatch still read
  one declaration.
- [DECISION] The **entry-point contract changes** from "add a subparser to an
  argparse action" to contributing a Click command; the four providers
  (`web`, `tray`, `mcp`, `diagnose`) are updated with it. Whether that is
  `click-plugins` or a thin `register(group)` is left to the implementing slice.
- [DECISION] Every `CR_*` variable is declared **on its option** (`envvar=` /
  `auto_envvar_prefix`), so the names are discoverable in `--help` and the
  precedence lives in one mechanism instead of several modules.
- [DECISION] `prog` stays `clear-record`. **`--help` output will change** — that
  break is accepted and deliberate.
- [DECISION] The **commands' default stdout stays byte-identical**: the parser
  port left what each command prints alone, and the extraction that later took
  the printing out of the stages kept every byte — the stages print nothing and
  the command surface renders every word of what they returned and reported.
  `tests/cli/test_stage_stdout.py` is the pin, command by command.
- [DECISION] `click` joins the base dist's runtime dependencies, and the packaging
  guard (`test_packaging.py`'s `RUNTIME_DEPS`) is widened to say so.

## Rationale

- The env surface, not the parser, is what hurts: 21 ad-hoc variables with
  precedence copied per module is a defect that grows with every feature. Click
  makes the declaration the single source for a flag's name, its env var and its
  help.
- A supported plugin seam (`click-plugins`) replaces hand-rolled argparse
  introspection, which is also what `test_cli_surface.py` currently reaches into.
- With 12 subcommands and a growing option surface, Click's grouping, help and
  test `CliRunner` are a real ergonomic win for contributors.
- Doing it **now**, as a prefactor, is cheaper than after the next few slices add
  more flags and more `CR_*` reads.

## Discarded alternatives

- **Keep argparse and only unify the env layer** — cheaper and it fixes the
  measured pain, but leaves the ergonomics and the introspective test seam. The
  owner chose the port; the env work is a subset of it.
- **Port only *new* surfaces to Click** — two CLI frameworks in one product, and
  the entry-point seam becomes polymorphic. Rejected.
- **A third-party CLI framework beyond Click** (Typer, etc.) — Typer is Click
  underneath but adds a dependency and a typing style; Click is the smaller,
  better-known choice.

## Consequences / review hook

- **Blast radius:** `cli.py`, the 4 entry-point providers, `tests/cli/*`,
  `tests/test_packaging.py`, and the base `pyproject.toml`. It should land as one
  slice with `just verify` green, and the entry-point providers updated in the same
  commit (a half-migrated seam would break `clear-record web`/`tray`/`mcp`/`diagnose`).
- [OPEN] `click-plugins` vs a thin `register(group)` — decided in the slice.
- [OPEN] Whether to keep a compatibility shim for the old `argparse` contract (do
  not — nothing external uses it).
- Revisit if Click's help formatting or its dependency proves unwelcome.

## Update (2026-09-25) — the byte pin's scope, after the command line became a node client

- [FACT] The Decision's *"the commands' default stdout stays byte-identical"* is
  scoped to the **stage commands** (and `calibrate`). ADR-0032's facade landed
  `run` as a **node run**: `clear-record run` submits to the node and prints the
  run's own stream, read back by `cli/runs.py`, so `run`'s default stdout is no
  longer this module's rendering of what the stages returned and reported.
  `cli.py`'s contract bullet and `tests/cli/test_stage_stdout.py` were qualified in
  that landing.
- [FACT] **`run`'s block is the pre-move equivalence check again**, since the
  channel learned to carry the words (the same date, ADR-0032's matching Update):
  the run's stream reports every line and data item a stage produces, so the
  follower prints the very bytes the stage commands print, and `run`'s block is
  the pre-move block with the surface's end line inserted before `[next]`. Every
  **stage's** word this module prints is a report read off that one channel —
  `_cmd_run`'s own `[run] #<id> <status>` and `[next]` lines are the surface's,
  as they were before; what a stage *returns* is for a caller that wants the
  typed value.
