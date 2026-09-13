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

# Build the dist (wheel + sdist) into dist/ (never committed). `--no-sources`
# proves the published graph resolves without `tool.uv.sources` (the workspace pins).
build:
    uv build --all-packages --no-sources

# Show the published version. The member `clear-record` is the single published
# dist; the virtual root is bumped in step with it by the recipes below, so the
# two `version =` literals never drift. `--short` prints only the version string
# (scriptable). `--frozen` keeps the version tools from re-locking mid-edit (the
# lockfile follows in a separate, explicit `uv lock`).
version:
    uv version --frozen --package clear-record --short

# Advance the dev segment (a new dev snapshot): X.Y.Z.devN -> X.Y.Z.dev(N+1).
# Native `uv version` refuses a bump that would not increase the version, so this
# only applies while the member carries a dev version; to open the next dev series
# from a stable release use `just set-version X.Y.(Z+1).dev0`. Follow with
# `uv lock`.
bump-dev:
    uv version --frozen --package clear-record --bump dev
    uv version --frozen --bump dev

# Cut or advance the rc segment: X.Y.Z.devN -> X.Y.Zrc1; X.Y.ZrcN -> X.Y.Zrc(N+1).
# (uv's `--bump rc` cannot cut an rc from a stable X.Y.Z: it refuses the bump, which
# would not increase the version. Use `set-version` for that.) Follow with `uv lock`.
bump-rc:
    uv version --frozen --package clear-record --bump rc
    uv version --frozen --bump rc

# Set an explicit version (stable release: drop the suffix, e.g. `just set-version 0.1.1`).
# Unlike `--bump`, an explicit value forces the write (it may lower the version).
set-version VERSION:
    uv version --frozen --package clear-record {{VERSION}}
    uv version --frozen {{VERSION}}
