# Release notes

These are the notes kept for each release: what it adds, what an existing
install does to move onto it, and what it removes. The publishing machinery — the
three tiers, the TestPyPI rehearsal and the release loops, versioning — is in
[`docs/releasing.md`](releasing.md), which is also the source this page quotes
for the registry policy.

Install or upgrade with `pip install --upgrade clear-record`, or run it without
installing with `uvx clear-record`.

## 0.3.0

**A consolidation release with one breaking change.** 0.3.0 ships the work that
landed on `main` since `v0.2.0`, plus the release's one breaking change:
clear-record stops being an LLM client and becomes a **backend for AI harness
agents**.

There is **no telemetry, no account and no cloud**, and the local-first stance is
unchanged: recordings, models and the registry are local files, and everything
works offline once your backends and models are provisioned (the only network
request the tool makes on its own is the first-use model download).

### What 0.3.0 ships

The bullets below are what the release offers. Those tagged **carried from
`0.2.0`** are capabilities a published `0.2.0` install already has; the rest are
new or extended in this release.

- **A run queue with live status.** The console's **Activity** page (`/activity`)
  shows the runs in flight and the most recently finished runs across every
  project on the machine — including a run an MCP client started — and the header
  chip reads the same registry rows, so the header and the page cannot disagree
  (`web/app.py`, `web/views.py`).
- **Cancel and resume, with honest interruption.** A queued run is stopped where
  it stands; a running one is *asked* to stop and ends at a safe boundary, so the
  audio cache stays consistent. A run whose owning process died is recorded
  **`interrupted`** — distinct from `failed` — with the reason stored, and
  resume starts a new run continuing the old one, re-using everything the chunk
  cache can prove (`service/lifecycle.py`, `service/runs.py`).
- **A per-run cost record.** Every run that stops — done, failed, stopped or
  interrupted — records its own measurement: per-stage wall-clock, audio seconds,
  the chunk economy (reused / re-decoded / total), backend, model, jobs, chunk
  size and peak worker memory. A primitive the run never produced stays empty
  rather than guessed (`service/runs.py`, `service/benchmark.py`).
- **The tray's live console state.** The tray menu's status line is a fresh local
  health read, not a cached snapshot — "Running at <url>", "Not responding" (the
  process is alive but the health check is not answering) or "Server not running"
  — refreshed every two seconds (`tray/app.py`, `tray/service.py`).
- **A re-run scope on the command line.** The scope (`--rerun-source`,
  `--rerun-range`) re-decodes only the chunks it selects and reuses every other
  chunk from the cache. **An edit with no scope still re-decodes every chunk of
  every source** — the glossary is the decoder's initial prompt, so any chunk's
  output can move; the scope is how you say which part to re-decode instead of
  paying for all of it. The scope is a command-line option: the console and the
  MCP tools do not offer it (`cli/cli.py`, `cli/stages.py`, `core.ChunkScope`,
  `engine/text.py`). **Carried from `0.2.0`.**
- **Profiles and `--auto`.** The built-in decoder presets (`fast`, `balanced`,
  `accurate`) trade decoder effort at a fixed model; the opt-in `--auto` flag
  picks a profile and a model and explains the choice; `--backend auto` picks the
  first available ASR backend. `--auto` never downloads a model
  (`core/options.py`, `cli/auto.py`, `cli/cli.py`). **Carried from `0.2.0`.**
- **The MCP tuning loop.** The stdio MCP server exposes the loop as tools — read
  the transcript, write candidate terms, start a run with an explicit
  profile/backend/model or let the auto resolvers choose — so a harness, not the
  app, does the model work. Its surface is 22 tools (`mcp/server.py`). **Carried
  from `0.2.0`.**
- **The archive view.** The project's **Meetings** tab lists its archives (a copy
  plus a sha256 manifest); each row checks its archive as it appears, and the
  **Verify** button re-runs the check (`web/templates/_archives.html`,
  `web/app.py`, `service/archive.py`). **Carried from `0.2.0`.**
- **A tape upload that starts only when there is room.** An upload into a managed
  workspace is refused when the workspace has no room for the declared size; the
  body is copied to a scratch file and moved into place only once it is complete,
  a failure removes the partial file and leaves no tape behind, and a scratch file
  left by a killed process is refused on reuse — resume is not built
  (`service/managed.py`, `web/app.py`). **Carried from `0.2.0`.**
- **The registry, with no separate migration step.** An ordinary start brings the
  schema current. The SQLite registry is mapped with the SQLAlchemy ORM (with the
  Core expression kept where a statement must be exactly what the database does),
  its history is Alembic revisions applied when the registry opens, and **one
  active run per meeting is enforced by a partial unique index in the database**
  (`service/entities.py`, `service/store.py`,
  `service/migrations/versions/0009_active_run_per_meeting.py`).
