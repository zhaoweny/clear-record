# ADR-0030 — The registry's persistence layer, and the order of the `restructure-1` work

Status: active
Date: 2026-09-19

Provenance of the decisions below: each bullet is labelled with **who decided** —
the owner's own statement of 2026-09-19, a recommendation the owner accepted, or
the sprint spec's own scheduling decision. The **Update** at the end carries its
own labels for what changed after that date: what landed, the one deviation
recorded without the owner's word, and the question that leaves open. The
tracker's `restructure-1` lane carries the spec they were taken from; per
ADR-0029 nothing here cites it by address, and the environment-local material
behind an accepted recommendation is not a committed document.

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
- [DECISION: owner, 2026-09-19] **Alembic is adopted as revision one, and runs
  at open.** Revision one is the schema the registry already has: adopting the
  tool changes no table and moves no data, and every later schema change is a
  revision on top of it. The process applies the revisions when it opens the
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
- **The schema's history is a file from its first revision on.** A registry the
  user already has is taken as it is — no rewrite, no data loss — and the next
  change is recorded as a revision rather than as a comment about the current
  version. That argument does not depend on where revision one *starts*: it holds
  for a single baseline describing today's schema and, as the Update below
  records, for the ladder's own steps adopted one apiece.
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
  user's existing registry at risk for no product change. The shape adopted
  instead — the ladder's own steps, one apiece (see the Update) — rewrites
  nothing either, and it is what lets an existing registry be stamped at the
  version it actually reached.
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
- **Four published names changed home: three moved, one is gone.**
  ``service/webhooks`` no longer *declares*
  ``RUN_STARTED``/``RUN_FINISHED``/``RUN_FAILED``: the run half of the webhook
  vocabulary is derived from the run lifecycle's own moves
  (``service/lifecycle.py``, each move's derived ``event``) and the three names
  are declared there. ``clear_record.service`` still exports them, so every
  in-tree caller is unaffected; an **out-of-tree** caller that imported one of
  the three from ``clear_record.service.webhooks`` now gets an ``ImportError``
  and must take it from ``clear_record.service`` (or read the move), the same
  one-way cutover this batch's other deletions made. ``SCHEMA_VERSION`` — the
  fourth — is not re-exported anywhere: the ladder's own version number stopped
  being a published fact when Alembic became the schema's history, and what
  survives of it is ``service/store.py``'s private ``_LADDER_VERSION``, the
  number a registry predating this build is placed at. An **out-of-tree** caller
  that imported ``SCHEMA_VERSION`` from ``clear_record.service`` now gets an
  ``ImportError`` with no name to move to: the question it asked — how far is
  this registry's schema? — is Alembic's to answer, from ``alembic_version``.
  Both breaks are intended, and neither has an in-tree caller.
- **The per-meeting uniqueness index makes one revision alter data — the first
  one that does.** `CREATE UNIQUE INDEX` cannot be created over rows the index
  forbids, and the rows it forbids are exactly the ones *this application wrote*:
  two submissions that raced past the run manager's guard both inserted an active
  run for one meeting. Refusing to open such a registry would leave an upgrade
  path stuck forever on a state the product itself produced — the opposite of
  taking a user's registry as it is — so revision `0009` reconciles before it
  creates the index: one active run per meeting survives (a `running` row that
  names an owner, else the newest), the rest end as `interrupted` with the reason
  in the run's own `error` column, and nothing else is touched. Dropping the
  duplicates instead would hide history the user is being asked to keep, and an
  index that simply fails to build is the blocker this bullet exists to record.
  The step is a no-op on every registry that never raced.
- **The released line cannot read a registry this build has opened.** The
  persistence work moves a registry onto Alembic, and the first number a
  pre-Alembic build reads is the ladder's own: this build levels that row at the
  ladder's last version (`service/store.py`'s `_LADDER_VERSION`, 8), which is
  what lets a **trunk** build from before Alembic treat the ladder as already
  applied instead of re-running it. The **released** line (`v0.2.x`) stopped at
  **6**, so it refuses a migrated registry outright — "registry schema version 8
  is newer than this build supports (6); upgrade clear-record" is the whole
  repair path — and, meeting a registry this build *created* (which carries no
  `schema_version` row at all), it never reaches that sentence: it runs the
  ladder and dies on v4's `ALTER TABLE meeting ADD COLUMN notes` with a raw
  `sqlite3.OperationalError: duplicate column name: notes`. Seeding the ladder's
  row in revision `0001` for registries this build creates would have turned that
  second death into the first sentence; it was considered and not taken, because
  the row records the ladder history a registry *migrated from the ladder*
  carries, and a fresh registry has none.

## Update (2026-09-20) — the revision order, as landed

