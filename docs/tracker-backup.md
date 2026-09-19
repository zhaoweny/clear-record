# Backing up the tracker: dump, audit, restore

The tracker lives on the owner's private Gitea instance — tickets are issues, a
lane's spec is an umbrella ticket, and the lane's *other* documents are wiki pages
(`docs/agents/issue-tracker.md`). Ticket history therefore has exactly one home,
and ADR-0029 named the way out of it: `gitea dump` inside the instance's
container. This document is that way out, end to end — a dump is taken where the
instance runs, it is audited against the instance it came from, and it is
restored **on the machine that ran the audit**, with that machine's own Gitea,
and compared there.

Nothing here is automated or scheduled. It is a procedure a person runs.

Two machine roles, held to throughout: the **daemon** is the Docker endpoint the
instance's container runs on — not necessarily this machine's, and never named in
this repository: the operator names it (`--context NAME`, a docker *context*, or
`--docker-host URL` for a raw endpoint, or whatever `docker` already selects).
The **drill machine** is where the audit and the restore run, and it needs the
source's Gitea release installed locally.

Three words, held to throughout: a **dump** is one `gitea dump` archive; the
**source** is the instance a dump is compared against, named by `--url`/`--source-url`
— the live instance, or a restored copy of the dump itself; a **record** is a JSON
file a run writes — the audit's, and the drill's own `drill.json`. The scripts,
the records' keys and the justfile recipes say `source` in that sense too.

## The list that has to survive

The dump is the tracker when it holds: the issues themselves (tickets, closed
ones included, and the umbrella specs), the comments on them, the wiki pages, the
labels, the accounts, and the state that makes them the tracker rather than a
transcript — each ticket's number, title, body, open/closed state and created and
updated timestamps. That is what the audit counts and compares; it is also the
list of things whose loss would be silent otherwise.

Two things in the dump are *not* the tracker: the mirrored copy of the code
repository under `repos/` (its truth is the public GitHub repository, not the
instance) and the instance's own housekeeping (sessions, queues, indexes). The
audit counts and compares neither. They are in the dump, and they reach the report
only as part of the zip's member count — no line of the report names either, and
no field of the record does, so a restore that lost them still reads MATCH.

## 1. Take a dump

Through the daemon the container runs on — `docker --context <name>` if the daemon
is not this machine's:

```sh
docker --context <name> exec -u git <container> gitea dump
docker --context <name> cp <container>:<the path gitea printed> .local/backups/
```

`gitea dump` writes its zip inside the container and prints where it put it.
`just tracker-restore --take-dump` does the same three steps itself — dump, copy
out, and remove the in-container file — for the restore route below. The dump is
environment-local: it is kept under `.local/` with the rest of the machine's
scratch state, which `.gitignore` excludes (the boundary ADR-0006 draws), and
**never committed** — no dump, and no ticket it contains, enters this
repository's history.

**A dump taken from a running instance is not a consistency-guaranteed backup.**
Gitea's own documentation is blunt about it: *"to ensure the consistency of the
Gitea instance, it must be shutdown during backup"*, because the database and the
repositories move independently — a migration in flight can leave a repository
incomplete while the rows say otherwise. Taking the dump while the instance
serves is a point-in-time read, and that is what this procedure does, because the
tracker is worked most days and stopping it is not always the operator's to do.
What the drill adds is that a torn read is *noticed*: the audit compares counts,
names and a sample, so an inconsistency shows up as DRIFT or FAIL rather than
passing quietly. If the instance can be paused, pause it — the question then
disappears.

What is inside the zip is what the restore has to put back:

| in the zip | what it is |
| --- | --- |
| `data/gitea.db` | the database — SQLite; the whole list above except the wiki |
| `data/conf/app.ini` | the instance's configuration |
| `data/` (`avatars/`, `indexers/`, `jwt/`, …) | the AppDataPath: the files the database points at |
| `repos/<owner>/<name>.git`, `repos/<owner>/<name>.wiki.git` | the repository root: the mirrored code, and the wiki |
| `gitea-db.sql` | the same database as SQL text (`data/gitea.db` is what is read) |
| `app.ini` | a copy of the configuration at the archive root |

LFS is not a path of its own beside the repository root: Gitea's default is
`data/lfs`, inside the AppDataPath the table above already carries — the
drill's own generated configuration writes `[lfs] PATH = {data}/lfs`. The
tracker's issues and wiki do not use LFS, so neither the audit nor the drill
counts it; an instance whose *code* clones do use LFS gets those objects back
with the rest of `data/`, with nothing to copy separately.

## 2. Audit the dump

```sh
just tracker-backup --dump .local/backups/gitea-dump-<date>.zip
just tracker-backup --dump .local/backups/gitea-dump-<date>.zip --url "$CLEAR_RECORD_GITEA_URL"
```