- **The draft store, as a version chain with author provenance.** Each version
  records the author identity the writer declared, when it was written, and the
  human's accept/reject decision (with who and when), and a new version re-opens
  a decided draft. Drafts are written through MCP like every other artifact. The
  store is not part of the removal below: drafts and the human's accept/reject
  remain (`service/agent_drafts.py`).

### Upgrading an existing install

Clear-record opens your registry when it starts, and what the registry records
decides what happens next. A released install's registry is carried; a
development install's is carried only where the chain holds what it records.

**A released install — `0.2.0` and its release candidates.** That is the only
line that ever shipped a registry (`0.1.x` has no registry file at all). Your
registry **migrates in place on the first 0.3.0 start**: the projects, meetings,
runs, tapes and glossary terms it holds stay. This is the policy
[`docs/releasing.md`](releasing.md) states, quoted here unchanged:

> **A registry the released line left behind is carried forward.**

**A development install — a build from `main` before this release.** No migration
is promised for one, and clear-record refuses the registries it left that stand
outside the compressed chain — the ladder registries, and the stamps the cut
dropped — rather than half-understanding them. The policy, again quoted unchanged:

> **A registry the tip left behind is not carried — wipe it and start clear-record
> again.**

Three stored states decide, in this order:

1. **A revision this release carries (`0006`–`0009`).** The registry opens and
   migrates, **whatever its `schema_version` row says** — `0006`–`0009` is the
   part of what the `main` branch wrote before this release that the chain still
   holds, and the compression changed no table. The `schema_version` row is read
   only when there is no such revision.
2. **No such revision, and a `schema_version` row of `6`** — the number the
   released line recorded. Clear-record records the registry at the released
   baseline and runs only the changes made since; the rows it holds stay.
3. **No such revision, and any state a development build left** — a
   `schema_version` row of `0` (a build killed before it recorded where it got
   to), `1`–`5`, `7` or `8` (the ladder steps no released line stands at), or a
   stored revision this release no longer carries (`0001`–`0005`). Clear-record
   refuses to open it and names the file.

The repair is the same for the registries in 3: **delete the registry file and
start clear-record again** — the registry is rebuilt empty, and the package
itself is not reinstalled. On Linux the file is
`~/.local/share/clear-record/registry.sqlite3` where `paths.data_dir` in the
configuration has not moved the data directory. Nothing else is touched —
recordings, workspaces, archives and models stay where they are; the registry is
one SQLite file of metadata beside them (ADR-0007, ADR-0013). Not every refusal is
a wipe: a registry recording a revision *newer* than this build carries — or a
`schema_version` row above the ladder's last version, `8`, which only a build
newer than this one writes — meets *upgrade clear-record* instead.

**Your agent configuration — what to do.** If your install pointed the console at
an LLM endpoint, point a harness at the MCP surface instead: the stdio server
needs no endpoint, no key and no model to be configured. An `[agent]` table and
`CR_AGENT_*` variables written for the `0.2.0` in-process path are **ignored, with
a message** — never fatal, never migrated, and left exactly as they are, so an
existing config file keeps working. The message appears where setup is reported
(**Settings → Agent** and `/setup/agent`), which are now harness setup: where to
point a harness, the MCP command line to register, and the hello-world acceptance
check.

**Drafts the `0.2.0` app wrote itself.** Its in-process agent wrote one directory
per task (`<workspace>/agent/<kind>-<run_id>/`). Those are **not part of the 0.3
draft chain**: they stay on disk and are not shown, and the console's drafts panel
says so when such directories exist. Nothing is deleted.

### What 0.3.0 removes — the one breaking change

**The in-process BYOK path is gone.** So are the endpoint runner and the command
runner, the prompt renderer, the task plumbing with its output contracts, the
`[agent]` endpoint/model credential plumbing and the `CR_AGENT_*` precedence, the
setup wizard's endpoint rung, and — because it existed only for the deleted
output contract — the `json-repair` dependency. No shipped code path speaks an
LLM protocol, and the app holds no model credential to configure.

**MCP is the only agent integration.** The three LLM-shaped jobs — glossary
collection, transcript check and minutes — are the harness's tool calls, and the
console's Agent page is harness setup, not a credential form: a page that would
prompt for a key is a bug now. The optional `just agent-drive` is a harness
stand-in that drives the same tools, with no key needed for its scripted legs. If
your install pointed the console at an LLM endpoint, the step to take is in
*Upgrading an existing install* above.
