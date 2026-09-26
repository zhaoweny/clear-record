# ADR-0032 — The node and its clients: one API server at the centre, the surfaces as backends-for-frontends

Status: active
Date: 2026-09-24

The direction is in force and is being built in batches; its **first batch** — the
pipeline leaving the command surface — landed on 2026-09-24 (ADR-0030's `C3`;
ADR-0004's text of 2026-09-24, an inline restatement inside its 2026-09-13 Update;
and ADR-0012's matching Update), and **the facade itself landed 2026-09-25** — a
command-line run is a node run, carried by the Update *the facade landed: a
command-line run is a node run* below. The
spec, its batches and its remaining tickets are in the tracker's `architecture`
lane; per ADR-0029 nothing here cites it by address. The owner's words this ADR
rests on are carried verbatim by
[`docs/vox/records/2026-09-21-node-and-clients.md`](../vox/records/2026-09-21-node-and-clients.md).

Provenance of the decisions below: each is labelled with **who decided** — the
owner's own words of 2026-09-21; a reading the owner's answers confirm (the spec
author's, adopted here so the answer is a decision rather than drift); a shape this
ADR **reaches for itself**, labelled `[DESIGN]`; or the direction as it landed. The
owner's wording is tentative in form (*"I think …"*), which by the voice record's
own rule leaves it waiting for an ADR; **this ADR is that firmer word**, and it
promotes nothing the owner did not say beyond the label each clause carries.

## Context

- [VOICE: owner, 2026-09-21] The direction, verbatim: *"I think we do a server
  centric move and CLI become a facade of the server, which is reasonable at this
  stage and very much suited."*
- [VOICE: owner, 2026-09-21] Asked to choose between the facade and a smaller
  client arm, the answer was *"I think option C?"* — the spec records that as the
  owner taking the facade; asked to hold the scope, *"I think we can wait"*.
- [VOICE: owner, 2026-09-21] The shape: *"we reverse the order of things and move to
  a 'api-server centric, then all kinds of facades and adapters on top'
  architecture. essentially we have a bunch of BFFs - MCP CLI and WEB"*.
- [VOICE: owner, 2026-09-21] One app, several prefixes: *"yes, we can certainly
  share a FastAPI endpoint and do e.g. /web /mcp /api/v1 etc"*.
- [VOICE: owner, 2026-09-21] The preliminaries, verbatim — lifecycle: *"do a state
  of pid file or a named pipe or a note of http server endpoint, or just set it in a
  config file so everyone (CLI MCP API WEB etc) agree where to call the server"*;
  authority: *"I think CLI joins the queue"*; auth and the backchannel: *"a
  backchannel is a option here; and I think the CLI is currently mainly serving the
  local host, not really the remote host. but one day the CLI would be another
  endpoint to call a remote host, I guess."*
- [VOICE: owner, 2026-09-21] The precedent the owner named: *"we can learn from
  opencode2 by have a opencode service subcommand and have all backend operations
  there"*.
- [FACT] **The server already exists** and is not a new component: uvicorn +
  FastAPI, started by `clear-record web`, `clear-record serve` or the tray;
  loopback by default, with **no authentication by decision** (ADR-0021: remote
  access is the operator's reverse proxy). *(Superseded 2026-09-26 by
  [ADR-0033](0033-the-auth-position.md)'s auth gate: the server bound to loopback
  now holds one console credential and every route but the setup page, the
  liveness route and the compiled assets needs a signed-in session. The ingress
  posture — the proxy is where a *remote* client enters — stands.)* Its run API is
  `POST /api/v1/meetings/{id}/runs` → 202, `GET /api/v1/runs/{id}`, and
  `GET /api/v1/runs/{id}/events?after=<cursor>` — cursor-paged progress events.
- [FACT] **A client is already written.** `tray/service.py` supervises the server,
  health-probes it and speaks HTTP with the **stdlib `urllib`** — so a client needs
  no new third-party dependency, and that property is a fact rather than a hope.
- [FACT] **The registry is the authority.** SQLite, the single owner of run state,
  because "two writers (console, MCP) meet at the same table" (ADR-0030), with runs
  claimed from a shared, cross-process queue.
- [FACT] **The documented intent was already this shape, applied to the other
  surfaces.** ADR-0016: the JSON API exists for "scripts, the MCP server, and any
  future client", with `clear_record.service`'s operations underneath both.
  ADR-0017: MCP is a thin adapter over `clear_record.service`. ADR-0013: "headless
  service first". What was missing is the command line's membership.
- [FACT] **The gap that makes the direction concrete** (measured 2026-09-21, and
  **closed 2026-09-25** — see the Update *the facade landed: a command-line run
  is a node run*; quoted here as it stood when
  the gap was measured): a run started from the
  command line **writes no run row** — `clear_record/service/runs.py` says so in
  `RunManager`'s own docstring ("the CLI is not a third writer: it runs the pipeline
  in-process … and writes no run row") — so the console cannot see it; while `cli`
  is already a declared run **origin** in `clear_record/service/lifecycle.py`
  (`ENQUEUE`, whose origins are `console`, `api`, `mcp`, `cli`) with **no surface
  that enqueues it**. The design expects a CLI-shaped surface to go *through* the
  service. Nothing implements one.
- [FACT] **What is missing, measured** — the spec's own census rather than this
  ADR's:
  - **Per-stage operations do not exist server-side.** `ingest`, `align`,
    `transcribe`, `reconcile`, `export`, `diarize` and `attribute` have no route:
    the server can run a **whole run**, and only over a *meeting* (a registry
    object), never over the command line's *local directory*.
  - **The knobs**: `RunCreate` accepts 8 of the command line's ~20+
    (`chunk_seconds`, `overlap_seconds`, `glossary`, rerun scope and the decoder
    knobs generated from `core.RUN_KNOBS` are absent).
  - **The channel carries progress, not words**: the events are `JobEvent`s, and 16
    data items the stages print reach no event.
  - **Addressing is the structural problem.** The command line's argument is a
    **local directory**; the server's are **registry ids**. A client that sends a
    path sends a path *on the server's filesystem*; the only upload route is for
    tapes, with no resume, and none exists for reference transcripts or glossaries; a
    model must already be on the node.
  - **Machine-local verbs cannot be facades**: `synth` (it generates fixtures where
    the pipeline runs), `backends` (it probes *the client's* machine — under a
    facade its subject silently becomes the node's), `bench`, `diagnose`.
  - **The tray and `mcp` invert**: the tray supervises a server it starts, and
    `mcp` is a stdio adapter over the service in process; both would become
    clients of a node they may no longer start.
  - **Lifecycle: nothing decides it.** At the census's own date (2026-09-21) there
    was no discovery of any kind (no pidfile, no socket, no port environment
    variable — the default port `8765` was then written in four places), no
    behaviour for "no node answers", no `serve --supervise` (a docstring promise,
    not a flag), and the Tailscale console URL was printed but never recorded for
    another process to find. **The address batch (2026-09-24) has since landed the
    record, the one answer and the one declaration, and the supervision batch
    (2026-09-25) has landed `serve --supervise`** — this bullet is the gap as it
    stood when the direction was written, not as it stands now.
- [FACT] **The precedent the owner named**, read from OpenCode v2's own
  documentation: a backend subcommand owns every backend operation and publishes an
  **OpenAPI 3.1 spec** at `/doc`, from which its **client SDK is generated**; the
  default invocation starts a backend and **attaches** a client to it; `attach
  [url]` does the same against a remote backend; the remote case carries its own
  auth (`OPENCODE_SERVER_PASSWORD`, `--cors`) while the local case does not; events
  are SSE (`GET /event`). What **transfers** is the recorded-endpoint idea
  (preliminary 1 below), the default invocation starting a node and attaching to it
  (preliminary 5's residue), and a **schema-derived** client — which is how the wire
  shapes stop being hand-copied above `cli`. What **diverges** is the in-process
  surfaces: OpenCode's server is its only implementation, while clear-record keeps
  the console and MCP as in-process adapters. The SSE-versus-polling difference is
  an input to the channel work, not a requirement: clear-record already serves a
  cursor-paged events endpoint and polls it, and adopting SSE is a separate
  decision.
- [FACT] **The prerequisite landed.** A node cannot own execution by importing
  `clear_record.cli`, so the pipeline left the command surface for its own
  `clear_record.pipeline` layer (2026-09-24, ADR-0030's `C3`); the guard's
  `ALLOWED_INTERNAL` no longer carries `service → cli`, and
  `test_no_layer_imports_the_cli` now covers `core`, `engine`, `providers` **and
  `service`** (ADR-0004's restatement of 2026-09-24, inside its 2026-09-13 Update,
  and ADR-0012's matching Update).

## Decision

- [DECISION: owner, 2026-09-21] **One API server is the centre, and the surfaces
  are backends-for-frontends over it** — MCP, the command line and the browser
  console. This is the direction ADR-0016 already describes ("two surfaces over one
  service adapter … neither contains domain logic") and ADR-0017 already applies to
  MCP; the change is that **the command line joins them**, and the server stops
  being *one of* the surfaces.
- [DECISION: owner, 2026-09-21] **The command line becomes a facade of a node**:
  for the operations a node owns, `clear-record` speaks the node's protocol instead
  of executing in process. It is the surface this direction changes; the console and
  MCP already sit over the service.
- [DECISION: spec author, 2026-09-21] **BFF is about the architecture, not about
  every surface becoming a network client.** The console **stays an in-process
  backend-for-frontend**: it calls `clear_record.service` in process, one lifecycle,
  no internal hop. Only the command line becomes a client of the node **for its
  operations**; the MCP adapter dials the recorded address only to ask whether the
  node is there, the tray probes the socket it bound, and the console answers in
  process, with no request of its own. The owner's one-app answer below is what
  confirms this reading; the spec asked the question so the answer is a decision
  rather than drift. A console that becomes a **channel client** is **deferred, not
  rejected** — worth revisiting only when the console must run elsewhere.
- [DECISION: owner, 2026-09-21] **The node is one FastAPI app with its surfaces
  mounted at prefixes**, not several processes:

  | prefix | surface | today |
  |---|---|---|
  | `/api/v1` | the JSON API — the contract every client speaks (the command line first, the MCP server as it chooses) | **built**: the API answers under `/api/v1`, and `/api/v1/docs` publishes its schema. Every route FastAPI provides is under the prefix, so the schema and the two documentation UIs are **machine requests**: a browser's console session never rides them (the session cookie is scoped to `/web`), and a reader presents a credential in a header (`curl -H "Authorization: Bearer …" …/api/v1/docs`, ADR-0033's token Update). (Unversioned under `/api/…` until 2026-09-26.) |
  | `/web` | the console BFF — pages and htmx fragments | **built**: the console answers under `/web` — pages at `/web/…`, fragments at `/web/ui/…`. (At `/` and `/ui/*` until 2026-09-26.) |
  | `/mcp` | the agent surface | today **stdio only** (`clear-record mcp`) |

- **The five preliminaries**, each labelled for itself — four resting on the
  owner's words (three of them directly, and offline/rescue through the requirement
  ADR-0013 already carries from the owner's 2026-09-14 voice), and the fifth left as
  the residue of the one question the shape did not answer:
  1. [DECISION: owner, 2026-09-21] **Lifecycle and discovery.** The node's endpoint
     is **recorded** — in the app state/config area the path ADR-0025 resolves — and
     the surfaces **find** it instead of scanning: the command line and the MCP
     adapter dial it, and the console answers with it in process. The tray is not
     one of them: it starts a node, publishes the record, and probes the socket it
     bound. Whether the recorded channel is a loopback HTTP endpoint or a **unix
     socket / named pipe** is the backchannel question in (4).
  2. [DECISION: spec author, 2026-09-21] **Offline and rescue — narrower than the
     direction first stated.** *Offline* is about the cloud, not about a server:
     ADR-0013's requirement is that the console works after one install step,
     offline, and the repo's framing is that processing runs offline once models are
     provisioned (`docs/architecture.md` §1, `CONTEXT.md`). A local node is not a
     network dependency — same machine, same workspace, same models — so requiring
     one does not break offline. *Rescue*'s **recorded** reading is that the
     **browser** path is unusable (the tracker's `console-ia` lane: the web path is
     the primary wizard, the command line is for rescue operations — scripted or
     headless bootstrap, a broken session), and a command line that is a client of a
     local node satisfies it completely once auth ships. An earlier draft read
     rescue as "the node is unusable"; that is an **inference**, not the owner's
     words, and is recorded as such. The residual is therefore only **what happens
     when no node process exists** — and for the one case neither reading covers, a
     machine where a node **cannot** run, the honest answer is that **the node *is*
     the tool**: the command line's job is to say so clearly rather than to fall
     back into a second implementation.
  3. [DECISION: owner, 2026-09-21] **Authority.** A command-line-started run is a
     **node run**: it writes a run row and joins the one-run-per-node queue. This
     closes the gap that made the direction concrete — a run the console cannot see.
  4. [DECISION: owner, 2026-09-21] **Auth, and the backchannel.** The design target
     is **the local host**: a backchannel the local user is already trusted over
     (unix socket or loopback) rather than a credential story. A remote client is a
     **future** case whose auth belongs with the deferred in-app-auth work and
     ADR-0021's "the operator's reverse proxy is the ingress" posture.
  5. [OPEN → **answered 2026-09-25**] **Packaging — the one residue.** If an invocation
     must be able to **bring a node up**, the node's stack stops being an optional
     extra (today FastAPI and uvicorn sit behind the `web` extra, ADR-0013). The
     owner's fifth answer was the **shape** (the table above), not the packaging
     question the spec also asked: **does an invocation ensure a local node** — the
     precedent's "start a backend and attach" — **or does it require one already
     running?** The answer decides whether the base install carries the node's
     stack.
- [DECISION: spec author, 2026-09-21] **The layer edges that follow are the ones
  already enforced, unchanged.** A command line that is a **wire** client imports
  nothing from `service`, `web` or `tray`: the guard's `cli` row stays as it is
  (`cli → {core, engine, providers, pipeline}`), and `test_no_layer_imports_the_cli`
  continues to hold with `service` inside it. **No new layer edge is introduced by
  this direction**, and the shapes a client speaks come from the API's own schema —
  the precedent's generated client — not from hand-copied Python models above
  `cli`.
- [DESIGN] **`/mcp` is a reserved prefix, unimplemented, and mounting it is
  ADR-0017's own trigger firing** — not a reversal. Its discarded alternative said a
  network transport "would add a listening socket and an auth story to a local-first
  tool that already has a JSON API for remote needs", and it ends "Revisit if a
  client needs a network transport, or if the extra proves to be friction." This
  direction supplies precisely the two things that objection named — a recorded
  endpoint every surface can find, and clients that are not the local user — so the
  mount belongs to the **auth** change, which restates ADR-0017 and ADR-0021
  together.
- [DECISION: spec author, 2026-09-21] **The `serve` subcommand's name does not
  change.** What transfers from the precedent is *ownership* — one subcommand
  holding every backend operation — not the word: `clear-record serve` already is
  that subcommand (headless; `web` is its interactive posture). A rename to
  `service` would ride along with help-text and catalog churn for no structural
  gain, and is recorded here as a separate, cosmetic decision the owner has not
  asked for.
- [DECISION: spec author, 2026-09-21] **The command line's workspace contract
  changes, and the two statements that carry it today understate it.** `README.md`
  says *"A `--dir` workspace and a meeting's user-chosen `workspace_path` are
  unchanged"*, and `docs/service-deployment.md` says *"a `--dir` workspace and a
  meeting's user-chosen `workspace_path` stay user documents"*. Both stay true about
  **where the files live**, and neither is the whole story about a **run** once this
  direction lands: a run that joins the queue **writes a registry row**, and a client
  needs a node to talk to. That is the real contract change this direction makes, and
  it is stated rather than discovered; the two statements (and the hand-over) need
  the qualifier when the facade lands.

## Rationale

- **One queue, one authority, every surface.** The console already sees everything
  the node owns; the command line is the last surface that is not a client, and its
  runs are invisible to the console precisely because they bypass the registry that
  ADR-0030 made the single owner of run state.
- **The promise ADR-0016 already made is what this pays.** Its JSON API was
  documented for "scripts, the MCP server, and any future client"; the command line
  was the client that never arrived, and nothing about the API is new for the
  purpose.
- **A client costs no new dependency.** `tray/service.py` is the proof: HTTP over
  the stdlib `urllib` is enough to be a client, so the facade puts no *third-party*
  client library or SDK into the base install.
- **Keeping the console in process keeps the change honest.** "Reverse the order of
  things" is about **who owns the operations**, not about inserting an HTTP hop and
  a second lifecycle inside one process; an internal hop would buy nothing the
  in-process call does not already have, and would make one process's failure modes
  two.
- **The schema is the contract, so it cannot be hand-copied.** A client whose shapes
  are derived from the API's own schema cannot drift from the server the way a
  second set of Python models would — which is the precedent's reason for publishing
  a spec at all.

## Discarded alternatives

- **The command line keeps a local path and gains a read-only client arm** — what
  the 2026-09-14 record actually deferred, and the smallest option: it preserves the
  self-sufficient-directory contract and the rescue path. Not taken: the pipeline
  would still have two callers, and the console still could not see a
  command-line-started run. Recorded so the alternative is considered rather than
  silent.
- **A client with an in-process fallback** — both worlds, two code paths forever,
  and it needs discovery anyway.
- **The command line enqueues through the service while still executing itself**
  (using the unused `cli` run origin) — fixes console visibility without requiring a
  node, but it needs the **opposite** layer edge, `cli → service`, which the guard
  asserts against today. That is a second deliberate layering revision, recorded in
  ADR-0004/ADR-0012 beside the pipeline move's — not a freebie riding on it.
- **The console becomes a channel client** — the other answer to the BFF question;
  deferred rather than rejected, and the revision to revisit if the console must run
  somewhere other than the node.
- **SSE now** — the precedent uses event streams, but clear-record already serves a
  cursor-paged events endpoint; adopting SSE is a separate decision about the
  channel, not part of the direction.
- **Renaming `serve` to `service`** — cosmetic, and it renames the one subcommand
  that already owns the backend operations.

## Consequences / review hook

- **The layering guard is unchanged by this direction.** The `cli` row stays
  `→ {core, engine, providers, pipeline}`, and `test_no_layer_imports_the_cli` keeps
  covering `service`. A facade that reached for `service`, `web` or `tray` would be
  a deliberate layering revision — update the DAG and ADR-0004/ADR-0012 together, in
  one landing, exactly as the pipeline move's correction did (ADR-0030's pattern:
  the guard and the ADRs restated together).
- **What the node owes a client is batch work, not this ADR's** — per-stage routes,
  the knobs `RunCreate` is missing, the words on the channel beside the progress
  events, and addressing (the next bullet). Each is a ticket in the tracker's
  `architecture` lane.
- [OPEN → **decided 2026-09-25**] **How a client addresses a workspace.** (The
  question this bullet names is **settled**; the text below is kept as it stood
  when it was open, and the settlement is the Update *how a client names what it
  wants, settled*.)
  The command line's subject is a
  local directory; a node's subjects are registry ids, and a path a client sends is
  a path *on the node's filesystem*. The upload route that exists covers tapes only
  and has no resume; reference transcripts, glossaries and models have none. This is
  the direction's central unsolved design problem, named here so a ticket takes it
  deliberately rather than by accident.
- **The machine-local verbs stay the client's own.** `synth`, `backends`, `bench`
  and `diagnose` have the client's machine as their subject — under a facade that
  subject would silently become the node's — so they are not part of the facade's
  surface. What each becomes is a ticket decision, not this ADR's.
- **ADR-0016 is extended, not superseded**: its "two surfaces over one service
  adapter" becomes three named surfaces on one app. ADR-0013's "headless service
  first" is what makes the node the centre rather than a second implementation.
  ADR-0017 is unchanged; its revisit trigger is what mounts `/mcp`, together with
  the auth change.
- **The release notes and the hand-over owe the contract change** in the Decision
  above: a command-line run now writes a registry row and needs a node to talk to,
  so "a workspace is a self-sufficient directory" is no longer the whole truth.
- **Revisit** if the console must run elsewhere (the BFF question reopens then), if
  an invocation must be able to bring a node up (the packaging residue, [OPEN]
  above), or if the node's stack behind an extra proves to be friction the other
  way.

## Update (2026-09-25) — the facade landed: a command-line run is a node run

- [FACT] **The Context's first gap is closed, and the docstring it quoted is
  gone.** `clear-record run` is the CLI-shaped surface that goes *through* the node
  — the Decision's third preliminary, landed: the command **ensures** a node
  (attaching to the recorded one, or starting one and leaving it running),
  submits the run over the node's run API with the `cli` origin, follows the row
  the node owns and prints the run's own stream (`cli/runs.py`); a flag the node's
  run API does not carry is refused rather than dropped. So a run started from the
  command line **writes a registry row**, and the console's Activity list shows it
  while it runs and after. `RunManager`'s docstring quoted in the Context has been
  replaced; both that bullet and the quotation above are kept as they stood when
  the gap was measured. One consequence is recorded where the pin lives: `run`'s
  default stdout is now the node's own stream, so ADR-0022's byte-identical clause
  is scoped to the stage commands (that ADR's own Update).
- [FACT] **The Context's channel gap is closed: the events carry the words.** The
  census's third bullet — the events are `JobEvent`s carrying progress, and the
  data items the stages print reach no event — is what the channel batch landed:
  a stage's mid-stage lines, each pass's summary, and the rows its own report
  holds are all reported as events whose `message` is the text, with the source
  they are about where they have one and in the order the pass produced them. So
  one payload serves every consumer — the command line prints it, a client
  reading the run's stream reads it, and the console's activity row and run
  fragment show it — and a pure progress report carries no words at all, which is
  what lets a client print every message and nothing else. Two consequences recorded
  where they live: `run`'s block is the stage commands' own bytes read back, with
  the surface's end line before `[next]` (ADR-0022's Update, again), and the
  stages' returns are now for a caller that wants the typed value rather than for
  rendering.
- [FACT] **`serve --supervise` is a flag now**, not the docstring promise the
  Context's census listed among the gaps: it is declared on `serve` alone, and an
  unasked stop restarts the server while an asked-for stop or a signal ends it.
- [FACT] **The two statements the Decision flagged carry their qualifier.** README's
  "A `run` is a node run" passage says a run is the node's and that the command
  ensures one, and `docs/service-deployment.md` says a run over a workspace writes a
  registry row and a client needs a node to talk to — so "a workspace is a
  self-sufficient directory" is qualified where a reader meets it.
- [FACT] **Preliminary 5 is answered, and it has an edge the base install does not
  cover.** The question was whether an invocation must be able to **bring a node
  up**; it must, so the node's stack has to be present for `run` to work at all.
  `serve` — the command the ensure path starts — is registered **unconditionally**
  from the base wheel's entry points (the provider contributes its subcommands so
  they are visible in `--help` without the extra; running one explains how to
  install it), and what the `web` extra supplies is the **stack**, not the verb:
  without it `serve` exits non-zero with the install hint, and the ensure path's
  child dies on that exit — its streams are on `DEVNULL`, so the hint never
  reaches the user. `run` there ends on the one sentence, and the two remedies
  that sentence names are the paths that speak. The desktop bundle carries the
  stack; whether the base install should carry it too, so `run` works without the
  extra, is the release decision this leaves open rather than decides.

## Update (2026-09-25) — how a client names what it wants, settled

- [DECISION] **A path is a local client's noun; everything else is addressed the
  way the registry addresses it, and a model is the exception in both
  directions.** The Consequences bullet above asked how a client addresses a
  workspace; the answer is that it names a **path** only when it addressed the
  node **itself** — the node's own address, not another name for it (a run's
  `<directory>`, a tape's files, an archive root) — and otherwise names the
  registry object by its id. A **model** is addressed neither way: it must already
  be on the node that runs the work, so a run request carries the *name* the node
  resolves in its own models directory (`CR_MODELS_DIR` / `--models-dir`, which
  are the node's) and never a path.
- [DECISION] **"Local" is decided by the name the client addressed, not by the
  connection.** A request's `Host` has to name the node **itself**: a loopback
  name (where a node binds by default, and what the recorded wildcard bind is
  dialled at), or the very address this node is listening on — `serve --host
  192.168.1.5` records and dials exactly that, so a client on the node's machine
  sends it. `web.app._served_by` answers that address in process, the same one
  `GET /api/v1/node` vouches for and the record carries. It is also the same fact the
  request guard already reads.
- [FACT] **What that admits, stated rather than left to be discovered.** `Host`
  says which name the client addressed, never where it sits, so this is **not**
  same-machine-only and cannot be: a LAN client dialling the node at that same
  address is admitted too. Two things bound it. It is not new exposure — the
  request guard already requires every `Host` to be loopback or named in
  `CR_TRUSTED_HOSTS`, so a node on a named address is reachable there only because
  the operator published it there, and without this a client on the node's *own*
  machine would be refused its own node. And what the rule refuses is a client
  addressing the node by **another** name — the hostname a proxy publishes, say —
  which is a client elsewhere, naming a path of its own; that is the case the
  sentence answers. The peer address is deliberately *not* the test — an
  operator's proxy forwards from loopback and a published container port arrives
  over the host's bridge, so a peer test would refuse a local client in the
  documented container posture while accepting a remote one in every proxied
  deployment.
- [FACT] **The rule is enforced once, at the machine-facing edge, and reads the
  same to every client.** `web.app` takes a path only from a request that named
  the node itself: `POST /api/v1/runs` (a run's directory), `POST
  /api/v1/projects/{slug}/meetings` (a `workspace_path`), `PUT
  /api/v1/meetings/{id}/tapes` (a tape set), `POST /api/v1/projects` and `PATCH
  /api/v1/projects/{slug}` (a project's `default_archive_root`, which is where its
  archives are later written), `POST /api/v1/meetings/{id}/archives` (a named
  root), and either run edge when the body names a **`glossary`** — the file the
  run decodes with, which is a path for the same reason a directory is — answer
  **403** with one sentence naming the rule and, per case, the shape to reach for
  instead (`PATH_IS_LOCAL`). A request that names no path is untouched. A
  **path-valued `model`** is refused **400** by each of the two machine-facing run
  edges *before* it resolves anything (`MODEL_IS_THE_NODES`), so a refused
  request writes nothing — not even the meeting the directory edge would have
  registered; the console's run form and the MCP adapter carry `model` through in
  process, as the node's own surfaces.
- [FACT] **The registry-addressed run is unchanged in behaviour.** `POST
  /api/v1/meetings/{id}/runs` still answers any client that can reach the node: a
  meeting is named by id, no directory is involved, and the run takes the queue,
  the claim and the row it always took. That is the route a client elsewhere
  uses, and the sentence above names it.
- [FACT] **The rules are stated where a client author meets them**, not
  discovered: in the request shapes' own declarations, so the OpenAPI schema at
  `/api/v1/docs` publishes them (`RunCreate`, `WorkspaceRunCreate`, `MeetingCreate`,
  `TapesUpdate`, `ArchiveCreate`, `ProjectCreate`, `ProjectUpdate`, and each
  route's description) — a machine request, so a reader presents a credential in a
  header and a browser's console session never rides it (ADR-0033); in the command
  line's `run` help; in README's "A `run` is a node run" passage; and in the
  operator guide, where the proxy recipes **pin the published name** rather than
  forward the client's own `Host` (§2, §3).
- [DECISION] **The console keeps its path fields, and only the machine-facing
  edge refuses.** The Decision makes the console an in-process backend-for-frontend —
  it *is* the node's own face, it shows back the paths the node resolved (the storage
  panel), and its forms name the node's folders rather than a client's — so the
  machine-facing JSON routes are where the rule binds and the `/web/ui/*` forms and
  pages are deliberately **not** guarded. A visitor who reaches the console
  through the operator's proxy can therefore still type a path there; that path
  is the node's, and it is not offered as a way to name a file on the visitor's
  own machine. What such a visitor is *not* given is the machine-facing refusal,
  because the console is not that surface: what the console shows a remote
  visitor is unchanged by this batch.
- [OPEN] **A reference transcript has no way to reach the node** — not by upload
  and not as a registry object. Tapes alone have an upload route (ADR-0024), and
  the census line that named this stands; the reference transcript's carriers are
  the command line's own `calibrate --reference-transcript` and the machine-local
  `bench --reference`, neither of which has a node route (a run's `--reference` is
  an **alignment source id**, not a transcript), so that noun remains to be named
  by whichever ticket gives it a route. The **glossary** half of this bullet is
  closed by the fact below: a run's glossary is carryable now, as a path a *local*
  client names. What stays open for it is a client **elsewhere**, which still has
  no way to send the file itself (neither an upload route nor a registry object).
- [FACT] **The knobs a client may set are the command line's, and the run record
  keeps them.** The census's second bullet is closed: `RunCreate` declares one
  field per row of `core.RUN_KNOBS` — the decoder block included — plus the
  glossary and the re-run scope (`rerun_sources` / `rerun_range`), and `None` is
  the declaration's own *unset* sentinel, so an omitted body field leaves the run
  to the **node's** `CR_*` environment, the requested profile and its built-in
  defaults. The command line carries every knob it accepts and sends the value the
  parser produced: a knob unset **everywhere** — no flag, and nothing in the
  client's own `CR_*`, which an unset flag resolves from there — goes as unset so
  the node decides it, while a `CR_*` value on the client's machine is a value the
  client sends. Its refusal list is down to the stage and probe flags and
  `--models-dir`, which is the node's own, and the console's run form offers the
  declaration's non-decoder rows only — the decoder block stays the profile
  picker's, and a blank box there means *unset*, never `0`. Both JSON run edges
  thread the body's knobs into the options the run resolves from, so what a client
  set is what the run executes with and what its row records (`run_options`); and
  a body field nobody declares is **refused** rather than dropped. A path-valued
  `glossary` follows the rule above: 403 with the same sentence on either run
  edge, while a client that names none gets the node's own glossary.

## Update (2026-09-25) — the two surfaces that invert

- [FACT] **The tray attaches before it starts anything.** The Context's "the tray
  and `mcp` invert" bullet is half closed: `ServiceController.start` resolves the
  recorded address and completes one request against it — the attach path
  `cli.ensure_node` already takes — so a node that is already up is the node the
  tray becomes a client of and **no second node is started**; only when nothing
  answers does it start one, embedded in its own process as before, publishing the
  record exactly as every other posture does. Preliminary 1's "it starts a node,
  publishes the record, and probes the socket it bound" — and the BFF
  decision's "the tray probes the socket it bound" — therefore describe the
  case where none answered, not the tray's first act. Two consequences are
  recorded where they live: the live state is the **node's** health rather than
  this process's thread (a node the tray joined has no thread here at all), and a
  node the tray only joined is not its to stop or restart — `stop`/`restart` act
  on the node this process started, and the menu offers restart only for that one.
- [FACT] **The MCP adapter's posture is stated for both cases.** The other half of
  that bullet: the adapter stays an in-process adapter over
  `clear_record.service` — the Decision's "dials the recorded address only to ask
  whether the node is there" — and what it hands the agent now says so: the tools
  are the same service the node serves, in this process (one registry, one run
  queue), they answer with or without a node, and the adapter starts none. Which
  case holds follows as the one sentence, a node listening (at its address) or
  none at all, so an absent node no longer reads as "these tools cannot work".