Without `--url` this makes no network call at all: it opens the zip, opens the
SQLite database in it read-only, counts the tickets, comments, wiki pages, users,
labels and attachments, and prints what the dump holds — including the database's
format (its magic bytes are checked before anything is read, and a dump that is
not SQLite is refused rather than guessed at), the xorm timestamp line from
`gitea-db.sql`, the sha256 of the dump, and the wiki's HEAD revision.

With `--url` (or `$CLEAR_RECORD_GITEA_URL`, the convention
`scripts/migrate_tracker.py` already uses) it also reads the same counts from the
source's API and compares them, plus:

- the **names** the tracker is made of — which issue numbers, wiki page titles
  and label names the dump holds, which only the source has, which only the dump
  has. Labels are compared because nothing else carries them: a restore can drop
  every label and still return every ticket and comment;
- a **sample of issues field by field** — title, state, body digest, created and
  updated timestamps, comment count, attachment count. The sample is
  deterministic: the lowest N and the highest N issue numbers (`--sample`, 5 by
  default, so ten tickets), so two runs sample the same tickets and both ends of
  the history are covered.

The verdict is four-valued, and the difference matters. An offline run — no
`--url` — records and prints `dump-only`, because there is nothing to compare
against; the other three are:

- **MATCH** (exit 0) — the dump and the source agree: every compared count, the
  issue numbers, page titles and label names one by one, and all ten sampled
  issues field by field. This is what a restore must read.
- **DRIFT** (exit 0) — the dump holds everything it should, and the source has
  moved on since it was taken: tickets created, comments added, fields edited.
  Expected against the live instance, whose tracker is being worked all day.
- **FAIL** (exit 1) — the source is missing what the dump holds: a ticket, a
  comment, a page, a label or an account the dump has and the source does not, or
  a dump that is not from the source it was compared against. History the dump
  carries would be lost.

Token: `$GITEA_TOKEN` or the `tea` login store, as `scripts/migrate_tracker.py`
resolves it. The token is never printed and never written to the record. The
instance host is never defaulted and never written into a committed file.

## 3. Restore it here

```sh
# the drill, end to end: take a fresh dump through the daemon, restore it here
just tracker-restore --take-dump --context <name> --container <container> \
    --source-url "$CLEAR_RECORD_GITEA_URL"

# a reader with a dump already in hand, and no Docker at all
# (--source-version is how the version check is satisfied without a daemon:
#  it names the release the dump came from)
just tracker-restore --dump .local/backups/gitea-dump-<date>.zip \
    --source-version 1.27.3

# plan only, touching nothing (--container is required with --take-dump)
just tracker-restore --take-dump --container <container> --dry-run
```

The route is **take the dump where the instance runs, reproduce it here**. With
`--take-dump` the drill reads the source's release from the image that container
runs, takes a fresh dump with `gitea dump` inside it, copies the zip out into
`.local/backups/`, and removes the in-container temp file — the only thing it
writes on that host, and it takes it away again. It creates no container there.

Then the restore is local, with **this machine's own `gitea` binary** serving the
dump's data behind a configuration the drill generates. The dump is extracted
under `.local/restore-drill/`: `data/` at `<scratch>/data` and `repos/` at
`<scratch>/repos`, the layout `gitea dump` writes.

**The dump's own `app.ini` is deliberately not used**, and this is the piece that
decides whether the drill proves anything. It points at the *container's*
absolute paths — `WORK_PATH = /data/gitea`, `[repository] ROOT =
/data/git/repositories`, `[database] PATH = /data/gitea/gitea.db`,
`[server] APP_DATA_PATH = /data/gitea`, `[session] PROVIDER_CONFIG`,
`[indexer] ISSUE_INDEXER_PATH`, `[picture] AVATAR_UPLOAD_PATH` and
`REPOSITORY_AVATAR_UPLOAD_PATH`, `[attachment] PATH`, `[repository.local]
LOCAL_COPY_PATH`, `[repository.upload] TEMP_PATH`, `[lfs] PATH`,
`[log] ROOT_PATH` — none of which exists here. An instance started against it
comes up on no data, and an audit of *that* compares the live counts against
nothing: a green light for a restore that did not happen. The Docker shape of
this drill got the correspondence for free by mounting the dump *at* the paths
the config names; the local shape has to do the work explicitly, so the drill
**generates** the configuration instead of rewriting the dumped one — a whitelist
rather than a rewrite, which also disposes of the hostname and the services the
instance's config names.

The generated `app.ini` therefore names **every path the live one names**, each
one under the scratch root, plus:

- `HTTP_ADDR = 127.0.0.1` — **loopback only**, on a port chosen free (`--port`
  fixes one), so the copy is reachable from this machine and nowhere else;
