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
