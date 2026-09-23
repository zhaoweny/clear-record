# ============================================================================
# clear-record dev commands (just).
#
# Tooling convention (operator, 2026-09-09):
#   SIMPLE scripts (no branching statement) -> the recipe body lives here.
#
#   Anything WITH a branching statement runs as a Python pointer-script behind a
#   *pointer* recipe below, so `just` stays the single entry point for automation
#   and chaining. There are two pointer-script shapes. A script that needs no
#   project declares its own dependencies in a PEP-722/PEP-723 `# /// script`
#   block and runs via `uv run --no-project <file>.py` (e.g.
#   scripts/check_web_assets.py). A
#   script that imports the local `clear_record` package instead needs the
#   project environment and runs via `uv run --all-packages python
#   scripts/<name>.py` (e.g. scripts/agent_drive.py, scripts/agent_setup.py); it
#   must not pass `--no-project`.
#
#   Exemption: scripts/verify is a deliberate branching-free shim
#   (`exec just verify`) kept only so tooling that already calls
#   ./scripts/verify (e.g. the agent git-worktree Verify gate) keeps working.
#   Don't add more such shims; new scripts follow the rule above.
#
# Recipes run from the repo root (where this justfile lives); no `cd` boilerplate.
# ============================================================================

# List available recipes.
default:
    @just --list

# Full verify gate: sync --locked, lint, format-check, tests.
verify:
    uv sync --all-packages --locked
    uv run --all-packages ruff check .
    uv run --all-packages ruff format --check .
    uv run --all-packages pytest
    @echo "verify: OK"

# Lint only.
lint:
    uv run --all-packages ruff check .

# Format-check only.
format-check:
    uv run --all-packages ruff format --check .

# Format in place.
format:
    uv run --all-packages ruff format .

# Run the test suite.
test:
    uv run --all-packages pytest

# Terminal wizard for the human-only agent provisioning: point at an MCP harness
# and register clear-record's MCP server in its client config. Pointer to a
# Python script because it branches; `--all-packages` because the script imports
# `clear_record`, so it must run in the project environment (unlike
# `web-assets-check`, whose script declares its own dependencies). Note
# `--no-project` is a latent trap here: it fails with ModuleNotFoundError
# outside a synced checkout but *accidentally succeeds* inside the repo root,
# where a project `.venv` already exists.
agent-setup:
    uv run --all-packages python scripts/agent_setup.py

# Optional agent test-drive: stands in for a harness and drives the three jobs
# (glossary collection, transcript check, minutes) through clear-record's MCP
# tools over a throwaway seeded data dir, then prints a redacted report. It is
# NOT part of `verify` or `e2e` — those stay offline and deterministic. The
# scripted stories need no key; with a model key in `CR_DRIVE_API_KEY` (or the
# file `CR_DRIVE_API_KEY_FILE` names) a real model drives the same tools, and
# without one that leg reports SKIP and the drive still exits 0. A supplied
# recording drives the tape leg: `just agent-drive --tape <file.wav>`; with no
# `--tape` the TTS hello-world tape is used (a missing system voice is a
# reported finding). Pointer to a Python script because it branches;
# `--all-packages` because it imports `clear_record`.
agent-drive *ARGS:
    uv run --all-packages python scripts/agent_drive.py {{ARGS}}

