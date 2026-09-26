# ADR-0024 — A managed workspace: app-owned tapes with guarded upload

Status: active
Date: 2026-09-15

- Amends [ADR-0007](0007-deployment-directories-xdg.md) **in part**: its rule
  still governs the CLI's `--dir` and a meeting's user-chosen `workspace_path`;
  the managed root is app-owned **additionally**.
- Extends [ADR-0021](0021-localhost-only-deployment.md): a node that accepts
  uploads is a node whose proxy auth matters more, not less.

## Context

- [VOICE: owner, 2026-09-15] The feature request, verbatim:

  > clear-record managed workspace: the user creates a meeting, then upload tapes
  > to clear-record; clear-record manages tapes on behalf of user and do all the
  > transcription work - this is a route to self-host and manage clear-record
  > remotely

- [FACT] Today the console takes **local paths**: a meeting carries a
  `workspace_path`, and its tape set is a list of paths the user typed. The
  pipeline then runs over that directory.
- [FACT] **ADR-0007 decided one part of this the other way**: *"The workspace is
  not app-owned XDG data. The `directory` argument names a user-chosen workspace
  holding the source recordings and the derived record. Those are the user's
  documents, kept wherever the user points, not `clear-record`'s own data."* A
  **managed** workspace is app-owned, so this feature must amend that rule
  without deleting it.
- [FACT] The pipeline reads a directory through one abstraction,
  `clear_record.pipeline.workspace.Workspace`, and a run's tapes arrive through
  `PipelineOptions.audio_files`; the stage never learns where a path came from.
- [FACT] Remote use has a hole in the middle: a self-hosted node can run the
  pipeline and serve the console, but the tapes are on the user's laptop and the
  console can only be pointed at paths **on the node**.
- [FACT] **ADR-0021** chose localhost-only with the operator's reverse proxy
  owning auth. Every surface before this one only *read* local files. Upload
  **writes** multi-GB files to the node.
- [FACT] The archive (ADR-0006, `service.archive`) already copies tapes and
  artifacts into an immutable, checksummed directory — the durable-copy half of
  a storage policy already exists.

## Decision

- [DECISION] **Two workspace modes, one abstraction.** A *user-chosen* workspace
  (today's CLI `--dir` and a meeting's `workspace_path`) and a *managed*
  workspace rooted in an app-owned directory. Both are an ordinary
  `Workspace`; the pipeline is never told which. **ADR-0007 is amended in
  part**: its rule continues to govern the CLI's `--dir` and any user-chosen
  `workspace_path`; the managed root is app-owned **additionally**, used only by
  a meeting created for it.
- [DECISION] **Managed root**: `<data>/workspaces/` by default, resolved by the
  one paths resolver
  (`core.paths.resolve_workspace_root`, ADR-0025), overridable by the
  ``CR_WORKSPACE_ROOT`` environment variable or a `[paths] workspace_root`
  config entry. The tapes are large, so a NAS or a dedicated disk is the
  expected setting. Layout is the **identical** `Workspace` shape
  (`manifest.json`, `audio/`, `record.json`, `export/`, `glossary.txt`); the
  resumable chunk cache is app-owned *cache*, not a workspace document
  (ADR-0007/ADR-0025). Uploaded tapes live in a `tapes/` subdirectory, which is
  not one of the workspace's own output dirs, so `discover_audio` finds them.
- [DECISION] **A managed meeting's workspace is inside that root**, created when
  the meeting is (`POST /api/v1/projects/{slug}/meetings` with `managed: true`), or
  lazily on first upload. A user-chosen `workspace_path` keeps working
  unchanged; an upload aimed at one is refused, because the app must not write
  its files into a user document.
- [DECISION] **Uploads stream to disk**, never into a single in-memory buffer:
  multipart → a sibling `.part` file in the workspace → `fsync` → **atomic
  rename** → recorded as a tape with its **sha256 and size**, and appended to
  the meeting's tape set in the same registry transaction. A partial or failed
  upload never becomes a tape (the scratch file is removed).
- [DECISION] **Guards, because this is a write surface.** Each failure is an
  actionable message, never a traceback:
  - filename sanitization — a bare name only (no `..` traversal, no absolute
    path, no separator, no control character);
  - an audio-extension allow-list, reusing `workspace.is_audio` (one list, not
    two);
  - a size cap, `CR_MAX_UPLOAD_BYTES` (default 8 GiB), checked against the
    declared size **before** the body is read and again while streaming;
  - a disk-space precheck that refuses **before** the transfer (declared
    `Content-Length` + headroom against free space on the managed root);
  - **no symlink following** — the destination directory must resolve inside the
    managed root, the `.part` file is created `O_EXCL | O_NOFOLLOW`, and a
    pre-existing symlink at the destination is never overwritten.
- [DECISION] **The tape set is the existing mechanism.** An upload is recorded
  as a tape *like a path is*: `start_run`, the archive and the MCP surface read
  the same `recording_set.paths` with no new plumbing.
- [DECISION] **Storage visibility.** A meeting reports its workspace size and
  its uploaded tapes (`GET /api/v1/meetings/{id}/storage`), and a **managed** tape
  can be deleted (`DELETE /api/v1/meetings/{id}/tapes/{tape_id}`). A tape in a
  user-chosen workspace cannot — it is the user's document. **The archive is the
  durable copy**, and the delete response says so.
- [DECISION] **Simple upload only, this slice.** A single streaming POST. A
  dropped multi-GB upload restarts from zero; chunked/resumable upload (tus or
  a resume token) is **out of scope** until it is asked for.
