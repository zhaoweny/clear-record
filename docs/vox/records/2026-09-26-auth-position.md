# The auth position — owner-voice record (2026-09-26)

Status: **In force as a decision.** The ADR it rests on is
[ADR-0033](../../adr/0033-the-auth-position.md) (2026-09-26), which carries the
decisions with their labels. The position it supersedes in part — ADR-0021's
"ships no authentication, for now" — is annotated in place in
[ADR-0021](../../adr/0021-localhost-only-deployment.md), and ADR-0017's transport
clause is annotated in place in [ADR-0017](../../adr/0017-mcp-server.md).

Recorded here on 2026-09-26. The words below are the owner's **verbatim**, captured
in the design session of that date and in the exported conversation it worked from
(environment-local, not committed). The **option wording is agent-authored**; each
answer is glossed with what its option named, so the record reads without the
session.

## The direction (2026-09-25)

- [VOICE: owner, 2026-09-25] Verbatim: *"restrict deleting from agent reachable
  paths, or at least do a priviledge access management to let human to do
  just-in-time authorization from a known good interface."*

## The design conversation (2026-09-26)

- [VOICE: owner, 2026-09-26] On the trust boundary the design should hold,
  verbatim: *"so for these kind of operations I could say it's safe to archive in
  database, and not safe to delete the tape set and transcription. and
  transcription have a change-log so no edits could cover the original
  transcription result. then for deleting I would require a logged-in session and
  re-typing the password to confirm the deletion. or of course someone could just
  wipe the workspace ... and we'd have no idea who why and when"*

## The session's answers (2026-09-26)

- [VOICE: owner, 2026-09-26] On the trust boundary, the identity model, the
  authorization rule, machine tokens, and the rewrite hole, verbatim: *"q1. option
  a; q2. option a, until I discover I need full RBAC; q3. option a; q4. option a -
  archive is fine, but destructive is not; q5. option a but I'd like to note that
  we are retaining the history of changes so nothing would be lost"*.
  - What the options named (agent-authored): **Q1(a)** the operating-system
    account is the trust boundary, and in-app auth buys ingress control,
    attribution and accident-prevention only. **Q2(a)** one subject with many
    actors recorded from the transport, RBAC deferred. **Q3(a)** an operation needs
    fresh human authorization iff it destroys data no durable copy can
    reconstruct. **Q4(a)** a machine token never authorizes destruction. **Q5(a)**
    rewrite-in-place counts as destructive, with run-scoped snapshots and prior
    versions retained.
- [VOICE: owner, 2026-09-26] On the tape-delete contract, the surface rule, the
  console-ia auth items, and the audit, verbatim: *"q6. option a. q7. I think I'd
  go option c. we have the server to verify the password on the fly, and I guess
  we could say a password is a password. q8. option a. q9. option a."*
  - What the options named (agent-authored): **Q6(a)** managed-tape deletion
    requires a verified archive, and a glossary delete becomes a status change —
    no approvals machinery before a non-reconstructible operation exists.
    **Q7(c)** no per-surface authority matrix: the operation's fresh password
    check, verified by the server at the act, is the discriminator. **Q8(a)** the
    authentication half is AUTH-01…AUTH-08 absorbed, with AUTH-02 gaining
    step-up-at-the-act and AUTH-03 gaining *no destructive verbs*. **Q9(a)** every
    mutating service call appends an audit row with the actor its transport
    supplies.
- [VOICE: owner, 2026-09-26] On the `/mcp` mount, verbatim: *"q10 option b, but
  I'm open to explainer to select option a"*, then, after the explainer:
  *"let's defer it. say it's open to revisit but we are not building it just yet,
  and it's a transport change rather than to-be-designed state, I think - unless
  there's to-be-designed stuff, then we just record current state of thinking in a
  ADR and leave open questions for HTTP `/mcp` revisit"*.

## The sanitize turn (2026-09-26, the exported conversation)

- [VOICE: owner, 2026-09-26] Verbatim: *"*normally* neither can silently rewrite
  history, unless it's sometime intentional and we'd better to not question why, in
  the field"*.
- [VOICE: owner, 2026-09-26] On the surviving record, verbatim: *"so in the end, we
  could just state, in the record history: - it is sanitized by who, at when - the
  current version of sanitized content"*.
- [VOICE: owner, 2026-09-26] On a hypothetical sensitive record history, verbatim:
  *"let's say hypothetically a record history is something sensitive. and someone
  invoked sanitization. then the remaining is the sanitized version and the
  sanitizer's credential. is that a good outcome, and nobody would really question
  why and what have been sanitized?"*.
- [VOICE: owner, 2026-09-26] On the shape, verbatim: *"to me that's a `git merge
  --squash` operation, almost"*.
- [VOICE: owner, 2026-09-26] On the scope of the idea, verbatim: *"you realize that
  this is not just for clear-record, right?"* — recorded as testimony; the repo's
  scope is unchanged, and the general model lives outside it.

## What kind of position this is

- The selections are **DECISION: owner** clauses in ADR-0033; the option wording
  they select is agent-authored, so nothing here is promoted past the owner's own
  selection, and the ADR labels each clause for itself.
- It **supersedes in part** ADR-0021's "ships no authentication" clause (that ADR
  is annotated in place) and **discharges** ADR-0017's revisit condition for a
  network transport (also annotated in place) — without mounting the surface.
- It **does not build anything by itself**: the authentication and attribution
  work is scoped by the v0.4.0 milestone's access stage, and the tape/glossary
  contract changes are scheduled with it.
