# ADR-0033 — The auth position: one subject, many actors; nothing irreversible without a human

Status: active
Date: 2026-09-26

The owner selected among agent-authored options in the 2026-09-26 design session;
the option wording is agent-authored and the selections are the owner's. The
verbatim answers and the direction they answer are the
[vox record](../vox/records/2026-09-26-auth-position.md).

## Context

- [FACT] The app ships **no authentication**. `web/` carries two middlewares and
  neither is auth: the locale middleware, and the request guard — which rejects
  an untrusted `Host` and cross-origin state-changing requests, and **deliberately
  allows** one carrying neither. No credential, session, token or `Authorization`
  path exists in `web/`, `service/` or `tray/`.
- [FACT] [ADR-0021](0021-localhost-only-deployment.md) decided localhost-only
  with no auth *"for now"* — in-app LAN auth **deferred, not rejected** — and
  named the operator's reverse proxy as the ingress. [ADR-0017](0017-mcp-server.md)
  chose stdio *"because stdio needs no port, no auth and no network"*, and ended
  with its own revisit condition: *"Revisit if a client needs a network
  transport."*
- [FACT] [ADR-0032](0032-the-node-and-its-clients.md) made one node the centre
  with every surface a client, treated the local caller as a backchannel already
  trusted, and reserved the `/mcp` prefix — its mount handed to the auth change.
- [FACT] The destructive surface is exactly two verbs: deleting a **managed
  tape** (unlink the file, forget the row) and deleting a **glossary term** (row
  delete). Nothing deletes runs, artifacts, meetings, projects, workspaces,
  archives or models, and artifact rows are append-only. The tape delete checks
  only that the tape is managed: "the archive is the durable copy" is a note in
  the response, **not a precondition**.
