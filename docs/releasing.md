# Releasing clear-record

The five workspace members are published as five PyPI distributions in lockstep:

| Distribution | Import | Contents |
|---|---|---|
| `cr-core` | `cr_core` | backend-agnostic domain model (no third-party deps) |
| `cr-engine` | `cr_engine` | audio I/O, alignment, reconcile (numpy, soundfile) |
| `cr-providers` | `cr_providers` | per-vendor ASR adapters (optional extras) |
| `cr-cli` | `cr_cli` | CLI implementation package; declares no console script |
| `clear-record` | `clear_record` | facade over `cr-cli`; the public install name (a `clear_record` module) |

Each release moves all five `version =` fields **together** — the intra-project
dependencies are exact-pinned (`cr-engine==0.1.0`), so a half-released set cannot
resolve. There are **no PyPI tokens or repository secrets**: publishing uses
OIDC trusted publishing ([`docs/adr/0009-packaging-and-distribution.md`](adr/0009-packaging-and-distribution.md)).

`clear-record` is a **facade**: it ships a small `clear_record` module
(`clear_record.main` re-exports `cr_cli.cli.main`) and is the **sole owner** of
the `clear-record` console script, so `uvx clear-record` resolves and runs it
without the dependency-provided-command warning. `cr-cli` is the CLI
implementation package and declares **no console script**, so `uvx cr-cli`
provides no command by design; the public install name is `clear-record`.
Publish `cr-cli` **before** (or in the same run as) `clear-record`, because the
facade depends on `cr-cli==X.Y.Z`.

## One-time setup (per PyPI project)

Do this once for each of `cr-core`, `cr-engine`, `cr-providers`, `cr-cli`, and
`clear-record`, before its first upload. On PyPI, a project that does not exist
yet is claimed through a **pending publisher**:

1. Sign in to <https://pypi.org> and open **Your account → Publishing →
   Add a pending publisher → GitHub**.
2. Fill in, exactly:

   | Field | Value |
   |---|---|
   | PyPI project name | `cr-core` (then `cr-engine`, `cr-providers`, `cr-cli`, `clear-record`) |
   | Owner | `zhaoweny` |
   | Repository name | `clear-record` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

3. Repeat for the other four projects.

The `pypi` **GitHub environment** is the second half of the gate: in the repo's
**Settings → Environments → `pypi`**, require reviewers so every publish waits
for a human approval.

> The CLI dist name is settled: `cr-cli` is the implementation package and
> `clear-record` is the public install name (a facade over `cr-cli`; ADR-0009,
> owner direction 2026-09-13). Claim **both** pending publishers, and publish
> `cr-cli` before `clear-record`.

## Version-bump checklist

For a release `X.Y.Z`, all of these move together in one commit:

1. **Version fields** — set `version = "X.Y.Z"` in all five:
   - `packages/core/pyproject.toml`
   - `packages/engine/pyproject.toml`
   - `packages/providers/pyproject.toml`
   - `packages/cli/pyproject.toml`
   - `packages/clear-record/pyproject.toml`
2. **Intra-project pins** — update every `cr-*` dependency to `==X.Y.Z`:
   - `packages/engine/pyproject.toml` → `cr-core==X.Y.Z`
   - `packages/providers/pyproject.toml` → `cr-core==X.Y.Z`
   - `packages/cli/pyproject.toml` → `cr-core`, `cr-engine`, `cr-providers` at
     `==X.Y.Z`
   - `packages/clear-record/pyproject.toml` → `cr-cli==X.Y.Z`
   - root `pyproject.toml` → `cr-providers[...]==X.Y.Z` in the `apple` /
     `nvidia` / `amd` / `all` extras
3. **Lockfile** — `uv lock`, then commit `uv.lock`.
4. **Verify** — `just verify` green.
5. **Sanity-check the metadata** — build and read the pins back:

   ```sh
   just build
   unzip -p dist/cr_cli-X.Y.Z-py3-none-any.whl '*/METADATA' | grep Requires-Dist
   ```

   You should see `cr-core==X.Y.Z`, `cr-engine==X.Y.Z`, `cr-providers==X.Y.Z`.

   The facade wheel should show the `cr-cli` pin, the `clear_record` module,
   and the console script:

   ```sh
   unzip -l dist/clear_record-X.Y.Z-py3-none-any.whl | grep clear_record/__init__.py
   unzip -p dist/clear_record-X.Y.Z-py3-none-any.whl '*/METADATA' | grep -E 'Requires-Dist|Provides-Extra'
   unzip -p dist/clear_record-X.Y.Z-py3-none-any.whl '*/entry_points.txt'
   ```

   You should see `clear_record/__init__.py`, `Requires-Dist: cr-cli==X.Y.Z`, and
   `clear-record = clear_record:main`.

## Publish

The workflow [`.github/workflows/publish.yml`](../.github/workflows/publish.yml)
runs on a `v*` tag **or** manually via **workflow_dispatch**. Its `build` job
runs `just verify` + `just build` and uploads `dist/`; its `publish` job
downloads `dist/` and uploads with
[`pypa/gh-action-pypi-publish`](https://github.com/pypa/gh-action-pypi-publish)
using the OIDC token from the `pypi` environment.

For a **new** release:

1. Land the version-bump commit on `main`.
2. Tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. Create the matching **GitHub Release**.
4. Watch the workflow; approve the `pypi` environment when prompted.

The **first** publish is different: the `v0.1.0` tag already exists, so it will
not re-trigger the workflow. Dispatch it by hand instead — **Actions → publish →
Run workflow** on `main` (or the release commit). Everything after that is
tag-driven.

## Version immutability

**A PyPI version can never be reused or overwritten.** Uploading files that
already exist for a version fails; deleting and re-uploading the same version
number is not allowed. If a release is bad:

- publish a **new patch version** (`X.Y.(Z+1)`) with the fix, and
- optionally **yank** the bad version on PyPI (it disappears from resolution but
  stays available to anyone who already pinned it) — yank, don't delete.

Never re-tag or re-run a publish for a version that already went out.
