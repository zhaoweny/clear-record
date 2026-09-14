# ADR-0026 — The local tracker stays unpublished; committed files cite lanes, not paths

Status: active
Date: 2026-09-15

## Context

- [FACT] The repository is public (MIT). The issue tracker lives in a gitignored
  directory in the main checkout and is never committed — no commit has ever
  contained it, and `git ls-files` returns nothing under it. Four committed
  files name its path, each because it has to: the ignore rule itself, the
  standing instructions, the tracker convention, and the guard.
- [FACT] Committed documents nevertheless referenced tracker paths: 52
  occurrences across 15 files, including **three rendered markdown links** that
  were dead on GitHub (`docs/research/2026-09-14-nvidia-dgx-spark.md`,
  `docs/research/2026-09-15-clear-record-as-a-service.md`,
  `docs/service-deployment.md`).
- [VOICE: owner, 2026-09-15] The owner considered publishing the tracker in-tree
  (a `.agents/tickets/` corpus, mirroring the agent-notes pattern) and rejected
  it: a published corpus is permanent, and the owner wants to keep the freedom to
  clear local tickets.
- [FACT] ADR-0006 already set the sibling boundary for recordings, derived
  transcripts and model weights; ADR-0001 sets the clean-room boundary.

## Decision

- [DECISION] **The tracker stays local-only and unpublished.** It is not moved
  in-tree, not mirrored to a branch, and not migrated to GitHub Issues.
- [DECISION] **A committed file must not reference the tracker by path.** This
  covers documentation, source comments and test docstrings alike. The four
  boundary-defining files named in Context are the only exemption.
- [DECISION] **Committed prose names the lane**, not the path: *the local
  tracker's `hardware-backends` lane*. A **lane** is a feature directory of the
  tracker; the term is defined in `docs/agents/issue-tracker.md`.
- [DECISION] **The citation arrow points one way.** Tracker entries link to
  `docs/`; a committed file never links back into the tracker. A finding that
  needs to be citable **graduates into `docs/`** — as the hardware-backends
  findings already did into `docs/research/`.
- [DECISION] Enforced by `packages/clear-record/tests/test_tracker_refs.py`,
  following the `test_layering.py` repo-structure-guard pattern, so `just verify`
  and CI enforce it with no separate job.

## Rationale

- A public reader cannot follow a reference into an unpublished directory. The
  link is dead the moment it is written, and — unlike a broken test — nothing
  fails when it rots.
- A durable record that depends on an ephemeral one is backwards: an ADR citing
  a ticket is weaker than the ADR citing a durable source.
- Publishing the tracker would make the corpus permanent, which directly
  conflicts with the owner's wish to clear local tickets. The two cannot both
  hold, so the free wipe wins and the corpus stays private.

## Alternatives considered

- **Publish the tracker in-tree (`.agents/tickets/`).** Rejected: it makes the
  corpus permanent (no free wipe), and because ticket files are the one artifact
  every worktree branch would touch, it re-introduces merge conflicts that the
  control-plane geometry otherwise avoids.
- **Snapshot the tracker to an orphan `tracker` branch.** Rejected: it buys
  durability but does *not* fix the links — GitHub resolves relative links
  against the ref being viewed, so `main` would still render them dead — and it
  adds a fetch step to every agent session.
- **Publish a summary for each cited finding.** Rejected as a default: most
  references are provenance ("scoped in lane X"), not content a reader needs.
  Graduating a finding into `docs/` covers the cases that genuinely need it.
- **Keep the links and accept the rot.** Rejected: three published documents
  carry dead links today, and the count grows with every new ADR.

## Consequences / review hook

- Committed prose reads *the local tracker's `<lane>` lane*; a reader outside the
  machine learns that work is scoped somewhere unpublished, without a path to
  nowhere.
- The guard's `IGNORED_PATH_ROOTS` is the extension point, matched as
  repo-relative path segments, so a new entry cannot trip on the same name in a
  home-relative or absolute path (`~/.local/share/…`, `/home/…/.local/bin/…`)
  or in a longer name (`.scratchpad`). Adding another gitignored class still
  needs its own ruling on which files may legitimately name it — do not widen it
  blindly.
- Revisit if the tracker is ever published, or if an outside contributor needs
  the tracker to follow a decision. Either would overturn the first decision
  above, not merely the guard.