# Rerunnable import of the retired local tracker into Gitea issues + wiki pages.
# Pointer to a Python script because it branches; `--no-project` because it is
# stdlib-only and imports no project package. Dry-run by DEFAULT: it writes a
# manifest to `.local/migrate-tracker-manifest.json` and prints the counts, making
# no network call at all — so it is safe to run anywhere, token or no token.
#
# `just migrate-tracker --apply` performs the import. It reads a Gitea token from
# $GITEA_TOKEN or the `tea` login store and never prints it. Every created issue
# carries a `<!-- scratch:<lane>/<relpath> sha=… -->` marker, so a second
# `--apply` creates nothing new. It creates what the archive has and the tracker
# lacks, and leaves everything else exactly as the tracker has it: a ticket
# closed there is not reopened, a ticket's body or comment edited there is not
# rewritten, and a wiki page edited there is not overwritten. The tracker is
# canonical from the cutover on.
#
# `--repair-from-archive` is the one thing that re-imposes the archive, and it is
# for the pre-cutover state alone: it posts the comments, sets each issue's state
# to the archive's, closed or reopened, restores the blocked-by lines of issues
# that already exist, overwrites wiki pages that differ, and reports how many
# issues and pages it changed. It exists to finish an import that died half way
# *before* the tracker was cut over; on a live tracker it undoes decisions made
# there, so it is not for one.
#
# A lane's `spec.md` is not imported: those files are counted in the report and
# written nowhere, because the spec's `type/spec` umbrella ticket (ADR-0029)
# superseded this script's `<lane>/spec` wiki page. Where a lane's spec is a
# `spec/` directory instead, its documents are wiki pages like the lane's others,
# and are imported as before.
#
# The tracker directory is named by the caller, never defaulted here — the
# convention keeps that path out of committed files (docs/agents/issue-tracker.md,
# enforced by packages/clear-record/tests/test_tracker_refs.py). So `--tracker DIR`
# is required unless $CLEAR_RECORD_TRACKER_DIR names it. Flags pass straight
# through, with or without a `--` separator: `just migrate-tracker --only
# console-ia`. Other levers: `--repo OWNER/NAME`, `--url URL` (or
# $CLEAR_RECORD_GITEA_URL), `--manifest PATH`.
migrate-tracker *ARGS:
    uv run --no-project scripts/migrate_tracker.py {{ARGS}}

# The tracker's history has a way back (ADR-0029: the instance is the one place
# ticket history lives, and `gitea dump` inside its container is the backup
# lever). This is the check that a dump really holds the tracker. Pointer to a
# Python script because it branches; `--no-project` because it is stdlib-only and
# imports no project package.
#
# The dump itself is taken on the host that runs the container — `docker exec -u
# git <container> gitea dump`, then a `docker cp` of the zip out — and kept
# outside the repository (`.local/backups/`), never committed. `--dump FILE`
# audits one with no network at all: it opens the zip, opens the SQLite database
# inside it read-only, and counts the tickets, comments, wiki pages, users,
# labels and attachments it holds. `--url URL` (or $CLEAR_RECORD_GITEA_URL)
# additionally compares those counts, the issue numbers and a sample of issues —
# title, state, body digest, created and updated timestamps, comments,
# attachments — against the instance's API. The token comes from $GITEA_TOKEN or
# the `tea` login store, and is never printed; the host is never defaulted, so no
# committed file names it.
#
# The verdict separates the two questions a comparison can answer. FAIL (exit 1)
# means the source is missing what the dump holds — a ticket, a comment, a page, a
# label or an account the dump has and the instance does not, which is what a
# restore that dropped it produces, and what a dump from another instance looks
# like. DRIFT (exit 0) means the dump holds everything it should and the source
# has moved on since it was taken — expected against a live instance. MATCH
# (exit 0) means the two agree.
#
# `just tracker-restore` is the other half — take the dump where the instance
# runs, restore it here with the local Gitea, and audit there, where the verdict
# must read MATCH. docs/tracker-backup.md is the procedure, the evidence each run
# leaves, and who runs it.
tracker-backup *ARGS:
    uv run --no-project scripts/audit_tracker_backup.py {{ARGS}}