- [DECISION] **A node that accepts uploads is a node whose proxy auth matters.**
  ADR-0021's posture is unchanged — the app still binds loopback and ships no
  auth — but the operator must read uploads as the reason the proxy is the
  ingress, not a formality (see `docs/service-deployment.md`).

## Rationale

- "Manage clear-record remotely" is impossible while the tapes must be placed on
  the node by hand. The managed workspace closes the only gap the local-first
  shape left.
- Amending ADR-0007 in part — rather than superseding it — keeps the property
  that matters: a user's recordings and derived record are **not silently
  treated as evictable app data**. Only tapes the user deliberately uploaded to
  the node are app-owned, and only there.
- Reusing `Workspace`/`is_audio`/`recording_set` keeps the pipeline and archive
  untouched, so the feature is "who chose the directory" plus storage
  management, not a second pipeline.
- The guards are the feature's other half: without them, exposing a node over a
  tailnet hands a write primitive to anything that can reach it.

## Discarded alternatives

- **Supersede ADR-0007 entirely / move the CLI workspace under XDG** — would
  overturn a documented boundary and move the user's documents without consent.
  Rejected; the amendment is scoped.
- **Buffer the upload in memory, then write** — fails on a multi-GB tape and
  contradicts the streaming requirement. Rejected.
- **Write completed uploads straight into the final name** — a partial upload
  would be visible to `discover_audio` and the pipeline. Rejected in favour of
  `.part` + atomic rename.
- **Trust the client filename as a path** — traversal and clobbering. Rejected.
- **Follow symlinks under the managed root** — a planted link could redirect a
  write outside the root. Rejected; symlinks are never followed.
- **Put uploads under the existing `audio/` dir** — that is the pipeline's
  *normalized output* and is excluded from `discover_audio`; uploads are
  *inputs*. Rejected in favour of `tapes/`.
- **Build tus/resumable now** — unrequested scope; "simple first, record the
  limitation" is the owner's stated default (spec `[OPEN]`). Deferred, not
  rejected.

## Consequences / review hook

- The managed root becomes one more app-owned directory, so its precedence lives
  in `core.paths` with the others — a third copy of the precedence is
  explicitly avoided by sharing one `_resolve_app_dir`.
- `CR_MAX_UPLOAD_BYTES` and `CR_WORKSPACE_ROOT` join the documented `CR_*`
  environment surface; the deployment guide gains an upload section.
- The registry schema advances to v5 (a `tape` table with `sha256`/`bytes`),
  forward-only like every other migration.
- The console **UI** for upload **ships**: an upload control on the meeting's
  storage panel, with per-tape delete.
- Revisit if a real multi-GB upload over a flaky link becomes common — then
  chunked/resumable upload is the next decision, not a patch.
- Revisit the single-user assumption if the node ever serves more than one
  person: quotas and per-user roots would change this ADR's shape (ADR-0021's
  "revisit if the product grows multi-user").

## Update (2026-09-15) — the upload id lands; resume is still unbuilt

The Decision bullet above ("Simple upload only, this slice … a resume token is
**out of scope**") is **amended**, not superseded: the endpoint now carries the
id that bullet ruled out, while the limitation it describes is unchanged. The
original text is kept for the record.

- [DECISION] **The upload endpoints take an optional `upload_id`.** It is a bare,
  bounded token (`[A-Za-z0-9][A-Za-z0-9._-]*`, at most 64 characters) — never a
  path — validated **before** the body is read, and it names the transfer's
  `.part` scratch file. The owner's answer on dropped connections (2026-09-15)
  was *"simple now, but shape the endpoint for resumability"*, so the shape lands
  now and the layer can follow without a breaking request change.
- [DECISION] **Nothing resumes yet, and a refusal says so.** A fresh id still
  starts at zero. An id whose scratch file already exists — an interrupted or
  in-flight transfer — is refused as unsupported (501) rather than overwritten or
  silently restarted: an id names one transfer, and destroying the partial file
  would remove the only thing a resume layer could have used. A malformed id is a
  400. See `docs/service-deployment.md` §4.
- [DESIGN] **Storage reports the root's free space** (`free_bytes`), from the
  same `root_free_bytes` the upload guard checks. The console's panel previously
  called `shutil.disk_usage` itself — two implementations of one fact, which is
  how a panel comes to say there is room while the guard refuses at 64 MiB of
  headroom.
- Revisit hook unchanged: this is the shape, not the layer. Chunked/resumable
  upload is still the next decision, taken when a real tape over a bad link
  earns it.

## Update (2026-09-26) — the managed-tape delete requires a verified archive

The Decision bullet above ("Storage visibility … **The archive is the durable
copy**, and the delete response says so") is **superseded** by ADR-0033's
shrink-the-irreversible-set rule: the archive stops being a note beside the
delete and becomes its precondition. The original text is kept for the record.

- [DECISION] **`DELETE /api/v1/meetings/{id}/tapes/{tape_id}` refuses unless the
  meeting has a verified archive.** The registry's archives are re-checked
  newest first against their manifests (`verify_archive`); the first that
  verifies is the durable copy the delete leans on, and the response's note names
  it. With no
  verified archive the route answers 400 with a message naming the archive action
  — archive the meeting first — and nothing is unlinked. A user-chosen
  workspace's tape is still refused as before (it is the user's document).
- [DESIGN] **The console's delete controls read the same rule.** The per-tape and
  delete-all controls still confirm manually, but their wording names the
  verified archive as the precondition, and a refusal renders the service's own
  message.
- [DESIGN] **Deleting a glossary term is now a retire** — see ADR-0033; the row
  survives with `added_by`/`created_at` and can be restored.
