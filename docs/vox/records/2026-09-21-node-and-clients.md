# The node and its clients — owner-voice record (2026-09-21)

Status: **In force as the direction.** The ADR it rests on is
[ADR-0032](../../adr/0032-the-node-and-its-clients.md) (2026-09-24), which carries the
decisions; the position it supersedes — the 2026-09-14 `serve` + `mcp` CLI decision — is
annotated in place in
[`2026-09-14-grilling-rounds-1-4.md`](2026-09-14-grilling-rounds-1-4.md).

Recorded here on 2026-09-24, from the direction's spec's own quoting of the owner's words
of 2026-09-21 — the spec is in the tracker's `architecture` lane, and the words below are
the owner's **verbatim**, exactly as that spec carries them. Why a new file rather than a
move: the words were never in `docs/vox/voice-of-owner.md`, so nothing was moved and
nothing was rewritten; this is the dated record of a position that had no home in the
record. Date per the spec's own stamp — a session or an export is written after the fact
(see *How to read this file* in
[`voice-of-owner.md`](../voice-of-owner.md)), so a 2026-09-21 capture may rest on words first
spoken earlier.

## The direction (2026-09-21)

- [VOICE: owner, 2026-09-21] Verbatim: *"I think we do a server centric move and CLI
  become a facade of the server, which is reasonable at this stage and very much suited."*
- [VOICE: owner, 2026-09-21] Asked to choose between the facade and a smaller client arm
  (a read-only convenience CLI, which is what the 2026-09-14 decision deferred), the
  answer was *"I think option C?"* — which the spec records as the owner taking the
  facade; asked to hold the scope rather than start it, *"I think we can wait"*.

## The shape (2026-09-21)

- [VOICE: owner, 2026-09-21] Verbatim: *"we reverse the order of things and move to a
  'api-server centric, then all kinds of facades and adapters on top' architecture.
  essentially we have a bunch of BFFs - MCP CLI and WEB"*.
- [VOICE: owner, 2026-09-21] One app, several prefixes, verbatim: *"yes, we can certainly
  share a FastAPI endpoint and do e.g. /web /mcp /api/v1 etc"*.
- [VOICE: owner, 2026-09-21] The precedent to learn from, verbatim: *"we can learn from
  opencode2 by have a opencode service subcommand and have all backend operations
  there"*.

## The preliminaries (2026-09-21)

Four of the five the spec asked rest on the owner's words — three verbatim below, and
offline/rescue through a requirement the record already carries from an earlier owner
word; the fifth is the residue of the question the owner answered with the *shape*
instead.

- [VOICE: owner, 2026-09-21] **Lifecycle and discovery**, verbatim: *"do a state of pid
  file or a named pipe or a note of http server endpoint, or just set it in a config file
  so everyone (CLI MCP API WEB etc) agree where to call the server"*.
- [VOICE: owner, 2026-09-21] **Authority**, verbatim: *"I think CLI joins the queue"*.
- [VOICE: owner, 2026-09-21] **Auth and the backchannel**, verbatim: *"a backchannel is a
  option here; and I think the CLI is currently mainly serving the local host, not really
  the remote host. but one day the CLI would be another endpoint to call a remote host, I
  guess."*
- [REQ] **Offline and rescue** is the fourth, and it rests on an earlier owner word
  rather than a new one of 2026-09-21. Offline is
  [ADR-0013](../../adr/0013-bundled-web-and-service-surface.md)'s requirement that the
  console works after one install step, offline, from the owner's 2026-09-14 voice — a
  local node does not break it: same machine, same workspace, same models. Rescue's
  recorded reading, in the tracker's `console-ia` lane, is that the **browser** path is
  the unusable one (a spec's reading, not the owner's words). The residual is what
  happens when no node process exists.
- [OPEN: owner, 2026-09-21] **Packaging** is the fifth, and the owner's answer to it was
  the *shape* above, not the packaging question: whether an invocation must be able to
  bring a node up (which would stop the node's stack being an optional extra) is left
  open.

## What kind of position this is

- The wording is **tentative in form** ("I think …"), which by this record's own rule
  leaves it as the direction **chosen for this work**, not as a decision in force. What
  settles it is an ADR — and
  [ADR-0032](../../adr/0032-the-node-and-its-clients.md) (2026-09-24) is that ADR. Its
  Decision clauses are labelled with who decided each one — the owner's, or the spec
  author's — so nothing here is promoted past the owner's own words.
- It is a **revision of a position already on record** — the 2026-09-14 decision that the
  service CLI stays `serve` + `mcp` and a read-only convenience CLI is deferred. That
  line is **annotated in place**, never rewritten, in
  [`2026-09-14-grilling-rounds-1-4.md`](2026-09-14-grilling-rounds-1-4.md).
- It **extends a direction already in flight**: the app is a backend for AI harness agents,
  with MCP as the only agent integration (ADR-0017, ADR-0031), which is the direction the
  node's centre already serves for the console and for MCP. The command line is the last
  surface that is not a client.
- The direction's **first batch** — the pipeline leaving the command surface — landed on
  2026-09-24; the direction is no longer only a spec.
