# ADR-0030 — The registry's persistence layer, and the order of the `restructure-1` work

Status: active
Date: 2026-09-19

Provenance of the decisions below: each bullet is labelled with **who decided** —
the owner's own statement of 2026-09-19, a recommendation the owner accepted, or
the sprint spec's own scheduling decision. The tracker's `restructure-1` lane
carries the spec they were taken from; per ADR-0029 nothing here cites it by
address, and the environment-local material behind an accepted recommendation is
not a committed document.

## Context

- [FACT] The **registry** is one SQLite owner: `clear_record.service.store`, one
  connection rule, and the statements the console and the MCP server both write
  (`service/store.py`, `service/runs.py`). Two defects of that shared ownership
  were closed in code on the sprint's integration branch before this ADR: the
  reconciliation compare-and-set (`service/runs.py`) and the single tape-storage
  owner (`service/tapestore.py`).
- [FACT] `clear_record.core` may import **no third-party package**, by rule
  (`AGENTS.md`) and by guard (`packages/clear-record/tests/test_layering.py`;
  ADR-0012). Anything the core uses must therefore be stdlib — which is why the
  run options cannot become a Pydantic model.
- [FACT] The boundary types the pipeline hands across layers are the **eight
  frozen dataclasses** of `core.model` (`Source`, `Alignment`, `Segment`,
  `TranscriptionResult`, `RecordDocument`), `core.message` (`Message`, `Joined`)
  and `core.events` (`JobEvent`).
- [FACT] On 2026-09-19 an architecture review of the whole
  `packages/clear-record` tree proposed ten deepening items (`C1`–`C10`), each
  with a strength grading and a recommended landing order. It is an
  environment-local analysis, not a committed document; its content survives
  here, in the decisions it became.
- [FACT] The decision records carried the same defect in two places: ADR-0012's
  clause (c) and ADR-0004's Update stated a layering the guard does not enforce.
  Both are corrected in place (see "Consequences").

## Decision

- [DECISION: owner, 2026-09-19] **A minimal set of the review's items, in three
  batches, in this order.**
  1. **Batch 1 — the refactor and fix items.** One tape-storage owner; one
     declaration per run knob; one process seam at a depth every layer can
     reach; the vendor artifact name out of `core`; the pass-through modules and
     dead exports deleted — beside the fixes filed on top of them: the
     reconciliation compare-and-set, the e2e gate's provisioning hint, and the
     importer no longer taking the frozen archive as authoritative.
  2. **Batch 2 — the persistence work**, the decisions below: Alembic at open,
     the SQLAlchemy mapping, the per-meeting uniqueness index, Pydantic at the
     boundaries.
  3. **Batch 3 — the two structural items**: the run-lifecycle module, and the
     console view seam with its one lookup module.
- [DECISION: owner, 2026-09-19] **SQLAlchemy ORM, with a Core fallback for the
  atomic statements.** The registry's tables are mapped as ORM entities; where a
  statement must be exactly what the database does — the run claim, the
  reconciliation compare-and-set — the SQLAlchemy **Core** expression is kept
  rather than reconstructed out of mapped objects.
- [DECISION: owner, 2026-09-19] **Alembic is adopted, and runs at open.** The
  revisions are the retired ladder's own steps, one apiece: revision `0001` is
  the ladder's v1 (the project and glossary tables) and `0008` is its last step,
  so adopting the tool changes no table and moves no data, and every later schema
  change is a revision on top of them. The process applies the revisions when it opens the
  registry, so an ordinary start yields a current schema with no separate
  migration step for the user.
- [DECISION: owner, 2026-09-19] **Pydantic v2 at the boundaries only.** The
  CLI/HTTP/MCP edges validate their input with Pydantic models; the run options
  are validated by a `TypeAdapter` in the **service** layer, so validation lives
  at the seam that receives the values and not in `core`.
- [DECISION: owner, 2026-09-19] **Separate ORM entity classes; the eight frozen
  dataclasses stay the boundary types, unchanged.** An ORM class is a
  persistence shape, not a domain type: no domain behaviour moves into a table
  class, and the dataclasses the pipeline and the boundaries pass around keep
  their current form.
- [DECISION: owner, 2026-09-19] **The per-meeting uniqueness index is a named,
  tested deliverable of the migration, not a rule written twice** — the
  recommendation is the review's, the decision the owner's. One partial unique
  index over the active run per meeting lands with the mapping and is tested
  there, so the race the code-level compare-and-set closes is closed by the
  database as well.
