# ============================================================================
# clear-record dev commands (just).
#
# Tooling convention (operator, 2026-09-09):
#   SIMPLE scripts (no branching statement)       -> the recipe body lives here.
#
#   Anything WITH a branching statement           -> a Python inline-script
#                                                    (PEP-722/PEP-723
#                                                    `# /// script` block),
#                                                    run via `uv run <file>.py`,
#                                                    with a *pointer* recipe
#                                                    below so `just` stays the
#                                                    single entry point for
#                                                    automation and chaining.
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

# Advance the dev segment (a new dev snapshot): X.Y.Z.devN -> X.Y.Z.dev(N+1).
# Native `uv version` refuses a bump that would not increase the version, so this
# only applies while the member carries a dev version; to open the next dev series
# from a stable release use `just set-version X.Y.(Z+1).dev0`.
bump-dev:
    uv version --no-sync --package clear-record --bump dev
    uv version --no-sync --bump dev

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
