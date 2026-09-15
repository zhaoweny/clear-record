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
#   block and runs via `uv run <file>.py` (e.g. scripts/check_web_assets.py). A
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

# Terminal wizard for the human-only agent provisioning (ticket 20): detect and
# verify a local endpoint, record it, and point an MCP client at clear-record's
# tools. Pointer to a Python script because it branches; `--all-packages` because
# the script imports `clear_record`, so it must run in the project environment
# (unlike `web-assets-check`, whose script declares its own dependencies). Note
# `--no-project` is a latent trap here: it fails with ModuleNotFoundError
# outside a synced checkout but *accidentally succeeds* inside the repo root,
# where a project `.venv` already exists.
agent-setup:
    uv run --all-packages python scripts/agent_setup.py

# Optional bring-your-own-key (BYOK) agent test-drive (ticket 07): drives
# clear-record's own agent tasks against a real OpenAI-compatible endpoint over a
# throwaway seeded data dir, and prints a redacted report. It is NOT part of
# `verify` or `e2e` — those stay offline and deterministic. Without a key it
# skips with a message and exits 0. A supplied recording drives the tape leg:
# `just agent-drive --tape <file.wav>`; with no `--tape` the TTS hello-world
# tape is used (a missing system voice is a reported finding). Pointer to a
# Python script because it branches; `--all-packages` because it imports
# `clear_record`.
agent-drive *ARGS:
    uv run --all-packages python scripts/agent_drive.py {{ARGS}}

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

# Browser end-to-end and visual review for the console (Playwright +
# Chromium). Seeds a throwaway data dir, boots the real server against it,
# drives the UI headlessly, and writes screenshots to .local/e2e/screenshots.
# It needs bun, Python and a downloaded browser, so it is NOT part of `verify`.
# The seed writes the setup marker, so it must resolve the same state dir the
# server will (playwright.config.ts sets CR_STATE_DIR for the webServer; the
# seed runs as a separate process).
e2e:
    CR_DATA_DIR=.local/e2e/data CR_STATE_DIR=.local/e2e/state uv run --all-packages python packages/clear-record/frontend/e2e/seed.py
    CR_DATA_DIR=.local/e2e/data CR_STATE_DIR=.local/e2e/state E2E_SHOTS=.local/e2e/screenshots PLAYWRIGHT_BROWSERS_PATH={{justfile_directory()}}/.local/ms-playwright bun run --cwd packages/clear-record/frontend e2e

# One-time Chromium download for `e2e`. The browser lands in `.local/` so it
# stays out of the repo and out of the OS cache.
e2e-install:
    PLAYWRIGHT_BROWSERS_PATH={{justfile_directory()}}/.local/ms-playwright bun run --cwd packages/clear-record/frontend e2e:install

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

# Cut or advance the rc segment: X.Y.Z.devN -> X.Y.Zrc1; X.Y.ZrcN -> X.Y.Zrc(N+1).
# (uv's `--bump rc` cannot cut an rc from a stable X.Y.Z: it refuses the bump, which
# would not increase the version. Use `set-version` for that.)
bump-rc:
    uv version --no-sync --package clear-record --bump rc
    uv version --no-sync --bump rc

# Set an explicit version (stable release: drop the suffix, e.g. `just set-version 0.1.1`).
# Unlike `--bump`, an explicit value forces the write (it may lower the version).
set-version VERSION:
    uv version --no-sync --package clear-record {{VERSION}}
    uv version --no-sync {{VERSION}}