- `OFFLINE_MODE`, `[mailer] ENABLED = false`, `[federation]`, `[actions]` and
  `[cron]` off — nothing that could leave the machine, whatever the instance's
  own configuration names;
- fresh `SECRET_KEY`, `INTERNAL_TOKEN` and `JWT_SECRET` from
  `gitea generate secret`, so the copy carries the dump's data and none of the
  instance's keys.

Two checks make that hold, and both are refusals rather than warnings:

- **the paths the instance reports using.** Before the audit the drill reads the
  instance's own log — the `WorkPath`/`CustomPath`/`ConfigFile` it prints at
  startup, every `Creating new Local Storage at …` line, and the `Listen:` line —
  and refuses unless each path is inside the scratch root and the address is
  exactly `127.0.0.1:<port>`. The log is what the instance *did*; a config file
  an operator edited cannot fake it. Those lines are printed and recorded as the
  evidence that the copy is confined;
- **the database is the dump's, byte for byte.** The audit reads the database from
  the zip; the instance reads the file the generated config names. The drill
  hashes both and refuses if they differ, then prints what the dump holds
  (tickets, comments, pages, users, labels) — a count that is non-zero for a
  reason, and the same database on both sides.

The operator's existing token authenticates against the copy because the tokens
live in that database. The instance is never written to: the dump is taken by
`gitea dump` inside the container, the zip is copied out, and the one file the
drill leaves on the daemon's host — the dump's own temp file — is removed again.
Everything else a run writes is on this machine, under `.local/`.

**Why not a container.** `docker run -p 127.0.0.1:PORT` was this drill's first
shape, and it is wrong the moment the daemon is not this machine's: the publish
lands on *that host's* loopback, which the drill machine cannot reach, so the
audit here would compare the restored counts against nothing and read a green
light for a restore that never happened — while leaving a container and a
directory on a machine nobody was looking at.

**Versions are checked before anything is restored.** The source's release is the
tag of the image its container runs
(`docker --context <name> inspect --format '{{.Config.Image}}' <container>`);
when that tag names no release (`latest`, `1.27`), the drill asks the instance's
own `/api/v1/version` instead, or takes `--source-version` from the operator —
which is also how a reader with no Docker and no reachable source satisfies the
check.
That is compared against `gitea --version` locally, and **a mismatch is refused,
not restored across**: Gitea migrates the schema forward on start, so restoring
across versions returns the rows into a different schema than they were dumped
from — a different experiment. The refusal names both versions and says which
release to install; `--gitea PATH` points the drill at that release when it is
not the `gitea` on `PATH`.

Step 2's audit then runs against the restored copy, where the verdict must read
**MATCH**: a restored copy of a dump has had no chance to move on, so a DRIFT or
FAIL line there is a failed restore, not a live tracker growing. When the source
is named (`--source-url`, or `$CLEAR_RECORD_GITEA_URL`) the same audit runs
against the source too, and *there* the readings are split: **DRIFT** is expected
(the tracker is worked all day, so tickets and comments appear after the dump),
while **FAIL** is not — FAIL means the source is missing something the dump
holds, which is either a bad dump or history gone.

Prerequisites, honestly:

- the **matching Gitea binary** locally, which is the load-bearing one: install
  the release the source runs (`brew install gitea`, or the release tarball) and
  put it on `PATH`, or pass `--gitea PATH`. This is what a refusal is telling you
  to fix;
- `git` on `PATH`, for the wiki page count (the wiki is a git repository inside
  the dump, and its page list is the tree at HEAD) — step 2 needs it too, whenever
  the dump carries a wiki;
- **docker**, and only when taking the dump is this drill's job (`--take-dump`,
  or `--container` to read its image). A reader with a dump file needs none — but
  does need `--source-version X.Y.Z` (or a reachable `--source-url`) to answer the
  version check;
- a token the instance accepts — `$GITEA_TOKEN` or the `tea` store.

`--dry-run` prints the whole sequence — the dump commands and both reader
commands — without touching Docker or the dump.

Exit status, and what each means:

| status | meaning |
| --- | --- |
| 0 | the restored copy read MATCH — that verdict alone decides the status. When a source was named, its comparison (MATCH, DRIFT or **FAIL**) is printed and recorded, but does not change it |
| 1 | the restored copy did not read MATCH: a **FAIL** verdict against the dump, or an audit that wrote no record |
| 2 | the run was refused before or during the restore: a missing prerequisite or bad input (no Docker, a version mismatch, an unreadable dump, a scratch directory this drill did not write), an instance that would not come up, or a path or listen address outside the scratch root |