# The restore half of the drill: take the dump where the instance runs (through
# the operator's Docker endpoint, `--context`/`--docker-host`), restore it HERE
# with this machine's own Gitea, and audit the restored copy — where the verdict
# must read MATCH. Pointer to a Python script because it branches; `--no-project`
# because it is stdlib-only and imports no project package.
#
# It refuses to restore across Gitea versions: the local binary must be the
# release the source runs, or the drill would be testing a forward migration
# instead. The restored instance binds 127.0.0.1 only, and carries the dump's own
# database, so the operator's token authenticates against it and the audit reads
# it exactly as it reads the source. `--dry-run` prints the whole sequence,
# including what a reader with no Docker at all would run.
# docs/tracker-backup.md carries the procedure, the evidence and who runs it.
tracker-restore *ARGS:
    uv run --no-project scripts/restore_tracker_dump.py {{ARGS}}

# Build the console's compiled assets into
# packages/clear-record/src/clear_record/web/static/ (needs bun; ADR-0023). The
# output is committed, so `just verify` and end users never need Node.
web-assets:
    bun install --frozen-lockfile --cwd packages/clear-record/frontend
    bun run --cwd packages/clear-record/frontend build

# Freshness guard: rebuild the assets and fail if the committed output differs
# from its source. Pointer to a Python script because it branches; it needs bun,
# so it is NOT part of `verify` — CI runs it as its own job.
web-assets-check:
    uv run --no-project scripts/check_web_assets.py

# Where `e2e` and `e2e-install` keep the Playwright browser: `.local/` is
# gitignored, so the download stays out of the repo and out of the OS browser
# cache. A caller may point the run elsewhere by exporting
# PLAYWRIGHT_BROWSERS_PATH (which is also how the guard below is exercised
# against an empty directory).
e2e_browsers_path := env_var_or_default("PLAYWRIGHT_BROWSERS_PATH", justfile_directory() / ".local" / "ms-playwright")

# Browser end-to-end and visual review for the console (Playwright +
# Chromium). Seeds a throwaway data dir, boots the real server against it,
# drives the UI headlessly, and writes screenshots to .local/e2e/screenshots.
# It needs bun, Python and a downloaded browser, so it is NOT part of `verify`.
#
# The browser download is PER MACHINE by convention, not per worktree: it lands
# once in the main checkout's `.local/ms-playwright`, and a worktree shares that
# copy when its own `.local/ms-playwright` symlinks there (three levels up) — a
# link environment provisioning makes, not a committed step. On a machine
# provisioned that way `just e2e-install` is a one-off rather than a step per
# tree; a worktree without the link downloads its own copy.
# The first line below is the provisioning guard: it checks before the seed
# runs, before the server boots and before the first spec, and exits non-zero
# naming the command that provides whichever piece is missing — the frontend's
# dependencies (`bun install --frozen-lockfile --cwd
# packages/clear-record/frontend`) or the browser (`just e2e-install`). It is a
# pointer to a Python script because it branches.
#
# The seed writes the setup marker, so it must resolve the same state dir the
# server will (playwright.config.ts sets CR_STATE_DIR for the webServer; the
# seed runs as a separate process).
e2e:
    PLAYWRIGHT_BROWSERS_PATH={{e2e_browsers_path}} uv run --no-project scripts/check_e2e_provisioning.py
    CR_DATA_DIR=.local/e2e/data CR_STATE_DIR=.local/e2e/state uv run --all-packages python packages/clear-record/frontend/e2e/seed.py
    CR_DATA_DIR=.local/e2e/data CR_STATE_DIR=.local/e2e/state E2E_SHOTS=.local/e2e/screenshots PLAYWRIGHT_BROWSERS_PATH={{e2e_browsers_path}} bun run --cwd packages/clear-record/frontend e2e

# One-time Chromium download for `e2e`. The browser lands in `.local/` so it
# stays out of the repo and out of the OS cache.
e2e-install:
    PLAYWRIGHT_BROWSERS_PATH={{e2e_browsers_path}} bun run --cwd packages/clear-record/frontend e2e:install

