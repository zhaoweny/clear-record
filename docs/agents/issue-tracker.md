# Issue tracker: Local Markdown

Issues and specs for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01` — never a single combined tickets file
- Triage state is recorded as a `Status:` line near the top of each issue file (see `triage-labels.md` for the role strings)
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if needed).

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a file with one **child** file per ticket.

- **Map**: `.scratch/<effort>/map.md` — the Notes / Decisions-so-far / Fog body.
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records `claimed`/`resolved`.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.

## Citing the tracker from committed files

The tracker is gitignored and never published (ADR-0006 sets the same
environment-local boundary for recordings and model weights). A committed file
therefore must not reference it **by path**: a reader of the public repo can
follow neither a markdown link nor a backticked path into a directory that is
not there, and nothing in a normal checkout flags the rot.

A tracker directory is a **lane**. Committed prose names the lane and drops the
path:

- **Never link** into the tracker — no `[…](../.scratch/…)`.
- **Never cite a tracker file as evidence.** Durable claims stand on durable
  sources: an ADR, `docs/research/…`, or `docs/vox/voice-of-owner.md`.
- **Name the lane, not the path**: write *the local tracker's
  `hardware-backends` lane*, not `.scratch/hardware-backends/`.
- **The arrow points one way.** Tracker entries link *to* `docs/`; a committed
  file never links back. A tracker finding that needs to be citable graduates
  into `docs/`.

Enforced by `packages/clear-record/tests/test_tracker_refs.py` — the
`test_layering.py` pattern, so `just verify` and CI reject a new reference with
no separate job. Extending the guard to another gitignored class is one entry in
that test's `IGNORED_PATH_ROOTS`, plus a ruling on which files may legitimately
name it.