- [FACT] Every run rewrites the meeting workspace's `record.json`/`segments.json`
  **in place**; only accepted transcript revisions are versioned (the draft chain
  plus the revision artifacts). History can be covered with no delete at all.
  *(Landed 2026-09-26 — see the Update below: a run now writes and retains its own
  copy, and no run rewrites another's.)*
- [FACT] Draft versions record *"the author identity the writer declared"*
  ([ADR-0031](0031-harness-is-the-only-agent.md)) — over MCP that parameter's
  default is the literal string `human`, so a harness can be recorded as its own
  reviewer.
- [VOICE: owner, 2026-09-25] The direction, verbatim: *"restrict deleting from
  agent reachable paths, or at least do a priviledge access management to let
  human to do just-in-time authorization from a known good interface."*
- [VOICE: owner, 2026-09-26] The session's answers, verbatim: *"q1. option a; q2.
  option a, until I discover I need full RBAC; q3. option a; q4. option a -
  archive is fine, but destructive is not; q5. option a but I'd like to note that
  we are retaining the history of changes so nothing would be lost"*; *"q6. option
  a. q7. I think I'd go option c. we have the server to verify the password on the
  fly, and I guess we could say a password is a password. q8. option a. q9.
  option a."*; and on the network mount: *"let's defer it. say it's open to
  revisit but we are not building it just yet, and it's a transport change rather
  than to-be-designed state."* What each option named is in the vox record.

## Decision

- [DECISION: owner, 2026-09-26] **The trust boundary is the operating-system
  account.** In-app auth buys three things and no more: ingress control (who can
  reach the node), attribution (who did what), and accident-prevention (an
  autonomous agent cannot stumble into destruction). A process running as the
  same user is **inside** the boundary — it can call the loopback API and write
  the registry — and the documents say so rather than imply otherwise. Real
  separation is a deployment move (the node under its own uid, or a sandbox),
  never an app feature.
- [DECISION: owner, 2026-09-26] **One subject, many actors.** There is one human;
  no usernames, no accounts table. Every mutation records the **actor** the
  transport supplies — `console`, `api` (later `api:<token label>`), `mcp`,
  `cli` — or `queue`, the word for the moves the node's own queue makes on its own
  behalf; never a string an argument chooses. There is no word for the tray: it
  makes no mutating call — its one request is a `GET /health` read, and the console
  it opens does the writing as the **console's** actor — so it never supplies one.
  A run's `origin` is the precedent and the vocabulary.
- [DECISION: owner, 2026-09-26] **The authorization rule.** An operation requires
  fresh human authorization **iff it destroys data not reconstructible from a
  durable copy**. Everything else — archive, cache invalidation, glossary edits,
  draft writes, tape-set edits — stays agent-allowed.
- [DECISION: owner, 2026-09-26] **No credential alone authorizes destruction.**
  Machine tokens in particular: *archive is fine, destructive is not.* No
  session, token or MCP client is sufficient by itself for a destructive act.
- [DECISION: owner, 2026-09-26] **Rewrite-in-place is destructive in kind.** Run
  outputs become run-scoped snapshots, and prior versions are **retained**: a
  later run must not be able to cover an earlier run's record. Retention, not
  only classification — nothing silently lost.
- [DECISION: owner, 2026-09-26] **Shrink the irreversible set by construction.**
  Managed-tape deletion requires a **verified archive** of the meeting (the check
  archive verification already performs). Glossary deletion becomes a **retire**
  status, the row surviving with its `added_by`/`created_at`. No approval
  machinery ships before a non-reconstructible operation exists.
- [DECISION: owner, 2026-09-26] **Enforcement is at the operation, not the
  surface.** When such an operation is introduced, the server verifies a **fresh
  password at the act** — the sudo shape: the human presents it at the moment of
  the act (TTY prompt or browser form), no capability token is minted or handed
  to a caller, and no per-surface authority matrix exists. Replayability is
  accepted — *a password is a password* — with rotation as the lever. Nothing in
  the current class exercises it; the rule binds the moment the class returns.
- [DECISION: owner, 2026-09-26] **The authentication half is AUTH-01…AUTH-08
  absorbed**: one credential in SQLite, human sessions, machine tokens, trusted
  proxies, fail-loud, the `/web/` + `/api/v1/` re-root, and `/web/setup` +
  `/health` the only anonymous surfaces — with two amendments: AUTH-02 gains
  step-up-at-the-act, AUTH-03 gains *no destructive verbs*.
- [DECISION: owner, 2026-09-26] **Audit.** Every mutating service call appends
  `(at, actor, action, target, outcome)` to an append-only record — a conditional
  write that matched no row, and a key miss, append nothing, because nothing
  happened — and `actor` is a required argument on mutating service entry points so
  it cannot be forgotten.
- [DECISION: owner, 2026-09-26] **`/mcp` over HTTP: the story is decided, the
  mount is deferred.** The network transport's credential is the node's own
  machine-token story (a pre-shared bearer; the MCP spec makes authorization
  optional and its best-practices guidance asks a local HTTP server for a token),
  and the MCP tool surface carries no destructive verbs. Mounting it is a
  **transport change** over settled policy, pulled when a client needs a
  non-local MCP transport.
- [DECISION: owner, 2026-09-26] **RBAC stays deferred** — single authority, no
  per-project authorization. Revisit when a second human or per-project sharing
  appears.
- **Restatements.** ADR-0021's ingress posture (the operator's reverse proxy is
  the only ingress) **stands**; its "ships no authentication" clause is
  **superseded by this decision** — the code follows when the build lands.
  ADR-0017's stdio decision **stands** and remains what a local harness uses; its
  revisit condition is **discharged** (the network transport has a decided auth
  story) without mounting the surface. ADR-0031's *declared* author is superseded
  in part: a draft's author becomes the actor its transport supplies.

## Sanitization — the anticipated first member of the destructive class

- [VOICE: owner, 2026-09-26] From the same design conversation, verbatim: *"*normally*
  neither can silently rewrite history, unless it's sometime intentional and we'd
  better to not question why, in the field"*; *"so in the end, we could just state,
  in the record history: - it is sanitized by who, at when - the current version of
  sanitized content"*; *"to me that's a `git merge --squash` operation, almost"*.
- [DECISION: owner, 2026-09-26] **When an operation in the destructive class
  exists, sanitization is the exceptional authorized rewrite.** Ordinary history
  stays append-only; a sanitize establishes a new trusted root whose prior states
  are unrecoverable **by design** — an authorized rewrite plus aggressive GC, never
  "v17 retained, v18 supersedes it".
- [DESIGN] **The surviving marker is the whole of it: authority, time, scope, and
  the resulting version** — and nothing that reconstructs what left: no reason, no
  prior-content hash, no diff, no sizes. The interrogable question is authority, not
  motive: this credential, valid at this time, held SANITIZE authority, used it on
  this record. Even the identity may be recorded as an authority class.
- [DESIGN] **A sanitize must reach the reconstructible copies the app owns** — the
  verified archive the delete rule leans on, and the run-scoped snapshots the
  rewrite rule retains. The storage and backup domain outside the app is the
  operator's, per the boundary decision above; a sanitize the app cannot complete is
  a partial one and says so.