- [FACT] **The retired ladder's steps are the chain's revisions, one apiece
  (`0001`–`0008`), and this records what landed rather than a decision of the
  owner's.** The bullet above still reads "Alembic is adopted as revision one …
  Revision one is the schema the registry already has"; that text stands as the
  owner wrote it on 2026-09-19, and this Update is the deviation recorded beside
  it instead of a rewrite of it (`AGENTS.md`: provenance is preserved, and
  nothing is promoted without the owner's own word). What landed is the ladder's
  own DDL as the first eight revisions, in the ladder's order
  (`packages/clear-record/src/clear_record/service/migrations/versions/`, each
  body moved across as written). The reason is the **stamp**: a registry that
  predates Alembic stands at a *ladder* number, and a registry whose first pass
  was killed replays from the base — both need the chain's revisions to be the
  ladder's own steps, because a registry has to be placed at the version it
  actually reached, and a single baseline carries no number to place it at.
  Everything the 2026-09-19 bullet was taken for holds: adopting the tool
  changed no table and moved no data, every later schema change is a revision on
  top of these eight, and the process still applies the revisions when it opens
  the registry, so an ordinary start yields a current schema with no separate
  migration step for the user.
- [FACT] **The released line's registry is the one this is proved on.** A
  registry an older build left behind — the ladder row at **6**, the number the
  released line (`v0.2.x`) stopped at, over the schema that line built — opens,
  migrates and keeps every row, and so does one at 3:
  `test_a_registry_from_the_retired_ladder_migrates_on_open` in
  `tests/service/test_store.py` covers both, and the 6 case is what exercises the
  guarded `ALTER` path for every revision after it (`0007`, `0008`, `0009`).
  *Superseded at the 2026-09-23 cut (below): the test now covers the released
  baseline alone, and the ladder row at 3 is a refusal —
  `test_a_ladder_registry_from_a_development_build_is_not_carried`.*
- [OPEN] **Whether the owner adopts this order as their own decision.** It is
  recorded here without their word because the shape had already shipped when the
  deviation was noticed. If they adopt it, this bullet becomes the dated decision
  and the two [FACT]s above stand as its record; if they prefer the single
  baseline the 2026-09-19 bullet names, that is a revision of its own, and the
  eight ladder steps are what it would replace.
- [FACT] The criterion the ticket behind this carried — *"the revision history
  begins from the current schema"* — is honoured **in effect, not literally**:
  nine revisions (`0001`–`0009`) replay the ladder's eight DDL steps rather than
  one baseline describing today's schema. No table changed, no data moved, the
  chain is forward-only, and a registry migrates at open, which is what the
  criterion exists for; the literal form is not claimed as met here or in the
  sprint's hand-over. Landing the single baseline instead is a follow-up, not a
  correction: it would have to place an existing registry at its own ladder
  number, which is the same problem in the other direction.
- Revisit if the revision ids ever stop being the ladder's own numbers (a chain
  that starts fresh, ids rewritten by hand): the placement of an existing registry
  and the levelling of the ladder's row both rest on that identity, and nothing
  else in the design does.

## Update (2026-09-23) — the baseline lands at the release cut

- [DECISION: owner, 2026-09-20] **The revision history is compressed at the
  release cut, and a tip install is not carried across it.** The chain begins at
  a **released baseline** — the schema a released install actually has — and
  carries only the real deltas from it to the head; a registry the released line
  left behind migrates forward in place, and a registry a development build left
  behind is not carried: the user deletes the registry file and starts
  clear-record again. Which states are refused is the [FACT]s' to name, and they
  name two — a ladder row that is a step other than the released line's 6, and a
  stamped revision the cut folded away. The owner's own words
  on the timing: *at the release branch cut*. This is the decision the two
  [FACT]s about the ladder's own steps (2026-09-20, above) were waiting on, and
  it settles the [OPEN] beside them in the direction of the single baseline the
  2026-09-19 bullet names — not by rewriting either statement, which stand as
  written, but by shifting where the chain *starts*.