**A source-side FAIL does not fail the run.** The restore already proved itself
against the dump before the source was even asked, so status 0 can carry a
`source comparison: FAIL` line — the source is missing something the dump
holds. A caller scripting against the exit status alone will not see that;
reading the printed line, or `drill.json`'s `verdict.source`, is how it is
caught.

A restore that started always leaves `drill.json`, including when it failed: the
record names the failure rather than being absent, which is when it is worth most.

### Using the restored copy

The instance is stopped when the audit finishes, and on a pass the extracted dump
is removed — the records, the generated configuration and the instance's log stay.
On a failure (or with `--keep`) the extracted data stays too, so the copy can be
started again by hand and read:

```sh
cd .local/restore-drill
GITEA_WORK_DIR=$PWD GITEA_CUSTOM=$PWD/custom gitea web -c $PWD/custom/conf/app.ini
```

It is a normal instance on `127.0.0.1:<port>`: browse it, or read a specific
ticket there by hand when a comparison line needs a closer look. It is disposable
by design — delete the scratch directory when done.

## What each run leaves behind

An audit run that reads its input writes a JSON record, and it is the evidence
that the run happened and what it found; the restore drill writes one of its
own. A run *refused* before it read anything — a missing `--dump`, a version
mismatch, a scratch directory this drill did not write — exits 2 without one,
because there is nothing to record yet:

| run | record |
| --- | --- |
| `just tracker-backup` | `.local/tracker-backup-audit.json` |
| `just tracker-restore` (restored copy) | `<scratch>/audit.json`, default `.local/restore-drill/audit.json` |
| `just tracker-restore` (source, when named) | `<scratch>/audit-source.json` |
| `just tracker-restore` (the drill itself) | `<scratch>/drill.json` |

An audit record names the moment, the dump's path, size and **sha256**, the counts
on both sides, the names compared (issue numbers, wiki page titles, label names),
the sampled issues with a per-field verdict and the value behind every mismatch,
the lost and drift lines, and the verdict. A run is evidenced by that file, and
the sha256 is how a reader ties a record to the exact dump it audited: auditing
the same dump file again reproduces the same hash, a changed or different dump a
different one. The path is not that proof — `just tracker-backup` writes the one
default path (`--record` names another), so auditing the same dump twice without
`--record` overwrites the first record rather than leaving a second; keeping more
than one means naming a distinct `--record` path each time.

The drill's record (`drill.json`) is what ties the two audits together: the
dump's provenance (its path, size, sha256, and whether this run took it, and the
image it came from), the versions on both sides and where the source's was read
from, the bind address and port the copy ran on, the paths of the audit records,
both verdicts, what the cleanup did, and a `failure` field — null on a run that
got as far as comparing, and a sentence on one that did not, so a drill that
started always leaves a record of where it stopped. Nothing in it names the
instance.

All of it lives under `.local/` with the environment, for the reason the dump
does: the records name the instance they compared against, and none of it is
committed. What accumulates is the dumps: `.local/backups/` gains one zip per
`just tracker-backup`/`--take-dump` run, and nothing here prunes the old ones —
that housekeeping is the operator's. The scratch directory is the opposite:
`.local/restore-drill/` (or `--scratch DIR`) is wiped and rebuilt at the start
of every run — a directory this drill did not mark is refused, never silently
reused — and its extracted payload is removed again on a pass; only a failed
run (or `--keep`) leaves it standing, for a look before the next run clears it.

## Who runs it, and how often

**Decided: nobody is scheduled, and no runner is appointed.** The owner takes a
remote backup by hand when they judge one is needed — a dump, and the audit and
restore drill above, run on the owner's own machine. Nothing in this repository
schedules it: Gitea Actions is disabled on the instance (ADR-0029), and the
GitHub-side CI cannot reach a tailnet-only host. So the cadence is *ad hoc, on
the owner's judgement*, and the mechanism does not depend on it.

Who *can* run each half, and what it takes:

- **taking a fresh dump** needs the daemon the container runs on (an endpoint the
  operator names: `--context NAME`, `--docker-host URL`, or whatever `docker`
  already selects) — the owner, or whoever the owner hands that to;
- **auditing and restoring it** needs the source's Gitea release installed
  locally, `git`, and a token the instance accepts. A dump taken once can be
  audited and drilled anywhere, by anyone the owner gives the file to — the
  drill machine never has to reach the instance at all, though naming the source
  (`--source-url`) is what compares the dump against the instance it came from.

What a run leaves, and where: `.local/` on the machine that ran it — the dump
itself under `.local/backups/`, and under `.local/restore-drill/` the audit
records, the drill's own record, the generated configuration and the restored
instance's log, with the extracted data removed on a pass (kept on a failure, or
with `--keep`). They are environment-local and never committed, so "the last
drill passed" lives only on the machine that ran it; a record worth keeping past
that machine has to be copied somewhere durable deliberately.