- [DESIGN] **Its credential check is the class gate**: a fresh password verified by
  the server at the act, presented by the human at the act. It is not exposed in the
  MCP tool namespace — exposure discipline and the operation's authority check are
  different axes, and the gate above is the authority.
- [FACT] **Nothing above is built and no ticket carries it**: it is the shape the
  first operation in the class inherits when a field need arrives.

## Rationale

- **The boundary statement is the design's honesty.** An app cannot distinguish a
  same-uid agent from its user, so the promises are scoped to what can actually
  be enforced, and the deployment path to a real boundary is named instead of
  implied.
- **Attribution is cheap now and impossible to reconstruct later.** A
  self-reported author is worse than none because it looks like evidence.
- **Reconstructibility moves the decisions into data design** — archives,
  snapshots, retire-not-delete — where they hold by construction, instead of into
  a credential ceremony a local process could forge anyway.
- **The rule and its gate are recorded together**, so the first
  non-reconstructible operation arrives with its authorization shape already
  decided.

## Discarded alternatives

- **Attack-resistant same-uid defense** (WebAuthn/TPM, capabilities pinned
  outside the database): a large tax against a residual a same-uid process with
  filesystem write can defeat anyway — it can rewrite the pin, or the code.
  Revisit only if the node holds something worth that.
- **A per-surface authority matrix** (destructive verbs only under the console,
  403 for tokens): rejected — the operation's fresh-credential check is the
  discriminator, and a matrix adds places for drift.
- **Capability tokens bound to an operation**: rejected — *a password is a
  password*; no minted capability crosses to a caller.
- **Keeping the unarchived tape delete behind an approval ceremony**: rejected —
  the archive precondition is the smallest thing that makes the invariant hold;
  the ceremony ships when a real non-reconstructible operation exists.
- **Approvals machinery now** (a pending inbox, retry matching): nothing in the
  class to gate.
- **Two-stage tombstone deletion for tapes**: destruction would then happen
  unattended after a grace window — exactly what a human act should gate. The
  archive precondition gives "delete is reversible" with a durable copy instead
  of a timer.

## Consequences / review hook

- **The draft author changes meaning.** The MCP tools' `author` parameters die:
  a draft version's recorded author becomes the actor its transport supplies —
  `mcp`, which writes every version — while an accept/reject **decision** is
  recorded against the transport that makes it (`console`, `api`, `mcp`). The
  tool count is unchanged, so the ADR-0017 drift guard keeps holding.
- **The destructive surface changes shape.** Tape deletion gains the
  verified-archive precondition; glossary deletion becomes a status change. The
  tests for both move to the new contract.
- **The registry gains the audit record and the operational tables** (secrets,
  sessions, tokens); the migrations are owed through ADR-0030's chain.
- **Docs the build must update**: the run/draft provenance statements in
  `docs/architecture.md` and ADR-0031's Update; `docs/service-deployment.md`'s
  trusted-proxy item the moment AUTH-04 lands; the console-ia lane's auth items
  as they are absorbed.
- **Revisit triggers**: a second human or per-project sharing (RBAC); a client
  that needs a non-local MCP transport (the `/mcp` mount, open questions below);
  the first non-reconstructible operation (its authorization flow).

## Update (2026-09-26) — the run scope and the irreversible set land

- [FACT] The Context item *"Every run rewrites the meeting workspace's
  `record.json`/`segments.json` **in place**"* is no longer true of the build.
  A run writes its manifest, transcript segments, reconciled record and exports
  into its **own copy** (`<workspace>/runs/<run id>/`) and a **finished** run
  publishes that copy at the workspace root, which stays the default read. A run
  that stops, fails or dies publishes nothing, so no run can cover an earlier
  run's record or the workspace's copy; the run's manifest, its segments' `meta`,
  its record's `metadata` and so its JSON export name the run, and its artifact
  rows point at its own copy (the Markdown/SRT/VTT exports carry no run id).
  Retention holds by construction, before any delete rule leans on it — the
  sanitize section's *"the run-scoped snapshots the rewrite rule retains"* now
  names a layout that exists.
