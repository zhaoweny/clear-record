# ADR-0029 — The tracker moves to a private Gitea instance; committed files still cite lanes

Status: active
Date: 2026-09-19

- Supersedes in part [ADR-0026](0026-local-tracker-stays-unpublished.md) (2026-09-19): its
  first decision only. The tracker is still unpublished, and committed files still cite
  lanes; its home is now a private Gitea instance.

## Context

- [FACT] ADR-0026 kept the issue tracker as gitignored local markdown in the main
  checkout: private by construction, freely wipeable, and never referenced by path from
  a committed file. It explicitly named "migrated to GitHub Issues" as rejected.
- [FACT] The corpus outgrew that shape: 28 lanes, 155 files, 110 tickets, 18 specs. It
  lives on one machine, is invisible to any other checkout or machine, cannot be
  cross-linked, and keeps no history of state changes.
- [VOICE: owner, 2026-09-19] *"we now have gitea as a staging area and safe to dump all
  the tickets there"*; and, asked for the shape, the owner chose: tickets in the issues
  of a **private mirror repo**, Gitea **canonical**, the full history imported (done
  tickets landed closed), and the lane documents on the wiki — a shape the owner revised
  the same day for the spec, which moved off the wiki to the umbrella ticket (next
  bullet).
- [VOICE: owner, 2026-09-19] Asked again once the import had landed, the owner moved the
  spec's home: a lane's **spec becomes an umbrella ticket** — the spec verbatim as the
  body, its remaining parts as comments, and a checklist of the lane's tickets — and
  **when the lane's work is done the spec's job is done**, so the umbrella closes with
  the lane. The wiki keeps the lane's *other* documents.
- [VOICE: owner, 2026-09-19] *"the Gitea action is now disabled; we are here on Gitea
  for private tickets"* — so nothing on the instance builds or syncs this repository.
- [FACT] The repo side carries no automation for the instance: the Gitea repository is a
  copy of this repository refreshed by hand, and none of the seven files under
  `.github/` (two templates, five workflows) references it.
- [FACT] The instance is a self-hosted Gitea on the owner's NAS, reachable only over the
  owner's tailnet. It is not on the public internet and is not the public repository's
  host.

## Decision

- [DECISION] **The canonical tracker is the private Gitea instance**: tickets are
  issues, a lane's spec is an **umbrella ticket** labelled `type/spec` — its body the
  spec verbatim, its remaining parts as comments, a checklist of the lane's tickets, and
  it closes when the lane's work is done — the lane's other documents are wiki pages,
  lane/type/triage metadata are labels. One lane is one label (`lane/<slug>`).
- [DECISION] **The Gitea repository is the ticket home, not the code's home, and nothing
  syncs it automatically.** The public repository on GitHub remains where the code lives;
  the Gitea repository exists so tickets and specs sit next to a copy of the code, and
  that copy is refreshed by a manual push. Gitea Actions is disabled — no workflow
  mirrors it.
- [DECISION] **The local corpus is frozen as an archive** after a one-shot import. No
  new ticket is created under `.scratch/`; it remains readable for provenance.
- [DECISION] **The tracker is still unpublished**, in ADR-0026's sense: not on GitHub,
  not committed in-tree, not reachable by a reader of the public repository. Only the
  *location* changed, not the boundary.
- [DECISION] **The naming rule stands, and now covers the instance too.** A committed
  file names *the tracker's `<lane>` lane* — never `.scratch/<lane>/`, never the
  instance hostname or a ticket URL. The dead-end argument that motivated ADR-0026
  applies unchanged to a tailnet-only host: a public reader can follow neither.
- [DECISION] **The arrow still points one way.** A ticket links to `docs/`; a committed
  file never links back into the tracker. A finding that must be citable graduates into
  `docs/`.
- [DECISION] Enforced by `packages/clear-record/tests/test_tracker_refs.py`; the guard
  gains the instance hostname alongside its existing gitignored path roots.

## Rationale

- Private-by-construction was never the point on its own; the point was that the tracker
  must not become a public, permanent artefact of an OSS project. A tailnet-only
  instance satisfies that while adding what the local corpus could not: reachability
  from every checkout and worktree, labels that cross lanes, real state history, and
  issues a remote agent can claim.
- The cost is the opposite of ADR-0026's: the corpus is no longer freely wipeable, and
  ticket work now needs the instance up. That is the trade the move makes: wipeability is
  the price paid for reachability from every checkout and worktree, labels that cross lanes,
  and real state history.

## Alternatives considered

- **Keep local markdown as canonical** (status quo). Rejected: leaves the corpus
  machine-local, unlinkable and historyless.
- **GitHub Issues on the public repo.** Rejected exactly as ADR-0026 rejected it: it
  publishes the corpus permanently, and the tracker is to stay private — tickets and all
  (Context: *"we are here on Gitea for private tickets"*).
- **Publish the tracker in-tree** (`.agents/tickets/`). Rejected as in ADR-0026: it
  re-introduces cross-branch merge conflicts on the one artefact every branch touches.
- **Bidirectional sync between `.scratch` and Gitea.** Rejected: two homes for one rule
  drift apart, and the drift is silent — each copy still reads as authoritative where it
  is consulted, so nobody learns the other one changed.

## Consequences / review hook

- Ticket metadata is now shared state; the triage vocabulary in
  `docs/agents/triage-labels.md` becomes the label set on the instance.
- The instance is a single point of failure for ticket *work* (not for the record, which
  is in git). `gitea dump` inside the container is the backup lever.
- Committed prose that reads *the local tracker's `<lane>` lane* is understood to mean
  this private tracker; the term can be renamed in a later wording pass.
- Revisit if the instance is ever exposed beyond the tailnet, or if the tracker needs to
  be reachable by someone outside the owner's machines.