# Message catalogs (Babel, build-time only; see docs/i18n.md). Source strings are
# the English message IDs in the code and templates; `i18n-extract` merges new
# and changed ones into each locale's messages.po, and `i18n-compile` writes the
# compiled messages.mo beside it. Both run through the `i18n` dependency group,
# so `just verify` never needs Babel. The output is committed; change a source
# string and commit the source and the regenerated catalog together.
#
# `--exact` prunes the environment to exactly that group, which is what makes this
# guard faithful: without it a leftover default-group install (which pulls Jinja2
# in via the web stack) lets the check pass locally while a fresh CI environment —
# holding only the group — fails. That is precisely how a missing `jinja2` in the
# group stayed green here and broke CI for seven consecutive pushes.
i18n-extract:
    uv run --all-packages --no-default-groups --group i18n --exact python scripts/i18n.py extract

# Compile every messages.po into the committed messages.mo (needs Babel).
i18n-compile:
    uv run --all-packages --no-default-groups --group i18n --exact python scripts/i18n.py compile

# Freshness guard: re-extract and re-compile and fail if the committed catalogs
# differ from source — the `web-assets-check` pattern. It needs Babel, so it is
# NOT part of `verify`; CI runs it as its own job, and a contributor who never
# touches a translation is not blocked.
i18n-check:
    uv run --all-packages --no-default-groups --group i18n --exact python scripts/i18n.py check

# Build the release dist (wheel + sdist) into dist/ (never committed). Names the
# published package explicitly — NOT `--all-packages` — so a future workspace
# member (a GUI, an MCP server) can never silently join the PyPI payload.
# `--no-sources` proves the published graph resolves without `tool.uv.sources`.
build:
    uv build --package clear-record --no-sources

# Build the double-clickable desktop app (PyInstaller) into dist/: the tray
# launcher (the default entry point), the web launcher, the CLI, and (on macOS) a
# `clear-record.app` bundle. Unsigned — see packaging/pyinstaller/README.md. The
# `app` group keeps PyInstaller out of the verify environment; the `web` extra
# supplies the console's stack and the `tray` extra supplies PySide6 — the tray
# supervises the console, so it needs both.
app:
    uv run --all-packages --group app --extra web --extra tray pyinstaller --noconfirm --clean packaging/pyinstaller/clear-record.spec

# Show the published version. The member `clear-record` is the single published
# dist; the virtual root is bumped in step with it by the recipes below, so the
# two `version =` literals never drift. `--short` prints only the version string
# (scriptable). This read passes `--frozen`; the bump/set recipes below pass
# `--no-sync` so they relock `uv.lock` (it records the member's version) without
# syncing the venv.
version:
    uv version --frozen --package clear-record --short

# Advance the dev snapshot on `main`: X.Y.Z.devN -> X.Y.Z.dev(N+1), and after a
# candidate X.Y.ZrcN -> X.Y.Zrc(N+1).dev0, so `main` leaves the version that was
# tagged and the next `bump-rc` cuts the *next* candidate. `uv version --bump dev`
# cannot do the rc case, so this is a pointer script; a stable `X.Y.Z` is refused
# with the `just set-version X.Y.(Z+1).dev0` form to use instead.
bump-dev:
    uv run --no-project scripts/bump_dev.py

# Cut or advance the rc segment: X.Y.Z.devN -> X.Y.Zrc1; X.Y.ZrcN.devM -> X.Y.ZrcN
# (the dev snapshot is *of* rcN, so cutting drops the suffix); X.Y.ZrcN ->
# X.Y.Zrc(N+1). `uv version --bump rc` mishandles the rcN.devM case by giving
# rc(N+1), so this is a pointer script. A stable X.Y.Z is refused -- use
# `set-version`.
bump-rc:
    uv run --no-project scripts/bump_rc.py

# Set an explicit version (stable release: drop the suffix, e.g. `just set-version 0.1.1`).
# Unlike `--bump`, an explicit value forces the write (it may lower the version).
set-version VERSION:
    uv version --no-sync --package clear-record {{VERSION}}
    uv version --no-sync {{VERSION}}