- [FACT] A run's **documents** are run-scoped, and so is what it publishes; what
  a run **reads** stays the workspace's — its tapes, the `glossary.txt` a hand-edit
  lands in, and the `.clear-record-ignore` declaration — so `ingest` stays
  idempotent and the `manifest.json` declarations an operator hand-edits at the
  root still reach the next run. Three things a **node** run writes at
  the workspace root all the same: the normalized `audio/` (the ingest stage writes
  it there, not under `runs/<run id>/`), the `glossary.txt` its glossary resolution
  publishes when the registry has confirmed terms (the ADR-0031 tuning loop), and
  the durable `transcribe.log` a running pipeline appends to. The app-owned chunk
  cache is neither: it lives in the app's own cache directory, keyed per workspace,
  and a run only advances it (a resume continues the same cache). The stage
  commands and `calibrate` are the writers that leave **everything** in place, at
  the root and naming no run.
- [FACT] Two decisions above now hold in the build, recorded in
  [ADR-0024](0024-managed-workspace-tape-upload.md)'s Update: a managed tape's
  delete requires a **verified archive** of its meeting — refused with nothing
  unlinked, and the archive action named, when none verifies — and deleting a
  glossary term is a **retire**: the row keeps `added_by`/`created_at`, stops
  biasing the decoder, and Restores to the status the retire took it from.

## Open questions — the `/mcp` mount's revisit