- [FACT] **What landed: four revisions where nine stood, and the ladder's history
  no longer replays.** Revision `0006` is the released baseline, `0007` and
  `0008` are the deltas the trunk added on top of it, and `0009` is the
  per-meeting index the persistence work added. The ladder's steps 1–6 are not
  revisions any more: their DDL *is* the baseline's, with the two
  column-adding steps folded into the tables that carry the column (``notes``
  ends ``meeting``, ``run_options`` ends ``pipeline_run``, each where ``ALTER
  TABLE … ADD COLUMN`` had put it), so the baseline describes a shape rather
  than replaying a history. The ladder's steps 7 and 8 survive as the revisions
  they always were, read from a released registry's viewpoint as the deltas they
  are. Every table, column, index and constraint a released registry has is
  still what a registry this build creates holds, and the tree **checks** it
  rather than asserting it: `test_the_released_builds_own_registry_migrates_to_a_fresh_schema`
  in `tests/service/test_store.py` runs the `v0.2.0` tag's own store module out
  of the history, lets it build the registry that release built, opens it with
  this build, and compares the migrated schema with a fresh one's — tables,
  columns in declaration order, indexes and foreign keys. A drift inside `0006`
  would leave the released tables as `v0.2.0` wrote them while a fresh registry
  took the drifted shape, and the two would part company there, where every other
  fixture (which builds the baseline with this build's own chain) could see
  nothing. `test_a_registry_at_the_released_baseline_gains_every_delta` covers
  the upgrading path from the baseline, row by row.
- [FACT] **The criterion the ticket carried is honoured literally now.** The
  2026-09-20 Update recorded *"the revision history begins from the current
  schema"* as honoured **in effect, not literally**, since nine revisions replayed
  the ladder's eight DDL steps. The cut's compression closes that: the chain
  begins at *one* revision describing a schema an install actually has, and the
  revisions after it are the changes made **since** that schema — which is the
  criterion's own subject, at the granularity the owner's decision names (the
  released schema, not the tip's, because the tip's is the one the policy does
  not carry).
- [FACT] **What a released install does, and what a development install does.**
  A `0.2.0` registry opens `0.3.0` and migrates in place — the deltas
  (`0007`, `0008`, `0009`) run against the schema it already has, and its rows
  are untouched.
  `test_a_registry_from_the_retired_ladder_migrates_on_open` builds that
  registry the way the released line left it (its tables at the baseline, its
  ``schema_version`` row at 6, no revision recorded) and opens it: the projects
  it holds survive, and every delta answers. A **development** registry is
  refused in exactly two shapes, and which build wrote it is not one of them: a
  ``schema_version`` row at a ladder step other than the released line's 6, and a
  stamp at a revision the cut folded away (``0001``–``0005``).
  `test_a_ladder_registry_from_a_development_build_is_not_carried` pins the
  first for the ladder's steps 0, 3, 7 and 8, and
  `test_a_registry_stamped_at_a_revision_the_cut_dropped_meets_the_wipe_remedy`
  the second. A registry the *pre-compression trunk itself* left — stamped at
  ``0006``, ``0007``, ``0008`` or ``0009``, with no ladder row at all — is
  **carried**, because the compression changed no table; that is what makes the
  refusal a statement about where a registry stands rather than about which build
  wrote it, and it is why `docs/releasing.md` says so in those words.
- [FACT] **The placement is the released line's own number, and nothing else.**
  The baseline's id is the number the released line recorded its schema as — one
  identity, not a coincidence: a released registry's ``schema_version`` row is
  read once and the registry is stamped at the revision of that number
  (``service/store.py``'s ``_RELEASED_BASELINES``, ``_pending_stamp``), so the
  deltas on top are the only revisions that run. Every other ladder number at or
  below the ladder's last version is a development build's and is refused with
  the file to delete in the sentence; a number *above* it is a build newer than
  this one, and its sentence says to upgrade instead.
  The accident is gone with it: with steps 1–5 no longer revisions, a registry a
  sprint build stamped at one of them is refused rather than silently placed —
  which is the policy, not a gap.
- [FACT] **`0.2.0` is the released line carried, and `0.1.x` is not carried.**
  ``v0.2.0`` and its release candidates (`rc1`, `rc4`) shipped the registry and
  recorded version 6, which is the baseline; ``v0.1.x`` (``v0.1.0``, ``v0.1.1``,
  ``v0.1.1rc2``) predates the registry entirely — it ships no ``service``
  package and no registry file — so it has no version to place and nothing to
  carry. That is the whole of the supported set at this cut.
- [FACT] **The levelling shim's role for released versions, stated.** The
  ladder's row is now read for one purpose — placing a registry the **released
  line** left behind — and written for one: the write levels the row at the
  retired ladder's last version (8), above every released line's, so a `v0.2.x`
  build meeting a migrated registry refuses it with its own sentence instead of
  reading a schema that is not its own. The second audience that write had, a
  pre-Alembic *trunk* build, is a property of the development tree now: no
  released build ever reached 8. What the shim has lost is its breadth: a row at
  any ladder number used to place a registry, and today only a released
  baseline's number does.
- [FACT] **The tip refusal is stated where a user meets it.**
  ``docs/releasing.md`` carries the two sentences the 0.3.0 release notes will
  quote — *a registry the released line left behind is carried forward* and *a
  registry the tip left behind is not carried — wipe it and start clear-record
  again* — and it names the two shapes the refusal meets, because "the tip" is
  not the same thing as "a development build" any more: a registry the trunk
  itself stamped inside the compressed chain is carried. The shim's wipe refusals
  name the file to delete, so the remedy travels with the failure.
- [FACT] **The stamped branch tells its two cases apart.** A registry stamped at
  a revision this build does not carry meets one of two sentences: *upgrade
  clear-record* when the revision's number is above the head's (a build newer
  than this one, which is the case that sentence was written for), and the wipe
  sentence when it is below — a revision this release's cut folded away
  (``0001``–``0005``), where advising an upgrade cannot work for someone already
  on this build. `test_a_registry_stamped_at_a_revision_the_cut_dropped_meets_the_wipe_remedy`
  pins the second. The ids being decimal is a decision (``migrations/env.py``),
  and it is what makes the comparison available.
- [FACT] **The revisit condition above is answered, not merely avoided.** The
  chain still begins at a ladder number (``0006``), so the identity the placement
  uses survives *for the one number that matters*, and the numbers above it
  being the ladder's is now a record of where the deltas came from rather than
  something anything rests on. A later cut adds the next released line's
  baseline to ``_RELEASED_BASELINES`` and folds the deltas below it into that
  baseline's DDL; the shape does not otherwise change.

