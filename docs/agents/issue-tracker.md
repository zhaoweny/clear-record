# Issue tracker: private Gitea

Issues and specs for this repo live on the owner's private Gitea instance, reachable
only over the tailnet. `.scratch/` is a frozen archive of the pre-migration corpus
(ADR-0029) — read it for provenance, never write to it.

## Where things are

- **Tickets** — issues in `zhaow/clear-record` on the instance. A **lane** (a former
  feature directory) is a label: `lane/<slug>`.
- **A lane's spec is an umbrella ticket** — one issue per lane, labelled `type/spec`: its
  body is the spec verbatim plus a checklist of the lane's tickets, and its remaining
  parts are comments. It is the lane's entry point, and **the umbrella closes when the
  lane's work is done** — a finished lane leaves no open spec behind.
- **The lane's other documents are wiki pages** — one page per document, titled
  `<lane>/<stem>`: `console-ia/ticket`, `service-deployment/02-amendment`. The set is
  `ticket.md`, `02-amendment.md`, `map.md`, `order.md`, `user-stories.md`, `framing.md`,
  `session.md`, `02-machinery.md`.
- **Labels** — `lane/<slug>`; `type/<t>` from the ticket's `**Type:**` line, plus
  `type/spec` for a lane's umbrella; the triage roles and the other families recorded in
  `triage-labels.md`; `status/<value>` when the original status was outside that
  vocabulary; `from/scratch` on everything imported from the archive.
- **Closed** is the terminal state: `done`, `wontfix` and `resolved` tickets were
  imported closed, and closing is how completion is recorded.

## Conventions

- Create and edit through `tea` or the instance API, never by editing the archive.
- Titles are the ticket's own H1. Bodies are the original markdown verbatim, with a
  footer naming the archive file and its sha, and a `Blocked by: #n` line resolved
  against the imported set.
- Conversation history is comments, appended chronologically — the archive's
  `## Comments` sections were imported as the first comment on each ticket.
- Blocking is expressed as `Blocked by: #n` in the body. A ticket is unblocked when
  every issue it lists is closed.

## When a skill says "publish to the issue tracker"

Create an issue in the ticket's lane, labelled `lane/<slug>` and with the type label.

## When a skill says "fetch the relevant ticket"

Read it from the instance; the user normally passes the issue number or its URL. The
archive copy at `.scratch/<lane>/issues/<NN>-<slug>.md` remains readable for provenance.

## Wayfinding operations

A lane's work hangs off its umbrella ticket (`type/spec`): that is where the lane's
specification and the checklist of its tickets live, and it is what says the lane is
done. The map for an effort is a wiki page (`<effort>/map`); child tickets are issues in
that effort's lane, with the question in the body. The map's body is three sections —
**Notes**, **Decisions-so-far**, and **Fog**.

- **Frontier**: open issues in the lane that are unblocked (no open `Blocked by`) and
  unassigned; lowest issue number wins.
- **Claim**: assign the issue to yourself before any work.
- **Resolve**: post the answer as a comment, close the issue, then add a context pointer
  (gist + issue link) to the map's Decisions-so-far section in the wiki page.
- **Spec**: read the lane's umbrella ticket before its tickets, tick the checklist as
  tickets close, and close the umbrella when the lane's work is done.

## Citing the tracker from committed files

The tracker is private and unreachable for a reader of the public repository, and it
always will be. A committed file therefore must not reference it **by path or URL**: no
markdown link into the archive, no backticked `.scratch/…` path, no instance hostname,
no ticket URL. Nothing in a normal checkout flags such rot.

A lane is a **label**. Committed prose names the lane and drops the URL:

- **Never link** into the tracker or the instance.
- **Never cite a ticket as evidence.** Durable claims stand on durable sources: an ADR,
  `docs/research/…`, or `docs/vox/voice-of-owner.md`.
- **Name the lane, not the address**: write *the tracker's `hardware-backends` lane*.
- **The arrow points one way.** Tracker entries link *to* `docs/`; a committed file never
  links back. A tracker finding that needs to be citable graduates into `docs/`.

Enforced by `packages/clear-record/tests/test_tracker_refs.py` — the
`test_layering.py` pattern, so `just verify` and CI reject a new reference with no
separate job. The guard matches the archive's path roots and the instance hostname;
adding another private class needs its own ruling on which files may legitimately
name it.