- **Credential conformance**: pre-shared bearer only (works in the
  developer-facing MCP hosts; deviates from the spec's OAuth/PRM path) versus a
  minimal Protected Resource Metadata + OAuth path when a strict client requires
  discovery.
- **Remote-safe tool surface**: verbs that take local paths (`set_meeting_tapes`,
  `start_run`'s glossary path) are meaningful only on the node's own machine —
  refuse them over the mount, filter them from `tools/list` per authorization
  (the spec lets the set vary by authorization), or add an upload-based verb.
- **Session model**: stateless versus the SDK's session-id stateful mode, and
  concurrency with the node's single run queue.

## Update (2026-09-26) — the auth gate lands

- [FACT] The authentication half's first landing is in the build: **one
  credential** in the registry (`console_credential`, one row by the schema's
  own `CHECK`, holding a salted `scrypt` hash and never the password), **human
  sessions** (`console_session`, keyed by the cookie's digest, with an idle
  timeout and an absolute lifetime, both re-read on every request), and the
  **anonymous surface** as a list the route-table test enumerates: the setup route
  (the first run's credential step, then the sign-in form), `GET /health`
  (exactly `{"status": "ok"}`, the tray's probe), and the compiled assets under
  `/static`. Every other route needs a session — pages redirect to the setup
  route, the machine API answers `401` — and the old `/api/health`, which
  named the registry path, is gone.
- [FACT] **The rescue is a command**: `clear-record password` sets or replaces
  the credential in the registry itself, with no session, no browser and no
  running node, and fails closed on a registry it cannot read. Replacing a
  credential ends every session the old one opened.
- [FACT] **A non-loopback bind with no named trust source refuses to start**
  (`CR_TRUSTED_HOSTS` is the declaration the guard's own `Host` check reads, so it
  is the one the startup refusal asks for — a declared `CR_TRUSTED_PROXIES` peer
  makes no `Host` trustable and is deliberately not an admission;
  `--tailscale` trusts its own resolved name), because the request guard would
  answer `403` to every request such a bind received. The loopback default is
  unchanged.
- [FACT] The "not built" list this Update opened with — the trusted-proxy half,
  and machine tokens for scripts — has since emptied of both: **both clauses are
  superseded** — tokens by the machine-token Update above, and trusted proxies by
  [ADR-0021](0021-localhost-only-deployment.md)'s Update *the trusted proxies land*
  (2026-09-26: the console's own code now reads `X-Forwarded-*`, and only from the
  peers `CR_TRUSTED_PROXIES` declares). The credential's *act* — a fresh password
  at a destructive operation — still has no member of its class to gate.
- [FACT] **The re-root landed**: the console answers under `/web/` (pages at
  `/web/…`, fragments at `/web/ui/…`, the credential step at `/web/setup`), the
  machine API under `/api/v1/`, and the compiled assets stay at `/static`;
  `GET /health` stays at the **root**, because it is the tray's and the command
  line's probe — the tray probes it to tell a healthy node from a sign-in page,
  and `clear-record node` proves a recorded address through the same call — and
  it names nothing else. **No old path answers.** What that answer *is* depends on
  the gate, which runs first: anonymously, `/`, `/ui/*` and `/setup` are answered
  as any gated page is (a `303` to the setup route) and `/api/*` with `401`; with
  a live session each reaches the router and is a `404`, because no route and no
  redirect shim survived the move (pre-1.0 breaking change). The
  gate's decisions are unchanged — the session cookie's `Path` is the console
  prefix, so a move of the console moves the cookie with it — and the anonymous
  surface is still the list `web/auth.py`'s `answers_anonymously` returns,
  re-pointed at the new paths, never widened.

## Update (2026-09-26) — the machine tokens land, and the node's own-machine credential is formalised

- [FACT] **Machine tokens are in the build.** `machine_token` (revision `0012`)
  holds one row per token: a **unique** `label`, the SHA-256 **digest** of the
  plaintext as the look-up key, when it was minted, and when it was last used
  (nullable — a token never presented says so). The operator mints one in the
  console (Settings → Status → *Machine tokens*), sees the plaintext **once** in
  that response and never again: nothing stores it, so no reload, second visit or
  registry copy can reproduce it, and losing it means revoking and minting
  another. Revoking **deletes** the row, and the gate reads the row per request,
  so a revoked token is refused on the very next request with no restart — the
  same property a session has, from the same place, and the reason no process
  holds auth state at all. A token's **use** moves `last_used_at` — **lazily**, at
  most once per touch interval, the shape the session's own idle clock uses, so a
  script's burst of calls performs no write at all and the gate's synchronous
  commit cannot stall the event loop it runs inside; a write it does make is one
  the gate tolerates losing to a locked registry, because it must never fail an
  authenticated request. Minting and revoking append `token.mint` /
  `token.revoke`, with `token:<label>` as the target, because "who holds a key"
  is an attribution question and a use is not.
- [FACT] **A token's reach is the machine surface alone.** It satisfies
  `AUTH_REQUIRED` for `/api/v1/…` as `Authorization: Bearer <token>`, and it is
  consulted for nothing else: the console's pages, its fragments and the two
  routes that mint and revoke tokens refuse a token exactly as they refuse an
  anonymous request (`303` to the setup route). The session cookie's path and the
  `answers_anonymously` list are unchanged, so the browser surface is exactly as
  wide as it was; and the refusal the machine surface answers when it has **no**
  credential — `AUTH_REQUIRED` in the `401`'s `detail` — now names both ways in,
  so a script that is refused is told what would have been accepted.
- [FACT] **The actor vocabulary does not change.** A token-authenticated write is
  recorded against the HTTP API's own word, `api`. The `api:<token label>` form
  the Decision above reserves is still **not a value**: it would be a second
  actor grammar in `0010`'s `CHECK`, in `audit.require_actor` and in every reader
  of the record, and the label is the operator's handle on a credential rather
  than an identity. It stays reserved for the change that needs it.
- [FACT] **The destructive contract now has its proof under token auth.** The
  machine surface's two `DELETE` routes are the glossary **retire** (the row
  survives the verb and restores) and the managed-tape delete (refused with
  nothing unlinked until a meeting has a **verified archive**, which its answer
  names as the durable copy). Both are exercised with a token as the credential,
  and the route table is walked so a **third** `DELETE` fails the suite rather
  than arriving quietly — the decision point this ADR's authorization rule asks
  for.
- [FACT] **The node's own-machine credential stays a session file, and this
  Update is where that choice is recorded.** The earlier Update lists "machine
  tokens for scripts" as not built; that clause is **superseded** for tokens — and
  the local session the command line presents is deliberately **not** replaced by
  one. A token's plaintext is shown once and stored only as a digest, so a node
  could not hand its own command line a token across restarts without minting (and
  listing) a fresh credential per start; and a credential the operator can revoke
  from a page is the wrong shape for a client that must keep working while nobody
  is looking. The file is instead named for what it is — the node's
  **own-machine credential** — with the five constraints it is kept under pinned
  by test: it is a session opened through the same `ConsoleAuth` path a sign-in
  uses, judged by the same two clocks from the app's one policy, written beside
  the address record at mode `0600`, removed on a clean exit, and refused like any
  other dead session once it is stale or unknown. The client half — that it is
  presented only to the **recorded** node — is unchanged. The two credentials
  share the property that matters: neither can destroy what a durable copy cannot
  reconstruct.
- [FACT] **Docs the build carries**: `docs/service-deployment.md` §1 gains the
  operator's machine-token page (minting, presenting, revoking, and what a token
  can never do) and the own-machine-credential paragraph, and §6 no longer lists
  machine tokens among what is not built.