- [DECISION: spec author, 2026-09-19] **The pipeline module — item `C3` — is the
  next sprint's headline; the windowed vote in `engine` (`C9`) is optional and
  the service interface regrouping (`C10`) parked.** This is the spec's
  scheduling decision, reviewed by the owner when the spec was dispatched, not
  the owner's own wording. `C3` moves the pipeline whose stage wiring is
  `clear_record.cli.stages` out of the CLI package into a module under the
  command surface; it is the item that brings the layering revision with it
  (ADR-0012's correction), and it is what lets
  `service → clear_record.cli` stop being a deliberate edge.

## Rationale

- **The database can hold an invariant the code can only check.** One active run
  per meeting is a fact about the meeting, and two writers (console, MCP) meet
  at the same table; a partial unique index states it once, where every writer
  must obey it, instead of in each statement that writes a run.
- **An ORM narrows the surface the tests must cover.** The hand-written SQL that
  drifted in `store.py` is what the reconciliation fix had to correct; mapped
  entities move that class of mistake into a schema definition, while the Core
  fallback keeps the two atomic statements exactly as written.
- **Revision one at open makes the schema's history a file.** A registry the
  user already has is taken as it is — no rewrite, no data loss — and the next
  change is recorded as a revision rather than as a comment about the current
  version.
- **Validation belongs at the boundary.** The values that enter through the CLI,
  HTTP and MCP seams are the untrusted ones; the core's own types are constructed
  inside the process, and `core` may not import a validator anyway.

## Alternatives considered

- **Hand-written SQL only, no ORM.** Rejected: the statements drifted — the
  tape-storage duplication and the reconciliation defect batch 1 removed were
  both hand-written-SQL mistakes — and the mapping is the layer that removes the
  class.
- **Pydantic models as the domain types.** Rejected: `core` is third-party-free
  by rule and by guard (ADR-0012), and the eight dataclasses are the boundary
  contract both the pipeline and its callers already speak.
- **A schema rewrite as Alembic's revision one.** Rejected: nothing about
  adopting the tool requires changing the schema, and a rewrite would put the
  user's existing registry at risk for no product change.
- **Writing the uniqueness rule once, in code.** Rejected: any second writer can
  bypass a code path; the index is the version the database enforces, and the
  one that is tested as a deliverable of the migration.
- **Taking all ten review items in one batch.** Rejected: the batch order exists
  to keep each landing's test surface small, and the dependencies between the
  items (the persistence work follows the two storage fixes) are real.

## Consequences / review hook

- **ADR-0004 and ADR-0012 are corrected, not yet superseded.** Their clause
  lists predate the layers ADR-0013 added; until `C3` lands, the guard keeps
  `core`, `engine` and `providers` free of `clear_record.cli` while `service`
  imports it deliberately. `C3` restates those clauses with the guard when the
  pipeline moves out of the CLI package.
- **The batch order is the sprint's, not a standing rule.** It lives in the
  tracker's `restructure-1` lane; the windowed vote in `engine` (`C9`) may be
  taken at any point or never, and the service interface regrouping (`C10`) is
  parked rather than rejected.
- Revisit if a second registry grows — the mapping's entity classes are the
  place a second persistence shape would show up as a decision, not a copy.
- **Batch 1's dead-export deletion removed a user-visible flag.** `--reference`
  is gone from `ingest`, `transcribe` and `export` — the three stage commands
  that took it and silently ignored it — and stays exactly where it is honoured:
  `align` (the alignment reference) and `reconcile` (the merge's preferred
  source), plus `run`/`calibrate`, which thread it to both. A script that passed
  `--reference` to one of the three now gets Click's own "no such option" rather
  than a silently accepted no-op.
- **The mapping changes the class a registry failure arrives as.** With SQLAlchemy
  between the store and `sqlite3`, a duplicate is raised as
  `sqlalchemy.exc.IntegrityError` and a locked database as
  `OperationalError`/`SQLAlchemyError` — SQLAlchemy's own errors, which *wrap* the
  driver's exception rather than being it. Every catch inside this tree was
  retargeted with the mapping (`service/runs.py`'s heartbeat guard most visibly),
  so in-tree callers are unaffected; an **out-of-tree** caller that catches
  `sqlite3.IntegrityError`, or `sqlite3.Error` around a registry call, now catches
  nothing and must catch the SQLAlchemy class — or `sqlalchemy.exc.SQLAlchemyError`
  where it means the whole family.
- **Three public names moved.** ``service/webhooks`` no longer *declares*
  ``RUN_STARTED``/``RUN_FINISHED``/``RUN_FAILED``: the run half of the webhook
  vocabulary is derived from the run lifecycle's own moves
  (``service/lifecycle.py``, each move's derived ``event``) and the three names
  are declared there. ``clear_record.service`` still exports them, so every
  in-tree caller is unaffected; an **out-of-tree** caller that imported one of
  the three from ``clear_record.service.webhooks`` now gets an ``ImportError``
  and must take it from ``clear_record.service`` (or read the move), the same
  one-way cutover this batch's other deletions made.
- **The per-meeting uniqueness index makes one revision alter data — the first
  one that does.** `CREATE UNIQUE INDEX` cannot be created over rows the index
  forbids, and the rows it forbids are exactly the ones *this application wrote*:
  two submissions that raced past the run manager's guard both inserted an active
  run for one meeting. Refusing to open such a registry would leave an upgrade
  path stuck forever on a state the product itself produced — the opposite of
  taking a user's registry as it is — so revision `0009` reconciles before it
  creates the index: one active run per meeting survives (a `running` row over a
  `queued` one, else the oldest), the rest end as `interrupted` with the reason in
  the run's own `error` column, and nothing else is touched. Dropping the
  duplicates instead would hide history the user is being asked to keep, and an
  index that simply fails to build is the blocker this bullet exists to record.
  The step is a no-op on every registry that never raced.
